"""Bounded HTTP/HTTPS forward proxy: public IPs only, DNS pinned per connection.

The browser has no external network route. CONNECT is limited to TCP/443;
plain HTTP to TCP/80. No credentials, cache or request URLs are logged.
"""
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import select
import socket
import threading
import time
from urllib.parse import urlsplit


MAX_BODY = 4 * 1024 * 1024
MAX_RESPONSE = 64 * 1024 * 1024
IDLE_SECONDS = 30
LIFETIME_SECONDS = 120
HOP_HEADERS = {"connection", "proxy-connection", "proxy-authorization", "proxy-authenticate",
               "keep-alive", "transfer-encoding", "te", "trailer", "upgrade"}


def public_address(value):
    address = ipaddress.ip_address(value.split("%", 1)[0])
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        # Reject transition mechanisms that can conceal a different IPv4 target.
        if address.ipv4_mapped or address.sixtofour or address.teredo:
            return False
        if any(address in network for network in (
            ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"),
        )):
            return False
    return True


def connect_public(host, port):
    """Resolve once, reject mixed public/private answers, and dial numeric IPs only."""
    if not host or "%" in host or any(ord(char) <= 32 for char in host):
        raise ValueError("Invalid target")
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not public_address(item[4][0]) for item in addresses):
        raise ValueError("Non-public target denied")
    for family, kind, protocol, _, address in addresses:
        connection = socket.socket(family, kind, protocol)
        connection.settimeout(IDLE_SECONDS)
        try:
            connection.connect(address)
            return connection
        except OSError:
            connection.close()
    raise OSError("Public target unavailable")


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "WebEgress"
    sys_version = ""
    timeout = IDLE_SECONDS

    def log_message(self, *_):
        pass

    def handle_expect_100(self):
        self.send_error(417, "Expect is unsupported")
        return False

    def do_CONNECT(self):
        upstream = None
        established = False
        self.close_connection = True
        try:
            target = urlsplit("//" + self.path)
            if (target.port != 443 or target.username is not None or target.password is not None
                    or target.path or target.query or target.fragment):
                raise ValueError("CONNECT requires a host and port 443")
            upstream = connect_public(target.hostname, 443)
            self.send_response(200, "Connection Established")
            self.end_headers()
            self.wfile.flush()
            established = True
            deadline = time.monotonic() + LIFETIME_SECONDS
            transferred = 0
            while time.monotonic() < deadline and transferred < MAX_RESPONSE:
                readable, _, _ = select.select([self.connection, upstream], [], [],
                                               min(IDLE_SECONDS, max(0, deadline - time.monotonic())))
                if not readable:
                    break
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    transferred += len(data)
                    destination = upstream if source is self.connection else self.connection
                    destination.sendall(data)
        except ValueError:
            if not established:
                self.send_error(403, "Target denied")
        except OSError:
            if not established:
                self.send_error(502, "Target unavailable")
        finally:
            if upstream is not None:
                upstream.close()

    def forward(self):
        self.close_connection = True
        upstream = None
        response_started = False
        try:
            target = urlsplit(self.path)
            if (target.scheme != "http" or not target.hostname or target.port not in (None, 80)
                    or target.username is not None or target.password is not None or target.fragment):
                raise ValueError("Only absolute HTTP URLs on port 80 are allowed")
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) > 1:
                raise ValueError("Ambiguous request framing")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= MAX_BODY:
                raise ValueError("Request body exceeds limit")
            body = self.rfile.read(length) if length else None
            if body is not None and len(body) != length:
                raise ValueError("Incomplete request body")
            upstream = HTTPConnection(target.hostname, 80, timeout=IDLE_SECONDS)
            upstream.sock = connect_public(target.hostname, 80)
            connection_headers = {part.strip().lower() for part in self.headers.get("Connection", "").split(",")}
            headers = {name: value for name, value in self.headers.items()
                       if name.lower() not in HOP_HEADERS | connection_headers | {"host", "content-length"}}
            host = target.hostname
            headers["Host"] = f"[{host}]" if ":" in host else host
            headers["Connection"] = "close"
            upstream.request(self.command, (target.path or "/") + ("?" + target.query if target.query else ""),
                             body=body, headers=headers)
            response = upstream.getresponse()
            if response.status == 101:
                raise ValueError("HTTP protocol upgrades are unsupported")
            self.send_response(response.status)
            response_headers = {part.strip().lower() for part in response.getheader("Connection", "").split(",")}
            for name, value in response.getheaders():
                if name.lower() not in HOP_HEADERS | response_headers:
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            response_started = True
            deadline = time.monotonic() + LIFETIME_SECONDS
            transferred = 0
            while self.command != "HEAD" and time.monotonic() < deadline and transferred < MAX_RESPONSE:
                data = response.read1(min(65536, MAX_RESPONSE - transferred))
                if not data:
                    break
                transferred += len(data)
                self.wfile.write(data)
        except ValueError:
            if not response_started:
                self.send_error(403, "Request denied")
        except (OSError, HTTPException):
            # HTTP parser/protocol errors also close the connection; never relay details.
            if not response_started:
                self.send_error(502, "Target unavailable")
        finally:
            if upstream is not None:
                upstream.close()

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.forward()

    do_HEAD = forward
    do_POST = forward
    do_OPTIONS = forward
    do_PUT = forward
    do_PATCH = forward
    do_DELETE = forward


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


if __name__ == "__main__":
    BoundedServer(("0.0.0.0", 3128), Proxy).serve_forever()
