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
    if grep -q 'R9700' "$gpu/vbios_version" 2>/dev/null; then
        if [[ ! -s "$gpu/pp_od_clk_voltage" ]] ||
           ! grep -q '[^[:space:]]' "$gpu/pp_od_clk_voltage"; then
            echo "OverDrive ist für $gpu nicht verfügbar oder nicht freigeschaltet." >&2
            echo 'AMDGPU-Kernelparameter prüfen und gegebenenfalls neu starten.' >&2
            exit 1
        fi
        found=1
    fi
done
if (( found == 0 )); then
    echo 'Keine GPU mit R9700 in der VBIOS-Version gefunden. Nichts installiert.' >&2
    exit 1
fi

install -d -m 755 /usr/local/sbin /etc/systemd/system
cat > /usr/local/sbin/gpu-undervolt <<'SCRIPT'
#!/bin/sh
set -eu

attempt=0
while [ "$attempt" -lt 30 ]; do
    for gpu in /sys/class/drm/card[0-9]*/device; do
        if ! grep -q 'R9700' "$gpu/vbios_version" 2>/dev/null; then
            continue
        fi
        [ -e "$gpu/pp_od_clk_voltage" ] || continue

        printf '%s\n' 'manual' > "$gpu/power_dpm_force_performance_level"
        printf '%s\n' 'vo -80' > "$gpu/pp_od_clk_voltage"
        printf '%s\n' 'c' > "$gpu/pp_od_clk_voltage"
        echo "Spannungsoffset -80 mV auf $gpu angewendet."
        cat "$gpu/pp_od_clk_voltage"
        exit 0
    done
    attempt=$((attempt + 1))
    sleep 1
done

echo 'Keine passende R9700 mit OverDrive-Schnittstelle gefunden.' >&2
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
