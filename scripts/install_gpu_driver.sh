#!/usr/bin/env bash
# Sourced by install_ai_stack.sh after acquiring its installation lock.
# Host bootstrap policy: Radeon, Ubuntu 26.04 / GA 7.0; selected AMD release.
# Sources and scope are documented in INSTALL.md. Never execute .env/state files.
GPU_STATE_DIR=/var/lib/ai-stack/gpu-driver
# Central default. --gpu-driver-series overrides this value for one installer run.
GPU_DRIVER_SERIES=31.60
GPU_DRIVER_SERIES=${GPU_DRIVER_SERIES_OVERRIDE:-$GPU_DRIVER_SERIES}

gpu_validate_series() {
  [[ "$GPU_DRIVER_SERIES" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]] ||
    die 'Invalid GPU driver series: use a release such as 31.50 or 31.60.'
}

gpu_repository_versions() {
  # Compare the entire URI and distribution, not a regex or substring. In
  # particular, 31.60 must not select 31.600, another Ubuntu release or mirror.
  apt-cache madison amdgpu-dkms | awk -F '|' \
    -v uri="https://repo.radeon.com/amdgpu/$GPU_DRIVER_SERIES/ubuntu" \
    -v suite="$CODENAME/main" '
      { gsub(/^[[:space:]]+|[[:space:]]+$/, "", $2);
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", $3);
        split($3, source, /[[:space:]]+/);
        sub(/\/$/, "", source[1]);
        if (source[1] == uri && source[2] == suite && source[3] == "amd64") print $2;
      }'
}

gpu_check_saved_series() {
  local saved
  if [[ -f "$GPU_STATE_DIR/driver-series" ]]; then
    read -r saved < "$GPU_STATE_DIR/driver-series"
    [[ "$saved" == "$GPU_DRIVER_SERIES" ]] ||
      die "Saved GPU installation uses AMD $saved; requested $GPU_DRIVER_SERIES. Complete any pending reboot with the original selection. For a version change, follow the manual migration in INSTALL.md."
  fi
}

gpu_check_selected_driver() {
  local installed saved series
  installed=$(gpu_package_version)
  if [[ -f "$GPU_STATE_DIR/driver-series" && -f "$GPU_STATE_DIR/package-version" ]]; then
    read -r series < "$GPU_STATE_DIR/driver-series"
    read -r saved < "$GPU_STATE_DIR/package-version"
    if [[ "$series" == "$GPU_DRIVER_SERIES" && "$installed" == "$saved" && -n "$installed" ]]; then
      return 0
    fi
  fi
  # This also handles installations predating driver-series state and manual
  # APT updates. Cached signed repository metadata must establish provenance.
  if [[ -n "$installed" ]] && gpu_repository_versions | grep -Fx "$installed" >/dev/null; then
    return 0
  fi
  die "Installed AMDGPU package '${installed:-none/inbox driver}' cannot be verified against requested AMD $GPU_DRIVER_SERIES. No driver was replaced. Refresh the selected signed repository metadata or migrate the driver manually (INSTALL.md)."
}

gpu_find_rocminfo() {
  local candidate
  ROCMINFO=$(command -v rocminfo || true)
  if [[ -z "$ROCMINFO" ]]; then
    for candidate in /opt/rocm/bin/rocminfo /opt/rocm-*/bin/rocminfo /opt/rocm/core-*/bin/rocminfo; do
      if [[ -x "$candidate" ]]; then ROCMINFO=$candidate; break; fi
    done
  fi
}

gpu_probe() {
  GPU_PROBLEM='AMD GPU devices/module are missing; check PCI passthrough, Secure Boot and reboot status.'
  [[ -c /dev/kfd && -d /dev/dri && -d /sys/module/amdgpu ]] || return 1
  gpu_find_rocminfo
  GPU_PROBLEM='rocminfo is missing; the host discovery tool needs to be installed.'
  [[ -n "$ROCMINFO" ]] || return 1
  GPU_PROBLEM='rocminfo failed; check the driver, HSA runtime and GPU passthrough.'
  GPU_INFO=$(timeout 60 "$ROCMINFO" 2>&1) || return 1
  DETECTED=$(printf '%s\n' "$GPU_INFO" | awk '$1 == "Name:" && $2 ~ /^gfx[0-9a-f]+$/ && $2 != "gfx000" {print $2}' | sort -u)
  GPU_PROBLEM='rocminfo did not detect a GPU target.'
  [[ -n "$DETECTED" ]]
}

