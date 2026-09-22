# AMD GPU Passthrough on Proxmox with Ubuntu VM and AI Stack

> This guide covers AMD GPU passthrough on Proxmox VE for an Ubuntu virtual
> machine and deployment of the Docker-based AI stack in this repository.
> Ollama is optional and is only used as a separate GPU smoke test.

The stack includes llama.cpp, Open WebUI, Open Terminal, ComfyUI, SearXNG web search, and observability services.
ComfyUI is configured to build from
`rocm/pytorch:rocm10.0_ubuntu26.04_py3.14_pytorch_release_2.13.0`, with
the latest stable ComfyUI release and bundled ComfyUI-GGUF nodes.

---

# Overview

For a file-by-file explanation of runtime settings, workflow JSON, and update
behavior, see [CONFIGURATION.md](CONFIGURATION.md).

## Prerequisites

- Proxmox VE host
- AMD GPU
- Ubuntu VM (UEFI)
- Hardware virtualization enabled in BIOS
- Root access to both Proxmox and Ubuntu

---

# Hardware Preparation (BIOS)

Before configuring Proxmox, enable the required virtualization and PCIe features in your BIOS.

## Enable the Following Settings

- **IOMMU (Intel VT-d or AMD-Vi)**
- **Above 4G Memory Mapping**
- **Resizable BAR**
- **IOMMU Protection**

> Option names may vary depending on the motherboard manufacturer.

---

# Prepare the Proxmox Host

## Update GRUB Configuration

Edit:

```bash
/etc/default/grub
```

### For Intel Systems

```bash
GRUB_CMDLINE_LINUX_DEFAULT="quiet intel_iommu=on iommu=pt"
```

### For AMD Systems

```bash
GRUB_CMDLINE_LINUX_DEFAULT="quiet amd_iommu=on iommu=pt"
```

---

## Load VFIO Modules

Create:

```bash
/etc/modules-load.d/vfio.conf
```

Add:

```text
vfio
vfio_pci
vfio_iommu_type1
vfio_virqfd
```

---

## Blacklist the AMD Driver on the Host

Edit or create:

```bash
/etc/modprobe.d/blacklist.conf
```

Add:

```text
blacklist amdgpu
```

---

## Identify GPU Hardware IDs

Run:

```bash
lspci -nn | grep -iE "VGA|Audio"
```

Example output:

```text
03:00.0 VGA compatible controller [0300]:
Advanced Micro Devices, Inc. [AMD/ATI]
Navi 48 [Radeon AI PRO R9700] [1002:7551]

03:00.1 Audio device [0403]:
Advancedevices, Inc. [AMD/ATI]
Navi 48 HDMI/DP Audio Controller [1002:ab40]
```

### Record the Following Information

#### Hardware IDs

```text
1002:7551
1002:ab40
```

#### PCI Bus Address

```text
03:00.0
```

---

## Configure VFIO

Create:

```bash
/etc/modprobe.d/vfio.conf
```

Add:

```bash
options vfio-pci ids=1002:7551,1002:ab40 disable_vga=1
```

> Replace the IDs with those of your GPU.

---

## Apply Changes

```bash
update-grub
update-initramfs -u -k all
reboot
```

After the reboot, verify that every function passed to the VM is bound to
`vfio-pci` on the Proxmox host:

```bash
lspci -nnk -s 03:00.0
lspci -nnk -s 03:00.1
```

The output should contain `Kernel driver in use: vfio-pci`. If other devices
are in the same IOMMU group, pass through all devices in that group or move the
GPU to a different PCIe slot. Do not bypass IOMMU isolation without first
understanding the security implications.

---

# Create the Ubuntu Virtual Machine

## VM Configuration

When creating the VM, use:

### Machine Type

```text
q35
```

### CPU Type

```text
host
```

Create the VM but **do not start it yet**.

---

## Add the PCI Device

Navigate to:

**Hardware → Add → PCI Device**

Configure:

- Select the raw PCI device
- Choose the GPU PCI address (e.g. `03:00.0`)
- Enable **All Functions**
- Open **Advanced**
- Enable **ROM-Bar**
- Enable **PCI-Express**

---

# Install Ubuntu

Install Ubuntu as usual.

---

# Install QEMU Guest Agent

```bash
sudo apt update
sudo apt install qemu-guest-agent
```

Enable and start the service:

```bash
sudo systemctl enable --now qemu-guest-agent
```

---

# Install Packages

```bash
sudo apt install libssl-dev cmake curl ca-certificates gnupg openssl git wget rsync python3 acl
```

---

# Transfer SSH Keys

Copy your existing SSH keys to the VM.

Example:

```bash
ssh-copy-id user@vm-ip
```

---

# Install AMD Drivers and ROCm

## Download the Installer

```bash
wget https://repo.radeon.com/amdgpu-install/31.50/ubuntu/resolute/amdgpu-install_31.50.315000-1_all.deb
```

## Install the Package

```bash
sudo dpkg -i amdgpu-install_31.50.315000-1_all.deb
sudo apt update
```

## Install AMD Drivers

```bash
sudo amdgpu-install --usecase=graphics,opencl,rocm
```

---

## Configure UEFI Secure Boot

If Secure Boot is enabled, a Machine Owner Key (MOK) must be enrolled during
installation. If the kernel module is not signed or the MOK is not enrolled,
the AMD driver will not load.

1. Set a UEFI password when prompted.
2. Reboot the VM.
3. Open the VM console from Proxmox.
4. Enroll the MOK during startup.

---

## Add User to Required Groups

```bash
sudo usermod -aG render,video $USER
```

Log out and back in afterward.

---

## Verify ROCm Installation

```bash
rocminfo
```

If the GPU is detected, ROCm has been installed successfully.

---

# Automated Installation on a Prepared Ubuntu VM

If the AMD driver and ROCm are already installed and `rocminfo` detects the GPU,
use [install_ai_stack.sh](install_ai_stack.sh) to install the remaining packages
and deploy the stack:

```bash
sudo bash /path/to/ai-stack/install_ai_stack.sh --host 192.168.1.50
```

Replace the IP address with the Ubuntu VM's IP or DNS name. The installer uses
its own directory as the source and deploys to `/opt/ai-stack`. It installs
Docker, Compose, Buildx and AMD Container Toolkit, creates credentials and TLS,
builds both ROCm images, downloads every chat preset and all five Qwen image
assets, checks startup and enables systemd autostart.
Only files listed in `scripts/deployment_files.txt` are copied into `/opt/ai-stack`.
Installer scripts, setup helpers, tests, templates and documentation stay in the
source tree. Runtime Python programs remain installed because services need them.
Keep the source tree outside the deployment directory for updates and maintenance.
See [INSTALL.md](INSTALL.md) for prerequisites, options and rerun behavior.

For an installation created by the script, include its generated override in
manual Compose commands:

```bash
cd /opt/ai-stack
sudo docker compose -f docker-compose.yml -f compose.install.yml ps
```

The override references GPU and llama.cpp build parameters from `.env`. Both
installation paths use the supplied `ai-stack.service`, which starts existing
images without building, pulling or downloading models. Upgrades from another
source directory use `--update-files` to back up and replace managed files;
see [INSTALL.md](INSTALL.md).

---

# Install Docker

## Add Docker's Official GPG Key

