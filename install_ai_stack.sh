#!/usr/bin/env bash
# Install the AI stack on Ubuntu amd64 with a working AMD driver/ROCm.
set -Eeuo pipefail
umask 022
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '\n==> %s\n' "$*"; }
trap 'printf "Installation failed (line %s). Fix the issue and run the installer again.\n" "$LINENO" >&2' ERR

SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
TARGET=/opt/ai-stack
HOST_NAME=
GFX=
LLAMA_REF=
WAIT_SECONDS=1800
START=1
NON_INTERACTIVE=0
UPDATE_FILES=0
usage() {
  cat <<'EOF'
sudo bash install_ai_stack.sh --host ai.example.local [options]
  --source PATH       Complete ai-stack directory (default: script directory)
  --host HOST         DNS name or IPv4 address without protocol/port
                     Optional for an existing installation
  --gfx TARGET        e.g. gfx1201; otherwise detected using rocminfo
  --llama-ref REF     llama.cpp commit/tag; otherwise existing value or master
  --wait SECONDS      Service readiness timeout (default: 1800)
  --no-start          Configure, build and download models without starting the stack
  --update-files      Back up and replace managed application/configuration files
  --non-interactive   Use saved values/defaults without prompting
  --help              Show help

Target: /opt/ai-stack. Requires Ubuntu amd64, systemd, AMD driver/ROCm,
internet access and sufficient disk space for large ROCm images and models.
Docker and AMD Container Toolkit are installed if needed.
Existing differing managed files require --update-files. Backups are retained.
Credentials, TLS files, .env and data are always preserved during file updates.
Only manifest-listed build/runtime files are copied; keep the source tree for reruns.
All preset chat models and Qwen image assets are downloaded before startup.
EOF
}
while (($#)); do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --no-start) START=0; shift ;;
    --update-files) UPDATE_FILES=1; shift ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    --source|--host|--gfx|--llama-ref|--wait)
      (($# >= 2)) && [[ -n "$2" ]] || die "Missing value for $1."
      case "$1" in
        --source) SOURCE=$2 ;; --host) HOST_NAME=$2 ;; --gfx) GFX=$2 ;;
        --llama-ref) LLAMA_REF=$2 ;; --wait) WAIT_SECONDS=$2 ;;
      esac
      shift 2 ;;
    *) die "Unknown option: $1" ;;
  esac
done
[[ "$WAIT_SECONDS" =~ ^[1-9][0-9]*$ ]] || die '--wait must be positive.'
[[ -z "$GFX" || "$GFX" =~ ^gfx[0-9a-f]+$ ]] || die 'Invalid GPU target.'
[[ -z "$LLAMA_REF" || "$LLAMA_REF" =~ ^[a-zA-Z0-9][a-zA-Z0-9._/-]*$ ]] || die 'Invalid llama.cpp reference.'
[[ $EUID == 0 ]] || die 'Please run with sudo bash.'
[[ $(uname -s) == Linux && -r /etc/os-release ]] || die 'Ubuntu is required.'
# Only operating system metadata is sourced as shell code, never .env.
. /etc/os-release
[[ "$ID" == ubuntu && $(dpkg --print-architecture) == amd64 ]] || die 'Ubuntu amd64 is required.'
CODENAME=${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}
[[ "$CODENAME" =~ ^[a-z]+$ ]] || die 'Missing Ubuntu codename.'
[[ -d /run/systemd/system ]] || die 'A running systemd instance is required.'
[[ -c /dev/kfd && -d /dev/dri ]] || die 'AMD GPU devices are missing. Check the driver/passthrough setup and whether a reboot is needed.'
SOURCE=$(cd -- "$SOURCE" && pwd -P)
[[ "$SOURCE" != "$TARGET" ]] || die 'Keep the complete source tree outside /opt/ai-stack and run the installer from there.'
MANIFEST="$SOURCE/scripts/deployment_files.txt"
[[ -f "$MANIFEST" ]] || die "Missing deployment manifest: $MANIFEST"
while IFS= read -r file; do
  [[ -f "$SOURCE/$file" ]] || die "Missing deployment file: $file"
