"""Validate the example Compose configuration without stored or reusable credentials."""
import os
from pathlib import Path
import secrets
import subprocess


def main():
    root = Path(__file__).resolve().parents[1]
    # Keep only CLI discovery settings; do not inherit deployment overrides.
    env = {name: os.environ[name] for name in
           ("PATH", "HOME", "DOCKER_CONFIG") if name in os.environ}
    for name in ("LLAMA_CPP_API_KEY", "GRAFANA_ADMIN_PASSWORD", "GRAFANA_SECRET_KEY",
                 "COMFYUI_AUTH_PASSWORD", "OPEN_TERMINAL_API_KEY", "WEBUI_SECRET_KEY",
                 "SEARXNG_SECRET"):
        env[name] = secrets.token_hex(32)
    subprocess.run(["docker", "compose", "--env-file", str(root / ".env.example"),
                    "-f", str(root / "docker-compose.yml"), "config", "--quiet"],
                   env=env, cwd=root, check=True)
    print("Compose syntax valid; temporary credentials were not saved and no services were started.")


if __name__ == "__main__":
    main()
