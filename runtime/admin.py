"""Local workspace backup and restore. No API key or telemetry secrets are exported."""
import argparse
import os
from pathlib import Path
import sqlite3


def check(db):
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("Database integrity check failed")
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"jobs", "settings"} <= tables:
        raise ValueError("Not a current CoRCA workspace database")


def copy_database(source, destination):
    """SQLite's backup API makes a consistent snapshot, including with a live writer."""
    source, destination = Path(source), Path(destination)
    # Read-only mode prevents a typo creating an empty source database.
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as src:
        check(src)
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        try:
            with sqlite3.connect(destination) as dst:
                src.backup(dst)
                check(dst)
        except BaseException:
            destination.unlink()
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup", help="Create a consistent workspace snapshot; never overwrite")
    backup.add_argument("--db", default=os.getenv("CORCA_DB", "runtime/.state/corca.db"))
    backup.add_argument("--out", required=True)
    restore = sub.add_parser("restore", help="Restore into a new database path while the service is stopped")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--db", required=True)
    args = parser.parse_args()
    try:
        copy_database(args.db if args.command == "backup" else args.backup,
                      args.out if args.command == "backup" else args.db)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, f"{args.command} failed: {exc}\n")
    if args.command == "restore":
        with sqlite3.connect(args.db) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'").fetchone():
                db.execute("DELETE FROM sessions")
                from runtime.access import audit
                audit(db, {"id":"local-operator","name":"Local operator"}, "workspace.restored", "workspace")
    print(f"Workspace {args.command} complete. API keys and telemetry secret values are managed separately.")


if __name__ == "__main__":
    main()
