#!/usr/bin/env bash
# Controleert een draaiende LLM-thuis-installatie en zegt per punt wat je moet
# doen als het misgaat. Draai het IN de container:
#
#   bash /opt/llm-thuis/deploy/proxmox/check-llm-thuis.sh
#
# of vanaf de Proxmox-host:
#
#   CTID=<id> bash -c "$(curl -fsSL https://raw.githubusercontent.com/Theberas04/llm-thuis/main/deploy/proxmox/check-llm-thuis.sh)"
set -uo pipefail

APP_DIR="${THUIS_APP_DIR:-/opt/llm-thuis}"
ETC_DIR="${THUIS_ETC_DIR:-/etc/llm-thuis}"
BORD_POORT="${THUIS_POORT:-80}"
GATEWAY_POORT="${THUIS_GATEWAY_POORT:-4000}"
GOED=0
FOUT=0

kop(){ printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok(){ printf '  \033[1;32m[ok]\033[0m   %s\n' "$*"; GOED=$((GOED+1)); }
mis(){ printf '  \033[1;31m[fout]\033[0m %s\n' "$*"; FOUT=$((FOUT+1)); }
let_op(){ printf '  \033[1;33m[let op]\033[0m %s\n' "$*"; }
raad(){ printf '        -> %s\n' "$*"; }

# Op de Proxmox-host uitgevoerd? Dan eerst de container in (CTID meegegeven).
if ! [ -f "$APP_DIR/bord/bord.py" ] && [ -n "${CTID:-}" ]; then
  if command -v pct >/dev/null 2>&1; then
    exec pct exec "$CTID" -- bash "$APP_DIR/deploy/proxmox/check-llm-thuis.sh"
  fi
  echo "Dit is niet de container (geen $APP_DIR/bord/bord.py) en pct ontbreekt." >&2
  exit 1
fi

kop "Diensten"
for dienst in llm-thuis-gateway llm-thuis-bord; do
  toestand="$(systemctl is-active "$dienst.service" 2>/dev/null || echo onbekend)"
  if [ "$toestand" = "active" ]; then
    ok "$dienst is actief"
  else
    mis "$dienst is '$toestand'"
    raad "systemctl status $dienst --no-pager -l"
    journalctl -u "$dienst.service" -n 12 --no-pager 2>/dev/null | sed 's/^/        /'
  fi
done

kop "Poorten"
if ss -ltn 2>/dev/null | grep -q ":$BORD_POORT "; then
  ok "het bord luistert op poort $BORD_POORT"
else
  mis "niets luistert op poort $BORD_POORT"
  raad "draait het bord wel? systemctl status llm-thuis-bord --no-pager"
  raad "poort 80 binden mag alleen met CAP_NET_BIND_SERVICE (staat in de dienst)."
fi
if ss -ltn 2>/dev/null | grep -q "127.0.0.1:$GATEWAY_POORT "; then
  ok "de gateway luistert op 127.0.0.1:$GATEWAY_POORT"
else
  mis "de gateway luistert niet op 127.0.0.1:$GATEWAY_POORT"
  raad "journalctl -u llm-thuis-gateway -n 30 --no-pager"
fi

kop "Het bord"
PROEF="$(mktemp)"
KOPPEN="$(mktemp)"
if curl -fsS -m 8 "http://127.0.0.1:$BORD_POORT/gezondheid" -D "$KOPPEN" -o "$PROEF" 2>/dev/null; then
  if grep -qi "x-llm-thuis: bord" "$KOPPEN"; then
    ok "gezondheidsroute antwoordt 'ok' met de eigen kopregel"
  else
    let_op "er antwoordt iets op poort $BORD_POORT, maar niet ons bord (geen x-llm-thuis-kopregel)"
    raad "een andere webserver houdt de poort bezet: systemctl status nginx apache2 2>/dev/null"
  fi
else
  mis "de gezondheidsroute van het bord antwoordt niet"
fi
if curl -fsS -m 10 -H 'Accept: text/html' "http://127.0.0.1:$BORD_POORT/" -o "$PROEF" 2>/dev/null; then
  if grep -q "\-\-merk:linear-gradient" "$PROEF" && grep -q "</html>" "$PROEF"; then
    ok "de pagina komt heel uit de server, met de huisstijl erin"
  else
    mis "de pagina mist de huisstijl (plaatshouder niet vervangen?)"
    raad "herstart het bord en kijk of __HUISSTIJL__ uit de HTML verdwenen is"
  fi
else
  mis "de pagina laadt niet"
fi
rm -f "$PROEF" "$KOPPEN"

kop "De gateway"
SLEUTEL=""
if [ -r "$ETC_DIR/gateway.env" ]; then
  SLEUTEL="$(awk -F= '/^THUIS_MASTER_KEY=/{print substr($0, index($0,"=")+1)}' "$ETC_DIR/gateway.env")"
fi
if [ -n "$SLEUTEL" ]; then
  MODELLEN="$(curl -fsS -m 20 -H "Authorization: Bearer $SLEUTEL" \
    "http://127.0.0.1:$GATEWAY_POORT/v1/models" 2>/dev/null || true)"
  if [ -n "$MODELLEN" ]; then
    AANTAL="$(printf '%s' "$MODELLEN" | python3 -c 'import json,sys
try:
    print(len(json.load(sys.stdin).get("data") or []))
except Exception:
    print(0)' 2>/dev/null || echo 0)"
    ok "de gateway geeft $AANTAL model(len) terug op /v1/models"
    [ "$AANTAL" -gt 0 ] || { mis "geen enkel model in de gateway"; raad "kijk in $APP_DIR/config.yaml"; }
  else
    mis "de gateway antwoordt niet op /v1/models"
    raad "journalctl -u llm-thuis-gateway -n 40 --no-pager (de eerste start haalt het databaseschema binnen)"
  fi
else
  let_op "geen moedersleutel gevonden in $ETC_DIR/gateway.env; ik kan de gateway niet toetsen"
fi

kop "Sleutelbeheer (database)"
if [ -r "$ETC_DIR/gateway.env" ] && grep -q '^DATABASE_URL=' "$ETC_DIR/gateway.env"; then
  if [ -n "$SLEUTEL" ] && curl -fsS -m 15 -H "Authorization: Bearer $SLEUTEL" \
       "http://127.0.0.1:$GATEWAY_POORT/key/list" >/dev/null 2>&1; then
    ok "sleutelbeheer werkt: sleutels, budgetten en verbruik worden bijgehouden"
  else
    mis "de gateway draait zonder werkende database: sleutelbeheer staat uit"
    raad "systemctl status postgresql --no-pager"
    raad "draai het inrichtscript opnieuw: bash $APP_DIR/deploy/proxmox/provision-llm-thuis.sh"
  fi
else
  let_op "er is geen DATABASE_URL ingesteld, dus de gateway draait zonder sleutelbeheer"
  raad "installeer PostgreSQL en draai het inrichtscript opnieuw voor virtuele sleutels met budget"
fi

kop "Je eigen model"
EIGEN_BASIS="$(awk '/^ *api_base:/{print $2; exit}' "$APP_DIR/config.yaml" 2>/dev/null || true)"
if [ -n "${EIGEN_BASIS:-}" ]; then
  if curl -fsS -m 8 "${EIGEN_BASIS%/}/models" >/dev/null 2>&1; then
    ok "je eigen LLM antwoordt op $EIGEN_BASIS"
  else
    let_op "je eigen LLM antwoordt niet op $EIGEN_BASIS"
    raad "draait die engine nog? Test met: curl ${EIGEN_BASIS%/}/models"
    raad "het bord en de gateway blijven werken; alleen dat model valt weg."
  fi
else
  let_op "geen api_base gevonden in $APP_DIR/config.yaml"
fi

kop "Netwerk in de container"
ip -4 -o addr show 2>/dev/null | awk '{print "        " $2 " " $4}'
if getent hosts deb.debian.org >/dev/null 2>&1; then ok "DNS werkt"; else mis "DNS lost niet op"; fi

kop "Geheugen en schijf"
free -m 2>/dev/null | sed 's/^/        /'
df -h / 2>/dev/null | tail -1 | sed 's/^/        /'

printf '\n\033[1;36m== Uitslag\033[0m\n'
printf '  %s punt(en) goed, %s fout.\n' "$GOED" "$FOUT"
if [ "$FOUT" -gt 0 ]; then
  printf '  Los de [fout]-regels hierboven op; staan er geen aanwijzingen bij, stuur dan deze uitvoer door.\n'
  exit 1
fi
printf '  Alles wat de installatie hoort te doen, doet het.\n'