done < "$MANIFEST"
for f in config/openwebui/apply_open_terminal.py scripts/download_models.py scripts/check_managed_files.py scripts/configure_install.py scripts/check_amd_runtime.py scripts/verify_comfyui_gpu.py config/openwebui/apply_qwen_images.py config/openwebui/apply_model_parameters.py config/openwebui/verify_qwen_workflows.py ai-stack.service .env.example; do
  [[ -f "$SOURCE/$f" ]] || die "Missing source file: $SOURCE/$f (see --source)."
done
if [[ "$SOURCE" != "$TARGET" && -d "$TARGET" ]] && (( ! UPDATE_FILES )); then
  python3 "$SOURCE/scripts/check_managed_files.py" "$SOURCE" "$TARGET"
fi
# Hold one lock across package setup, file updates and service recreation so
# concurrent runs cannot interleave credential writes or deployment backups.
exec 9>/run/lock/ai-stack-install.lock
flock -n 9 || die 'Another installation is already running.'
ROCMINFO=$(command -v rocminfo || true)
if [[ -z "$ROCMINFO" ]]; then
  for f in /opt/rocm/bin/rocminfo /opt/rocm-*/bin/rocminfo /opt/rocm/core-*/bin/rocminfo; do
    if [[ -x "$f" ]]; then ROCMINFO=$f; break; fi
  done
fi
[[ -n "$ROCMINFO" ]] || die 'rocminfo was not found in PATH or under /opt/rocm.'
GPU_INFO=$("$ROCMINFO") || die 'rocminfo failed.'
DETECTED=$(printf '%s\n' "$GPU_INFO" | awk '$1 == "Name:" && $2 ~ /^gfx[0-9a-f]+$/ && $2 != "gfx000" {print $2}' | sort -u)
[[ -n "$DETECTED" ]] || die 'rocminfo did not detect a GPU target.'
if [[ -z "$GFX" ]]; then
  [[ "$DETECTED" != *$'\n'* ]] || die 'Multiple GPU types detected: please specify --gfx.'
  GFX=$DETECTED
fi
printf '%s\n' "$DETECTED" | grep -Fxq "$GFX" || die 'The selected --gfx target was not detected by rocminfo.'

log 'Installing base packages'
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl gnupg openssl python3 rsync acl
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1 || ! docker buildx version >/dev/null 2>&1; then
  for package in docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc; do
    if [[ $(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true) == 'install ok installed' ]]; then
      die "Package conflict: $package. Resolve the existing container installation manually first."
    fi
  done
  log 'Installing Docker Engine, Compose and Buildx from the official repository'
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL --retry 3 https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod 0644 /etc/apt/keyrings/docker.asc
  cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $CODENAME
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker
if ! command -v amd-ctk >/dev/null; then
  log 'Installing AMD Container Toolkit'
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL --retry 3 https://repo.radeon.com/rocm/rocm.gpg.key | gpg --batch --yes --dearmor -o /etc/apt/keyrings/rocm.gpg
  chmod 0644 /etc/apt/keyrings/rocm.gpg
  printf 'deb [arch=amd64 signed-by=/etc/apt/keyrings/rocm.gpg] https://repo.radeon.com/amd-container-toolkit/apt/ %s main\n' "$CODENAME" > /etc/apt/sources.list.d/amd-container-toolkit.list
  apt-get update
  apt-get install -y amd-container-toolkit
fi
# Reconfigure the daemon only if the AMD runtime is missing; a Docker restart
# also affects unrelated containers on this host.
if ! docker info --format '{{json .Runtimes}}' | python3 "$SOURCE/scripts/check_amd_runtime.py"; then
  if [[ -f /etc/docker/daemon.json ]]; then cp -p /etc/docker/daemon.json "/etc/docker/daemon.json.ai-stack-$(date +%Y%m%d%H%M%S).bak"; fi
  amd-ctk runtime configure
  systemctl restart docker
