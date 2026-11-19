#!/usr/bin/env bash
# Maakt een unprivileged Debian-LXC aan en richt daar LLM-thuis in: de gateway
# (LiteLLM), een PostgreSQL-database voor sleutels en verbruik, en het bord
# (statuspagina + sleutelbeheer) op poort 80.
#
# Draai als root op de Proxmox-host:
#   bash create-llm-thuis-lxc.sh
#
# Bij te sturen met omgevingsvariabelen, bijvoorbeeld:
#   CORES=4 MEMORY=4096 DISK=30 STATIC_IP=192.168.1.100/24 GATEWAY=192.168.1.1 \
#   EIGEN_LLM=http://192.168.1.235:11434 bash create-llm-thuis-lxc.sh
set -euo pipefail

CTID="${CTID:-}"
CT_HOSTNAME="${CT_HOSTNAME:-llm-thuis}"
ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-}"
ROOTFS_STORAGE="${ROOTFS_STORAGE:-}"
BRIDGE="${BRIDGE:-vmbr0}"
STATIC_IP="${STATIC_IP:-}"
GATEWAY="${GATEWAY:-}"
DNS_SERVER="${DNS_SERVER:-1.1.1.1}"
# De Python-omgeving van LiteLLM is fors (enkele honderden MB's) en PostgreSQL
# wil ook wat ruimte; 4 cores en 4 GB is een prettige ondergrens.
CORES="${CORES:-4}"
MEMORY="${MEMORY:-4096}"
SWAP="${SWAP:-2048}"
DISK="${DISK:-30}"
DEBIAN_MAJOR="${DEBIAN_MAJOR:-13}"
EIGEN_LLM="${EIGEN_LLM:-http://192.168.1.235:11434}"
OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}"
BEHEERDER_WACHTWOORD="${BEHEERDER_WACHTWOORD:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

log(){ printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn(){ printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die(){ printf '\033[1;31m[fout]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Draai dit als root op de Proxmox-host."
command -v pct >/dev/null || die "pct niet gevonden: dit is geen Proxmox VE-host."
command -v pveam >/dev/null || die "pveam niet gevonden."
[ -f "$SCRIPT_DIR/provision-llm-thuis.sh" ] || die "$SCRIPT_DIR/provision-llm-thuis.sh ontbreekt."
[ -f "$KIT_DIR/bord/bord.py" ] || die "$KIT_DIR/bord/bord.py ontbreekt: de kit is incompleet."
ip link show "$BRIDGE" >/dev/null 2>&1 || die "Bridge '$BRIDGE' bestaat niet. Beschikbaar: $(ip -o link show | awk -F': ' '$2 ~ /^vmbr/ {print $2}' | tr '\n' ' ')"

case "$ARCH" in
  amd64) ARCH_NAAM="x86-64" ;;
  arm64) ARCH_NAAM="arm64" ;;
  *) die "Onbekende architectuur '$ARCH'. Deze kit is er voor amd64 (Intel/AMD) en arm64." ;;
esac
log "Host: $ARCH ($ARCH_NAAM)"

