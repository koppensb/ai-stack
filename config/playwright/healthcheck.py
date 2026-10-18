"""Check the protocol and JavaScript execution without accessing the Internet."""

def validate_sandbox_status(rows):
    """Accept Chromium's current/legacy namespace labels, requiring both layers."""
    status = {cells[0].strip().casefold(): cells[1].strip().casefold()
              for cells in rows if len(cells) == 2}
    # Chromium now reports 'Layer 1 Sandbox: Namespace'; older releases used
    # 'Namespace Sandbox: Yes'. Do not accept SUID or an unknown first layer.
    namespace = (status["layer 1 sandbox"] == "namespace" if "layer 1 sandbox" in status
                 else status.get("namespace sandbox") == "yes")
    if not (namespace and status.get("pid namespaces") == "yes"
            and status.get("network namespaces") == "yes"
            and status.get("seccomp-bpf sandbox") == "yes"):
        raise RuntimeError(f"Chromium namespace/seccomp sandbox is not active; reported status: {status!r}")


def check(endpoint="ws://127.0.0.1:3000/browser"):
    from playwright.sync_api import sync_playwright

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
                # The status table is populated by Chromium's DOMContentLoaded handler.
                page.locator("#sandbox-status tr").first.wait_for(timeout=5000)
                rows = [row.locator("td").all_text_contents()
                        for row in page.locator("#sandbox-status tr").all()]
                validate_sandbox_status(rows)
            finally:
                context.close()
        finally:
            # For a connected browser this disconnects this client, retaining the server.
            browser.close()


if __name__ == "__main__":
    check()