fi
docker info --format '{{json .Runtimes}}' | python3 "$SOURCE/scripts/check_amd_runtime.py"

log 'Preparing stack files and configuration'
install -d -m 0755 "$TARGET"
if [[ "$SOURCE" != "$TARGET" ]]; then
  # Only explicitly listed build/runtime files enter the deployment directory.
  # Setup helpers, documentation, tests and source credentials are not in the manifest.
  if ((UPDATE_FILES)); then
    backup="$TARGET/.installer-backups/$(date +%Y%m%d%H%M%S)-$$"
    install -d -m 0700 "$backup"
    rsync -ar --checksum --backup --backup-dir="$backup" --files-from="$MANIFEST" "$SOURCE/" "$TARGET/"
    # Retire only the replaced managed workflows; retain backups and user workflows.
    for retired in \
      config/openwebui/qwen-image-2512_image.json \
      config/openwebui/qwen-image-2512_nodes.json \
      config/openwebui/qwen-image-edit-2511_image.json \
      config/openwebui/qwen-image-edit-2511_nodes.json \
      config/comfyui/workflows/unsloth_qwen_image_2512.json \
      config/comfyui/workflows/unsloth_qwen_image_edit_2511.json; do
      if [[ -f "$TARGET/$retired" ]]; then
        install -d -m 0700 "$backup/$(dirname "$retired")"
        mv "$TARGET/$retired" "$backup/$retired"
      fi
    done
  else
    rsync -ar --ignore-existing --files-from="$MANIFEST" "$SOURCE/" "$TARGET/"
  fi
  if [[ ! -f "$TARGET/.env" && -f "$SOURCE/.env" ]]; then
    install -m 0600 "$SOURCE/.env" "$TARGET/.env"
  fi
fi
# Bind-mounted configuration may have changed without changing Compose.
# Retain this marker through --no-start or failed installation attempts.
touch "$TARGET/.installer-recreate-required"
cd "$TARGET"
if [[ ! -f .env ]]; then install -m 0600 "$SOURCE/.env.example" .env; fi
# Compose parses .env directly. Values are never converted into shell code.
# The parent environment must not override the saved configuration.
compose() { env -i PATH="$PATH" HOME=/root docker compose --project-name ai-stack --env-file "$TARGET/.env" -f "$TARGET/docker-compose.yml" -f "$TARGET/compose.install.yml" "$@"; }
export AI_INSTALL_SOURCE="$SOURCE"
export AI_INSTALL_NON_INTERACTIVE="$NON_INTERACTIVE"
export AI_INSTALL_HOST="$HOST_NAME" AI_INSTALL_GFX="$GFX" AI_INSTALL_REF="$LLAMA_REF"
python3 "$SOURCE/scripts/configure_install.py"

# Keep the legacy override for existing installer/systemd commands.
# Build values live in .env and are also mapped by the base Compose file.
cat > compose.install.yml <<'EOF'
services:
  llama-cpp:
    build:
      args:
        ROCM_GFX_TARGETS: ${ROCM_GFX_TARGETS:?Set ROCM_GFX_TARGETS in .env (see .env.example)}
        LLAMA_CPP_REF: ${LLAMA_CPP_REF:?Set LLAMA_CPP_REF in .env (see .env.example)}
