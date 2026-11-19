# Installatiekit (Proxmox)

Deze map zet LLM-thuis in één regel op je Proxmox-host: een container met de
gateway (LiteLLM), PostgreSQL voor sleutels en verbruik, en het bord
(statuspagina + sleutelbeheer) op poort 80.

## De one-liner

Draai dit als root in de shell van de Proxmox-host:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Theberas04/llm-thuis/main/deploy/proxmox/install.sh)"
```

Eerst alleen kijken wat er zou gebeuren, zonder iets aan te maken:

```bash
DROOG=1 bash -c "$(curl -fsSL https://raw.githubusercontent.com/Theberas04/llm-thuis/main/deploy/proxmox/install.sh)"
```

## Bijsturen

Alles mag vóór het commando:

```bash
CORES=4 MEMORY=4096 DISK=30 \
STATIC_IP=192.168.1.100/24 GATEWAY=192.168.1.1 \
EIGEN_LLM=http://192.168.1.235:11434 \
OPENROUTER_API_KEY=sk-or-... \
BEHEERDER_WACHTWOORD=kies-je-eigen \
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Theberas04/llm-thuis/main/deploy/proxmox/install.sh)"
```

| variabele | standaard | wat het doet |
|---|---|---|
| `CTID` | eerste vrije nummer vanaf 100 | nummer van de container |
| `CT_HOSTNAME` | `llm-thuis` | hostnaam in de container |
| `CORES` / `MEMORY` / `DISK` | `4` / `4096` MB / `30` GB | rekenkracht, geheugen, schijf |
| `STATIC_IP` + `GATEWAY` | dhcp | vast adres, bijvoorbeeld `192.168.1.100/24` |
| `BRIDGE` | `vmbr0` | netwerkbrug van de host |
| `EIGEN_LLM` | `http://192.168.1.235:11434` | je eigen LLM (Ollama- of OpenAI-stijl) |
| `OPENROUTER_API_KEY` | leeg | zet er ook cloudmodellen bij |
| `BEHEERDER_WACHTWOORD` | wordt gegenereerd | wachtwoord voor sleutelbeheer |
| `ROOTFS_STORAGE` / `TEMPLATE_STORAGE` | automatisch | dwing een opslag af |
| `THUIS_KIT_DIR` | `/opt/llm-thuis-kit` | waar de kit blijft staan |
| `THUIS_BASIS_URL` | leeg | adres dat het bord toont, bijvoorbeeld achter een eigen naam |
| `GH_TOKEN` | leeg | alleen nodig als de repo privé staat |

De kit wordt ook opnieuw bruikbaar gehouden: `check-llm-thuis.sh`,
`update-from-github.sh` en `diagnose-llm-thuis.sh` staan na de installatie in
`/opt/llm-thuis-kit/deploy/proxmox/`.

## Wat er in de container staat

| onderdeel | waar | poort |
|---|---|---|
| gateway (LiteLLM, OpenAI-compatibel) | `127.0.0.1:4000` | 4000 |
| bord (statuspagina, testknop, sleutels) | `0.0.0.0:80` | 80 |
| dashboard van LiteLLM | `127.0.0.1:4000/ui` | 4000 |
| PostgreSQL | `127.0.0.1:5432` | 5432 |

Bestanden: `/opt/llm-thuis` (programma's en `config.yaml`), `/etc/llm-thuis`
(`gateway.env`, `bord.env` — alleen root leesbaar), `/var/lib/llm-thuis` (huis van
de dienstgebruiker `thuis`; hier komt ook de Node-runtime van Prisma terecht).

Diensten: `llm-thuis-gateway.service` en `llm-thuis-bord.service`.

## Later

```bash
# vanaf de Proxmox-host
pct exec <id> -- bash /opt/llm-thuis/deploy/proxmox/check-llm-thuis.sh     # controleert alles per punt
pct exec <id> -- bash /opt/llm-thuis/deploy/proxmox/update-from-github.sh  # nieuwe versie ophalen
pct exec <id> -- cat /root/llm-thuis-login.txt                             # wachtwoord + moedersleutel
```

Start de container niet, of antwoordt hij niet op het netwerk? Draai op de host:

```bash
CTID=<id> bash -c "$(curl -fsSL https://raw.githubusercontent.com/Theberas04/llm-thuis/main/deploy/proxmox/diagnose-llm-thuis.sh)"
```

## Als er iets misgaat

| verschijnsel | oorzaak | oplossing |
|---|---|---|
| `Failed to exec "/sbin/init"` bij `pct start` | rootfs niet uitgepakt (opslag) | `pct destroy <id>` en opnieuw met `ROOTFS_STORAGE=<naam>` |
| de gateway start traag (minuten) | de eerste start haalt Node, de Prisma-CLI en 230+ databasemigraties op | wachten; het controlescript laat zien of hij al luistert |
| `sleutelbeheer uit` op het bord | PostgreSQL deed het niet | `systemctl status postgresql` en daarna `bash /opt/llm-thuis/deploy/proxmox/provision-llm-thuis.sh` |
| poort 80 antwoordt met andermans pagina | een andere webserver houdt de poort | `systemctl status nginx apache2` — LLM-thuis zet er zelf geen neer |
| eigen model valt weg | je eigen LLM is uit of onbereikbaar | `curl <api_base>/models`; het bord en de cloudmodellen blijven werken |

De opzet is bewust zonder Docker, zonder nginx en zonder npm: de gateway is een
Python-programma in zijn eigen omgeving, het bord is de standaardbibliotheek van
Python, en sleutels en verbruik staan in PostgreSQL.
