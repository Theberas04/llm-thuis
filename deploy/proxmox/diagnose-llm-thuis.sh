#!/usr/bin/env bash
# Diagnose als de container NIET start of niet bereikbaar is. Draai dit op de
# Proxmox-HOST (als root):
#
#   CTID=<id> bash -c "$(curl -fsSL https://raw.githubusercontent.com/Theberas04/llm-thuis/main/deploy/proxmox/diagnose-llm-thuis.sh)"
#
# Zonder CTID zoekt het script de container op zijn hostnaam (llm-thuis).
set -uo pipefail

CTID="${CTID:-}"
HOSTNAME_CT="${CT_HOSTNAME:-llm-thuis}"
APP_DIR="/opt/llm-thuis"

kop(){ printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
regel(){ printf '  %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "Draai dit als root op de Proxmox-host." >&2; exit 1; }
command -v pct >/dev/null || { echo "pct niet gevonden: dit is geen Proxmox VE-host." >&2; exit 1; }

if [ -z "$CTID" ]; then
  CTID="$(pct list 2>/dev/null | awk -v h="$HOSTNAME_CT" '$3==h {print $1; exit}')"
fi
if [ -z "$CTID" ]; then
  kop "Geen container gevonden"
  regel "Ik zocht een container met hostnaam '$HOSTNAME_CT'. Dit draait er:"
  pct list 2>/dev/null | sed 's/^/    /'
  regel "Geef het nummer mee: CTID=<id> bash -c \"\$(curl -fsSL .../diagnose-llm-thuis.sh)\""
  exit 1
fi

kop "Container $CTID"
pct status "$CTID" 2>&1 | sed 's/^/  /'
pct config "$CTID" 2>/dev/null | grep -E '^(hostname|cores|memory|swap|rootfs|net0|onboot|unprivileged|features)' | sed 's/^/  /'

kop "Host"
regel "Proxmox: $(pveversion 2>/dev/null | head -1)"
regel "Kernel:  $(uname -r)"
regel "Opslag:"
pvesm status --content rootdir 2>/dev/null | sed 's/^/    /'
regel "Plaats op de host:"
df -h /var/lib/lxc 2>/dev/null | sed 's/^/    /'

TOESTAND="$(pct status "$CTID" 2>/dev/null | awk '{print $2}')"
if [ "$TOESTAND" != "running" ]; then
  kop "Starten met uitleg"
  pct start "$CTID" --debug 2>&1 | tail -30 | sed 's/^/    /'
  LOG="/tmp/llm-thuis-start-$CTID.log"
  pct start "$CTID" --debug > "$LOG" 2>&1 || true
  regel "Volledig log bewaard op $LOG"
  kop "Wat je hieraan kunt doen"
  if grep -q 'Failed to exec "/sbin/init"' "$LOG" 2>/dev/null || grep -q 'unexpected ostype' "$LOG" 2>/dev/null; then
    regel "De rootfs is leeg of niet uitgepakt (meestal de opslag)."
    regel "  pct destroy $CTID   en daarna opnieuw installeren met een andere opslag:"
    regel "  ROOTFS_STORAGE=<naam> bash -c \"\$(curl -fsSL .../install.sh)\""
  elif grep -q 'symlink encountered' "$LOG" 2>/dev/null; then
    regel "De opslag is een symlink (iSCSI/LVM op Proxmox 9). Kies een andere opslag voor de schijf."
  elif grep -q 'Failed to clone a new set of namespaces' "$LOG" 2>/dev/null \
    || grep -q 'Failed to mount "proc"' "$LOG" 2>/dev/null; then
    regel "De host houdt alle containers tegen (kernel- of lxc-update):"
    regel "  apt-get install -y --reinstall lxc-pve apparmor && systemctl restart apparmor"
  elif grep -q 'Failed to set up id mapping' "$LOG" 2>/dev/null; then
    regel "Zet 'root:100000:65536' in /etc/subuid en /etc/subgid op de host."
  else
    regel "Onbekend patroon. Stuur de regels hierboven door."
  fi
  exit 1
fi

kop "Binnen de container"
binnen(){ pct exec "$CTID" -- bash -lc "$1" 2>&1 | sed 's/^/  /'; }
regel "Adressen:"; binnen 'ip -4 -o addr | awk "{print \"    \" \$2 \" \" \$4}"'
regel "Standaardroute:"; binnen 'ip route | head -2'
regel "DNS:"; binnen 'getent hosts deb.debian.org || echo "    DNS lost niet op"'
regel "Diensten:"
binnen 'systemctl is-active llm-thuis-gateway llm-thuis-bord postgresql 2>/dev/null | tr "\n" " "'
regel "Luistert er iets?"
binnen 'ss -ltn | grep -E ":(80|4000) " || echo "    niets op 80 of 4000"'
regel "Journaal gateway (laatste 15):"
binnen 'journalctl -u llm-thuis-gateway -n 15 --no-pager | tail -15'
regel "Journaal bord (laatste 10):"
binnen 'journalctl -u llm-thuis-bord -n 10 --no-pager | tail -10'
regel "Geheugen:"; binnen 'free -m | head -2'
regel "Schijf:"; binnen 'df -h / | tail -1'

kop "Bereikbaar vanaf de host?"
IP="$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')"
if [ -n "$IP" ]; then
  regel "Adres: $IP"
  if timeout 3 bash -c "echo > /dev/tcp/$IP/80" 2>/dev/null; then
    regel "Poort 80 antwoordt (TCP)."
  else
    regel "Poort 80 antwoordt NIET. Draai in de container het controlescript:"
    regel "  pct exec $CTID -- bash $APP_DIR/deploy/proxmox/check-llm-thuis.sh"
  fi
else
  regel "De container heeft geen IP-adres. Controleer de netwerkregels hierboven."
fi

kop "Samengevat"
regel "Gaat het om een dienst die niet draait: pct exec $CTID -- bash $APP_DIR/deploy/proxmox/check-llm-thuis.sh"
regel "Stuur de [fout]-regels van dat script door; daar staat per punt de oplossing bij."