EOF
install -d -m 0750 data/{grafana,prometheus,openwebui,open-terminal,llama-cpp,comfyui,searxng}
install -d -m 0750 data/comfyui/{input,output,user,models,custom_nodes}
install -d -m 0750 data/comfyui/models/{checkpoints,vae,loras,unet,text_encoders}
# Open Terminal runs as user (UID/GID 1000); preserve ownership inside its workspace.
chown 1000:1000 data/open-terminal
# Match the upstream Grafana and Prometheus container UIDs for writable data.
chown -R 472:0 data/grafana
chown -R 65534:65534 data/prometheus
chown root:root .env config/llama-cpp/api-key.txt config/nginx/certs/nginx.key
chmod 0600 .env config/llama-cpp/api-key.txt config/nginx/certs/nginx.key
# Prometheus runs as UID 65534 and must read the same bind-mounted secret.
setfacl -m u:65534:r config/llama-cpp/api-key.txt
find config/grafana -type d -exec chmod 0755 {} +
find config/grafana -type f -exec chmod 0644 {} +
compose config --quiet

log "Building ROCm images (GPU: $GFX); the initial build may take a long time"
compose build llama-cpp comfyui
compose pull nginx openwebui open-terminal searxng llama-metrics-discovery prometheus grafana

# Prefetch is intentionally before service startup: readiness should not wait on
# multi-gigabyte model downloads. This step still runs with --no-start.
log 'Downloading every configured chat model and all Qwen image assets'
python3 "$SOURCE/scripts/download_models.py" --root "$TARGET"

if ((START)); then
  log 'Starting the stack and checking readiness'
  recreate_args=()
  if [[ -e .installer-recreate-required ]]; then recreate_args=(--force-recreate); fi
  if ! compose up -d --no-build --wait --wait-timeout "$WAIT_SECONDS" "${recreate_args[@]}"; then
    compose ps -a
    die "Services are not ready. In $TARGET, check logs with docker compose -f docker-compose.yml -f compose.install.yml logs."
  fi
  # Verify ComfyUI directly as well as its inherited Dockerfile health check.
  comfy_ready=0
  for ((attempt=0; attempt<60; attempt++)); do
    if compose exec -T comfyui python -m urllib.request http://localhost:8188/system_stats >/dev/null 2>&1; then
      comfy_ready=1; break
    fi
    sleep 5
  done
  ((comfy_ready)) || die 'ComfyUI did not respond within the readiness checks.'
  compose exec -T comfyui python - < "$SOURCE/scripts/verify_comfyui_gpu.py"
  curl -kfsS --retry 10 --retry-delay 3 --retry-all-errors --max-time 15 https://localhost:8443/health >/dev/null
  compose exec -T openwebui python /etc/openwebui/verify_qwen_workflows.py
  compose exec -T openwebui python /etc/openwebui/apply_qwen_images.py
  compose exec -T openwebui python /etc/openwebui/apply_open_terminal.py
  # Database-backed settings are cached in the web process; restart after writes.
  compose restart openwebui
  compose up -d --no-build --wait --wait-timeout "$WAIT_SECONDS" openwebui
  # Check that Prometheus actually parsed the configured rules.
  compose exec -T prometheus promtool check config /etc/prometheus/prometheus.yml
  rm -f .installer-recreate-required
  log 'Configuring systemd autostart'
  if [[ -f /etc/systemd/system/ai-stack.service ]]; then
    cp -p /etc/systemd/system/ai-stack.service "/etc/systemd/system/ai-stack.service.backup-$(date +%Y%m%d%H%M%S)"
  fi
  install -m 0644 "$SOURCE/ai-stack.service" /etc/systemd/system/ai-stack.service
  systemctl daemon-reload
  systemctl enable --now ai-stack.service
  compose ps
fi
log 'Setup complete'
printf 'Configuration and credentials: %s/.env (readable by root only)\n' "$TARGET"
printf 'Open WebUI: https://<Host>:8443/ | Grafana: https://<Host>:8443/grafana/ (admin)\nComfyUI: https://<Host>:8444/ (unless COMFYUI_HTTPS_PORT was changed)\n'
printf 'Self-signed TLS: expect a browser warning. Model files are downloaded; VRAM loading happens on demand.\n'
