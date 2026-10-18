"""Check the built browser pair in a disposable Compose project, without WebUI data."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid


CLIENT_CHECK = '''
import os
import urllib.error
import urllib.request
from playwright.sync_api import sync_playwright

for name in ('NO_PROXY', 'no_proxy'):
    os.environ.pop(name, None)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({
    'http': 'http://web-egress:3128', 'https': 'http://web-egress:3128',
}))
for url in ('http://127.0.0.1/', 'http://10.0.0.1/', 'http://169.254.169.254/',
            'http://[::1]/', 'http://[::ffff:127.0.0.1]/', 'https://127.0.0.1/'):
    # Ignore ambient no_proxy settings: these requests explicitly test the proxy.
    try:
        opener.open(url, timeout=10)
    except urllib.error.HTTPError as error:
        try:
            if error.code != 403:
                raise
        finally:
            error.close()
    except urllib.error.URLError as error:
        if '403' not in str(error.reason):
            raise
    else:
        raise RuntimeError('Egress proxy allowed an internal target')

with sync_playwright() as playwright:
    browser = playwright.chromium.connect('ws://playwright:3000/browser', timeout=15000)
    try:
        context = browser.new_context(service_workers='block')
        try:
            page = context.new_page()
            page.set_content("<p id='probe'></p><script>document.querySelector('#probe').textContent='ready'</script>")
            assert page.locator('#probe').inner_text() == 'ready'
            response = page.goto('http://127.0.0.1/', timeout=10000)
            assert response is not None and response.status == 403, 'Browser bypassed its proxy'
            response = page.goto('https://example.com/', timeout=30000)
            assert response is not None and response.ok, 'Public HTTPS fetch failed'
        finally:
            context.close()
    finally:
        browser.close()
print('WebUI client/server protocol, JavaScript and public/private egress checks passed.')
'''


def diagnostic_command(command, env):
    """Collect bounded diagnostics without allowing a second failure to hide the first."""
    try:
        result = subprocess.run(command, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, timeout=15, check=False)
        return {"exit_code": result.returncode, "output": result.stdout or ""}
    except (OSError, subprocess.SubprocessError) as error:
        return {"error": str(error)}


def save_failure(root, check, env, phase, error):
    """Persist only test output and container State, never full inspect/env/config data."""
    output = getattr(error, "stdout", None) or ""
    if isinstance(output, bytes):
        output = output.decode(errors="replace")
    report = {
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "error": str(error),
        "check_output": output,
        "services": {},
        "logs": diagnostic_command(check + ["logs", "--no-color", "--tail", "80",
                                              "playwright", "web-egress"], env),
    }
    for service in ("playwright", "web-egress"):
        identifiers = diagnostic_command(check + ["ps", "--all", "--quiet", service], env)
        states = []
        for identifier in identifiers.get("output", "").splitlines():
            # Ignore CLI errors/warnings; only inspect actual container identifiers.
            if len(identifier) in (12, 64) and all(char in "0123456789abcdef" for char in identifier):
                result = diagnostic_command(["docker", "inspect", "--format", "{{json .State}}", identifier], env)
                try:
                    states.append(json.loads(result.get("output", "")))
                except (TypeError, ValueError):
                    states.append(result)
        report["services"][service] = {"lookup": identifiers, "states": states}
    # Root-only diagnostics survive cleanup of the disposable containers.
    path = root / "playwright-check-failure.json"
    descriptor, temporary = tempfile.mkstemp(prefix=".playwright-diagnostic-", dir=root)
    try:
        with os.fdopen(descriptor, "w") as destination:
            json.dump(report, destination, indent=2)
            destination.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    if output:
        print(output, file=sys.stderr)
    print(json.dumps(report["services"], indent=2), file=sys.stderr)
    print(report["logs"].get("output", report["logs"].get("error", "")), file=sys.stderr)
    print(f"Browser failure diagnostics saved to {path}", file=sys.stderr)
    return path


def run_check(command, env, **kwargs):
    result = subprocess.run(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, check=True, **kwargs)
    if result.stdout:
        print(result.stdout, flush=True)


def verify(root):
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/root")}
    command = ["docker", "compose", "--project-name", "ai-stack", "--env-file", str(root / ".env"),
               "-f", str(root / "docker-compose.yml")]
    # Runtime behavior comes from base Compose; build-only overrides are unnecessary.
    config = json.loads(subprocess.check_output(command + ["config", "--format", "json"], env=env))
    project = "ai-browser-check-" + uuid.uuid4().hex[:12]
    client_name = project + "-client"
    services = {}
    for name in ("playwright", "web-egress"):
        service = dict(config["services"][name])
        for key in ("build", "container_name"):
            service.pop(key, None)
        service["restart"] = "no"
        services[name] = service
    networks = {name: {key: value for key, value in network.items() if key != "name"}
                for name, network in config["networks"].items()
                if name in ("browser-net", "browser-outbound")}
    # Resolve image identities once; never use a moving tag for the test client.
    webui = json.loads(subprocess.check_output(
        ["docker", "image", "inspect", config["services"]["openwebui"]["image"]], env=env))[0]["Id"]
    with tempfile.TemporaryDirectory(prefix="ai-browser-check-") as temporary:
        path = Path(temporary) / "compose.json"
        path.write_text(json.dumps({"services": services, "networks": networks}))
        check = ["docker", "compose", "--project-name", project, "-f", str(path)]
        phase = "browser/proxy startup and health checks"
        try:
            run_check(check + ["up", "-d", "--no-build", "--pull", "never", "--wait",
                               "--wait-timeout", "120"], env)
            phase = "WebUI client protocol, JavaScript and egress checks"
            client = ["docker", "run", "--rm", "--name", client_name, "--pull", "never", "--network", project + "_browser-net",
                      "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                      "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777", "--entrypoint", "python",
                      "-i", webui, "-"]
            run_check(client, env, input=CLIENT_CHECK, timeout=120)
        except (subprocess.SubprocessError, OSError) as error:
            try:
                diagnostic_path = save_failure(root, check, env, phase, error)
                detail = f"Read {diagnostic_path} for the actual failure and healthcheck output."
            except (OSError, subprocess.SubprocessError) as diagnostic_error:
                detail = f"Could not save diagnostics: {diagnostic_error}. Original failure: {error}."
            raise RuntimeError(
                f"Browser verification failed during {phase}. {detail} "
                "The Chromium sandbox must remain enabled. "
                "The installed image lock has not been replaced."
            ) from error
        finally:
            # A timed-out Docker CLI can leave its container running. Remove it
            # explicitly before tearing down the disposable network.
            for cleanup in (["docker", "rm", "-f", client_name],
                            check + ["down", "--volumes", "--remove-orphans"]):
                # Cleanup must not replace the useful original verification error.
                try:
                    result = subprocess.run(cleanup, env=env, check=False, timeout=30,
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                    if "down" in cleanup and result.returncode:
                        print(f"Browser cleanup failed: {result.stdout}", file=sys.stderr)
                except (OSError, subprocess.SubprocessError) as cleanup_error:
                    print(f"Browser cleanup failed: {cleanup_error}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/opt/ai-stack"))
    args = parser.parse_args()
    verify(args.root.resolve())


if __name__ == "__main__":
    main()
