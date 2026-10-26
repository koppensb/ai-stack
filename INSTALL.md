# Install the AI stack on Ubuntu

Requirements: Ubuntu amd64 with systemd and an AMD GPU visible to the host/VM.
A working AMD driver and `rocminfo` can be prepared manually or bootstrapped with
`--install-gpu-driver` on the supported systems described below. Internet access
and sufficient disk space for ROCm images, model weights and downloads are required.

Copy the complete source directory to the GPU host, then run:

```bash
sudo bash /path/to/ai-stack/install_ai_stack.sh --host 192.168.1.50
```

Replace the address with your host's IP or DNS name. The script defaults to its
own directory as source; `--source /path/to/ai-stack` selects another source.
Deployment is fixed to `/opt/ai-stack`.

For file responsibilities, configuration precedence, and workflow formats, see
[CONFIGURATION.md](CONFIGURATION.md).

## What the installer does

1. Checks Ubuntu and source completeness, optionally installs missing GPU packages,
   and verifies host GPU access (pausing for a reboot when needed).
2. Installs base packages, Docker Engine, Compose, Buildx and AMD Container Toolkit.
3. Configures the AMD runtime, restarting Docker if required.
4. Copies only manifest-listed build/runtime files and prepares persistent directories and permissions.
5. Generates missing credentials, a SearXNG secret, an Open Terminal API key and a self-signed TLS certificate.
6. Builds llama.cpp, ComfyUI and the Open WebUI Pandoc derivative, derives the
   browser version from that WebUI image, builds the matching isolated Playwright
   service, pulls other images, then checks llama.cpp GPU
   visibility and performs a small computation in the ComfyUI ROCm container.
   A disposable browser/proxy project checks the WebUI client's protocol,
   JavaScript, active Chromium sandbox and public/private network access before
   the new image lock is written. These checks also run with `--no-start`.
7. Downloads every chat preset from `config/llama-cpp/models.ini`, including MTP
   heads and automatically selected vision projectors, into `data/llama-cpp`.
8. Downloads the shared Qwen-Image-2.1 Q4_K_M model, Qwen3-VL BF16 text/vision
   encoder and Qwen Image 2.1 VAE into `data/comfyui/models`.
9. Recreates services, waits for readiness, checks ComfyUI GPU access and Qwen
   workflow schemas, applies Open WebUI image and terminal settings, and validates Prometheus.
10. Enables automatic model-preset initialization after admin signup and systemd autostart.

The main router exposes only `Big`, shared by Coding, Allround and
Creativ; workspace display names are configured separately.
On a new installation, create the first Open WebUI administrator. The four additional
models (Coding, Allround, Creativ, Image Generation) are created automatically,
normally within five seconds. Refresh the browser afterwards. Original models
remain unchanged. Completion is saved in the database, so later restarts preserve
your edits. No manual script invocation is needed. To explicitly reapply defaults,
see the model presets section in README.md.

Downloads populate disk caches; they do not load all models into VRAM. Presets
on the main router remain on demand with `load-on-startup = false`; the separate
image-prompt preset uses `load-on-startup = true`. The coordinator releases idle
llama.cpp models when ComfyUI needs VRAM, and releases idle ComfyUI models.
See the README for the polling and concurrency limits.

