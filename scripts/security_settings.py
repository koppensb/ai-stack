"""Installer security checks. Never print credential values."""
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import tempfile

EXAMPLE_LLAMA_KEY = "sk-llm-inference-stack-super-secret-key-12345"
EXAMPLE_SECRETS = {"admin", "changeme", "password", "ultrasecretkey", EXAMPLE_LLAMA_KEY,
                   "SW2YcwTIb9zpOOhoPsMm"}


def strong_secret(name, value, minimum=32):
    if len(value) < minimum or value in EXAMPLE_SECRETS or len(set(value)) < 8:
        raise ValueError(f"{name} is too short, repetitive or a known example; use a random secret")
    if any(c in value for c in "\n\r\x00'"):
        raise ValueError(f"{name} contains unsupported characters")
    return value


def select_llama_key(saved, file_key):
    # Filter both inputs before comparing; never copy the old example back.
    saved = "" if saved == EXAMPLE_LLAMA_KEY else saved
    file_key = "" if file_key == EXAMPLE_LLAMA_KEY else file_key
    if saved and file_key and saved != file_key:
        raise ValueError(".env and api-key.txt contain different keys; synchronize them first")
    return strong_secret("LLAMA_CPP_API_KEY", saved or file_key or secrets.token_hex(32))


def grafana_key(root, configured):
    if configured:
        return strong_secret("GRAFANA_SECRET_KEY", configured)
    db = root / "data/grafana/grafana.db"
    if db.exists():
        # Never assume that a populated database can tolerate a new encryption key.
        # Inspect read-only. Refuse unknown secret stores rather than destroy data.
        with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as con:
            for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                quoted = '"' + table.replace('"', '""') + '"'
                columns = [row[1] for row in con.execute(f"PRAGMA table_info({quoted})")]
                if any(word in table.lower() for word in ("secret", "data_keys", "encrypted")):
                    if con.execute(f"SELECT 1 FROM {quoted} LIMIT 1").fetchone():
                        raise ValueError("Existing Grafana database contains encryption material. Retain/migrate its original secret_key before setting GRAFANA_SECRET_KEY; no credentials have been changed")
                for column in columns:
                    if any(word in column.lower() for word in ("secure", "encrypted")) or (table in ("data_source", "alert_notification") and "password" in column.lower()):
                        col = '"' + column.replace('"', '""') + '"'
                        if con.execute(f"SELECT 1 FROM {quoted} WHERE {col} IS NOT NULL AND CAST({col} AS TEXT) NOT IN ('', '{{}}', 'null') LIMIT 1").fetchone():
                            raise ValueError("Existing Grafana database contains encrypted settings. Migrate its original secret_key before setting GRAFANA_SECRET_KEY; no credentials have been changed")
    return secrets.token_hex(32)


def write_comfyui_auth(root, username, password):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username):
        raise ValueError("COMFYUI_AUTH_USER must contain only letters, digits, dot, underscore or hyphen")
    strong_secret("COMFYUI_AUTH_PASSWORD", password, 24)
    result = subprocess.run(["htpasswd", "-niB", "-C", "12", username],
                            input=password + "\n", text=True, capture_output=True, check=True)
    path = root / "config/nginx/comfyui.htpasswd"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".comfyui-auth-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(result.stdout)
            output.flush()
            os.fsync(output.fileno())
        # Official nginx image: worker UID/GID 101. The root master also reads it.
        os.chown(temporary, 0, 101)
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
