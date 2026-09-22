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
for f in config/openwebui/apply_open_terminal.py scripts/download_models.py config/openwebui/apply_qwen_images.py config/openwebui/apply_model_parameters.py config/openwebui/verify_qwen_workflows.py ai-stack.service .env.example; do
  [[ -f "$SOURCE/$f" ]] || die "Missing source file: $SOURCE/$f (see --source)."
done
if [[ "$SOURCE" != "$TARGET" && -d "$TARGET" ]] && (( ! UPDATE_FILES )); then
  AI_INSTALL_SOURCE="$SOURCE" AI_INSTALL_TARGET="$TARGET" python3 - <<'CHECK'
import os
from pathlib import Path
source, target = Path(os.environ['AI_INSTALL_SOURCE']), Path(os.environ['AI_INSTALL_TARGET'])
managed = [source/name for name in (source/'scripts/deployment_files.txt').read_text().splitlines()]
stale = [str(p.relative_to(source)) for p in managed
         if (target/p.relative_to(source)).is_file()
         and p.read_bytes() != (target/p.relative_to(source)).read_bytes()]
if stale:
    raise SystemExit('Managed target files differ. Review your customizations and rerun with --update-files to back up and replace them:\n' + '\n'.join(stale))
CHECK
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
if ! docker info --format '{{json .Runtimes}}' | python3 -c 'import json,sys; sys.exit("amd" not in json.load(sys.stdin))'; then
  if [[ -f /etc/docker/daemon.json ]]; then cp -p /etc/docker/daemon.json "/etc/docker/daemon.json.ai-stack-$(date +%Y%m%d%H%M%S).bak"; fi
  amd-ctk runtime configure
  systemctl restart docker
fi
docker info --format '{{json .Runtimes}}' | python3 -c 'import json,sys; assert "amd" in json.load(sys.stdin), "AMD runtime is missing"'

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
python3 - <<'PY'
import datetime, getpass, ipaddress, os, pathlib, re, secrets, shutil, subprocess, warnings
root = pathlib.Path('/opt/ai-stack')
source = pathlib.Path(os.environ['AI_INSTALL_SOURCE'])
path = root / '.env'
original = path.read_text()
# A minimal Compose document resolves .env syntax without evaluating required
# service variables before empty/example credentials have been generated.
result = subprocess.run(['docker', 'compose', '--env-file', str(source/'.env.example'), '--env-file', str(path), '-f', '-', 'config', '--environment'], input='services:\n  bootstrap:\n    image: busybox\n', env={'PATH': os.environ['PATH'], 'HOME': '/root'}, text=True, stdout=subprocess.PIPE, check=True).stdout
values = dict(line.split('=', 1) for line in result.splitlines() if '=' in line)
host = os.environ['AI_INSTALL_HOST'] or values.get('PUBLIC_IP_OR_DOMAIN', '')
if not host or host == 'localhost':
    raise SystemExit('Specify --host with the DNS name or IPv4 address of the Ubuntu machine.')
try:
    addr = ipaddress.ip_address(host)
    if addr.version != 4: raise SystemExit('Use an IPv4 address or DNS name for --host.')
    san = 'IP:' + host
except ValueError:
    if len(host) > 253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', p) for p in host.split('.')):
        raise SystemExit('Invalid DNS name for --host.')
    san = 'DNS:' + host
def prompt_secret(name, default, empty_action):
    if os.environ.get('AI_INSTALL_NON_INTERACTIVE') == '1':
        return default
    try:
        with open('/dev/tty', 'w') as terminal:
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                entered = getpass.getpass(f'{name} (hidden; Enter to {empty_action}): ', stream=terminal)
    except (OSError, EOFError, getpass.GetPassWarning):
        raise SystemExit('A terminal is required for secret prompts. Use --non-interactive for unattended installation.')
    return entered or default

secret = root/'config/llama-cpp/api-key.txt'
old_key = secret.read_text().strip() if secret.exists() else ''
saved_key = values.get('LLAMA_CPP_API_KEY', '')
if saved_key in ('', 'sk-llm-inference-stack-super-secret-key-12345'):
    saved_key = old_key
if old_key and saved_key != old_key:
    raise SystemExit('.env and api-key.txt contain different keys; synchronize them first.')
saved_password = values.get('GRAFANA_ADMIN_PASSWORD', '')
if saved_password in ('', 'admin', 'changeme'):
    saved_password = ''
has_grafana_db = (root/'data/grafana/grafana.db').exists()
if has_grafana_db:
    print('Grafana already has a database. Enter its current admin password; this installer does not reset it.')
password = prompt_secret('GRAFANA_ADMIN_PASSWORD', saved_password,
                         'keep the saved value' if saved_password else ('leave unset' if has_grafana_db else 'generate a password'))
if has_grafana_db and not password:
    raise SystemExit('Existing Grafana database: enter the current password in .env first.')
password = password or secrets.token_hex(24)
hf_token = values.get('HF_TOKEN', '')
if hf_token == 'hf_your_token_here':
    hf_token = ''
hf_token = prompt_secret('HF_TOKEN', hf_token, 'keep the saved value' if hf_token else 'skip (optional)')
# Generate once without prompting; retain the synchronized deployment key.
key = saved_key or secrets.token_hex(32)
# Backfill newly introduced deployment variables without overwriting saved values.
# Compose parses both files, so quoted values and precedence follow Compose rules.
template_names = re.findall(r'^([A-Z][A-Z0-9_]*)=', (source/'.env.example').read_text(), re.M)
present_names = set(re.findall(r'^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=', original, re.M))
updates = {name: values[name] for name in template_names if name not in present_names}
updates.update({'AI_STACK_ROOT': str(root), 'PUBLIC_IP_OR_DOMAIN': host,
           'LLAMA_CPP_API_KEY': key, 'GRAFANA_ADMIN_PASSWORD': password,
           'ROCM_GFX_TARGETS': os.environ['AI_INSTALL_GFX'],
           'LLAMA_CPP_REF': os.environ['AI_INSTALL_REF'] or values['LLAMA_CPP_REF']})