gpu_check_platform() {
  local release=$1 codename=$2 kernel=$3
  [[ "$release:$codename" == 26.04:resolute && "$kernel" =~ ^7\.0\.[0-9]+-[0-9]+-generic$ ]] ||
    die "Automatic GPU installation requires Ubuntu 26.04 (resolute), GA 7.0 generic kernel; found $release/$codename/$kernel. Prepare a supported driver manually on other systems."
}

gpu_pci_target() {
  # Conservative subset of the ROCm 10.1 Radeon matrix; PCI IDs from pciutils.
  case "$1" in
    0x7550|0x7551) printf 'gfx1201\n' ;; # Navi 48: RX 9070 / AI PRO R9700
    0x7590) printf 'gfx1200\n' ;;        # Navi 44
    0x7448|0x7449|0x744a|0x744b|0x744c|0x745e) printf 'gfx1100\n' ;;
    0x7460|0x7461|0x7470|0x747e) printf 'gfx1101\n' ;;
    0x7480|0x7489) printf 'gfx1102\n' ;;
    *) return 1 ;;
  esac
}

gpu_check_pci() {
  local device vendor class device_id target binding found=0 selected=0
  for device in /sys/bus/pci/devices/*; do
    [[ -r "$device/vendor" && -r "$device/class" ]] || continue
    read -r vendor < "$device/vendor"
    read -r class < "$device/class"
    [[ "$vendor" == 0x1002 && "$class" == 0x03* ]] || continue
    found=1
    read -r device_id < "$device/device"
    target=$(gpu_pci_target "$device_id") || die "AMD GPU ${device##*/} ($device_id) is outside the automatic-install allowlist. Prepare its driver manually; see INSTALL.md."
    binding=$(readlink "$device/driver" || true)
    [[ -z "$binding" || "${binding##*/}" == amdgpu ]] || die "GPU ${device##*/} is bound to ${binding##*/}. Run this inside the Ubuntu GPU VM; check passthrough first."
    printf 'GPU %s: AMD %s (%s)\n' "${device##*/}" "$device_id" "$target"
    if [[ -z "$GFX" || "$GFX" == "$target" ]]; then selected=1; fi
  done
  ((found)) || die 'No AMD PCI display device found. Pass the GPU through to this Ubuntu VM first.'
  ((selected)) || die 'The requested --gfx target does not match the detected PCI devices.'
}

gpu_package_version() {
  dpkg-query -W -f='${Version}' amdgpu-dkms 2>/dev/null || true
}

gpu_reboot_required() {
  printf '\nGPU driver setup needs a reboot. The AI stack has not been started by this run.\n'
  printf 'If Secure Boot is enabled, enroll the MOK in the VM console during reboot.\n'
  printf 'Reboot when ready, then rerun the same installer command. Exit status: 20.\n'
  exit 20
}

gpu_check_loaded_module() {
  local disk loaded
  disk=$(modinfo -F srcversion amdgpu 2>/dev/null || true)
  loaded=$(cat /sys/module/amdgpu/srcversion 2>/dev/null || true)
  if [[ -n "$disk" && -n "$loaded" && "$disk" != "$loaded" ]]; then
    die 'The installed AMD module differs from the loaded module. Reboot and rerun before continuing.'
  fi
}

