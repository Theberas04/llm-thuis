#!/usr/bin/env bash
# Werkt een draaiende LLM-thuis bij vanaf GitHub (bord + controlescript) en
# herstart wat nodig is. Draai IN de container:
#
#   bash /opt/llm-thuis/deploy/proxmox/update-from-github.sh
#
# Vanaf de Proxmox-host:
#
#   CTID=<id> pct exec <id> -- bash /opt/llm-thuis/deploy/proxmox/update-from-github.sh
#
# De configuratie (/opt/llm-thuis/config.yaml), de sleutels en de database blijven
# staan: die zijn van jou, niet van de kit.
set -euo pipefail
export PATH="$PATH:/usr/sbin:/sbin"

REPO="${THUIS_REPO:-Theberas04/llm-thuis}"
TAK="${THUIS_TAK:-main}"
APP_DIR="${THUIS_APP_DIR:-/opt/llm-thuis}"
STAGING="$(mktemp -d)"
TARBALL="$STAGING/kit.tar.gz"

log(){ printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn(){ printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die(){ printf '\033[1;31m[fout]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Draai dit als root in de container."
[ -d "$APP_DIR" ] || die "$APP_DIR bestaat niet: is LLM-thuis hier wel ingericht?"

log "Nieuwe versie ophalen van $REPO ($TAK)…"
curl -fsSL --retry 3 "https://codeload.github.com/$REPO/tar.gz/refs/heads/$TAK?cb=$(date +%s)" \
  -o "$TARBALL" || die "Kon de nieuwe versie niet ophalen. Is de repo openbaar en is er internet?"
tar -xzf "$TARBALL" -C "$STAGING" --strip-components=1
rm -f "$TARBALL"
[ -f "$STAGING/bord/bord.py" ] || die "De nieuwe versie is incompleet (bord/bord.py ontbreekt)."

# Reservekopie van wat we overschrijven, zodat een mislukte update terug te draaien is.
RESERVE="$(mktemp -d)"
cp -a "$APP_DIR/bord" "$RESERVE/bord"

log "Bestanden bijwerken (config.yaml, sleutels en database blijven staan)…"
install -m 0644 "$STAGING/bord/bord.py" "$APP_DIR/bord/bord.py"
if [ -f "$STAGING/assets/merk.png" ]; then
  install -m 0644 "$STAGING/assets/merk.png" "$APP_DIR/assets/merk.png"
fi
install -m 0755 "$STAGING/deploy/proxmox/provision-llm-thuis.sh" "$APP_DIR/deploy/proxmox/provision-llm-thuis.sh"
install -m 0755 "$STAGING/deploy/proxmox/check-llm-thuis.sh" "$APP_DIR/deploy/proxmox/check-llm-thuis.sh"
install -m 0755 "$STAGING/deploy/proxmox/update-from-github.sh" "$APP_DIR/deploy/proxmox/update-from-github.sh"
[ -f "$APP_DIR/modellen.json" ] || cp -a "$STAGING/modellen.json" "$APP_DIR/modellen.json" 2>/dev/null || true
chown -R thuis:thuis "$APP_DIR/bord" "$APP_DIR/assets" 2>/dev/null || true

log "Controleren of de nieuwe code syntaxfouten heeft…"
if ! python3 -m py_compile "$APP_DIR/bord/bord.py"; then
  warn "De nieuwe bord-code is niet te compileren; ik zet de oude terug."
  cp -a "$RESERVE/bord/." "$APP_DIR/bord/"
  die "Update teruggedraaid. Stuur deze uitvoer door zodat het gerepareerd kan worden."
fi

log "Het bord herstarten…"
systemctl restart llm-thuis-bord.service
for i in $(seq 1 30); do
  if curl -fsS -m 3 "http://127.0.0.1:${THUIS_POORT:-80}/gezondheid" >/dev/null 2>&1; then break; fi
  if [ "$i" -eq 30 ]; then warn "Het bord antwoordt nog niet; kijk met: journalctl -u llm-thuis-bord -n 20 --no-pager"; fi
  sleep 1
done

# De gateway hoeft alleen opnieuw te lezen als de configuratie is aangepast; dat
# doet deze update niet, dus hij blijft draaien (minder onderbreking).
log "Klaar. Laatste controle:"
bash "$APP_DIR/deploy/proxmox/check-llm-thuis.sh" || true
rm -rf "$RESERVE" "$STAGING"
