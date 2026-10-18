"""Derive browser build inputs from the actual Open WebUI image, without starting it."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


PROBE = """from importlib.metadata import version, PackageNotFoundError
from pathlib import Path
import json
packages = {name: version(name) for name in ('playwright', 'unstructured')}
# Official images copy backend sources rather than installing the open-webui wheel.
try:
    packages['open-webui'] = version('open-webui')
except PackageNotFoundError:
    packages['open-webui'] = json.loads(Path('/app/package.json').read_text())['version']
print(json.dumps(packages))
"""


def run_json(command, env):
    return json.loads(subprocess.check_output(command, env=env, text=True, timeout=120))


def build_settings(packages, info):
    """Reject unknown versions/architectures instead of substituting a newer browser."""
    version = packages.get("playwright", "")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Open WebUI must contain a stable Playwright package (major.minor.patch).")
    if not packages.get("unstructured") or not packages.get("open-webui"):
        raise ValueError("Open WebUI is missing required web-loader packages.")
    webui = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(packages["open-webui"]))
    if webui is None or tuple(map(int, webui.groups())) < (0, 12, 0):
        raise ValueError("Open WebUI >= 0.12.0 is required for the managed config API and loader protections.")
    if info.get("Os") != "linux" or info.get("Architecture") != "amd64":
        raise ValueError("The installer requires a Linux amd64 Open WebUI image.")
    return version, f"mcr.microsoft.com/playwright/python:v{version}-noble"


def atomic_json(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix=".playwright-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(data, output, indent=2)
            output.write("\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def prepare(root):
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/root")}
    command = ["docker", "compose", "--project-name", "ai-stack", "--env-file", str(root / ".env"),
               "-f", str(root / "docker-compose.yml")]
    if (root / "compose.install.yml").exists():
        command += ["-f", str(root / "compose.install.yml")]
    # Never read the old image lock: this must inspect the newly built WebUI image.
    config = run_json(command + ["config", "--format", "json"], env)
    reference = config["services"]["openwebui"]["image"]
    info = run_json(["docker", "image", "inspect", reference], env)[0]
    image_id = info["Id"]
    packages = run_json([
        "docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--entrypoint", "python", image_id, "-c", PROBE,
    ], env)
    version, base = build_settings(packages, info)
    print(f"Open WebUI {packages['open-webui']} requires Playwright {version}; pulling {base}", flush=True)
    # Pull checks availability and target architecture. Never fall back to latest.
    subprocess.run(["docker", "pull", "--platform", "linux/amd64", base], env=env, check=True)
    browser_info = run_json(["docker", "image", "inspect", base], env)[0]
    if browser_info.get("Architecture") != "amd64" or browser_info.get("Os") != "linux":
        raise ValueError("The Playwright base image is not Linux amd64.")
    digests = browser_info.get("RepoDigests") or []
    digest = next((value for value in digests if value.startswith("mcr.microsoft.com/playwright/python@sha256:")), None)
    if digest is None:
        raise ValueError("The pulled Playwright image has no upstream registry digest.")
    # This file changes build inputs only; runtime settings remain in base Compose.
    # Publish only after all probes/pulls succeed, retaining prior inputs on failure.
    atomic_json(root / "compose.playwright.json", {"services": {"playwright": {"build": {"args": {
        "PLAYWRIGHT_VERSION": version, "PLAYWRIGHT_BASE_IMAGE": digest,
    }}}}})
    atomic_json(root / "playwright-inventory.json", {
        "openwebui_image_id": image_id, "packages": packages,
        "browser_base_reference": base, "browser_base_digest": digest,
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/opt/ai-stack"))
    args = parser.parse_args()
    try:
        prepare(args.root.resolve())
    except (ValueError, KeyError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Playwright preparation failed: {error}. No version fallback was used; fix and rerun.") from error


if __name__ == "__main__":
    main()