# Enable prompt generation when introducing the dedicated image-prompt service.
# Later installer runs preserve an intentional disable in the deployed .env.
if 'LLAMA_CPP_IMAGE_CTX_SIZE' not in present_names:
    updates['ENABLE_IMAGE_PROMPT_GENERATION'] = 'true'
# Migrate only the exact obsolete bundled image defaults; preserve custom values.
if values.get('IMAGE_GENERATION_MODEL') == 'flux2-dev-Q4_K_M.gguf':
    updates['IMAGE_GENERATION_MODEL'] = 'qwen-image-2.1-Q4_K_M.gguf'
    if values.get('IMAGE_SIZE') == '1104x1472':
        updates['IMAGE_SIZE'] = '1024x1024'
# Upgrade the bundled generation/editing pair to the unified image model.
for image_key, previous in (
    ('IMAGE_GENERATION_MODEL', 'qwen-image-2512-Q4_K_M.gguf'),
    ('IMAGE_EDIT_MODEL', 'qwen-image-edit-2511-Q4_K_M.gguf'),
):
    if values.get(image_key) == previous:
        updates[image_key] = 'qwen-image-2.1-Q4_K_M.gguf'
updates['HF_TOKEN'] = hf_token
search_secret = values.get('SEARXNG_SECRET', '')
updates['SEARXNG_SECRET'] = (secrets.token_hex(32)
                            if search_secret in ('', 'ultrasecretkey') else search_secret)
terminal_key = values.get('OPEN_TERMINAL_API_KEY', '')
updates['OPEN_TERMINAL_API_KEY'] = terminal_key or secrets.token_hex(32)
webui_secret = values.get('WEBUI_SECRET_KEY', '')
updates['WEBUI_SECRET_KEY'] = webui_secret or secrets.token_hex(32)
# Values are written as single-quoted Compose literals, not shell code. Reject
# unsupported characters before writing so credentials cannot alter .env syntax.
for name, value in updates.items():
    if any(c in value for c in "\n\r'"):
        raise SystemExit(f'{name}: the installer does not support line breaks or single quotes.')
# Keep comments and unknown keys; collapse duplicate assignments for managed keys.
lines, seen = [], set()
for line in original.splitlines():
    name = next((name for name in updates if re.match(r'^\s*(?:export\s+)?'+name+r'\s*=', line)), None)
    if name is None:
        lines.append(line)
    elif name not in seen:
        lines.append(f"{name}='{updates[name]}'")
        seen.add(name)
lines.extend(f"{k}='{v}'" for k,v in updates.items() if k not in seen)
updated = '\n'.join(lines) + '\n'
# Retain this marker across interrupted installs until consumers are recreated.
if old_key and old_key != key:
    (root/'.installer-recreate-required').touch(mode=0o600)
if updated != original:
    backup = path.with_name('.env.backup-' + datetime.datetime.now().strftime('%Y%m%d%H%M%S%f'))
    shutil.copyfile(path, backup); backup.chmod(0o600)
    temporary = path.with_name('.env.install-tmp')
    temporary.touch(mode=0o600); temporary.write_text(updated); temporary.chmod(0o600)
    temporary.replace(path)
path.chmod(0o600)
if not secret.exists() or old_key != key:
    secret.touch(mode=0o600); secret.chmod(0o600); secret.write_text(key + '\n')
# Preserve an existing certificate pair; changing --host does not renew it.
certdir = root/'config/nginx/certs'
certdir.mkdir(parents=True, exist_ok=True)
crt, certkey = certdir/'nginx.crt', certdir/'nginx.key'
if crt.exists() != certkey.exists(): raise SystemExit('Incomplete TLS certificate pair; fix this first.')
if not crt.exists():
    subprocess.run(['openssl', 'req', '-x509', '-nodes', '-newkey', 'rsa:4096', '-days', '365', '-keyout', str(certkey), '-out', str(crt), '-subj', '/CN='+host, '-addext', 'subjectAltName='+san+',DNS:localhost,IP:127.0.0.1'], check=True)
certkey.chmod(0o600); crt.chmod(0o644)
PY

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
    if compose exec -T comfyui python -c 'import urllib.request; urllib.request.urlopen("http://localhost:8188/system_stats",timeout=5)' >/dev/null 2>&1; then
      comfy_ready=1; break
    fi
    sleep 5
  done
  ((comfy_ready)) || die 'ComfyUI did not respond within the readiness checks.'
  compose exec -T comfyui python -c 'import torch; assert torch.cuda.is_available(), "ComfyUI cannot detect a GPU"'
  curl -kfsS --retry 10 --retry-delay 3 --retry-all-errors --max-time 15 https://localhost:8443/health >/dev/null
  compose exec -T openwebui python -c 'import sys; sys.path.insert(0, "/etc/openwebui"); exec(compile(sys.stdin.read(), "<setup>", "exec"))' < "$SOURCE/config/openwebui/verify_qwen_workflows.py"
  compose exec -T openwebui python -c 'import sys; sys.path.insert(0, "/etc/openwebui"); exec(compile(sys.stdin.read(), "<setup>", "exec"))' < "$SOURCE/config/openwebui/apply_qwen_images.py"
  compose exec -T openwebui python -c 'import sys; sys.path.insert(0, "/etc/openwebui"); exec(compile(sys.stdin.read(), "<setup>", "exec"))' < "$SOURCE/config/openwebui/apply_open_terminal.py"
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
