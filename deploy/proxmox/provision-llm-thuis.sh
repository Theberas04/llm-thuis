#!/usr/bin/env bash
# Richt LLM-thuis in binnen de container. Draait als root IN de container:
#   bash /opt/llm-thuis/deploy/proxmox/provision-llm-thuis.sh
#
# Wat het neerzet:
#   - de gateway (LiteLLM) in een eigen Python-omgeving, op 127.0.0.1:4000;
#   - PostgreSQL voor sleutels, budgetten en verbruik;
#   - het bord (statuspagina + sleutelbeheer) op poort 80;
#   - twee systemd-diensten en een modelbestand met jouw eigen LLM erin.
#
# Bij te sturen: EIGEN_LLM, OPENROUTER_API_KEY, BEHEERDER_WACHTWOORD.
set -euo pipefail

APP_DIR="/opt/llm-thuis"
ETC_DIR="/etc/llm-thuis"
VAR_DIR="/var/lib/llm-thuis"
GEBRUIKER="thuis"
EIGEN_LLM="${EIGEN_LLM:-http://192.168.1.235:11434}"
OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}"
BEHEERDER_WACHTWOORD="${BEHEERDER_WACHTWOORD:-}"
GATEWAY_POORT=4000
BORD_POORT=80

log(){ printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn(){ printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die(){ printf '\033[1;31m[fout]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Draai dit als root in de container."
[ -f "$APP_DIR/bord/bord.py" ] || die "$APP_DIR/bord/bord.py ontbreekt; de kit is incompleet."

# --------------------------------------------------------------------------- #
# 1. Pakketten
# --------------------------------------------------------------------------- #
log "Pakketten installeren (python3-venv, postgresql, curl)…"
export DEBIAN_FRONTEND=noninteractive
# De Proxmox-repo kan 401 geven zonder abonnement; dat mag de app niet tegenhouden,
# dus niet op de exitcode van apt vertrouwen maar op wat er daarna aanwezig is.
apt-get update -qq 2>/dev/null || true
apt-get install -y -qq python3-venv python3-pip postgresql curl ca-certificates >/dev/null 2>&1 || true
command -v python3 >/dev/null || die "python3 ontbreekt na de pakketinstallatie. Probeer: apt-get install -y python3"
command -v pg_ctlcluster >/dev/null || warn "PostgreSQL lijkt niet geïnstalleerd; sleutelbeheer kan straks uitvallen."

# --------------------------------------------------------------------------- #
# 2. Gebruiker en mappen
# --------------------------------------------------------------------------- #
id -u "$GEBRUIKER" >/dev/null 2>&1 || useradd --system --home "$VAR_DIR" --shell /usr/sbin/nologin "$GEBRUIKER"
mkdir -p "$APP_DIR/bord" "$APP_DIR/assets" "$APP_DIR/deploy/proxmox" "$ETC_DIR" "$VAR_DIR"
chown -R "$GEBRUIKER:$GEBRUIKER" "$VAR_DIR"

# --------------------------------------------------------------------------- #
# 3. Sleutels en wachtwoord
# --------------------------------------------------------------------------- #
log "Sleutels maken…"
MASTERSLEUTEL="$(python3 -c 'import secrets; print("sk-thuis-" + secrets.token_urlsafe(24))')"
if [ -z "$BEHEERDER_WACHTWOORD" ]; then
  BEHEERDER_WACHTWOORD="$(python3 -c 'import secrets,string; alf=string.ascii_lowercase+string.digits; print("".join(secrets.choice(alf) for _ in range(10))+"-"+str(secrets.randbelow(90)+10))')"
fi
DB_WACHTWOORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
AFDRUK="$(python3 "$APP_DIR/bord/bord.py" --afdruk "$BEHEERDER_WACHTWOORD")"

# --------------------------------------------------------------------------- #
# 4. PostgreSQL
# --------------------------------------------------------------------------- #
DB_AAN=0
if command -v pg_ctlcluster >/dev/null; then
  log "PostgreSQL starten en de database inrichten…"
  if systemctl enable --now postgresql >/dev/null 2>&1 || service postgresql start >/dev/null 2>&1; then
    for i in $(seq 1 30); do
      runuser -u postgres -- psql -tAc "SELECT 1" >/dev/null 2>&1 && break
      if [ "$i" -eq 30 ]; then warn "PostgreSQL antwoordt niet; ik ga verder zonder sleutelbeheer."; fi
      sleep 1
    done
    if runuser -u postgres -- psql -tAc "SELECT 1" >/dev/null 2>&1; then
      runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$GEBRUIKER'" 2>/dev/null | grep -q 1 \
        || runuser -u postgres -- psql -qc "CREATE ROLE $GEBRUIKER LOGIN" >/dev/null
      # Wachtwoord altijd zetten: bij een hernieuwde inrichting moet het kloppen
      # met wat er in gateway.env staat.
      runuser -u postgres -- psql -qc "ALTER ROLE $GEBRUIKER WITH LOGIN PASSWORD '$DB_WACHTWOORD'" >/dev/null
      runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname='$GEBRUIKER'" 2>/dev/null | grep -q 1 \
        || runuser -u postgres -- createdb -O "$GEBRUIKER" "$GEBRUIKER" >/dev/null
      DB_AAN=1
    fi
  else
    warn "PostgreSQL startte niet; ik ga verder zonder sleutelbeheer."
  fi
fi

# --------------------------------------------------------------------------- #
# 5. Eigen model zoeken
# --------------------------------------------------------------------------- #
log "Kijken of je eigen LLM bereikbaar is op $EIGEN_LLM…"
EIGEN_MODEL=""
EIGEN_BASIS="${EIGEN_LLM%/}"
case "$EIGEN_BASIS" in
  */v1) EIGEN_BASIS="${EIGEN_BASIS%/v1}" ;;
esac
EIGEN_MODEL="$(python3 - "$EIGEN_BASIS" <<'PY' || true
import json, sys, urllib.request
basis = sys.argv[1]
for pad in ("/v1/models", "/api/tags"):
    try:
        with urllib.request.urlopen(basis + pad, timeout=8) as antwoord:
            gegevens = json.loads(antwoord.read().decode("utf-8", "replace"))
    except Exception:
        continue
    lijst = gegevens.get("data") or gegevens.get("models") or []
    for model in lijst:
        naam = str(model.get("id") or model.get("name") or "").strip()
        if naam and "embed" not in naam.lower():
            print(naam)
            raise SystemExit(0)
PY
)"
if [ -n "$EIGEN_MODEL" ]; then
  log "Eigen model gevonden: $EIGEN_MODEL"
else
  warn "Geen eigen LLM gevonden op $EIGEN_BASIS. Ik neem hem wel in de lijst op, zodat je hem later"
  warn "kunt gebruiken zodra hij draait. Zet dan het juiste model in $APP_DIR/config.yaml."
fi

# --------------------------------------------------------------------------- #
# 6. Modelbestand en configuratie
# --------------------------------------------------------------------------- #
log "Configuratie schrijven naar $APP_DIR/config.yaml…"
EIGEN_NAAM_VOOR_CONFIG="${EIGEN_MODEL:-eigen-llm}"
{
  echo "model_list:"
  echo "  - model_name: eigen-llm"
  echo "    litellm_params:"
  echo "      model: openai/$EIGEN_NAAM_VOOR_CONFIG"
  echo "      api_base: $EIGEN_BASIS/v1"
  echo "      api_key: eigen-machine"
  if [ -n "$OPENROUTER_API_KEY" ]; then
    cat <<'CLOUD'
  - model_name: slimme-cloud
    litellm_params:
      model: openrouter/anthropic/claude-sonnet-4.5
      api_key: os.environ/OPENROUTER_API_KEY
  - model_name: snelle-cloud
    litellm_params:
      model: openrouter/openai/gpt-5-mini
      api_key: os.environ/OPENROUTER_API_KEY
  - model_name: goedkope-cloud
    litellm_params:
      model: openrouter/openai/gpt-4o-mini
      api_key: os.environ/OPENROUTER_API_KEY
  - model_name: automatisch
    litellm_params:
      model: openrouter/auto
      api_key: os.environ/OPENROUTER_API_KEY
CLOUD
  fi
  cat <<'STAART'
general_settings:
  master_key: os.environ/THUIS_MASTER_KEY
  ui_access_mode: all

litellm_settings:
  drop_params: true
  request_timeout: 1800
STAART
} > "$APP_DIR/config.yaml"
chown "$GEBRUIKER:$GEBRUIKER" "$APP_DIR/config.yaml"

python3 - "$APP_DIR" "$DB_AAN" "${EIGEN_MODEL:-}" "$OPENROUTER_API_KEY" <<'PY'
import json, sys
app_dir, db_aan, eigen_model, or_key = sys.argv[1:5]
modellen = {
    "eigen-llm": {
        "herkomst": "eigen",
        "omschrijving": "jouw eigen model op je eigen machine" + (f" ({eigen_model})" if eigen_model else " (nog niet bereikbaar)"),
    }
}
if or_key:
    modellen.update({
        "slimme-cloud": {"herkomst": "cloud", "omschrijving": "Claude Sonnet (via OpenRouter)"},
        "snelle-cloud": {"herkomst": "cloud", "omschrijving": "GPT-5 mini (via OpenRouter)"},
        "goedkope-cloud": {"herkomst": "cloud", "omschrijving": "GPT-4o mini (via OpenRouter)"},
        "automatisch": {"herkomst": "cloud", "omschrijving": "OpenRouter kiest zelf het beste model"},
    })
with open(f"{app_dir}/modellen.json", "w", encoding="utf-8") as bestand:
    json.dump(modellen, bestand, ensure_ascii=False, indent=2)
PY

# De omgevingsbestanden van de diensten: alleen root leesbaar. systemd leest ze
# als root en zet de waarden in de omgeving van de dienst, zodat de diensten zelf
# geen sleutelbestand hoeven te kunnen lezen.
umask 077
{
  echo "THUIS_MASTER_KEY=$MASTERSLEUTEL"
  if [ "$DB_AAN" = "1" ]; then
    echo "DATABASE_URL=postgresql://$GEBRUIKER:$DB_WACHTWOORD@127.0.0.1:5432/$GEBRUIKER"
  fi
  if [ -n "$OPENROUTER_API_KEY" ]; then
    echo "OPENROUTER_API_KEY=$OPENROUTER_API_KEY"
  fi
  # De Prisma-CLI haalt bij de eerste keer een eigen Node-runtime op en speelt
  # daarna alle databasemigraties af. Op een kleine machine duurt dat minuten;
  # de standaardlimiet van 600 s is dan te krap en laat een half geinstalleerde
  # schakel achter die zichzelf niet meer herstelt.
  echo "LITELLM_PRISMA_BOOTSTRAP_TIMEOUT=1800"
  echo "LITELLM_PRISMA_MIGRATE_DEPLOY_TIMEOUT=1800"
  echo "LITELLM_PRISMA_COMMAND_TIMEOUT=180"
  echo "LITELLM_MIGRATION_LOCK_TIMEOUT=1800"
} > "$ETC_DIR/gateway.env"

{
  echo "THUIS_MASTER_KEY=$MASTERSLEUTEL"
  echo "THUIS_BEHEER_HASH=$AFDRUK"
  echo "THUIS_GATEWAY=http://127.0.0.1:$GATEWAY_POORT"
  echo "THUIS_MODELLENBESTAND=$APP_DIR/modellen.json"
  echo "THUIS_MERK=$APP_DIR/assets/merk.png"
  echo "THUIS_POORT=$BORD_POORT"
} > "$ETC_DIR/bord.env"
chmod 0400 "$ETC_DIR/gateway.env" "$ETC_DIR/bord.env"
umask 022

# --------------------------------------------------------------------------- #
# 7. Ping toestaan (een unprivileged container mag anders geen ICMP)
# --------------------------------------------------------------------------- #
sysctl -q -w net.ipv4.ping_group_range="0 2147483647" 2>/dev/null || true
printf 'net.ipv4.ping_group_range = 0 2147483647\n' > /etc/sysctl.d/90-llm-thuis-ping.conf

# --------------------------------------------------------------------------- #
# 8. Python-omgeving en de gateway
# --------------------------------------------------------------------------- #
log "Python-omgeving maken (dit duurt een paar minuten)…"
if [ ! -x "$APP_DIR/venv/bin/python3" ]; then
  python3 -m venv "$APP_DIR/venv" || die "Kon geen Python-omgeving maken. Ontbreekt python3-venv?"
fi
"$APP_DIR/venv/bin/python3" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
log "De gateway (LiteLLM) installeren…"
"$APP_DIR/venv/bin/python3" -m pip install -q "litellm[proxy,extra_proxy]" >/dev/null 2>&1 \
  || "$APP_DIR/venv/bin/python3" -m pip install -q "litellm[proxy]" \
  || die "Kon LiteLLM niet installeren. Controleer de internetverbinding van deze container."
"$APP_DIR/venv/bin/python3" -c "import litellm" 2>/dev/null || die "LiteLLM is geïnstalleerd maar niet te importeren."
chown -R "$GEBRUIKER:$GEBRUIKER" "$APP_DIR"

if [ "$DB_AAN" = "1" ]; then
  log "De databaseschakel voorbereiden (eenmalig: Node en de Prisma-CLI ophalen)…"
  if ! runuser -u "$GEBRUIKER" -- env HOME="$VAR_DIR" PATH="$APP_DIR/venv/bin:/usr/local/bin:/usr/bin:/bin" \
       "$APP_DIR/venv/bin/prisma" --version >/dev/null 2>&1; then
    warn "Kon de Prisma-CLI niet voorbereiden; de eerste start van de gateway doet dat zelf en duurt dan een paar minuten."
  fi
fi

# --------------------------------------------------------------------------- #
# 9. Diensten
# --------------------------------------------------------------------------- #
log "Diensten instellen…"
cat > /etc/systemd/system/llm-thuis-gateway.service <<'UNIT'
[Unit]
Description=LLM-thuis gateway (LiteLLM) - een OpenAI-compatibel adres voor al je modellen
After=network-online.target postgresql.service
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
User=thuis
WorkingDirectory=/opt/llm-thuis
EnvironmentFile=/etc/llm-thuis/gateway.env
ExecStart=/opt/llm-thuis/venv/bin/litellm --config /opt/llm-thuis/config.yaml --host 127.0.0.1 --port 4000
Restart=always
RestartSec=5
TimeoutStartSec=1800
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/llm-thuis-bord.service <<'UNIT'
[Unit]
Description=LLM-thuis bord - statuspagina, testknop en sleutelbeheer
After=network-online.target llm-thuis-gateway.service

[Service]
User=thuis
WorkingDirectory=/opt/llm-thuis
EnvironmentFile=/etc/llm-thuis/bord.env
Environment=THUIS_GASTHEER=0.0.0.0
ExecStart=/usr/bin/python3 /opt/llm-thuis/bord/bord.py
# Poort 80 zonder root: alleen de bevoegdheid om een lage poort te binden.
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable llm-thuis-gateway.service llm-thuis-bord.service >/dev/null 2>&1 || true
systemctl restart llm-thuis-gateway.service
systemctl restart llm-thuis-bord.service

# --------------------------------------------------------------------------- #
# 10. Wachten en proeven
# --------------------------------------------------------------------------- #
log "Wachten tot de gateway luistert (de eerste start haalt de databaseschema's binnen)…"
GATEWAY_AAN=0
for i in $(seq 1 90); do
  if curl -fsS -m 3 "http://127.0.0.1:$GATEWAY_POORT/health/liveliness" >/dev/null 2>&1; then GATEWAY_AAN=1; break; fi
  sleep 5
done
if [ "$GATEWAY_AAN" != "1" ]; then
  warn "De gateway antwoordt nog niet. Laatste regels uit het journaal:"
  journalctl -u llm-thuis-gateway.service -n 15 --no-pager 2>/dev/null | sed 's/^/    /' || true
fi

log "Proeven…"
BORD_OK=0
curl -fsS -m 5 "http://127.0.0.1/gezondheid" >/dev/null 2>&1 && BORD_OK=1
MODEL_AANTAL="$(curl -fsS -m 10 -H "Authorization: Bearer $MASTERSLEUTEL" \
  "http://127.0.0.1:$GATEWAY_POORT/v1/models" 2>/dev/null \
  | python3 -c 'import json,sys
try:
    print(len(json.load(sys.stdin).get("data") or []))
except Exception:
    print(0)' || echo 0)"

# --------------------------------------------------------------------------- #
# 11. Rapport
# --------------------------------------------------------------------------- #
umask 077
{
  echo "LLM-thuis is ingericht op $(date)"
  echo
  echo "Bord (statuspagina):        http://$(hostname -I | awk '{print $1}')/"
  echo "Gateway (OpenAI-stijl):     http://$(hostname -I | awk '{print $1}'):80/v1"
  echo "Gateway-dashboard (LiteLLM): http://$(hostname -I | awk '{print $1}'):4000/ui"
  echo
  echo "Beheerderswachtwoord (voor sleutels maken): $BEHEERDER_WACHTWOORD"
  echo "Moedersleutel van de gateway:               $MASTERSLEUTEL"
  echo
  echo "Sleutelbeheer: $([ "$DB_AAN" = "1" ] && echo "aan (database)" || echo "UIT - PostgreSQL deed het niet")"
  echo "Eigen model:   ${EIGEN_MODEL:-niet gevonden op $EIGEN_BASIS}"
  echo "Modellen in de gateway: $MODEL_AANTAL"
  echo "Bord bereikbaar: $([ "$BORD_OK" = "1" ] && echo ja || echo NEE)"
} > /root/llm-thuis-login.txt
chmod 0600 /root/llm-thuis-login.txt
umask 022

printf '\n'
log "Klaar."
echo
echo "  Bord:      http://$(hostname -I | awk '{print $1}')/"
echo "  Gateway:   http://$(hostname -I | awk '{print $1}')/v1   (OpenAI-compatibel)"
echo "  Dashboard: http://$(hostname -I | awk '{print $1}'):4000/ui"
echo
echo "  Beheerderswachtwoord en moedersleutel staan in /root/llm-thuis-login.txt (alleen root)."
echo "  Modellen in de gateway: $MODEL_AANTAL   Bord bereikbaar: $([ "$BORD_OK" = "1" ] && echo ja || echo nee)"
echo "  Sleutelbeheer: $([ "$DB_AAN" = "1" ] && echo aan || echo uit)"
echo
