"""Private server-side credential files; workspace settings contain opaque references."""
import json
import os
from pathlib import Path
import re
import uuid

FIELDS = {"token": "token_env", "api_key": "api_key_env", "app_key": "app_key_env"}
REFERENCE = re.compile(r"CORCA_TELEMETRY_SAVED_([a-f0-9]{32})_(TOKEN|API_KEY|APP_KEY)\Z")


def credential_value(source, name):
    if not name:
        return ""
    return source.get("_credential_values", {}).get(name) or os.getenv(name, "")


class Credentials:
    def __init__(self, database):
        self.directory = Path(database).parent / "credentials"

    def prepare(self, provider, supplied):
        allowed = {"datadog": {"api_key", "app_key"}}.get(provider, {"token"})
        if set(supplied) - allowed:
            raise ValueError("Credentials do not match the selected provider")
        if provider != "prometheus" and not allowed <= set(supplied):
            raise ValueError("Enter all API credentials required by the selected provider")
        identifier, references, values = uuid.uuid4().hex, {}, {}
        for field, secret in supplied.items():
            value = secret.get_secret_value()
            if not value.strip() or len(value) > 8192 or "\n" in value or "\r" in value:
                raise ValueError("Use nonempty API credentials of at most 8,192 characters without line breaks")
            name = f"CORCA_TELEMETRY_SAVED_{identifier}_{field.upper()}"
            references[FIELDS[field]] = name
            values[name] = value
        return identifier, references, values

    def save(self, identifier, values):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.stat().st_mode & 0o077:
            raise ValueError("The server credentials directory must have owner-only permissions (0700)")
        path = self.directory / f"{identifier}.json"
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(values, stream)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path

    def read(self, source):
        values = {}
        for field in FIELDS.values():
            name = source.get(field, "")
            match = REFERENCE.fullmatch(name)
            if not match:
                continue
            path = self.directory / f"{match[1]}.json"
            if path.stat().st_mode & 0o077:
                raise ValueError("Saved credential files must have owner-only permissions (0600)")
            saved = json.loads(path.read_text())
            if not isinstance(saved.get(name), str) or not saved[name]:
                raise ValueError("Saved connection credentials are unavailable; reconnect this provider")
            values[name] = saved[name]
        return values
