"""Installer security checks. Never print credential values."""
import hashlib
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import tempfile

# Non-secret fingerprints preserve rejection/migration of historical examples.
# No usable example credential is stored in this source file.
LEGACY_PLACEHOLDER_SHA256 = frozenset({
    '057ba03d6c44104863dc7361fe4578965d1887360f90a0895882e58a6248fc86',
    '41bdbd2bcccb6fe15c90a0ca5957fa5277e47408e045547a85f74eda01f5bb1f',
    '5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8',
    '8c6976e5b5410415bde908bd4dee15dfb167a9c873fc4bb8a81f6f2ab448a918',
    'ac07b833641478a1f96a332edb53091cd30eea96225054210aa625be8e3f7221',
    'b30f8e9814e36564c37e7f8c64f075bd559dbae81abb335642625327dd519357',
    'ca612e3566fdfd7cf7efe2b1c9349f461158d07cb78a3750e5c5be686aa8ebdc',
    'f3c3964ca854e172366f81d8bdb327da487c2f8a15f954b116e28961dd6585c0',
})


def is_placeholder(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest() in LEGACY_PLACEHOLDER_SHA256


def strong_secret(name, value, minimum=32):
    if len(value) < minimum or is_placeholder(value) or len(set(value)) < 8:
        raise ValueError(f"{name} is too short, repetitive or a known example; use a random secret")
    if any(c in value for c in "\n\r\x00'"):
        raise ValueError(f"{name} contains unsupported characters")
    return value


def select_llama_key(saved, file_key):
    # Filter both inputs before comparing; never copy the old example back.
    saved = "" if is_placeholder(saved) else saved
    file_key = "" if is_placeholder(file_key) else file_key
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
