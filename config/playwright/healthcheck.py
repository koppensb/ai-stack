"""Check the protocol and JavaScript execution without accessing the Internet."""
import re
from playwright.sync_api import sync_playwright


def check(endpoint="ws://127.0.0.1:3000/browser"):
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect(endpoint, timeout=10000)
        try:
            context = browser.new_context(service_workers="block")
            try:
                page = context.new_page()
                page.set_content("<div id='probe'></div><script>document.querySelector('#probe').textContent='ready'</script>")
                if page.locator("#probe").inner_text() != "ready":
                    raise RuntimeError("Remote browser did not execute JavaScript")
                page.goto("chrome://sandbox", timeout=10000)
                status = page.locator("body").inner_text()
                if not (re.search(r"Namespace [Ss]andbox\s+Yes", status)
                        and re.search(r"Seccomp-BPF sandbox\s+Yes", status)):
                    raise RuntimeError("Chromium namespace/seccomp sandbox is not active")
            finally:
                context.close()
        finally:
            # For a connected browser this disconnects this client, retaining the server.
            browser.close()


if __name__ == "__main__":
    check()