gpu_secure_boot() {
  GPU_SECURE_BOOT=0
  [[ -d /sys/firmware/efi ]] || return 0
  local status
  status=$(LC_ALL=C mokutil --sb-state) || die 'Unable to determine Secure Boot state.'
  case "$status" in
    *'SecureBoot disabled'*) return 0 ;;
    *'SecureBoot enabled'*) GPU_SECURE_BOOT=1 ;;
    *) die "Unknown Secure Boot state: $status" ;;
  esac
  # Ubuntu's DKMS tooling uses the shim-signed MOK. Let Ubuntu create the key.
  update-secureboot-policy --new-key
  local certificate=/var/lib/shim-signed/mok/MOK.der
  [[ -r "$certificate" ]] || die 'Ubuntu did not create the DKMS signing certificate.'
  if mokutil --test-key "$certificate" >/dev/null 2>&1; then return 0; fi
  if ((NON_INTERACTIVE)) || ! (exec 8<>/dev/tty) 2>/dev/null; then
    printf '\nSecure Boot requires MOK enrollment before unattended installation.\n'
    printf 'Run: sudo mokutil --import %s\n' "$certificate"
    printf 'Choose a temporary password, reboot, enroll the key in the VM console, then rerun. Exit status: 21.\n'
    exit 21
  fi
  printf '\nSecure Boot: choose a temporary MOK password below. Enroll this key in the VM console on the next reboot.\n'
  mokutil --import "$certificate" </dev/tty >/dev/tty 2>&1
}

gpu_register_repository() {
  local file key_tmp
  # Do not mix a pre-existing AMD driver repository with a second release.
  for file in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
    [[ -f "$file" && "$file" != /etc/apt/sources.list.d/ai-stack-amdgpu.list ]] || continue
    if grep -Eq '^[[:space:]]*[^#].*repo\.radeon\.com/amdgpu/' "$file"; then
      die "Existing AMD driver repository in $file. Resolve it manually before automatic installation."
    fi
  done
  install -d -m 0755 /etc/apt/keyrings
  key_tmp=$(mktemp)
  if ! curl -fsSL --retry 3 https://repo.radeon.com/rocm/rocm.gpg.key -o "$key_tmp"; then
    rm -f "$key_tmp"; die 'AMD signing key download failed.'
  fi
  if ! gpg --batch --yes --dearmor -o "$GPU_STATE_DIR/key.gpg" "$key_tmp"; then
    rm -f "$key_tmp"; die 'AMD signing key is invalid.'
  fi
  rm -f "$key_tmp"
  install -m 0644 "$GPU_STATE_DIR/key.gpg" /etc/apt/keyrings/ai-stack-amdgpu.gpg
  rm -f "$GPU_STATE_DIR/key.gpg"
  printf 'deb [arch=amd64 signed-by=/etc/apt/keyrings/ai-stack-amdgpu.gpg] https://repo.radeon.com/amdgpu/%s/ubuntu %s main\n' \
    "$GPU_DRIVER_SERIES" "$CODENAME" > /etc/apt/sources.list.d/ai-stack-amdgpu.list
}

