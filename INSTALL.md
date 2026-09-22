# Install the AI stack on Ubuntu

Requirements: Ubuntu amd64 with systemd, an installed AMD driver/ROCm, working
`rocminfo`, and GPU access through `/dev/kfd` and `/dev/dri`. Internet access and
sufficient disk space for ROCm images, all model weights and temporary downloads
are required. The installer does not reinstall the host AMD driver or ROCm.

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

1. Checks Ubuntu, GPU access and source completeness.
2. Installs base packages, Docker Engine, Compose, Buildx and AMD Container Toolkit.
3. Configures the AMD runtime, restarting Docker if required.
4. Copies only manifest-listed build/runtime files and prepares persistent directories and permissions.
5. Generates missing credentials, a SearXNG secret, an Open Terminal API key and a self-signed TLS certificate.
6. Builds llama.cpp and ComfyUI and pulls the other service images.
7. Downloads every chat preset from `config/llama-cpp/models.ini`, including MTP
   heads and automatically selected vision projectors, into `data/llama-cpp`.
8. Downloads the shared Qwen-Image-2.1 Q4_K_M model, Qwen3-VL BF16 text/vision
   encoder and Qwen Image 2.1 VAE into `data/comfyui/models`.
9. Recreates services, waits for readiness, checks ComfyUI GPU access and Qwen
   workflow schemas, applies Open WebUI image and terminal settings, and validates Prometheus.
10. Enables automatic model-preset initialization after admin signup and systemd autostart.

The main router exposes only `Qwen3.8-27B`, shared by Coding, Allround and
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

## Deployment contents

`scripts/deployment_files.txt` is the explicit copy allowlist. The installation
folder receives Compose, both Dockerfiles, `.dockerignore`, service configuration,
Qwen API and native editor workflows and three runtime Python programs: the
Open WebUI startup wrapper, model-preset initializer, and VRAM coordinator.

The installer, downloader, setup helpers, tests, documentation, `.env.example`
and the systemd source file remain in the source tree. The installer runs the
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
`searxng` with `http://searxng:8080/search?q=<query>&format=json`.

## Options, updates and retries

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
  This is not a dry run. Existing running services are not stopped; changed files
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
function calling. The connection initially permits administrators only; grant
other trusted users access in **Settings → Admin → Integrations → Open Terminal**.
All users of this connection share `data/open-terminal` (`/home/user` in the
container). The service has outbound network access and no published host port.
Its key is generated once and preserved in `.env` as `OPEN_TERMINAL_API_KEY`;
`OPEN_TERMINAL_VERSION` controls the image tag. Existing connection permissions
and an intentional disable are preserved on installer reruns.

## Access and operations

- Open WebUI: `https://<host>:8443/`
- Grafana: `https://<host>:8443/grafana/` (user `admin`)
- ComfyUI: `https://<host>:8444/` (or `COMFYUI_HTTPS_PORT`)

The self-signed certificate causes a browser warning. Ports 8080, 8443 and the
ComfyUI HTTPS port bind to all host addresses. ComfyUI has no additional login;
restrict network access to the intended trusted clients.

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

The included offline regression suite covers preset sharing and migration with
test doubles. Compose validation checks resolved service configuration; neither
proves live API compatibility or inference correctness. The installer performs live
readiness, GPU visibility, workflow-schema and Prometheus checks on the host.
These checks do not perform inference. Validate one chat completion, one image
generation and one reference-image edit on the actual GPU after installation.
Floating image tags and source branches can change. ComfyUI deliberately follows
the latest stable release on each Compose build; record build output for diagnosis.
Other configurable image/source references can be pinned for reproducibility.