# --- Opslag kiezen -----------------------------------------------------------
# Eerst lokale opslag, en proeven of root er echt mag schrijven: een NAS geeft
# met root_squash 'Permission denied', waarna de template-download en het maken
# van de schijf mislukken.
opslag_kandidaten(){
  pvesm status --content "$1" 2>/dev/null | awk 'NR>1 && $3=="active"{
      t=$2
      p = (t=="lvmthin" || t=="zfspool" || t=="btrfs") ? 1 : (t=="lvm" || t=="dir") ? 2 : 3
      printf "%d %019d %s\n", p, 999999999999999999-$6, $1
    }' | sort | awk '{print $3}'
}
opslag_pad(){
  local s="$1" t pad
  t="$(pvesm status 2>/dev/null | awk -v s="$s" 'NR>1 && $1==s{print $2}')"
  pad="$(awk -v s="$s" '$1 ~ /:$/ {naam=$2} naam==s && $1=="path" {print $2; exit}' \
    /etc/pve/storage.cfg 2>/dev/null)"
  if [ -z "$pad" ]; then
    case "$t" in nfs|cifs|glusterfs) pad="/mnt/pve/$s" ;; esac
  fi
  printf '%s' "$pad"
}
opslag_schrijfbaar(){
  local pad; pad="$(opslag_pad "$1")"
  [ -n "$pad" ] || return 0
  [ -d "$pad" ] || return 1
  local proef="$pad/.thuis-schrijfproef.$$"
  touch "$proef" 2>/dev/null || return 1
  rm -f "$proef"; return 0
}
kies_opslag(){
  local k
  for k in $(opslag_kandidaten "$1"); do
    if opslag_schrijfbaar "$k"; then printf '%s' "$k"; return 0; fi
    warn "Opslag '$k' is niet beschrijfbaar door root (bijvoorbeeld een NAS met root_squash); ik sla die over."
  done
  return 1
}

if [ -z "$ROOTFS_STORAGE" ]; then
  ROOTFS_STORAGE="$(kies_opslag rootdir)" || die "Geen beschrijfbare opslag gevonden voor de schijf van de container.
Kijk zelf met: pvesm status --content rootdir
Wijs er een aan met: ROOTFS_STORAGE=<naam> ..."
fi
if [ -z "$TEMPLATE_STORAGE" ]; then
  TEMPLATE_STORAGE="$(kies_opslag vztmpl)" || TEMPLATE_STORAGE="local"
fi
log "Schijf op '$ROOTFS_STORAGE', template op '$TEMPLATE_STORAGE'"

if [ -n "$STATIC_IP" ]; then
  [ -n "$GATEWAY" ] || die "Geef GATEWAY op bij STATIC_IP."
  NET0="name=eth0,bridge=$BRIDGE,ip=$STATIC_IP,gw=$GATEWAY,type=veth"
else
  NET0="name=eth0,bridge=$BRIDGE,ip=dhcp,type=veth"
fi

if [ -z "$CTID" ]; then CTID="$(pvesh get /cluster/nextid 2>/dev/null || true)"; fi
[ -n "$CTID" ] || die "Kon geen vrij CTID bepalen; geef CTID=... mee."
pct status "$CTID" >/dev/null 2>&1 && die "Container $CTID bestaat al."

# --- Template ----------------------------------------------------------------
log "Debian $DEBIAN_MAJOR-template zoeken…"
pveam update >/dev/null 2>&1 || warn "pveam update mislukte; de lokale lijst wordt gebruikt."
# De lijst bevat templates voor meerdere architecturen; zonder filter pakt
# 'sort -V | tail -1' de arm64-versie en start de container niet.
kies_template(){
  pveam available --section system 2>/dev/null \
    | awk -v m="$DEBIAN_MAJOR" -v a="$ARCH_NAAM" '$2 ~ ("^debian-" m "\\..*" a "\\.tar") {print $2}' \
    | sort -V | tail -1 || true
}
TEMPLATE="$(kies_template)"
if [ -z "$TEMPLATE" ]; then
  warn "Geen template voor Debian $DEBIAN_MAJOR ($ARCH_NAAM) gevonden; ik probeer de nieuwste Debian die de host biedt."
  TEMPLATE="$(pveam available --section system 2>/dev/null \
    | awk -v a="$ARCH_NAAM" '$2 ~ ("^debian-.*" a "\\.tar") {print $2}' | sort -V | tail -1 || true)"
fi
[ -n "$TEMPLATE" ] || die "Geen Debian-template gevonden voor $ARCH_NAAM. Draai 'pveam update' en probeer opnieuw."
case "$TEMPLATE" in *_${ARCH_NAAM}.tar*) : ;; *) die "Template $TEMPLATE hoort niet bij $ARCH_NAAM." ;; esac
if ! pveam list "$TEMPLATE_STORAGE" 2>/dev/null | grep -q "$TEMPLATE"; then
  log "Template $TEMPLATE downloaden naar '$TEMPLATE_STORAGE'…"
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE" >/dev/null || die "Template downloaden mislukte."
fi
log "Template: $TEMPLATE"