gpu_prepare() {
  local phase='' boot_id old_boot='' version answer kernel install_driver=1
  gpu_validate_series
  gpu_check_saved_series
  kernel=$(uname -r)
  boot_id=$(cat /proc/sys/kernel/random/boot_id)
  if [[ -f "$GPU_STATE_DIR/phase" ]]; then
    read -r phase < "$GPU_STATE_DIR/phase"
    case "$phase" in installing|awaiting-reboot|ready) ;; *) die 'Invalid saved GPU installation phase.' ;; esac
  fi
  if [[ "$phase" == awaiting-reboot ]]; then
    read -r old_boot < "$GPU_STATE_DIR/boot-id"
    [[ "$boot_id" != "$old_boot" ]] || gpu_reboot_required
    gpu_probe || die "$GPU_PROBLEM Check MOK enrollment/DKMS logs. The reboot marker was retained."
    gpu_check_loaded_module
    gpu_check_selected_driver
    printf 'ready\n' > "$GPU_STATE_DIR/phase"
    log 'GPU driver verified after reboot'
    return 0
  fi
  if gpu_probe && [[ "$phase" != installing ]]; then
    gpu_check_loaded_module
    gpu_check_selected_driver
    log "Using the existing working AMD GPU driver (selected release: $GPU_DRIVER_SERIES) and rocminfo"
    return 0
  fi
  printf '%s\n' "$GPU_PROBLEM"
  if systemd-detect-virt --container --quiet; then
    die 'Install the driver on the Ubuntu host/VM, not inside a container.'
  fi
  gpu_check_platform "$VERSION_ID" "$CODENAME" "$kernel"
  gpu_check_pci
  gpu_find_rocminfo
  # Missing diagnostics alone must never cause replacement of a loaded driver.
  if [[ -c /dev/kfd && -d /sys/module/amdgpu && -z "$ROCMINFO" ]]; then install_driver=0; fi
  version=$(gpu_package_version)
  if ((install_driver)) && [[ -n "$version" && "$phase" != installing ]]; then
    die "AMDGPU package $version already exists but GPU checks failed. Reboot/check Secure Boot or repair it manually; automatic driver replacement is disabled."
  fi
  if (( ! INSTALL_GPU_DRIVER )) && [[ "$phase" != installing ]]; then
    (( ! NON_INTERACTIVE )) && (exec 8<>/dev/tty) 2>/dev/null ||
      die 'GPU setup is missing. Rerun with --install-gpu-driver to authorize installation.'
    printf 'Install missing GPU driver/discovery packages (AMD %s, Ubuntu %s)? [y/N] ' "$GPU_DRIVER_SERIES" "$VERSION_ID" >/dev/tty
    read -r answer </dev/tty || die 'No answer received.'
    case "$answer" in y|Y|yes|YES|j|J|ja|JA) ;; *) die 'GPU installation declined.' ;; esac
  fi
  install -d -m 0700 "$GPU_STATE_DIR"
  log 'Installing GPU setup prerequisites'
  DEBIAN_FRONTEND=noninteractive apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl gnupg kmod mokutil
  if (( ! install_driver )); then
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends rocminfo
    gpu_probe || die "$GPU_PROBLEM The existing driver was preserved."
    gpu_check_loaded_module
    gpu_check_selected_driver
    return 0
  fi
  local packages=("linux-headers-$kernel" dkms build-essential rocminfo)
  if [[ -d /sys/firmware/efi ]]; then packages+=(shim-signed); fi
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${packages[@]}"
  gpu_secure_boot
  gpu_register_repository
  DEBIAN_FRONTEND=noninteractive apt-get update
  # Select an exact package from the fixed release repository, never apt's
  # possibly newer candidate from an unrelated repository.
  local versions saved_version
  versions=$(gpu_repository_versions)
  [[ -n "$versions" ]] || die "No amdgpu-dkms package found in the signed AMD $GPU_DRIVER_SERIES repository for $CODENAME."
  if [[ "$phase" == installing ]]; then
    # Retry the same exact package, even if a newer package appeared meanwhile.
    read -r saved_version < "$GPU_STATE_DIR/package-version"
    printf '%s\n' "$versions" | grep -Fx "$saved_version" >/dev/null ||
      die "Pending package $saved_version is unavailable in AMD $GPU_DRIVER_SERIES. Keep the original selection and restore its repository package before retrying."
    version=$saved_version
  else
    version=${versions%%$'\n'*}
  fi
  printf '%s\n' "$version" > "$GPU_STATE_DIR/package-version"
  printf '%s\n' "$GPU_DRIVER_SERIES" > "$GPU_STATE_DIR/driver-series"
  printf 'installing\n' > "$GPU_STATE_DIR/phase"
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "amdgpu-dkms=$version"
  dkms status -m amdgpu -k "$kernel" | grep -q ': installed' || die 'AMDGPU DKMS was not installed for the running kernel.'
  if ((GPU_SECURE_BOOT)); then
    [[ -n $(modinfo -k "$kernel" -F signer amdgpu) ]] || die 'The installed AMD module is unsigned. Check DKMS signing before rebooting.'
  fi
  printf '%s\n' "$boot_id" > "$GPU_STATE_DIR/boot-id"
  printf 'awaiting-reboot\n' > "$GPU_STATE_DIR/phase"
  gpu_reboot_required
}