```bash
sudo apt update
sudo apt install ca-certificates curl

sudo install -m 0755 -d /etc/apt/keyrings

sudo curl -fsSL \
https://download.docker.com/linux/ubuntu/gpg \
-o /etc/apt/keyrings/docker.asc

sudo chmod a+r /etc/apt/keyrings/docker.asc
```

---

## Add Docker Repository

```bash
sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
```

---

## Install Docker

```bash
sudo apt update

sudo apt install \
docker-ce \
docker-ce-cli \
containerd.io \
docker-buildx-plugin \
docker-compose-plugin
```

---

## Update User Group Membership

Add the user to the Docker group:

```bash
sudo usermod -aG docker $USER
```

Log out and back in afterward.

---

# Install the AMD Container Toolkit

Docker must already be installed before configuring its AMD runtime.

```bash
sudo install -m 0755 -d /etc/apt/keyrings
wget -qO- https://repo.radeon.com/rocm/rocm.gpg.key \
  | gpg --dearmor \
  | sudo tee /etc/apt/keyrings/rocm.gpg >/dev/null
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/rocm.gpg] https://repo.radeon.com/amd-container-toolkit/apt/ $(. /etc/os-release && echo "$VERSION_CODENAME") main" \
  | sudo tee /etc/apt/sources.list.d/amd-container-toolkit.list
sudo apt update
sudo apt install amd-container-toolkit
sudo amd-ctk runtime configure
sudo systemctl restart docker
```

Verify that Docker knows the AMD runtime:

```bash
docker info | sed -n '/Runtimes/p'
```

The output must list `amd`. The Compose services `llama-cpp`, `comfyui` and
`llama-metrics-discovery` all require this runtime.

---

# Optional: Test the GPU with Ollama

Ollama is not used by the Compose stack. Install it only if a separate GPU
smoke test is useful:

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama run llama3
```

Stop Ollama after the test so that it does not reserve GPU memory needed by
llama.cpp.

---

# Deploy the AI Stack

## 1. Install the Repository

The supplied Compose file and systemd unit use `/opt/ai-stack` by default.
Copy only the deployment manifest entries. Keep the complete source tree at a
separate location for installer runs, setup helpers and tests:

```bash
sudo mkdir -p /opt/ai-stack
sudo rsync -ar --files-from=/path/to/ai-stack/scripts/deployment_files.txt /path/to/ai-stack/ /opt/ai-stack/
cd /opt/ai-stack
```

## 2. Configure Environment and Secrets

Create the local environment file:

```bash
sudo test -e .env || sudo install -m 600 /path/to/ai-stack/.env.example .env
sudo nano .env
```

At minimum, replace these example values:

- `LLAMA_CPP_API_KEY`: key used by Open WebUI and the test scripts.
- `GRAFANA_ADMIN_PASSWORD`: initial Grafana administrator password.
- `SEARXNG_SECRET`: random secret generated with `openssl rand -hex 32`.
- `PUBLIC_IP_OR_DOMAIN`: hostname or IP used in Grafana links.
- `HF_TOKEN`: Hugging Face token, when the selected model requires one.

llama.cpp reads its key from a Docker secret rather than directly from `.env`.
The value in `config/llama-cpp/api-key.txt` **must be identical** to
`LLAMA_CPP_API_KEY` in `.env`:

```bash
sudo test -e config/llama-cpp/api-key.txt || sudo install -m 600 /dev/null config/llama-cpp/api-key.txt
sudo nano config/llama-cpp/api-key.txt
sudo chown root:root .env config/llama-cpp/api-key.txt
sudo chmod 600 .env config/llama-cpp/api-key.txt
sudo setfacl -m u:65534:r config/llama-cpp/api-key.txt
```

Prometheus also reads this secret and runs as UID 65534. The ACL above grants
that UID read access while keeping `.env` readable only by root. File-backed
Compose secrets use the host file permissions; root/docker ownership with mode
640 alone does not grant access to Prometheus.

### Select Models and GPU Settings

The router presets are defined in `config/llama-cpp/models.ini`. Each section is
a model ID accepted by the OpenAI-compatible API. The installer prefetches every
preset, including MTP heads and automatically selected vision projectors, into
`data/llama-cpp`. The shared chat model keeps `load-on-startup = false`: files are
available locally, but enter GPU memory only when requested. The first inference
still needs time to load the weights. For manual setup or after adding presets,
build the images and run:

```bash
sudo python3 /path/to/ai-stack/scripts/download_models.py --root /opt/ai-stack --plan
sudo python3 /path/to/ai-stack/scripts/download_models.py --root /opt/ai-stack
```

The second command downloads chat presets and all five image assets. It uses
`llama download` in the same image and cache as the router, without starting
inference. A recent llama.cpp version with the unified `llama` application and
`download --mtp` support is required; the Dockerfile checks the application at
build time. Download failures stop installation. Rerun to reuse completed files
and resume partial downloads. Changing image models also requires matching
workflows and an updated image download manifest; a mismatch fails explicitly.

Review the following settings before deployment:

- Change `config/llama-cpp/models.ini` to select other models or quantizations.
- Set `LLAMA_CPP_MODELS_MAX` in `.env` if more than one model may stay loaded.
- Set `AMD_VISIBLE_DEVICES` and `GPU_INDEX` when the VM exposes multiple GPUs.
- Adjust `LLAMA_CPP_CTX_SIZE`, batch sizes, and `RESERVE_VRAM_MB` to fit the
  available VRAM. The defaults are optimized for a large-memory GPU.
- `COMFYUI_URL` defaults to `http://comfyui:8188`; the included ComfyUI service
  is already attached to `backend-net`. If it is unavailable, automatic VRAM
  discovery can fall back to AMD SMI, ROCm SMI, or sysfs.

The bundled ComfyUI image releases models and execution/allocator caches inside
its prompt worker **before** publishing history and the final `executing` event.
Open WebUI waits for that event before returning the generated image. This avoids
starting the next chat model load while ComfyUI still holds the previous job's
GPU allocations. The same handoff runs after image edits and failed executions;
image outputs and the original success/error status are preserved. Cleanup
failures are logged without terminating the worker or discarding the image.
Each image job must reload its models, including consecutive queued images.

This is a guarded build-time patch of the ComfyUI worker in
`comfyui_rocm.dockerfile`. An incompatible worker layout stops the build instead
of silently omitting the handoff. It uses ComfyUI's own unload, executor reset,
garbage collection and synchronized allocator cleanup operations.

The coordinator remains a fallback for external/unpatched ComfyUI instances.
It requests full cleanup immediately when an observed busy queue becomes empty;
a new job clears the previous release's cooldown. At startup, or without an
observed job, it waits `COMFYUI_IDLE_UNLOAD_SECONDS` (default 5 seconds).
This works even when llama.cpp is unavailable and does not require VRAM pressure.
Set this value to `0` to disable the coordinator's proactive idle unloading;
this does not disable the bundled image's completion handoff. Running or queued
jobs prevent coordinator cleanup. When llama.cpp is observed loading, aggressive
cleanup is requested for idle ComfyUI regardless of the reserve threshold.
Retries within the same idle period retain `VRAM_COOLDOWN_SECONDS`, including
after errors. Pressure-driven soft cleanup can escalate after
`VRAM_AGGRESSIVE_DELAY_SECONDS` without waiting for that cooldown.
ComfyUI's unused PyTorch reservations are excluded from externally free VRAM.

