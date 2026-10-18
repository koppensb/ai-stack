# AI Stack security policy

The stack assumes a trusted single Docker host and LAN/VPN clients. HTTPS does
not make the host safe for unrestricted Internet exposure. Docker/host administrators
remain trusted; internal service traffic uses HTTP. Certificate-directory ownership
and permissions are deliberately unchanged by this update.

## Applying this update

Use the source installer with `--update-files`. Deploy the whole manifest, not only
`nginx.conf` or Compose: NGINX now also needs `security.conf.template` and the locally
generated `config/nginx/comfyui.htpasswd`. Recreate services after environment changes.
The setup is validated for Open WebUI's 0.12.0 configuration/group APIs; test later
versions before changing the deployment image.

The installer generates missing ComfyUI and Grafana secrets. It never prints their
values. Read them only from the root-protected deployed `.env`; do not paste them
into shell command lines or logs.

## ComfyUI

Port 8444 stays LAN-accessible and requires HTTPS Basic authentication.
`COMFYUI_AUTH_USER` defaults to `comfyui`; `COMFYUI_AUTH_PASSWORD` defaults to a
random 48-character hexadecimal password when left empty. Interactive installation
prompts for it with hidden input (minimum 24 characters). Press Enter to keep an
existing value or generate a new one when none is saved. `--non-interactive` skips
the prompt and retains the same keep/generate behavior. The proxy stores only
a bcrypt hash, mode 0640, root:101 (official NGINX worker group). Custom NGINX
images with a different worker group require matching file ownership.

Open WebUI still calls ComfyUI over the internal Docker network without this
browser password. Changing the proxy password does not change image workflows.
Rerun the installer after changing it. The direct browser origin must match
`PUBLIC_IP_OR_DOMAIN` and `COMFYUI_HTTPS_PORT`; foreign Origin headers are rejected.

## Terminal group

`OPEN_TERMINAL_ACCESS_GROUP=Terminal-Zugriff` creates a managed group with no
members. Add only trusted users in Admin Settings → Groups. They share the same
workspace and can execute commands; this is not per-user isolation. Administrators
retain the upstream administrative bypass. Do not change that bypass without
checking the desired admin/group behavior.

On startup, the managed connection's grants are replaced by that group's read
grant; the former user wildcard is removed. Custom connection options and an
intentional disable are preserved. Unrelated connections are untouched. A name
collision with an unmanaged group stops this migration instead of silently granting
access to existing members. Choose a different group name in that case.

## Registration and session lifetime

Initial signup remains available for creating the first administrator. Once that
administrator exists, the startup helper sets persisted signup to false.
`CLOSE_SIGNUP_AFTER_BOOTSTRAP=false` deliberately disables this enforcement when
an administrator wants to reopen enrollment. New users default to pending.
The security migration sets JWT expiry to 4h once; later expiry edits are retained.
Existing issued tokens may remain valid until their original expiry; this setting
is not a token-revocation operation.

## API and encryption keys

New service keys use cryptographic randomness. The installer rejects known
examples (recognized by SHA-256 fingerprints), short or excessively repetitive keys; this is a format check, not proof
of entropy. It checks both llama.cpp key sources and rotates the obsolete bundled
example value. Real mismatched keys are rejected rather than guessed. The file
secret, `.env` and managed WebUI provider credentials are kept aligned. External
API clients must receive a newly rotated llama.cpp key separately.

Do not rotate `WEBUI_SECRET_KEY` casually: preserve the original value for existing
Open WebUI databases. Missing originals cause the installer to stop before writing
credentials. Recover the original from the old deployment before proceeding.

`GRAFANA_SECRET_KEY` is independent of the administrator password. New installations
receive a random value. Existing SQLite installations without a configured key
are inspected read-only: automatic generation is allowed only when no encryption
material is found in the inspected stores/columns. Otherwise the installer stops.
A non-default original key must be retained in `.env`; a default/weak original key
requires a deliberate Grafana secret migration before this update. Do not insert
a fresh key merely to bypass the check: existing ciphertext will become unreadable.
The conservative check can reject a database requiring version-specific review.
It does not implement arbitrary Grafana ciphertext conversion.

For a default-key database with encrypted values, retain a consistent database and
configuration backup, inventory affected data-source/alert/plugin credentials,
and perform the migration against its exact Grafana version in maintenance mode.
Re-provision those secrets under the new key or use a supported key-provider
migration. Grafana's re-encrypt command alone does not make an unknown old key
recoverable. Never delete the database as a shortcut.

