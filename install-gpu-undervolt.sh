#!/usr/bin/env bash
# Run on Ubuntu: sudo bash install-gpu-undervolt.sh
# Prerequisite: OverDrive is already enabled in the AMDGPU driver.
set -euo pipefail

if [[ $(uname -s) != Linux ]]; then
    echo 'This script must be run on the Ubuntu machine.' >&2
    exit 1
fi
if (( EUID != 0 )); then
    echo 'Please run with sudo bash install-gpu-undervolt.sh.' >&2
    exit 1
fi
if ! command -v systemctl >/dev/null || [[ ! -d /run/systemd/system ]]; then
    echo 'This script requires a running systemd system.' >&2
    exit 1
fi

found=0
for gpu in /sys/class/drm/card[0-9]*/device; do
    grep -qx '0x1002' "$gpu/vendor" 2>/dev/null || continue
    [[ $(basename "$(readlink -f "$gpu/driver")") == amdgpu ]] || continue
    # sysfs file sizes do not reliably indicate their contents.
    grep -q '[^[:space:]]' "$gpu/pp_od_clk_voltage" 2>/dev/null || continue
    found=$((found + 1))
    selected_gpu=$(readlink -f "$gpu")
done
if (( found == 0 )); then
    echo 'No AMD GPU with the amdgpu driver and a readable OverDrive interface found.' >&2
    echo 'Run in the Ubuntu VM; check the AMDGPU kernel parameters. Nothing was installed.' >&2
    exit 1
fi
if (( found > 1 )); then
    echo 'Multiple matching AMD GPUs found. An explicit PCI device selection is required.' >&2
    echo 'Nothing was installed.' >&2
    exit 1
fi
pci_address=$(basename "$selected_gpu")
if [[ ! $pci_address =~ ^[[:xdigit:]]{4}:[[:xdigit:]]{2}:[[:xdigit:]]{2}\.[0-7]$ ]]; then
    echo "Unexpected PCI address: $pci_address. Nothing was installed." >&2
    exit 1
fi
echo "Selected AMD GPU: $pci_address"

install -d -m 755 /usr/local/sbin /etc/systemd/system
printf 'PCI_ADDRESS=%s\n' "$pci_address" > /etc/gpu-undervolt.conf
chmod 644 /etc/gpu-undervolt.conf
cat > /usr/local/sbin/gpu-undervolt <<'SCRIPT'
#!/bin/sh
set -eu
. /etc/gpu-undervolt.conf
gpu="/sys/bus/pci/devices/$PCI_ADDRESS"

attempt=0
while [ "$attempt" -lt 30 ]; do
    if grep -qx '0x1002' "$gpu/vendor" 2>/dev/null &&
       [ "$(basename "$(readlink -f "$gpu/driver")")" = amdgpu ] &&
       grep -q '[^[:space:]]' "$gpu/pp_od_clk_voltage" 2>/dev/null; then
        printf '%s\n' 'manual' > "$gpu/power_dpm_force_performance_level"
        printf '%s\n' 'vo -80' > "$gpu/pp_od_clk_voltage"
        printf '%s\n' 'c' > "$gpu/pp_od_clk_voltage"
        echo "Applied a voltage offset of -80 mV to $gpu."
        cat "$gpu/pp_od_clk_voltage"
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 1
done

echo "AMD GPU $PCI_ADDRESS with an OverDrive interface is unavailable." >&2
exit 1
SCRIPT
chmod 755 /usr/local/sbin/gpu-undervolt

cat > /etc/systemd/system/gpu-undervolt.service <<'UNIT'
[Unit]
Description=Set GPU voltage offset to -80 mV
After=systemd-modules-load.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/gpu-undervolt
RemainAfterExit=yes
TimeoutStartSec=45

[Install]
WantedBy=multi-user.target
UNIT
chmod 644 /etc/systemd/system/gpu-undervolt.service

systemctl daemon-reload
# Reapply the settings when reinstalling as well.
if ! systemctl restart gpu-undervolt.service; then
    systemctl disable gpu-undervolt.service >/dev/null 2>&1 || true
    journalctl -u gpu-undervolt.service -b -n 30 --no-pager || true
    echo 'Failed to apply settings. Automatic startup was left disabled or has been disabled.' >&2
    exit 1
fi
systemctl enable gpu-undervolt.service

echo
echo 'Done: -80 mV applied and configured to be applied at every system startup.'
echo 'The existing kernel parameter remains unchanged.'
echo 'Logs: journalctl -u gpu-undervolt.service -b --no-pager'
echo 'Disable automatic startup: sudo systemctl disable gpu-undervolt.service'
echo 'Then reboot to reset the currently applied offset.'