# --- Container ---------------------------------------------------------------
log "Container $CTID aanmaken ($CORES cores, ${MEMORY} MB, ${DISK} GB)…"
pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" \
  --hostname "$CT_HOSTNAME" \
  --cores "$CORES" --memory "$MEMORY" --swap "$SWAP" \
  --rootfs "$ROOTFS_STORAGE:$DISK" \
  --net0 "$NET0" \
  --nameserver "$DNS_SERVER" \
  --unprivileged 1 \
  --features nesting=1,keyctl=1 \
  --onboot 1 \
  --start 1 >/dev/null || {
    warn "pct create/start mislukte. Hieronder de uitleg van de laatste poging:"
    pct start "$CTID" --debug 2>&1 | tail -25 | sed 's/^/    /' || true
    die "Container $CTID is aangemaakt maar draait niet. Diagnose (op de host):
  CTID=$CTID bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Theberas04/llm-thuis/main/deploy/proxmox/diagnose-llm-thuis.sh)\"
Weghalen kan met: pct destroy $CTID"
  }

log "Wachten tot de container netwerk heeft…"
for i in $(seq 1 60); do
  if pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1; then break; fi
  [ "$i" -eq 60 ] && die "De container kreeg geen netwerk. Controleer bridge '$BRIDGE' en DNS."
  sleep 2
done

# --- Inrichten ---------------------------------------------------------------
APP_DIR="/opt/llm-thuis"
pct exec "$CTID" -- mkdir -p "$APP_DIR" "$APP_DIR/deploy/proxmox" "$APP_DIR/bord" "$APP_DIR/assets"
pct push "$CTID" "$KIT_DIR/bord/bord.py"            "$APP_DIR/bord/bord.py"
pct push "$CTID" "$KIT_DIR/assets/merk.png"         "$APP_DIR/assets/merk.png"
pct push "$CTID" "$SCRIPT_DIR/provision-llm-thuis.sh" "$APP_DIR/deploy/proxmox/provision-llm-thuis.sh"
pct push "$CTID" "$SCRIPT_DIR/check-llm-thuis.sh"     "$APP_DIR/deploy/proxmox/check-llm-thuis.sh"
pct exec "$CTID" -- chmod 0755 "$APP_DIR/deploy/proxmox/provision-llm-thuis.sh" "$APP_DIR/deploy/proxmox/check-llm-thuis.sh"

PUSH_ENV=()
for v in EIGEN_LLM OPENROUTER_API_KEY BEHEERDER_WACHTWOORD CT_HOSTNAME; do
  eval "w=\${$v:-}"
  [ -n "$w" ] && PUSH_ENV+=("$v=$w")
done

log "Inrichten in container $CTID (de Python-omgeving installeren duurt een paar minuten)…"
pct exec "$CTID" -- env "${PUSH_ENV[@]:-THUIS_LEEG=1}" \
  bash "$APP_DIR/deploy/proxmox/provision-llm-thuis.sh"

IP="$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')"
printf '\n\033[1;32mKlaar.\033[0m\n\n'
printf '  Bord (statuspagina + sleutels):  http://%s/\n' "${IP:-<ip-van-container>}"
printf '  Gateway (OpenAI-compatibel):     http://%s/v1\n' "${IP:-<ip-van-container>}"
printf '  Gateway-dashboard (LiteLLM):     http://%s:4000/ui\n\n' "${IP:-<ip-van-container>}"
printf '  Wachtwoord van de beheerder en de moedersleutel staan in de container:\n'
printf '    pct exec %s -- cat /root/llm-thuis-login.txt\n\n' "$CTID"
printf '  Controleer later met:\n    pct exec %s -- bash %s/deploy/proxmox/check-llm-thuis.sh\n\n' "$CTID" "$APP_DIR"