Grafana cookies explicitly use Secure and SameSite=Lax. Secret files and runtime
workspaces are excluded from Git and the Docker build context. Keep database and
configuration backups protected; Docker environment values and Compose secrets
remain accessible to authorized host administrators.

## CORS and browser policy

Open WebUI accepts its configured HTTPS origin including port 8443. The direct
`/v1/` route enforces the same origin at NGINX and supplies controlled preflight
headers; wildcard upstream CORS headers are hidden. CLI/server clients without
Origin remain allowed but must authenticate with the model API key. CORS is not
an authentication mechanism.

NGINX replaces client-supplied X-Forwarded-For with the immediate client IP. The
current design assumes NGINX is the outermost proxy. If another trusted proxy is
added, configure and validate its real-IP trust boundary explicitly.

TLS 1.2/1.3, AEAD ECDHE ciphers and disabled session tickets remain. Unused DHE
entries were removed. TLS-1.3 cipher and curve selection retain OpenSSL defaults.
The enforcing CSP additionally restricts objects, base URLs and form submissions.
A stricter script/connect policy is Report-Only until browser checks cover uploads,
previews, WebSockets, Grafana and tools. Inspect browser CSP messages; no remote
report collector is configured. The stricter script policy is not yet enforced.
Microphone use is allowed for the WebUI origin only; camera/geolocation stay denied.

## Images and maintenance

After an explicit install/update, `scripts/lock_images.py` records all installed
image IDs in `compose.image-lock.json`, plus original references and available
registry digests in `image-inventory.json`. Neither file contains environment
values or credentials. Installer runtime commands and systemd load the lock with
pulling disabled. Manual commands must also include this override to use the lock:

```bash
cd /opt/ai-stack
sudo docker compose -f docker-compose.yml -f compose.image-lock.json up -d --no-build --pull never
```

Build/pull steps in a deliberate installer update use the configured source refs
and tags, then refresh the lock. The lock makes normal startup reproducible, not
source builds across future updates. Keep locked images available locally. Review
upstream releases and use an installed container scanner (for example Docker Scout
or Trivy) on the recorded images before production updates. No scan results are
claimed by this repository change.

## Deployment verification

### Remote browser isolation

Playwright runs separately as a non-root user, with Chromium's namespace/seccomp
sandbox enabled, dropped capabilities, no-new-privileges, a read-only root and
limited disposable storage/resources. Its control endpoint is reachable only on
the internal browser network; it has no host-published port or public proxy route.
Treat that endpoint as privileged browser control. Network isolation supplies its
access boundary, not an Internet-facing authentication mechanism.

The browser has no direct external route and uses the public-only web-egress proxy.
The proxy rejects private, loopback, link-local, multicast, reserved and IPv6
transition destinations and pins DNS answers to the actual connection address.
WebUI's own intercepted requests remain subject to its upstream SSRF controls;
WebUI still has its existing trusted integration/network access. These controls
do not isolate a compromised WebUI process. See the precise boundaries and limits
in [CONFIGURATION.md](CONFIGURATION.md#remote-playwright-web-loader).

The installer derives the browser version from the actual WebUI image and verifies
the pair in disposable containers before writing the image lock. Normal boot
downloads/builds nothing. Container isolation shares the host kernel; keep the
host and both application images maintained through deliberate installer updates.

Before exposing the updated services, verify on the host:

- `nginx -t` inside the recreated NGINX container and Compose health checks.
- ComfyUI without/wrong credentials returns 401; correct credentials work, including
  its WebSocket. A foreign Origin is rejected.
- `/v1/models` without/wrong API key is rejected by llama.cpp; a valid key works.
  A foreign browser Origin returns 403; permitted OPTIONS receives the exact origin.
- WebUI login, native tools, image workflows, streaming and microphone work.
- A normal user outside the terminal group cannot list/use the terminal; a group
  member can. Confirm the public grant is gone in saved Integrations settings.
- Grafana login cookies include Secure, and existing configured data sources work.
- TLS 1.0/1.1 fail, TLS 1.2/1.3 work, and the certificate is trusted, matches the
  hostname and is not near expiry. Arrange CA trust and certificate renewal on the
  host; this update does not replace or renew certificates.
- Review host firewall/NAT and recorded image vulnerabilities independently.

## Empty credential templates

Credential fields in `.env.example` and SearXNG settings are empty. Compose requires
nonempty runtime secrets; the installer generates missing values. Historical
placeholder values are recognized by non-secret fingerprints so upgrades do not
restore insecure example keys. Session/encryption keys are never blindly rotated.
Validate a configured deployment with `docker compose config --quiet`. This
uses its existing `.env`, prints no resolved credentials and starts no services.
