"""Check the built browser pair in a disposable Compose project, without WebUI data."""
import argparse
import json
import os
from pathlib import Path
import subprocess
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
        try:
            subprocess.run(check + ["up", "-d", "--no-build", "--pull", "never", "--wait",
                                    "--wait-timeout", "120"], env=env, check=True)
            client = ["docker", "run", "--rm", "--name", client_name, "--pull", "never", "--network", project + "_browser-net",
                      "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                      "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777", "--entrypoint", "python",
                      "-i", webui, "-"]
            subprocess.run(client, input=CLIENT_CHECK, text=True, env=env, check=True, timeout=120)
        except (subprocess.SubprocessError, OSError) as error:
            subprocess.run(check + ["logs", "--tail", "80", "playwright", "web-egress"], env=env, check=False)
            raise RuntimeError(
                "Browser verification failed. Check the logs, public HTTPS access and host "
                "user-namespace/AppArmor policy. The Chromium sandbox must remain enabled. "
                "The installed image lock has not been replaced."
            ) from error
        finally:
            # A timed-out Docker CLI can leave its container running. Remove it
            # explicitly before tearing down the disposable network.
            subprocess.run(["docker", "rm", "-f", client_name], env=env, check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(check + ["down", "--volumes", "--remove-orphans"], env=env, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/opt/ai-stack"))
    args = parser.parse_args()
    verify(args.root.resolve())


if __name__ == "__main__":
    main()
