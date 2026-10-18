"""Focused, Docker-free tests for browser preparation, migration and egress policy."""
import asyncio
import importlib.util
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare = load("prepare", "scripts/prepare_playwright.py")
proxy = load("egress", "config/web-egress/proxy.py")
verify = load("verify", "scripts/verify_playwright.py")
sys.path.insert(0, str(ROOT / "config/openwebui"))
presets = load("presets", "config/openwebui/apply_openwebui_parameters.py")


class PreparationTests(unittest.TestCase):
    packages = {"playwright": "1.60.0", "unstructured": "0.22.0", "open-webui": "0.12.0"}
    image = {"Id": "sha256:built-webui", "Architecture": "amd64", "Os": "linux"}

    def test_derives_exact_version_and_rejects_unsupported_inputs(self):
        version, reference = prepare.build_settings(self.packages, self.image)
        self.assertEqual(version, "1.60.0")
        self.assertEqual(reference, "mcr.microsoft.com/playwright/python:v1.60.0-noble")
        for version in ("latest", "1.60.0.dev1", "1.60.0;echo bad", ""):
            with self.assertRaises(ValueError):
                prepare.build_settings(dict(self.packages, playwright=version), self.image)
        for key, value in (("Architecture", "arm64"), ("Os", "windows")):
            with self.assertRaises(ValueError):
                prepare.build_settings(self.packages, dict(self.image, **{key: value}))
        with self.assertRaises(ValueError):
            prepare.build_settings(dict(self.packages, **{"open-webui": "0.9.5"}), self.image)

    def test_probe_supports_source_based_official_images(self):
        def version(name):
            if name == "open-webui":
                from importlib.metadata import PackageNotFoundError
                raise PackageNotFoundError(name)
            return self.packages[name]
        with patch("importlib.metadata.version", side_effect=version), \
                patch.object(Path, "read_text", return_value='{"version":"0.12.0"}'), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            exec(compile(prepare.PROBE, "<probe>", "exec"), {})
        self.assertEqual(json.loads(output.getvalue()), self.packages)

    def test_prepares_atomically_and_failure_preserves_previous_inputs(self):
        config = {"services": {"openwebui": {"image": "ai-stack-openwebui:latest"}}}
        browser = dict(self.image, RepoDigests=["mcr.microsoft.com/playwright/python@sha256:verified"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "compose.playwright.json"
            path.write_text("previous")
            with patch.object(prepare, "run_json", side_effect=[config, [self.image], self.packages]), \
                    patch.object(prepare.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "pull")):
                with self.assertRaises(subprocess.CalledProcessError):
                    prepare.prepare(root)
            self.assertEqual(path.read_text(), "previous")
            with patch.object(prepare, "run_json", side_effect=[config, [self.image], self.packages, [browser]]) as calls, \
                    patch.object(prepare.subprocess, "run") as pull:
                prepare.prepare(root)
            args = json.loads(path.read_text())["services"]["playwright"]["build"]["args"]
            self.assertEqual(args["PLAYWRIGHT_VERSION"], "1.60.0")
            self.assertEqual(args["PLAYWRIGHT_BASE_IMAGE"], browser["RepoDigests"][0])
            self.assertIn("--platform", pull.call_args.args[0])
            probe = calls.call_args_list[2].args[0]
            self.assertIn("none", probe)
            self.assertIn("--read-only", probe)
            self.assertNotIn("--volume", probe)
            self.assertIn("sha256:built-webui", probe)
            self.assertNotIn("compose.image-lock.json", str(calls.call_args_list))
            self.assertEqual(list(root.glob(".playwright-*")), [])

    def test_verification_uses_disposable_networks_and_always_cleans_up(self):
        config = {
            "services": {
                "openwebui": {"image": "webui"},
                "playwright": {"image": "browser", "build": {}, "read_only": True,
                               "networks": {"browser-net": None}, "security_opt": ["no-new-privileges:true"]},
                "web-egress": {"image": "proxy", "networks": {"browser-net": None, "browser-outbound": None}},
            },
            "networks": {"browser-net": {"name": "ai-stack_browser-net", "internal": True},
                         "browser-outbound": {"name": "ai-stack_browser-outbound", "driver": "bridge"}},
        }
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            if "up" in command:
                document = json.loads(Path(command[command.index("-f") + 1]).read_text())
                self.assertNotIn("name", document["networks"]["browser-net"])
                self.assertTrue(document["services"]["playwright"]["read_only"])
                self.assertNotIn("openwebui", document["services"])
            if "--entrypoint" in command:
                self.assertNotIn("--volume", command)
                self.assertIn("sha256:webui", command)
                self.assertEqual(command[-2:], ["sha256:webui", "-"])
                raise subprocess.CalledProcessError(1, "client")

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(verify.subprocess, "check_output", side_effect=[json.dumps(config), '[{"Id":"sha256:webui"}]']), \
                patch.object(verify.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "image lock has not been replaced"):
                verify.verify(Path(directory))
        self.assertIn("down", commands[-1])
        self.assertIn("--volumes", commands[-1])


class EgressTests(unittest.TestCase):
    def test_blocks_private_and_transition_addresses(self):
        for address in ("127.0.0.1", "10.1.2.3", "172.17.0.1", "192.168.1.1", "169.254.169.254",
                        "100.64.0.1", "0.0.0.0", "224.0.0.1", "::1", "fc00::1", "fe80::1",
                        "::ffff:127.0.0.1", "2002:7f00:1::", "64:ff9b::7f00:1", "ff02::1"):
            with self.subTest(address=address):
                self.assertFalse(proxy.public_address(address))
        self.assertTrue(proxy.public_address("93.184.215.14"))
        self.assertTrue(proxy.public_address("2606:4700:4700::1111"))

    def test_dns_is_pinned_and_mixed_answers_are_denied(self):
        public = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))
        private = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        with patch.object(proxy.socket, "getaddrinfo", return_value=[public, private]), \
                patch.object(proxy.socket, "socket") as dial:
            with self.assertRaises(ValueError):
                proxy.connect_public("rebind.example", 443)
            dial.assert_not_called()
        with patch.object(proxy.socket, "getaddrinfo", return_value=[public]) as dns, \
                patch.object(proxy.socket, "socket") as dial:
            proxy.connect_public("public.example", 443)
            dns.assert_called_once()
            dial.return_value.connect.assert_called_once_with(("93.184.215.14", 443))

    def test_live_proxy_denies_targets_and_supports_health(self):
        server = proxy.BoundedServer(("127.0.0.1", 0), proxy.Proxy)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            address = "http://127.0.0.1:" + str(server.server_port)
            direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with direct.open(address + "/health", timeout=2) as response:
                self.assertEqual(response.read(), b"ok")
            for target in ("http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/",
                           "http://example.com:22/", "https://127.0.0.1/"):
                with self.subTest(target=target), socket.create_connection(server.server_address, timeout=2) as client:
                    if target.startswith("https:"):
                        request = "CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1:443\r\n\r\n"
                    else:
                        request = f"GET {target} HTTP/1.1\r\nHost: ignored\r\nConnection: close\r\n\r\n"
                    client.sendall(request.encode())
                    self.assertIn(b"403", client.recv(4096).split(b"\r\n")[0])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_http_forwarding_and_redirect_revalidation(self):
        class Site(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(302 if self.path == "/redirect" else 200)
                if self.path == "/redirect":
                    self.send_header("Location", "http://127.0.0.1/private")
                self.send_header("Content-Length", "6")
                self.end_headers()
                self.wfile.write(b"public")

        site = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        server = proxy.BoundedServer(("127.0.0.1", 0), proxy.Proxy)
        threads = [threading.Thread(target=item.serve_forever, daemon=True) for item in (site, server)]
        for thread in threads:
            thread.start()
        original = proxy.connect_public

        def connect(host, port):
            # Test fixture only: the public site is represented by a local server.
            if host == "public.example" and port == 80:
                return socket.create_connection(site.server_address, timeout=2)
            return original(host, port)

        address = "http://127.0.0.1:" + str(server.server_port)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": address}))
        try:
            with patch.object(proxy, "connect_public", side_effect=connect), \
                    patch("urllib.request.proxy_bypass", return_value=False):
                with opener.open("http://public.example/", timeout=2) as response:
                    self.assertEqual(response.read(), b"public")
                with self.assertRaises(urllib.error.HTTPError) as error:
                    opener.open("http://public.example/redirect", timeout=2)
                self.assertEqual(error.exception.code, 403)
                error.exception.close()
        finally:
            for item in (site, server):
                item.shutdown()
                item.server_close()
            for thread in threads:
                thread.join()

    def test_https_connect_relays_bytes_to_validated_destination(self):
        class Echo(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.sendall(self.request.recv(1024))

        upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo)
        server = proxy.BoundedServer(("127.0.0.1", 0), proxy.Proxy)
        threads = [threading.Thread(target=item.serve_forever, daemon=True) for item in (upstream, server)]
        for thread in threads:
            thread.start()
        try:
            with patch.object(proxy, "connect_public", side_effect=lambda host, port:
                              socket.create_connection(upstream.server_address, timeout=2)) as connect, \
                    socket.create_connection(server.server_address, timeout=2) as client:
                client.sendall(b"CONNECT public.example:443 HTTP/1.1\r\nHost: public.example:443\r\n\r\n")
                headers = b""
                while b"\r\n\r\n" not in headers:
                    headers += client.recv(4096)
                self.assertIn(b"200", headers.split(b"\r\n")[0])
                client.sendall(b"tunnel-data")
                self.assertEqual(client.recv(1024), b"tunnel-data")
                connect.assert_called_once_with("public.example", 443)
        finally:
            for item in (upstream, server):
                item.shutdown()
                item.server_close()
            for thread in threads:
                thread.join()


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_incomplete_writes_and_preserves_later_edits(self):
        class Config:
            data = {}
            fail = True

            async def get(self, key):
                return self.data.get(key)

            async def upsert(self, values):
                self.data.update(values)
                if self.fail and "web.loader.engine" in values:
                    self.data.pop("web.loader.playwright_ws_url", None)

        config = Config()
        environment = {"WEB_LOADER_ENGINE": "playwright", "PLAYWRIGHT_WS_URL": "ws://playwright:3000/browser",
                       "PLAYWRIGHT_TIMEOUT": "30000"}
        with patch.dict("os.environ", environment):
            with self.assertRaises(RuntimeError):
                await presets.apply_browser_loader(config)
            self.assertNotIn("ai_stack.remote_playwright_v1", config.data)
            config.fail = False
            await presets.apply_browser_loader(config)
            self.assertTrue(config.data["ai_stack.remote_playwright_v1"])
            config.data["web.loader.playwright_timeout"] = 15000
            await presets.apply_browser_loader(config)
            self.assertEqual(config.data["web.loader.playwright_timeout"], 15000)


if __name__ == "__main__":
    unittest.main()
