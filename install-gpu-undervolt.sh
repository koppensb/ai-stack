#!/usr/bin/env bash
# Auf Ubuntu ausführen: sudo bash install-gpu-undervolt.sh
# Voraussetzung: OverDrive ist im AMDGPU-Treiber bereits freigeschaltet.
set -euo pipefail

if [[ $(uname -s) != Linux ]]; then
    echo 'Dieses Skript muss auf dem Ubuntu-Rechner ausgeführt werden.' >&2
    exit 1
fi
if (( EUID != 0 )); then
    echo 'Bitte mit sudo bash install-gpu-undervolt.sh ausführen.' >&2
    exit 1
fi
if ! command -v systemctl >/dev/null || [[ ! -d /run/systemd/system ]]; then
    echo 'Dieses Skript benötigt ein laufendes systemd-System.' >&2
    exit 1
fi

found=0
for gpu in /sys/class/drm/card[0-9]*/device; do
    grep -qx '0x1002' "$gpu/vendor" 2>/dev/null || continue
    [[ $(basename "$(readlink -f "$gpu/driver")") == amdgpu ]] || continue
    # sysfs-Dateigrößen sind kein verlässlicher Hinweis auf ihren Inhalt.
    grep -q '[^[:space:]]' "$gpu/pp_od_clk_voltage" 2>/dev/null || continue
    found=$((found + 1))
    selected_gpu=$(readlink -f "$gpu")
done
if (( found == 0 )); then
    echo 'Keine AMD-GPU mit amdgpu-Treiber und lesbarer OverDrive-Schnittstelle gefunden.' >&2
    echo 'In der Ubuntu-VM ausführen; AMDGPU-Kernelparameter prüfen. Nichts installiert.' >&2
    exit 1
fi
if (( found > 1 )); then
    echo 'Mehrere passende AMD-GPUs gefunden. Eine eindeutige PCI-Auswahl ist erforderlich.' >&2
    echo 'Nichts installiert.' >&2
    exit 1
fi
pci_address=$(basename "$selected_gpu")
if [[ ! $pci_address =~ ^[[:xdigit:]]{4}:[[:xdigit:]]{2}:[[:xdigit:]]{2}\.[0-7]$ ]]; then
    echo "Unerwartete PCI-Adresse: $pci_address. Nichts installiert." >&2
    exit 1
fi
echo "Ausgewählte AMD-GPU: $pci_address"

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
        echo "Spannungsoffset -80 mV auf $gpu angewendet."
        cat "$gpu/pp_od_clk_voltage"
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 1
done

echo "AMD-GPU $PCI_ADDRESS mit OverDrive-Schnittstelle nicht verfügbar." >&2
exit 1
SCRIPT
chmod 755 /usr/local/sbin/gpu-undervolt

cat > /etc/systemd/system/gpu-undervolt.service <<'UNIT'
[Unit]
Description=GPU-Spannungsoffset auf -80 mV setzen
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
# Auch bei einer erneuten Installation die Werte erneut anwenden.
if ! systemctl restart gpu-undervolt.service; then
    systemctl disable gpu-undervolt.service >/dev/null 2>&1 || true
    journalctl -u gpu-undervolt.service -b -n 30 --no-pager || true
    echo 'Anwenden fehlgeschlagen. Automatischer Start wurde nicht aktiviert bzw. deaktiviert.' >&2
    exit 1
fi
systemctl enable gpu-undervolt.service

echo
echo 'Fertig: -80 mV angewendet und für jeden Systemstart eingerichtet.'
echo 'Der vorhandene Kernelparameter bleibt unverändert.'
echo 'Protokoll: journalctl -u gpu-undervolt.service -b --no-pager'
echo 'Autostart abschalten: sudo systemctl disable gpu-undervolt.service'
echo 'Danach neu starten, um den aktuell gesetzten Offset zurückzusetzen.'
