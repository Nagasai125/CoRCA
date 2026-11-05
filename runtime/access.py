"""Local identities, revocable browser sessions, and transactional audit records."""
import argparse
import getpass
import hashlib
import json
import secrets
import sqlite3
import time

from fastapi import HTTPException

SESSION_SECONDS = 8 * 3600
COOKIE = "corca_session"
ITERATIONS = 600_000


def password_hash(password, salt=None):
    if salt is None and not 15 <= len(password) <= 256:
        raise ValueError("Use a password of 15–256 characters")
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS).hex()
    return f"{salt}:{digest}"


def audit(db, actor, action, target, details=None):
    db.execute("INSERT INTO audit_events (occurred_at,actor_id,actor_name,action,target,details) VALUES (?,?,?,?,?,?)",
               (time.time(), actor["id"], actor["name"], action, target, json.dumps(details or {}, sort_keys=True)))


class Access:
    def __init__(self, jobs):
        self.jobs = jobs
        with jobs._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('administrator','responder')),
                enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,expires_at REAL NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS session_user ON sessions(user_id)")
            db.execute("""CREATE TABLE IF NOT EXISTS login_attempts (
                bucket TEXT PRIMARY KEY, attempts INTEGER NOT NULL, expires_at REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at REAL NOT NULL, actor_id TEXT NOT NULL,
                actor_name TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, details TEXT NOT NULL)""")
        # Same password work for unknown accounts; this hash never grants access.
        self.dummy_hash = password_hash(secrets.token_urlsafe(32))

    def enabled(self):
        with self.jobs._db() as db:
            return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def create_user(self, username, password, role, actor=None, bootstrap=False):
        encoded = password_hash(password)
        identifier = secrets.token_hex(16)
        with self.jobs._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if bootstrap and db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise ValueError("Access is already enabled; an administrator must manage accounts")
            try:
                db.execute("INSERT INTO users (id,username,password_hash,role,created_at) VALUES (?,?,?,?,?)",
                           (identifier, username, encoded, role, time.time()))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "This username is already in use") from None
            audit(db, actor or {"id":"local-operator", "name":"Local operator"}, "user.created", identifier,
                  {"username":username, "role":role, "bootstrap":bootstrap})
        return {"id":identifier, "username":username, "role":role, "enabled":True}

    def login(self, username, password, client):
        now = time.time()
        # Persistent limits, reserved before hashing, bound concurrent attempts as well.
        buckets = [("user:" + hashlib.sha256(username.encode()).hexdigest(), 10),
                   ("ip:" + hashlib.sha256(client.encode()).hexdigest(), 60)]
        with self.jobs._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM login_attempts WHERE expires_at<=?", (now,))
            for bucket, maximum in buckets:
                row = db.execute("SELECT attempts FROM login_attempts WHERE bucket=?", (bucket,)).fetchone()
                if row and row[0] >= maximum:
                    raise HTTPException(429, "Too many sign-in attempts. Try again in 15 minutes.", headers={"Retry-After":"900"})
            for bucket, _ in buckets:
                db.execute("INSERT INTO login_attempts VALUES (?,1,?) ON CONFLICT(bucket) DO UPDATE SET attempts=attempts+1", (bucket, now+900))
            user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        encoded = user["password_hash"] if user else self.dummy_hash
        salt = encoded.split(":")[0]
        valid = secrets.compare_digest(password_hash(password, salt), encoded)
        with self.jobs._db() as db:
            db.execute("BEGIN IMMEDIATE")
            # Disable, password reset, or role change while hashing must take effect now.
            current = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone() if user else None
            if not valid or not current or not current["enabled"] or current["password_hash"] != encoded:
                audit(db, {"id":"unauthenticated", "name":"Unauthenticated"}, "session.rejected", "sign-in")
                failure = True
            else:
                failure = False
                token = secrets.token_urlsafe(32)
                db.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
                # Bound active sessions for an account; newest ten browser sessions survive.
                db.execute("DELETE FROM sessions WHERE user_id=? AND token_hash NOT IN (SELECT token_hash FROM sessions WHERE user_id=? ORDER BY expires_at DESC LIMIT 9)", (current["id"],current["id"]))
                db.execute("INSERT INTO sessions VALUES (?,?,?)", (hashlib.sha256(token.encode()).hexdigest(),current["id"],now+SESSION_SECONDS))
                db.execute("DELETE FROM login_attempts WHERE bucket=?", (buckets[0][0],))
                actor = {"id":current["id"], "name":current["username"], "role":current["role"]}
                audit(db, actor, "session.created", current["id"])
        if failure:
            raise HTTPException(401, "Invalid username or password")
        return token, actor

    def resolve(self, token):
        if not token or len(token) > 128:
            return None
        with self.jobs._db() as db:
            row = db.execute("""SELECT u.id,u.username AS name,u.role FROM sessions s JOIN users u ON u.id=s.user_id
                WHERE s.token_hash=? AND s.expires_at>? AND u.enabled=1""", (hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
        return dict(row) if row else None

    def logout(self, token, actor):
        with self.jobs._db() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
            audit(db, actor, "session.revoked", actor["id"])

    def users(self):
        with self.jobs._db() as db:
            return [dict(row) for row in db.execute("SELECT id,username,role,enabled,created_at FROM users ORDER BY username")]

    def change_user(self, identifier, role, enabled, actor):
        with self.jobs._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM users WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise HTTPException(404, "User not found")
            if identifier == actor["id"] and (role != "administrator" or not enabled):
                raise HTTPException(409, "Another administrator must change your own access")
            if row["role"] == "administrator" and row["enabled"] and (role != "administrator" or not enabled):
                if db.execute("SELECT count(*) FROM users WHERE role='administrator' AND enabled=1").fetchone()[0] <= 1:
                    raise HTTPException(409, "Keep at least one enabled administrator")
            db.execute("UPDATE users SET role=?,enabled=? WHERE id=?", (role,int(enabled),identifier))
            db.execute("DELETE FROM sessions WHERE user_id=?", (identifier,))
            audit(db, actor, "user.access_changed", identifier, {"username":row["username"],"before":{"role":row["role"],"enabled":bool(row["enabled"])},"after":{"role":role,"enabled":enabled}})

    def change_password(self, identifier, password, actor, current_password=None):
        encoded = password_hash(password)
        with self.jobs._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM users WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise HTTPException(404, "User not found")
            if current_password is not None and not secrets.compare_digest(password_hash(current_password,row["password_hash"].split(":")[0]),row["password_hash"]):
                raise HTTPException(403, "Current password is incorrect")
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (encoded,identifier))
            db.execute("DELETE FROM sessions WHERE user_id=?", (identifier,))
            audit(db, actor, "user.password_changed", identifier)

    def events(self, before=None, limit=100):
        with self.jobs._db() as db:
            rows = db.execute("SELECT * FROM audit_events WHERE id<? ORDER BY id DESC LIMIT ?", (before if before is not None else 2**63-1,limit)).fetchall()
        return [{**dict(row),"details":json.loads(row["details"])} for row in rows]


def main():
    from runtime.api import Jobs
    import re
    parser = argparse.ArgumentParser(description="Create the first CoRCA administrator or recover a local account")
    parser.add_argument("command", choices=["bootstrap-admin","reset-password"])
    parser.add_argument("--db", required=True)
    parser.add_argument("--username", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", args.username):
        parser.error("Use a lowercase username of 3–64 letters, numbers, dots, underscores, or hyphens")
    from pathlib import Path
    if not Path(args.db).is_file():
        parser.error("Start CoRCA once first; the workspace database must already exist")
    # Jobs.__init__ recovers running jobs; CLI must not instantiate it against a live worker.
    jobs = object.__new__(Jobs); jobs.path = args.db
    access = Access(jobs)
    password = getpass.getpass("New password (15–256 characters): ")
    if password != getpass.getpass("Repeat password: "):
        parser.error("Passwords do not match")
    try:
        if args.command == "bootstrap-admin":
            access.create_user(args.username,password,"administrator",bootstrap=True)
        else:
            user = next((u for u in access.users() if u["username"] == args.username),None)
            if not user:
                raise ValueError("User not found")
            access.change_password(user["id"],password,{"id":"local-operator","name":"Local operator"})
    except (ValueError,HTTPException) as exc:
        parser.exit(1,str(getattr(exc,"detail",exc))+"\n")
    print("Account saved. Sign in with your username and password. Existing sessions for a reset account are revoked.")


if __name__ == "__main__":
    main()
