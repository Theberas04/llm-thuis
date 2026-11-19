#!/usr/bin/env bash
# LLM-thuis — installatie op een Proxmox VE-host.
#
# Draai dit als root in de shell van de Proxmox-host, in één regel:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/Theberas04/llm-thuis/main/deploy/proxmox/install.sh)"
#
# Wat het doet: de kit ophalen naar /opt/llm-thuis-kit, uitpakken, en daarna
# create-llm-thuis-lxc.sh draaien. De kit blijft staan, zodat je check-llm-thuis.sh
# en diagnose-llm-thuis.sh bij de hand hebt.
#
# Bij te sturen met omgevingsvariabelen, bijvoorbeeld:
#
#   EIGEN_LLM=http://192.168.1.235:11434 CORES=4 MEMORY=4096 DISK=30 \
#   STATIC_IP=192.168.1.100/24 GATEWAY=192.168.1.1 \
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/Theberas04/llm-thuis/main/deploy/proxmox/install.sh)"
#
# Alleen kijken wat er zou gebeuren, zonder iets aan te maken:
#   DROOG=1 bash -c "$(curl -fsSL .../install.sh)"
set -euo pipefail

REPO="${THUIS_REPO:-Theberas04/llm-thuis}"
TAK="${THUIS_TAK:-main}"
KIT="${THUIS_KIT_DIR:-/opt/llm-thuis-kit}"
DROOG="${DROOG:-0}"

log(){ printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn(){ printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die(){ printf '\033[1;31m[fout]\033[0m %s\n' "$*" >&2; exit 1; }

# Versiestempel: het meegeplakte uitschrift zegt zelf welke versie er liep.
log "LLM-thuis installer (kit uit $REPO/$TAK)"

[ "$(id -u)" -eq 0 ] || die "Draai dit als root op de Proxmox-host."
command -v pct >/dev/null || die "pct niet gevonden: dit is geen Proxmox VE-host."
command -v pveam >/dev/null || die "pveam niet gevonden: dit is geen Proxmox VE-host."
command -v curl >/dev/null || die "curl ontbreekt. Installeer het met: apt-get install -y curl"
command -v tar >/dev/null || die "tar ontbreekt."

# Een token is alleen nodig als de repo privé staat; de waarde wordt nooit geprint.
TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
URL="https://codeload.github.com/$REPO/tar.gz/refs/heads/$TAK?cb=$(date +%s)"

if [ -e "$KIT" ]; then
  if [ -f "$KIT/deploy/proxmox/create-llm-thuis-lxc.sh" ]; then
    log "Oude kit in $KIT opruimen…"
    rm -rf "$KIT"
  else
    die "$KIT bestaat al en is niet de kit van deze app. Verplaats die map of geef een andere plek: THUIS_KIT_DIR=/pad/naar/kit"
  fi
fi
mkdir -p "$KIT"

log "Kit ophalen van $REPO ($TAK)…"
TARBALL="$(mktemp -t thuis-kit.XXXXXXXX.tar.gz)"
trap 'rm -f "$TARBALL"' EXIT
if [ -n "$TOKEN" ]; then
  curl -fsSL --retry 3 -H "Authorization: Bearer $TOKEN" "$URL" -o "$TARBALL" \
    || die "Kon de kit niet ophalen. Klopt het token nog?"
else
  curl -fsSL --retry 3 "$URL" -o "$TARBALL" \
    || die "Kon de kit niet ophalen van $URL.
Staat de repo op privé? Zet dan een token ervoor: GH_TOKEN=... bash -c \"\$(curl ...)\""
fi

tar -xzf "$TARBALL" -C "$KIT" --strip-components=1

# De kit is pas compleet als elk script dat we straks UITVOEREN er ook staat:
# een bestand dat in geen enkele lijst voorkomt is precies de fout die je pas
# ziet als het te laat is.
for f in deploy/proxmox/create-llm-thuis-lxc.sh \
         deploy/proxmox/provision-llm-thuis.sh \
         deploy/proxmox/check-llm-thuis.sh \
         bord/bord.py \
         assets/merk.png; do
  [ -f "$KIT/$f" ] || die "De kit is onvolledig: $f ontbreekt in de tarball van $REPO/$TAK."
done
log "Kit compleet in $KIT"

if [ "$DROOG" = "1" ]; then
  log "DROOG=1: ik maak niets aan. Dit zou er gebeuren:"
  echo "  - Debian 13-template zoeken en een unprivileged LXC aanmaken"
  echo "    (cores ${CORES:-4}, geheugen ${MEMORY:-4096} MB, schijf ${DISK:-30} GB)"
  echo "  - Daarin: Python-omgeving met de gateway (LiteLLM), PostgreSQL voor"
  echo "    sleutels en verbruik, het bord (statuspagina) op poort 80, systemd-diensten"
  echo "  - Eigen model: ${EIGEN_LLM:-http://192.168.1.235:11434} (als die bereikbaar is)"
  echo "  - Aan het eind: het adres van het bord + waar je wachtwoord en moedersleutel staan"
  echo
  echo "Draai het echt met: bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/$REPO/$TAK/deploy/proxmox/install.sh)\""
  exit 0
fi

log "LXC aanmaken en inrichten…"
exec bash "$KIT/deploy/proxmox/create-llm-thuis-lxc.sh"