The package setup follows [Docker's Ubuntu instructions](https://docs.docker.com/engine/install/ubuntu/)
and [AMD's runtime guide](https://instinct.docs.amd.com/projects/container-toolkit/en/latest/container-runtime/quick-start-guide.html).
Unavailable packages or images stop installation; another Ubuntu release is not
silently substituted.

## Optional host GPU driver installation

```bash
sudo bash /path/to/ai-stack/install_ai_stack.sh --host 192.168.1.50 --install-gpu-driver
```

Without this flag, an interactive run offers installation when GPU setup is
missing. The default answer is no. `--non-interactive` requires the flag for a
new setup; it never silently approves a driver change. Working installations
are reused only when the installed package can be verified against the selected
release, including when the flag is present. A loaded driver with only
`rocminfo` missing receives the diagnostic package without driver replacement.

Automatic bootstrap currently covers this deliberate subset of the
[ROCm 10.1 compatibility matrix](https://rocm.docs.amd.com/en/latest/compatibility/compatibility-matrix.html):

| Component | Automatic installation |
| --- | --- |
| Host OS | Ubuntu 26.04 (`resolute`), amd64 |
| Running kernel | Ubuntu GA `7.0.x-<ABI>-generic` |
| Driver repository | Selected AMD release (default `31.50`); exact `amdgpu-dkms` package from that repository |
| Navi 48 / gfx1201 | PCI IDs `1002:7550`, `1002:7551` (RX 9070 family / AI PRO R9700) |
| Navi 44 / gfx1200 | PCI ID `1002:7590` |
| Navi 31 / gfx1100 | PCI IDs `1002:7448`, `7449`, `744a`, `744b`, `744c`, `745e` |
| Navi 32 / gfx1101 | PCI IDs `1002:7460`, `7461`, `7470`, `747e` |
| Navi 33 / gfx1102 | PCI IDs `1002:7480`, `7489` |

PCI IDs identify GPU families and can cover several board variants; the later
runtime checks still have to pass. Other GPUs, APUs, Ubuntu releases and custom
kernels require manual preparation. An unsupported additional AMD display device
also blocks automatic installation. The normal installer still accepts existing
working installations. No kernel upgrade, PCI rebinding or Proxmox configuration
is performed. With passthrough, run the installer **inside the Ubuntu VM**, not
on the Proxmox host or inside a container.

The helper follows AMD's [31.50 Ubuntu package-manager instructions](https://instinct.docs.amd.com/projects/amdgpu-docs/en/docs-31.50.0/install/detailed-install/package-manager/package-manager-ubuntu.html).
It uses a dedicated APT signing keyring and `signed-by`, installs matching kernel
headers, DKMS and the driver, and takes the small `rocminfo` package and its HSA
runtime dependencies from the configured Ubuntu repositories (`universe` must
be enabled). It does not install the complete host ROCm SDK, desktop graphics or
OpenCL stack. Application ROCm libraries remain in the containers. Missing
packages stop the run; repositories for another Ubuntu version are never used.
Existing separate AMD driver repository definitions must be resolved manually.

### Selecting the driver release

The central default is `GPU_DRIVER_SERIES=31.50` at the top of
`scripts/install_gpu_driver.sh`. Changing it to `31.60` changes repository setup,
package selection, validation and messages together. Alternatively, override it
for a run without modifying the source:

```bash
sudo bash /path/to/ai-stack/install_ai_stack.sh --host 192.168.1.50 \
  --install-gpu-driver --gpu-driver-series 31.60
```

`--gpu-driver-series` selects the release; `--install-gpu-driver` authorizes
installation of missing packages. Supply the same selection again after reboot.
The helper ignores an environment variable of the same name; it uses the central
source default or the explicit command-line option, not the deployment `.env`.
AMD 31.50 and 31.60 are listed for the current ROCm 10.1 Radeon matrix. Before
selecting another release, verify its GPU, Ubuntu, kernel and container ROCm
compatibility. This option does not expand the hardware/OS allowlist.

The installer only selects packages from the exact release URI and Ubuntu suite;
there is no fallback to another release or APT's global candidate. On initial
installation it takes the first (highest-version) matching APT entry. It records
both the series and exact package version under `/var/lib/ai-stack/gpu-driver`.
An interrupted installation retries that exact package; if unavailable, it stops.
This is a release selector, not an exact patch-version lock for ordinary APT updates.

Existing drivers must match the selected release. Matching recorded series/package
metadata or matching cached repository metadata establishes this. An unknown,
inbox, mismatched or unverified driver causes an error instead of silent reuse.
For a manually prepared matching driver, configure the selected official signed
repository and refresh its APT metadata so the installed package can be verified.

**Changing the selection does not upgrade or downgrade an existing driver.** For
an intentional migration, stop GPU workloads, follow AMD's uninstall/install and
Secure Boot/reboot instructions, and verify the new driver. After completing any
pending installer operation, archive the old `/var/lib/ai-stack/gpu-driver` state
and remove/replace the old installer-managed APT source
`/etc/apt/sources.list.d/ai-stack-amdgpu.list` as part of that maintenance. Rerun
with the new selection and matching signed repository metadata. Do not clear
state to bypass a failed DKMS installation or pending reboot. An ordinary
`--update-files` run never performs driver migration.

### Secure Boot and restart

Secure Boot is detected with `mokutil`. Ubuntu creates the DKMS signing key; an
interactive run asks for a temporary MOK enrollment password. This password is
not saved by the installer. During the reboot, open the Proxmox VM console and
enroll the key using that password. Secure Boot is never disabled automatically.

For unattended installation, a key that has not yet been enrolled produces exit
status **21**, with the exact `mokutil --import` command to run manually. Import
the key, reboot, complete enrollment and rerun the installer. A later driver
installation may require another reboot.

After successful DKMS installation the installer returns **20** (reboot required).
It does not reboot automatically or continue to stack builds/downloads/startup.
Reboot when convenient and rerun the **same command with the same options**.
Progress in `/var/lib/ai-stack/gpu-driver` records the driver series, exact package, phase and boot ID,
not credentials or command arguments. A second run before reboot returns 20
again. After reboot, the installer checks the loaded module and `rocminfo` before
continuing. If verification fails, it retains the marker and reports the problem.

A failed package installation is retried only if this installer created the
installation marker. An existing broken or incompatible driver is not blindly
reinstalled. Driver upgrades/removal remain explicit manual maintenance using
AMD's instructions; `--update-files` and `--install-gpu-driver` do not request a
driver upgrade. The release repository follows the selected series, but installed packages are not
held against normal administrator-managed APT updates.

After building, temporary containers verify GPU access in both application
runtimes, including a small PyTorch matrix multiplication. These checks run with
`--no-start` too, before model downloads. They detect runtime failures but do not
replace a full chat/image inference test on the target hardware.

## Deployment contents

`scripts/deployment_files.txt` is the explicit copy allowlist. The installation
folder receives Compose, application Dockerfiles, `.dockerignore`, service
configuration, Qwen API and native editor workflows and the runtime Python helpers,
including the browser health check and public-only egress proxy.

The installer, downloader, setup helpers, documentation, `.env.example`
and the systemd source units remain in the source tree. The installer runs the
downloader from source with `--root /opt/ai-stack`, streams Open WebUI setup
helpers into the running container, and installs the service unit directly in
`/etc/systemd/system`. It reads the environment template from source and creates
the deployed `.env` as before.

Existing source-only files left by older installations are not removed by this
copy step. Deployment data, secrets and backups are preserved. Keep the source
tree available for future installs, downloads and maintenance checks.

In ComfyUI, open **Workflows → ai-stack** for the bundled generation and
reference-editing templates. The installer includes their files and Compose
mounts them into ComfyUI's default-user workflow directory. Upload both reference
images for editing, and use **Save As** outside the read-only managed folder to
customize a template. These native editor files complement the API workflows
that Open WebUI sends with each request.

## Credentials and configuration

The installer prompts with hidden input for `GRAFANA_ADMIN_PASSWORD` and
`HF_TOKEN`. Enter retains existing values. For a new deployment, Enter generates
a Grafana password and leaves the optional HF token empty. Set a token when
accessing gated models. Single quotes and line breaks are not supported in
these credential values.

`LLAMA_CPP_API_KEY` is generated automatically without a prompt when no saved key
exists. The installer uses 32 random bytes encoded as hex and retains an existing
key from `.env` or `config/llama-cpp/api-key.txt`. Conflicting saved keys still
stop installation so the two values can be synchronized.

`--non-interactive` uses saved values or generated defaults without a terminal.
Credentials are kept in the root-readable `.env`; the llama.cpp key is also
written to its protected Compose secret, with a read ACL for Prometheus.
`WEBUI_SECRET_KEY` is passed to Open WebUI for session signing and encryption.
The installer generates a random 32-byte hex key when the value is empty and
preserves it on subsequent runs. Manual deployments must set it in `.env`
(for example, generate a new key with `openssl rand -hex 32`). Before upgrading
an existing deployment, save its current Open WebUI secret as `WEBUI_SECRET_KEY`
in the deployed `.env` to retain sessions and access to encrypted tokens.
Keep this value stable and include `.env` in protected backups. See the
[Open WebUI secret-key documentation](https://docs.openwebui.com/reference/env-configuration/#webui_secret_key).

Changing the Grafana environment password does not change an existing database
password: retain the current password or reset it in Grafana separately.

The first deployment can import a source `.env`. Later runs preserve the target
`.env`, fill missing keys from `.env.example`, replace known example credentials,
and create a protected backup when modifying settings. `.env` is parsed through
Compose, never executed as shell code. Parent shell variables do not override
the installer's configuration. Existing target TLS certificates are retained;
source certificates are not imported. Replace deployed certificates separately
when changing the hostname.

Saved Open WebUI image settings are updated from the deployed environment and
bundled workflows on each successful installer startup. Image enable switches,
sizes and generation steps are respected. The known legacy FLUX model is
migrated to Qwen. Unrelated saved Open WebUI settings remain unchanged. For web
search, existing databases may need **Admin Settings → Web Search** set to
`searxng` with `http://search-gateway:8080/search?q=<query>&format=json`.
Replace an existing direct SearXNG or third-party gateway URL. The bundled private
gateway rotates free engines and applies shared pacing/cache/cooldowns; it needs
no API keys. The installer creates `data/search-gateway` for UID/GID 65534.

## Options, updates and retries

- `--gpu-driver-series 31.60`: override the central driver release selection for
  this run. Existing mismatched drivers stop installation; migration is manual.
- `--install-gpu-driver`: install missing GPU packages within the supported
  bootstrap scope above. May exit 20/21 and require a reboot/manual MOK enrollment.
- `--update-files`: back up and replace application/configuration files from the
  source. Review local customizations first, especially `models.ini`. Backups
  reside in `.installer-backups/<timestamp>-<pid>/`. Credentials, TLS files and
  runtime data are excluded. Obsolete files are not deleted automatically.
- Without `--update-files`, missing files are added. Differing managed files
  cause an early error so an upgrade cannot silently retain stale configuration.
- `--gfx gfx1201`: choose a GPU target detected by `rocminfo`; required when
  multiple different GPU types are detected.
- `--llama-ref COMMIT_OR_TAG`: pin llama.cpp; otherwise retain its configured
  reference, defaulting to `master`. The chosen revision must support the unified
  `llama download` command, MTP downloading and the configured model architectures.
- `--no-start`: configure, build, pull and download, without starting the service
  stack or enabling new autostart. Temporary downloader containers still run.
  GPU test containers also run. This is not a dry run. Existing running services are not stopped; changed files
  and secrets may affect them. Container recreation is deferred to a later run.
- `--wait 3600`: change the service-readiness timeout (default 1800 seconds).

Example upgrade:

```bash
sudo bash /path/to/updated/ai-stack/install_ai_stack.sh --update-files --non-interactive
```

Keep the complete source tree outside `/opt/ai-stack`; reruns require it.
Running the installer with the deployment directory as its source is rejected.
Recreation briefly interrupts services. A failure stops installation and leaves
completed downloads available for a retry; there is no automatic rollback.

After adding chat presets, prefetch them without rerunning the full installer:

```bash
cd /opt/ai-stack
sudo python3 /path/to/ai-stack/scripts/download_models.py --root /opt/ai-stack --plan
sudo python3 /path/to/ai-stack/scripts/download_models.py --root /opt/ai-stack
```

The plan is offline. The second command uses the built llama.cpp image and the
same bind-mounted cache as the router, then downloads the image assets. It
resolves paths and environment through Compose. A configured image model that
differs from the bundled manifest fails explicitly. Custom image workflows need
a corresponding download manifest. Completed image files are reused and `.part`
files resume; remove a corrupt completed file before retrying its download.

## Open Terminal

The installer starts Open Terminal and registers it in Open WebUI, including
existing databases. Select **Open Terminal** in a chat and use a model with native
function calling. Access is limited to administrators and members of the managed
`Terminal-Zugriff` group. Add trusted users under **Settings → Admin → Groups**.
All users of this connection share `data/open-terminal` (`/home/user` in the
container). The service has outbound network access and no published host port.
Its key is generated once and preserved in `.env` as `OPEN_TERMINAL_API_KEY`;
`OPEN_TERMINAL_VERSION` controls the image tag. Startup and installer reruns enforce
the group grant and remove the old wildcard grant. An intentional disable is preserved.

The installer also enables a daily full terminal reset at 04:00 Europe/Vienna.
All terminal files, installed packages and running commands are discarded; export
needed work beforehand. See the reset operations in [README.md](README.md#open-terminal-in-open-webui).
`--no-start` does not install or enable the reset timer.

## Access and operations

- Open WebUI: `https://<host>:8443/`
- Grafana: `https://<host>:8443/grafana/` (user `admin`)
- ComfyUI: `https://<host>:8444/` (or `COMFYUI_HTTPS_PORT`)

The self-signed certificate causes a browser warning. Ports 8080, 8443 and the
ComfyUI HTTPS port bind to all host addresses. ComfyUI requires the separate
`COMFYUI_AUTH_USER` / `COMFYUI_AUTH_PASSWORD` credentials stored in the root-only
`.env`. The interactive installer asks for the ComfyUI password with hidden input
(minimum 24 characters). Enter keeps the saved value or generates a password when
none exists. `--non-interactive` keeps/generates it without asking. Restrict network
access to the intended LAN/VPN clients.

```bash
cd /opt/ai-stack
sudo docker compose -f docker-compose.yml -f compose.install.yml ps
sudo docker compose -f docker-compose.yml -f compose.install.yml logs -f
sudo systemctl status ai-stack
```

The compatibility override contains the same build arguments as the base Compose
file. The shared `ai-stack.service` starts prebuilt images with `--no-build --pull
never`; boot performs no model prefetch. Shutdown stops containers and preserves
data. Updates and downloads are performed explicitly through the installer.

## Validation scope

No standalone regression-test scripts are kept in the repository. Compose
validation checks resolved service configuration; it does not prove live API
compatibility or inference correctness. The installer performs live
readiness, GPU visibility, workflow-schema and Prometheus checks on the host.
The GPU bootstrap adds a small compute test; these checks do not perform model inference. Validate one chat completion, one image
generation and one reference-image edit on the actual GPU after installation.
Floating image tags and source branches can change. ComfyUI deliberately follows
the latest stable release on each Compose build; record build output for diagnosis.
Other configurable image/source references can be pinned for reproducibility.

## Security update

Read [SECURITY.md](SECURITY.md) before upgrading an existing installation. The
installer creates ComfyUI proxy credentials and a separate Grafana encryption
key when safe. It rejects known example/weak secrets and refuses to invent a new
key for existing encrypted data. Keep the original `.env` and database backups.
After pulling/building, the installer writes `compose.image-lock.json` and
`image-inventory.json`; normal systemd startup uses these installed image identities.
An installer update deliberately refreshes this lock. For manual starts, append
`-f compose.image-lock.json` to the Compose file arguments. Do not prune locked
images until the next successful update. No vulnerability scan is implied by locking.

## Open WebUI document conversion

For the separately built browser and automatic version matching, see
[Remote Playwright web loader](CONFIGURATION.md#remote-playwright-web-loader).
`OPENWEBUI_VERSION=latest` remains supported; no Playwright version is maintained
in `.env`. Each installer update derives it from the newly built WebUI image.
Browser sandbox/network verification must pass before the new image lock is saved.
If host AppArmor or user-namespace policy rejects Chromium, review that host policy;
do not work around it with root, privileged mode or disabled sandboxing.

`openwebui_pandoc.dockerfile` derives `ai-stack-openwebui:latest` from Docker Hub
`openwebui/open-webui:${OPENWEBUI_VERSION}`. Every explicit build checks for an
updated base image. The build reads its installed `pypandoc` version and installs
the exact matching `pypandoc-binary` wheel without dependencies into an isolated
directory. The existing Python wrapper is retained. `PYPANDOC_PANDOC` selects
the bundled executable explicitly; the wheel release number is not the Pandoc
executable version. A missing matching wheel or a failed sandboxed ODT/DOCX
round trip stops the build. Sandbox protection remains enabled.

Deploy with the updated installer and `--update-files`; it builds the derivative
and refreshes the image lock before recreating services. Existing credentials
and data are preserved. Normal restarts install nothing. A manual
`docker compose pull openwebui` does not update this local image; use
`docker compose build openwebui` instead. Installer-managed deployments also
require a refreshed image lock, so use the installer for their upgrades.