After copying the updated Dockerfile and coordinator to the GPU host, apply this
fix from `/opt/ai-stack` (a container restart alone does not install the worker patch):

```bash
docker compose build comfyui
docker compose up -d --no-deps --force-recreate comfyui llama-metrics-discovery
docker compose logs --since=10m comfyui llama-metrics-discovery llama-cpp
```

Verify chat → image → chat, then repeat with an image edit. ComfyUI should log
`AI stack: ComfyUI VRAM released before completion` before returning the image;
the next chat request should load successfully without GPU allocation errors.
If `VRAM handoff failed` appears, inspect that traceback and the GPU memory state.
This handoff orders completion of an image before the subsequent chat; it does
not serialize simultaneous requests from multiple clients or guarantee that a
model/context fits the GPU. Persistent load failures need llama.cpp/ROCm logs.

Sources: [ComfyUI worker](https://github.com/Comfy-Org/ComfyUI/blob/v0.34.0/main.py),
[ComfyUI memory management](https://github.com/Comfy-Org/ComfyUI/blob/v0.34.0/comfy/model_management.py),
[Open WebUI completion handling](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/utils/images/comfyui.py).

When ComfyUI has running or queued jobs and free VRAM falls below
`RESERVE_VRAM_MB` (or usage reaches `VRAM_THRESHOLD_PCT`), the coordinator also
unloads idle llama.cpp models via authenticated `POST /models/unload`. It checks
the model's slots again before unloading; busy models and loading models are
protected, and unavailable or invalid status data prevents unloading. At most
one model is unloaded per check, followed by a fresh VRAM measurement on the
next check. Each model's unload attempts, including failures, are rate-limited
by `VRAM_COOLDOWN_SECONDS`. Successful requests appear as `llama_unloads` in
`/stats`. The existing router autoload reloads a model on its next request.

This is polling, not a load barrier: ComfyUI acknowledges `/free` asynchronously,
so simultaneous jobs or loading immediately after image generation can still race.
The coordinator does not delay either service's requests or guarantee that a model fits.
A new llama.cpp request can race with the slot check and unload; protecting
requests atomically would require a shared admission barrier. The reserve is a
fixed threshold, not an estimate of a ComfyUI workflow's memory requirements.
Check actual VRAM with `amd-smi` after the release request appears in
`sudo docker compose logs --tail=100 llama-metrics-discovery`.
After updating these files on the GPU host, apply with
`sudo docker compose up -d --no-deps --force-recreate llama-metrics-discovery`;
no ComfyUI image rebuild is needed for this coordinator change.

`.env` is the central deployment configuration. `.env.example` lists every
supported Compose variable, grouped by service, with its default/example value.
Compose contains no duplicate fallback values: missing required variables stop
validation with a message naming the variable. `HF_TOKEN` may be empty.
Internal service URLs, container paths and fixed security settings remain in
Compose; all `environment` blocks use explicit key/value mappings. The entire
`.env` is never injected into every container.

Keep `.env` private (`chmod 600`); it and installer backups are excluded from Git
and the Docker build context. Keep llama.cpp's file-backed API secret synchronized
as described above. Single-quote literal secrets containing `$` or `#`; never
source `.env` as a shell script. Ordinary Compose commands allow exported shell
variables to override `.env`; the installer clears the parent environment.
See [Docker's environment-variable guidance](https://docs.docker.com/compose/how-tos/environment-variables/best-practices/).

For an existing deployment, update the source tree (including `.env.example`,
`docker-compose.yml` and the installer), then rerun the installer: it adds missing template values to
`.env`, preserves existing values, and generates missing credentials. Its file
copy step requires `--update-files` when managed target files differ; replaced
files are backed up in `.installer-backups/`. Deployment data, credentials and
TLS certificates are retained. For manual migrations, merge the
missing keys from `.env.example` into `.env` before running Compose. Use
`docker compose --env-file .env config --quiet` to validate without printing
credentials. Environment changes require container recreation (`up -d`), and
build-argument changes require rebuilding the relevant image. Open WebUI may
retain admin settings in its database; update those through its admin settings.

## 3. Create a TLS Certificate

NGINX will not start unless both files below exist:

- `config/nginx/certs/nginx.crt`
- `config/nginx/certs/nginx.key`

For a local installation, create a self-signed certificate:

```bash
sudo openssl req -x509 -nodes -newkey rsa:4096 -days 365 \
  -keyout config/nginx/certs/nginx.key \
  -out config/nginx/certs/nginx.crt \
  -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
sudo chmod 600 config/nginx/certs/nginx.key
sudo chmod 644 config/nginx/certs/nginx.crt
```

Browsers will warn about this certificate. For remote or production access,
replace it with a certificate whose subject alternative names contain the value
of `PUBLIC_IP_OR_DOMAIN`.

## 4. Set Persistent-Volume Permissions

The containers do not all run under the same UID. Apply ownership before the
first start:

```bash
sudo mkdir -p data/{grafana,prometheus,openwebui,llama-cpp,searxng}
sudo mkdir -p data/comfyui/{input,output,user,models,custom_nodes}

# Grafana (UID 472)
sudo chown -R 472:0 data/grafana config/grafana

# Prometheus (nobody, UID/GID 65534)
sudo chown -R 65534:65534 data/prometheus

# Open WebUI, llama.cpp, and ComfyUI
sudo chown -R root:root data/openwebui data/llama-cpp data/comfyui config/llama-cpp

sudo find data -type d -exec chmod 750 {} \;
sudo find data -type f -exec chmod 640 {} \;
sudo find config/grafana -type d -exec chmod 755 {} \;
sudo find config/grafana -type f -exec chmod 644 {} \;
sudo find config/llama-cpp -type d -exec chmod 755 {} \;
sudo find config/llama-cpp -type f -exec chmod 644 {} \;
sudo chown root:root .env config/llama-cpp/api-key.txt
sudo chmod 600 .env config/llama-cpp/api-key.txt
sudo setfacl -m u:65534:r config/llama-cpp/api-key.txt
sudo chmod 600 config/nginx/certs/nginx.key
```

## 5. Build the ROCm Images

Both `llama-cpp` and `comfyui` have Compose build definitions. The llama.cpp
image is tagged `llama_cpp_rocm:latest`; its Dockerfile defaults to `gfx1201`
and the `master` branch. Determine the GPU target with `rocminfo`, then build
with `ROCM_GFX_TARGETS` and `LLAMA_CPP_REF` set in `.env`:

```bash
sudo nano .env
sudo docker compose build llama-cpp comfyui
```

Set `ROCM_GFX_TARGETS` to the detected GPU target and `LLAMA_CPP_REF` to a tested
llama.cpp commit or tag. The base Compose file maps both build arguments from
`.env`. The installer also maintains `compose.install.yml` for compatibility
with existing service commands; it references the same `.env` variables.

ComfyUI defaults to ROCm 10.0, Ubuntu 26.04, PyTorch 2.13.0 and Python 3.14,
with the latest stable ComfyUI release. Its container uses the AMD runtime and is attached
only to the internal `backend-net`; install dependencies during the image build
and place models in the host's persistent model directories. The Dockerfile
provides a health check against `/system_stats`.

The large ROCm base image makes the first build take some time and disk space.
Compose always builds `comfyui_rocm:latest`, resolves GitHub's latest stable
ComfyUI release, and disables the build cache so an older checkout cannot be reused.
Legacy `COMFYUI_REF` and `COMFYUI_IMAGE_TAG` entries in `.env` are ignored.
To update ComfyUI, run:

```bash
sudo docker compose build --pull comfyui
sudo docker compose up -d --no-deps comfyui
```

The installer also resolves the latest release on each build. A container restart
or `docker compose up` without a build keeps the existing image; running containers
do not update themselves. The guarded VRAM handoff patch still stops the build if
an upstream worker change is incompatible.
The ROCm base image remains controlled by `COMFYUI_ROCM_PYTORCH_IMAGE` in `.env`.
Do not
replace the ROCm-provided Torch packages with packages from the regular PyPI
index; that can silently remove GPU support.

ComfyUI data is persistent under `data/comfyui/`. Put checkpoints in
`data/comfyui/models/checkpoints/`, VAEs in `data/comfyui/models/vae/`, LoRAs in
`data/comfyui/models/loras/`, and custom nodes in
`data/comfyui/custom_nodes/`.

### GGUF Models in ComfyUI

The image includes [ComfyUI-GGUF](https://github.com/city96/ComfyUI-GGUF)
and its Python dependencies. Bundled nodes live in
`/opt/comfyui-bundled-nodes` and are registered through ComfyUI's
`extra_model_paths.yaml`, so the persistent `custom_nodes` mount does not hide
them. No download or dependency installation is needed at container startup.
Do not also install ComfyUI-GGUF in `data/comfyui/custom_nodes/`, as that would
load a second copy.

- Put diffusion GGUF models in `data/comfyui/models/unet/`.
- Put GGUF text encoders in `data/comfyui/models/text_encoders/`.
- In the workflow, use **Unet Loader (GGUF)** instead of **Load Diffusion Model**,
  and the appropriate **CLIPLoader (GGUF)** node for GGUF text encoders.
- Use models supported by ComfyUI-GGUF; llama.cpp chat GGUF models are not
  interchangeable with diffusion models.

After updating the stack files, rebuild and recreate ComfyUI on the GPU host:

```bash
sudo docker compose up -d --build --no-deps comfyui
sudo docker compose logs --tail=100 comfyui
sudo docker compose exec -T comfyui python -c 'import json, urllib.request; nodes = json.load(urllib.request.urlopen("http://localhost:8188/object_info")); assert "UnetLoaderGGUF" in nodes and "CLIPLoaderGGUF" in nodes; print("GGUF loaders available")'
```

`COMFYUI_GGUF_REF` defaults to `main`; set it to a commit SHA in `.env` for
reproducible builds. To refresh an unpinned `main` checkout, rebuild with
`sudo docker compose build --no-cache comfyui` and then run
`sudo docker compose up -d --no-deps comfyui`.

## 6. Validate and Start

Validate interpolation and syntax before creating containers:

```bash
sudo docker compose config --quiet
sudo docker compose up -d
sudo docker compose ps
```

Monitor the first start:

```bash
sudo docker compose logs -f llama-cpp comfyui nginx openwebui
```

Press `Ctrl+C` to stop following logs; the containers keep running. A model is
loaded on demand on the main router; the dedicated image-prompt router instead
uses `load-on-startup = true`.
Use the model ID exactly as returned by the API or as written in the section
header of `models.ini`.

## Prompt caching

Both llama.cpp services explicitly enable `--cache-prompt`. The four workspace
presets send `cache_prompt: true` through Open WebUI's `custom_params`. New
presets receive the option during initialization. Existing managed presets get a
one-time migration tracked by `ai_stack.model_presets.prompt_cache_v1`; it changes
only cache settings and preserves edited prompts, names, sampling values, other
custom parameters and grants. A conflicting legacy top-level `cache_prompt` is
removed. A later manual override is preserved on ordinary restarts; explicitly
rerunning the preset script reapplies caching with the other managed defaults.

After deploying the updated Compose file and preset script on the GPU host:

```bash
sudo docker compose up -d --no-deps llama-cpp llama-cpp-image
sudo docker compose restart openwebui
```

The startup helper runs the migration after backend readiness. Check its logs for
`Prompt caching enabled for all four model presets.` on an existing installation.
The llama.cpp build must support `--cache-prompt`; if an older image rejects it,
rebuild the llama.cpp image using a supported revision before recreating services.
No ComfyUI rebuild is needed for this change.

This reuses a matching token prefix, including a stable system prompt, while the
model process is resident. It does not cache answers or persist state across
model unloads/restarts. The main router's one-model limit and the VRAM coordinator
can evict the process and its cache; both policies remain unchanged. The external
task model benefits from the server default even when its task requests do not
use a workspace preset. Dynamic source/tool/memory injection can reduce reuse;
web research, citations, memory and retrieval settings remain unchanged.

To verify live reuse, send consecutive requests to the same loaded model with an
identical system prompt and inspect `usage.prompt_tokens_details.cached_tokens`
on a non-streaming response from a build that reports it. A first request after
loading is expected to be uncached. Offline configuration tests do not establish
a live cache hit or measure latency.

References: [llama.cpp server caching](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
and [Open WebUI prompt caching](https://docs.openwebui.com/features/chat-conversations/prompt-caching/).

## 7. Install the systemd Service (Optional)

The included unit starts the stack after Docker and stops it during shutdown:

```bash
sudo cp /path/to/ai-stack/ai-stack.service /etc/systemd/system/ai-stack.service
sudo systemctl daemon-reload
sudo systemctl enable --now ai-stack.service
sudo systemctl status ai-stack.service
```

The unit is fixed to `/opt/ai-stack`. If a different location is used, update
both `WorkingDirectory` in the unit and `AI_STACK_ROOT` in `.env` before
installing it.

The supplied unit uses `docker compose up -d --no-build --pull never` and stops
containers with `docker compose stop`. Complete the builds, pulls and model
downloads before enabling it. Its startup timeout is 300 seconds. It uses the
base Compose file; the compatibility override contains the same build settings.
For a custom deployment path, also update the absolute `--env-file` arguments.

---

# Access the Stack

NGINX publishes the main stack endpoints to the VM host:

- Open WebUI: `https://<host>:8443/`
- Grafana: `https://<host>:8443/grafana/` (user `admin`)
- OpenAI-compatible API: `https://<host>:8443/v1/`
- llama.cpp health: `https://<host>:8443/health`

ComfyUI is available through the same NGINX reverse proxy and TLS certificate:

- ComfyUI: `https://<host>:8444/`

Its native port 8188 is available only on the internal Docker network. Change
`COMFYUI_HTTPS_PORT` in `.env` if port 8444 is already occupied. The certificate
must contain the hostname used to open both HTTPS endpoints.

HTTP on port `8080` redirects to HTTPS on port `8443`. Ports `8000`, `8188`,
`8080` (container), `9090`, and `3000` are intentionally not published
directly.

Example API checks with a self-signed certificate:

```bash
LLAMA_CPP_API_KEY=$(sudo cat config/llama-cpp/api-key.txt)
curl -k https://localhost:8443/health
curl -k \
  -H "Authorization: Bearer $LLAMA_CPP_API_KEY" \
  https://localhost:8443/v1/models
```

Example chat request (replace the model ID if `models.ini` was changed):

```bash
curl -k https://localhost:8443/v1/chat/completions \
  -H "Authorization: Bearer $LLAMA_CPP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen3.8-27B",
    "messages": [{"role": "user", "content": "Say hello."}],
    "max_tokens": 32
  }'
```

---

# Validation and Tests

The available offline regression suite is `scripts/tests/test_model_preset_access.py`.
It covers new preset creation, sharing migration, preservation of existing edits,
retry behavior, and synchronous/asynchronous model stores using test doubles.
It does not start containers, verify live Open WebUI APIs, or perform inference.
Older references to a top-level `tests/` SRE, observability, load, or chaos suite
refer to scripts that are not present in this source tree.

The repository provisions both the Prometheus datasource and the dashboard in
`config/grafana/provisioning/dashboards/files/llama-cpp-inference-stack.json`.
Its UID is `llamacpp-server-overview`.
The dashboard provider scans the mounted directory every 30 seconds and disables
UI updates. Speculative-decoding panels can be empty when the running llama.cpp
build or model does not expose the optional metrics.

Prometheus loads `config/prometheus/rules.yml` and scrapes both its own endpoint
and the coordinator's `/metrics`, in addition to loaded llama.cpp models. Router
availability is monitored even when no model is loaded. Rules cover failed
scrapes, router availability, a stalled coordinator, low busy throughput and
queue pressure. The dashboard uses currently supported llama.cpp metrics.
No NGINX exporter or Alertmanager is included: rules are evaluated locally, but
external notifications require an Alertmanager deployment and configuration.

Run offline regression checks from the source tree:

```bash
python3 -B -m unittest discover -s scripts/tests -p 'test_*.py'
# Non-secret placeholders for syntax validation only; do not start services with them.
WEBUI_SECRET_KEY=validation-only OPEN_TERMINAL_API_KEY=validation-only docker compose --env-file .env.example config --quiet
```

Offline checks do not validate GPU inference. On the deployed host, also run:

```bash
sudo docker compose exec -T nginx nginx -t
sudo docker compose exec -T prometheus promtool check config /etc/prometheus/prometheus.yml
```

---

# Open Terminal in Open WebUI

`open-terminal` provides a persistent shell and file workspace at `/home/user`,
stored in `data/open-terminal`. Open WebUI connects over the dedicated
`terminal-net` network at `http://open-terminal:8000`. The service can reach the
internet for downloads and package installation, but publishes no host port and
has no Docker socket or other stack-data mounts.

Use the updated installer with `--update-files` to generate the shared API key,
prepare directory permissions, pull the image and register the connection in an
existing Open WebUI database (see [INSTALL.md](INSTALL.md)). Existing terminal
connections and the managed connection's access grants/disabled state are retained.

For manual deployments:

1. Add `OPEN_TERMINAL_VERSION=latest` and a random `OPEN_TERMINAL_API_KEY` to the
   deployed `.env` (for example, generate a key with `openssl rand -hex 32`).
2. Copy the updated Compose file and `config/openwebui/start.py` into the deployment.
3. In the deployment directory, run:

```bash
sudo install -d -m 0750 -o 1000 -g 1000 data/open-terminal
sudo docker compose pull open-terminal openwebui
sudo docker compose up -d --no-deps --wait open-terminal openwebui
sudo docker compose exec -T openwebui python -c 'import sys; sys.path.insert(0, "/etc/openwebui"); exec(compile(sys.stdin.read(), "<setup>", "exec"))' < /path/to/ai-stack/config/openwebui/apply_open_terminal.py
sudo docker compose restart openwebui
```

Include `-f docker-compose.yml -f compose.install.yml` for installer-managed
deployments. The startup wrapper supplies `TERMINAL_SERVER_CONNECTIONS` on new
installations; the setup helper merges it into saved settings on existing ones.
After changing the key, recreate both services and rerun the helper.

Refresh Open WebUI and select **Open Terminal** in the chat. Use native function
calling with a tool-capable model. Administrators have access initially; grant
other trusted users access under **Settings → Admin → Integrations → Open Terminal**.
This is a shared workspace for everyone granted access, not a separate container
per user. Pin tested `OPEN_TERMINAL_VERSION` and `OPENWEBUI_VERSION` tags when
reproducible upgrades are needed.

Check readiness with `sudo docker compose ps open-terminal openwebui`. In a chat,
ask the model to create a small file in `/home/user` and read it back; it should
also appear in the terminal file browser and survive container recreation.

References: [Open Terminal installation](https://docs.openwebui.com/features/open-terminal/setup/installation/)
and [Open WebUI terminal connections](https://docs.openwebui.com/reference/env-configuration/#terminal_server_connections).

# Web Search with SearXNG

Open WebUI uses the internal endpoint
`http://searxng:8080/search?q=<query>&format=json`. SearXNG has outbound internet
access through `public-net` and shares `backend-net` with Open WebUI. It publishes
no host port or NGINX route. JSON responses are enabled in
`config/searxng/settings.yml`; the limiter is disabled for this internal service,
so no Valkey service is needed. Do not expose it publicly with this configuration.
Configuration and cache persist in `config/searxng/` and `data/searxng/`.
The container manages ownership of these two directories at startup.

The search profile explicitly retains eight engines from the upstream defaults:

| Search type | Engines | Selection |
| --- | --- | --- |
| General web | Brave, DuckDuckGo, Wikipedia | Default |
| News | Brave News, DuckDuckGo News | `!news` or `categories=news` |
| IT | GitHub, Stack Overflow | `!it`, `!gh`, or `!st` |
| Science | arXiv | `!science` or `!arx` |

Wikipedia returns ordinary result entries so Open WebUI can consume them.
Specialist sources are available when the submitted search query selects them;
Open WebUI does not automatically route each question to these categories.
Other SearXNG engines are excluded by `keep_only`, including image/video,
shopping and file-search providers. Their sites can still appear in general
web results. To add a provider, add its exact upstream name to `keep_only`
and configure it under `engines` if necessary.

Google, Bing and Qwant are not forced on (currently disabled upstream).
Startpage is currently marked inactive upstream because of its proof-of-work
CAPTCHA. These choices follow the
[upstream engine configuration](https://github.com/searxng/searxng/blob/master/searx/settings.yml);
review them when upgrading. They are a conservative baseline, not a benchmark
of engine availability from your server's IP address.

Searches use moderate SafeSearch where supported and no fixed language filter,
so German and English sources remain available. Autocomplete is disabled.
The default engine timeout is 5 seconds, arXiv gets 8 seconds, and the maximum
request timeout is 10 seconds. Upstream error/CAPTCHA suspension behavior is
preserved. Engine weights remain equal; no domain is promoted unconditionally.
Open WebUI consumes up to six results per search, runs one search-engine request
at a time, and fetches up to three result pages concurrently. These are starting
values balancing source coverage, latency and upstream load.

When updating an existing deployment, change `WEB_SEARCH_RESULT_COUNT=6` and
`WEB_LOADER_CONCURRENT_REQUESTS=3` in its `.env` if those values were already set.
Saved Open WebUI admin settings may also need updating. After copying the new
settings, apply them with `sudo docker compose restart searxng` and
`sudo docker compose up -d --no-deps openwebui`. Restart SearXNG explicitly:
changing a bind-mounted YAML file alone does not recreate its container.


For an existing installation, copy the updated `docker-compose.yml` and new
`config/searxng/settings.yml` into the deployed stack. Add `SEARXNG_SECRET` to
its `.env`, using the output of `openssl rand -hex 32`. The updated installer
also generates missing secrets. Use `--update-files` to replace managed
configuration from an updated source tree, with backups.

From the deployment directory:

```bash
sudo docker compose config --quiet
sudo docker compose pull searxng
sudo docker compose up -d --no-deps searxng openwebui
sudo docker compose ps searxng
sudo docker compose exec -T openwebui python -c 'import json, urllib.request; r=json.load(urllib.request.urlopen("http://searxng:8080/search?q=searxng&format=json", timeout=60)); assert isinstance(r.get("results"), list); print("Results:", len(r["results"])); print("Unavailable engines:", r.get("unresponsive_engines", []))'
```

Wait for SearXNG to become healthy before running the search check. The health
check uses `/healthz` without sending searches to upstream engines. A JSON response
with no results may indicate upstream engine blocking; inspect the reported
unavailable engines and `docker compose logs searxng`.

For installer-managed deployments, add `-f docker-compose.yml -f compose.install.yml`
to these Compose commands. In existing Open WebUI installations, saved admin
settings can override environment defaults: enable **Web Search**, select
**searxng**, and save the query URL above in **Admin Settings → Web Search**.
Enable web search in the chat as well.

References: [SearXNG container setup](https://docs.searxng.org/admin/installation-docker)
and [Open WebUI SearXNG integration](https://docs.openwebui.com/features/chat-conversations/web-search/providers/searxng/).

---

# Open WebUI defaults and Qwen image generation/editing

Open WebUI uses llama.cpp for chat and the internal ComfyUI service for images.
The separate `llama-cpp-image` service runs Qwen3.5-4B Q4_K_M, configured in
`config/llama-cpp-image/models.ini`. It shares the built llama.cpp image and model
cache, but exposes a separate internal API at `http://llama-cpp-image:8000/v1`.
No host port is published. The current Compose configuration shares
`LLAMA_CPP_GPU_LAYERS` with the main router and loads this model at startup;
it is not CPU-only. `LLAMA_CPP_IMAGE_CTX_SIZE` defaults to 16384 tokens.
`LLAMA_CPP_IMAGE_THREADS` controls both `--threads` and `--threads-batch`
(default 8). The logical batch size uses the shared `LLAMA_CPP_BATCH_SIZE`. The VRAM coordinator monitors
only the main router and does not unload this separate service's model.

With `ENABLE_IMAGE_PROMPT_GENERATION=true`, Open WebUI uses the external task
model `llama-cpp-image` to expand the image request into an English description.
The template returns the JSON `prompt` field expected by Open WebUI, which then
passes that description through the existing ComfyUI workflow to Qwen-Image-2512.
Select image generation in the chat to use this path; ordinary chat messages
remain ordinary chat messages. Visible text requested for the image retains its
original language. Open WebUI's external task model is shared with other tasks,
so title/tag generation and similar tasks can also use this small model.

The installer prefetches this GGUF along with the existing models. On first
migration it enables image prompt generation; subsequent runs preserve the saved
switch. `apply_qwen_images.py` also appends both managed API connections to an
existing database, preserving unrelated endpoints and their indices, and saves
the task model and prompt template. Restart Open WebUI after applying settings.
For a manual upgrade, copy the new preset and updated runtime files, add the two
`LLAMA_CPP_IMAGE_*` variables from `.env.example`, set
`ENABLE_IMAGE_PROMPT_GENERATION=true`, run `scripts/download_models.py`, recreate
`llama-cpp-image` and `openwebui`, then run the settings command below.

Four additional Open WebUI workspace models are managed by `config/openwebui/apply_model_parameters.py`.
At container startup a background helper waits for Open WebUI to be healthy and
for the first administrator account to exist. It then creates the four presets
automatically, normally within five seconds of signup. Refresh the browser to see
them. No manual setup command or restart is required after registration.

Successful initialization is recorded in the Open WebUI database under
`ai_stack.model_presets.initialized`. Subsequent container restarts and installer
runs leave later preset edits untouched. Failed or interrupted initialization is
retried; completion is recorded only after all four presets have been saved.
The helper exits after completion. Temporary errors are reported in container logs.
An existing database without this marker is initialized once on the next startup.
A separate `ai_stack.model_presets.public_read_v1` marker tracks the one-time
sharing migration. On the first startup with the updated script, existing managed
presets and their base models receive read access for all signed-in users, without
resetting edited prompts, sampling parameters, names, or ownership. Failed grants
are retried and the marker is saved only after all grants have been verified.
New installations receive the same grants during initialization. This requires
an Open WebUI version with the access-grants API.

| Display name | Loaded model | Temperature | Top P | Top K |
| --- | --- | --- | --- | --- |
| Coding | Qwen3.8-27B | 0.6 | 0.90 | 20 |
| Allround | Qwen3.8-27B | 0.7 | 0.90 | 40 |
| Creativ | Qwen3.8-27B | 1.0 | 0.95 | 64 |
| Image Generation | Qwen3.5-4B | 0.9 | 0.90 | 20 |

These are adjustable role defaults, not manufacturer-optimal settings. Unlisted
fields such as `min_p`, `presence_penalty`, and `frequency_penalty` are preserved
when already saved; otherwise the backend defaults apply. The repeat penalty
is 1.05 for Creativ and 1.0 for the other roles. Context size remains controlled
by the llama.cpp service; existing token limits are preserved. Coding receives an
English system prompt for software development without prescribing a programming
language. Code comments, docstrings, new identifiers, and developer-facing diagnostics
are written in English regardless of the input language; conversational explanations
follow the user's language. Existing interface names and requested localization
are preserved. The prompt is defined as `CODING_SYSTEM_PROMPT` in
`config/openwebui/apply_model_parameters.py`. Image Generation receives the English
`IMAGE_GENERATION_SYSTEM_PROMPT` defined in the same file. It prepares visual
prompts and uses available image tools to request generation or editing through
ComfyUI. The system prompt does not add tool access: without a suitable tool,
it supplies the prompt and directs the user to Open WebUI's image-generation mode.
The separate external task-model prompt template remains responsible for automatic
JSON prompt rewriting. Creativ receives the English `CREATIV_SYSTEM_PROMPT` for
creative writing, including stories, lyrics, advertising copy, birthday wishes,
and personal messages. It follows the user's language, audience, tone, and format.
Allround receives the English `ALLROUND_SYSTEM_PROMPT` for concise answers and
focused web research, prioritizing factual accuracy, primary sources, citations,
and explicit uncertainty. It requires verification of current or uncertain claims
when search tools are available and discloses when verification is unavailable.
The prompt does not enable web search by itself; enable it in Open WebUI as
described in the web-search setup above.
ComfyUI renders the actual images.

The presets use separate IDs: `ai-stack-coding`, `ai-stack-allround`,
`ai-stack-creativ`, and `ai-stack-image-generation`. Each references its upstream
model via `base_model_id`. Original models remain selectable with their original
names and default settings. Missing base-model entries are registered with empty
parameters so regular users can access them; existing base-model entries are
preserved. All four presets and their two base models receive public read grants
(`user:*`, `read`). Existing grants are retained; no new write or anonymous access
is granted. Users must still have an approved account and the applicable feature
permissions for web search or image generation.
All three chat presets use the router API ID `Qwen3.8-27B` (Qwen 3.8 27B).
Image Generation keeps `Qwen3.5-4B`, matching its dedicated router and
`TASK_MODEL_EXTERNAL`.

Existing installations migrate the managed chat presets automatically on the
next Open WebUI startup. The one-time marker
`ai_stack.model_presets.qwen38_chat_v1` tracks this change. The migration preserves
custom names, prompts, sampling settings and existing grants; it verifies read
access to the shared base model before recording completion. Downloads now
prefetch only the shared chat model, the image-prompt model and the ComfyUI assets.
Previously cached model files are not automatically deleted.

The script updates only presets bearing its management marker and refuses to
replace unrelated models with colliding IDs. It does not remove legacy database
entries. If an older name-override script was already applied to the live database,
reset those base-model names and parameters in Open WebUI separately; creating
these additional presets does not undo earlier database changes.

To explicitly reapply the managed defaults later, run from the stack directory
on the GPU host after an admin exists:

```bash
sudo docker compose exec -T openwebui python - < /path/to/ai-stack/config/openwebui/apply_model_parameters.py
sudo docker compose restart openwebui
```

Refresh the browser afterwards. The script uses Open WebUI's model store and
persists the names and sampling presets in its database. Rerunning reapplies the
managed sampling fields and all four presets' system prompts, including removing
conflicting copies in `custom_params`. Existing installations that have already
completed initialization must run this command to apply the updated system prompts.
Unrelated model parameters, metadata, ownership, activation state and existing
access settings are preserved; the public read grants are added if missing. Edit `ROLE_PARAMS` in the script to change the managed defaults.
Account or chat sampling overrides can take precedence over these model defaults;
clear such overrides when testing the presets.
See [Open WebUI parameter precedence](https://docs.openwebui.com/features/chat-conversations/chat-features/chat-params/).
Model IDs and existing chat references remain unchanged. A fresh Open WebUI
database is initialized automatically after signup. The manual restart clears cached model lists.

ComfyUI's native workflow browser and Open WebUI use different JSON formats.
Open WebUI submits its existing API graph with every generation/edit request;
it does not need a saved workflow in ComfyUI's user folder. Native editor
workflows are now also included in `config/comfyui/workflows`:

- `unsloth_qwen_image_2512.json`: text-to-image generation.
- `unsloth_qwen_image_edit_2511.json`: editing with two uploaded references.

Compose mounts this directory read-only at
`/opt/ComfyUI/user/default/workflows/ai-stack`, inside the persisted user tree.
Open **Workflows → ai-stack** in ComfyUI, or use **File → Open** to import one of
these JSON files. Use **Save As** outside the managed folder to retain an editable
copy. Existing user workflows are preserved.

For editing, upload your own images in both reference nodes before running;
`reference-1.png` and `reference-2.png` are placeholders. The tutorial's unused
third reference requirement is removed, and the GGUF text encoder is explicitly
set to `qwen_image`. The workflows use the same model filenames as the installer,
Euler/simple sampling, 40 steps, CFG 4 and a sampling shift of 3.1. Generation
starts at 1024×1024; the editing workflow resizes the first reference to that
size and the second to 768×768, following the tutorial's example.

To add the native workflows to an existing deployment, run the updated installer
with `--update-files`. For a manual update, copy `config/comfyui/workflows` into
the deployment and update `docker-compose.yml`, then recreate ComfyUI:

```bash
cd /opt/ai-stack
sudo docker compose up -d --no-deps --force-recreate comfyui
```

Refresh ComfyUI after recreation. No image rebuild is needed for these JSON files
if the deployed image already has the required Qwen/GGUF nodes. An empty native
workflow list alone does not diagnose an Open WebUI image-generation failure;
check its saved image settings and ComfyUI logs as described below.

The image workflows follow the [Unsloth Qwen-Image guide](https://unsloth.ai/docs/models/tutorials/qwen-image-2512)
with GGUF diffusion models, the Qwen2.5-VL text encoder and its matching vision
tower, and the shared Qwen image VAE. No Lightning LoRA is required.

| Operation | API workflow | Open WebUI input mapping |
| --- | --- | --- |
| Generate | `config/openwebui/qwen-image-2512_image.json` | `qwen-image-2512_nodes.json` |
| Edit | `config/openwebui/qwen-image-edit-2511_image.json` | `qwen-image-edit-2511_nodes.json` |

`config/openwebui/start.py` loads both workflows into the generation and editing
environment settings before starting Open WebUI. Compose mounts this directory
into Open WebUI and runs the wrapper there.

Generation defaults to **1024 × 1024**; editing preserves the uploaded aspect
ratio at approximately **2 megapixels**. Both use **40 steps**, **Euler/simple**, CFG 4 and sampling
shift 3.1. The negative prompt is initially empty and can be changed in the
workflow. If results look blurry, try shift 12–13 as suggested by Unsloth.
The edit workflow uses `TextEncodeQwenImageEditPlus` with one uploaded reference
image, scaled with Lanczos without cropping. Node 13 uses
`ImageScaleToTotalPixels` with `megapixels: 2.0` and `resolution_steps: 16`.
Dimensions are rounded to multiples of 16 for latent compatibility, so the aspect
ratio can differ slightly. Higher megapixel values require more memory and time. Upload one
reference per edit in Open WebUI. For multi-reference work in ComfyUI, add image
loaders and connect `image2`/`image3` to both conditioning nodes. The single-image
API template does not automatically map additional uploads.

| Input | Generate node/key | Edit node/key |
| --- | --- | --- |
| Model | 1 / unet_name | 1 / unet_name |
| Prompt | 6 / text | 6 / prompt |
| Reference image | — | 12 / image |
| Width, height | 8 / width, height | Not mapped; derived from the reference aspect ratio |
| Steps | 9 / steps | Fixed at 40 in node 9 |
| Seed | 9 / seed | 9 / seed |
| Batch count | 8 / batch_size | One edited image |

Open WebUI's edit adapter does not supply a steps value, so it is intentionally
not mapped. Keep `IMAGE_EDIT_SIZE` set to an explicit size such as `1024x1024`
for Open WebUI request compatibility; it no longer controls this workflow's output
dimensions. Change node 13's `megapixels` value in the edit API JSON to adjust
resolution. Deploy workflow changes with the installer's `--update-files` option.
Edit outputs use full denoising with the original image supplied to Qwen's
reference conditioning. To use the API edit JSON directly in ComfyUI, load it
and choose an existing file in the `LoadImage` node (`reference.png` is a
placeholder replaced automatically by Open WebUI uploads).

### Model files and deployment

The installer downloads these five files automatically before startup through
`scripts/download_models.py`, which also handles all chat presets. Existing
completed files are retained and interrupted `.part` files resume. The ComfyUI
model directory is resolved from the deployed Compose configuration.

- `unet/qwen-image-2512-Q4_K_M.gguf`
- `unet/qwen-image-edit-2511-Q4_K_M.gguf`
- `text_encoders/Qwen2.5-VL-7B-Instruct-UD-Q4_K_XL.gguf`
- `text_encoders/Qwen2.5-VL-7B-Instruct-mmproj-BF16.gguf`
- `vae/qwen_image_vae.safetensors`

Do not rename the vision tower to plain `mmproj-BF16.gguf`: the GGUF loader
matches it to the text encoder by filename prefix. ComfyUI `v0.34.0` and the
bundled ComfyUI-GGUF nodes provide the required Qwen node types. Update/rebuild
the GGUF extension if your deployed image predates its Qwen vision support.

For automated migration, run the updated installer with `--update-files` from
the source tree. It downloads the models, validates the ComfyUI graph and applies
the image settings to Open WebUI. The known legacy FLUX generation model and
its old default size are migrated automatically. For manual migration, copy
the updated Compose file and complete `config/openwebui/` directory, then update
these values in the deployed `.env`, preserving other settings/secrets:

```dotenv
IMAGE_GENERATION_MODEL=qwen-image-2512-Q4_K_M.gguf
IMAGE_SIZE=1024x1024
IMAGE_STEPS=40
ENABLE_IMAGE_EDIT=true
IMAGE_EDIT_MODEL=qwen-image-edit-2511-Q4_K_M.gguf
IMAGE_EDIT_SIZE=1024x1024
```

Then run:

```bash
sudo docker compose --env-file .env config --quiet
sudo docker compose build --no-cache comfyui
sudo docker compose up -d --no-deps --force-recreate comfyui openwebui searxng nginx
sudo docker compose exec -T openwebui python -c 'import sys; sys.path.insert(0, "/etc/openwebui"); exec(compile(sys.stdin.read(), "<setup>", "exec"))' < /path/to/ai-stack/config/openwebui/verify_qwen_workflows.py
sudo docker compose exec -T openwebui python -c 'import sys; sys.path.insert(0, "/etc/openwebui"); exec(compile(sys.stdin.read(), "<setup>", "exec"))' < /path/to/ai-stack/config/openwebui/apply_qwen_images.py
sudo docker compose restart openwebui
```

Wait until ComfyUI and Open WebUI are healthy before running the two Python
commands. `apply_qwen_images.py` replaces the generation/editing and prompt-task settings
in the initialized Open WebUI database, using the image switches, model names,
sizes and generation steps from the container environment. It also adds the managed
API connections; chat models, users and unrelated settings are retained. It uses the current Open WebUI per-key config API. With an older
Open WebUI build, update the image or import both API workflows and mappings
under **Admin Settings → Images**, selecting ComfyUI for generation and editing.
Saved database settings override environment defaults, so apply/import this
configuration even after changing `.env`. Refresh the browser afterwards.

SearXNG and NGINX are recreated because the old Compose file incorrectly assigned
the Open WebUI wrapper to SearXNG and its config mount to NGINX. The updated file
places both under Open WebUI. For installer-managed deployments, include
`-f docker-compose.yml -f compose.install.yml` in Compose commands.

The old FLUX workflow exports are removed from this repository. After stopping
ComfyUI during the migration, these three obsolete model files and any old
workflow exports can also be removed from the deployment:

```bash
sudo rm -f -- data/comfyui/models/unet/flux2-dev-Q4_K_M.gguf \
  data/comfyui/models/text_encoders/mistral_3_small_flux2_fp4_mixed.safetensors \
  data/comfyui/models/vae/flux2-vae.safetensors \
  config/openwebui/flux2-dev_image.json config/openwebui/flux2-dev_nodes.json
```

The schema check validates installed nodes, model filenames and graph links;
it does not allocate GPU models. Verify runtime behavior with one image
creation and one edit using an uploaded reference. Inspect ComfyUI and Open
WebUI logs for model loading, missing vision tower or VRAM errors.

The existing Open WebUI document defaults remain hybrid retrieval, five results,
1,000-character chunks and 150-character overlap. Existing chunks require
re-indexing to change their split sizes. Use HTTPS through NGINX on port 8443;
new accounts default to `pending`, and image prompt rewriting is enabled by the
example configuration (saved admin settings can override that default).

References: [ComfyUI Qwen nodes](https://github.com/Comfy-Org/ComfyUI/blob/v0.34.0/comfy_extras/nodes_qwen.py),
[GGUF loader](https://github.com/city96/ComfyUI-GGUF/blob/main/loader.py),
[Open WebUI image mappings](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/utils/images/comfyui.py).

---

# Routine Operations

```bash
# Show status
sudo docker compose ps

# Follow all logs
sudo docker compose logs -f

# Restart one service
sudo docker compose restart llama-cpp

# Stop and remove containers while preserving bind-mounted data
sudo docker compose down

# Pull upstream images and recreate services
sudo docker compose pull --ignore-pull-failures
sudo docker compose up -d
```

Rebuild `llama_cpp_rocm:latest` explicitly when changing the ROCm target or
llama.cpp revision. Back up `.env`, `config/llama-cpp/`, the TLS certificate,
and `data/` before upgrades.

---

# Troubleshooting

- **`unknown or invalid runtime name: amd`:** rerun
  `sudo amd-ctk runtime configure` and restart Docker.
- **llama.cpp cannot see the GPU:** verify `rocminfo`, membership in the
  `render` and `video` groups, the AMD runtime, and the selected
  `AMD_VISIBLE_DEVICES` value.
- **ComfyUI cannot see the GPU:** check `docker compose logs comfyui`, verify
  that `HIP_VISIBLE_DEVICES` selects an available device, and run
  `docker compose exec comfyui python -c 'import torch; print(torch.cuda.is_available(), torch.version.hip)'`.
- **NGINX continuously restarts:** confirm `nginx.crt` and `nginx.key` exist,
  then run `docker compose logs nginx`.
- **Open WebUI receives 401 responses:** make the `.env` API key and the secret
  file identical, then recreate Open WebUI with
  `docker compose up -d --force-recreate openwebui`.
- **The first model request times out:** check `docker compose logs -f
  llama-cpp`. Run `sudo python3 /path/to/ai-stack/scripts/download_models.py --root /opt/ai-stack` after adding presets.
  Installation prefetches weights, but loading a large model into VRAM still takes time.
- **Prometheus has no llama.cpp target:** load a model first. Discovery exports
  only models whose router status is `loaded`.
- **A container cannot write its data:** reapply the ownership commands in the
  persistent-volume section.

---

# Validation Checklist

- IOMMU is enabled and every passed-through GPU function uses `vfio-pci`.
- Ubuntu boots with the GPU attached and `rocminfo` detects it.
- Docker lists the `amd` runtime.
- `.env`, the llama.cpp secret, models, and TLS files are configured.
- The local llama.cpp ROCm image was built for the correct GPU target.
- `docker compose config --quiet` succeeds.
- All ten Compose services are running and the gateway endpoints respond.
- ComfyUI responds through NGINX on TLS port 8444 and reports a ROCm-enabled
  PyTorch build.
- At least one configured model can answer an authenticated chat request.
- The provisioned Grafana dashboard is available and Prometheus parses its rules.
  Missing optional speculative-decoding metrics may leave their panels empty.
