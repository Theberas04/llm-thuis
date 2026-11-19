#!/usr/bin/env python3
"""Controleert de installatiekit op de fouten die je pas op andermans host ziet.

De installatie bestaat uit twee stappen die elkaar nodig hebben:
  1. install.sh haalt de tarball op en controleert een lijst bestanden;
  2. create-llm-thuis-lxc.sh pusht bestanden de container in en voert scripts uit.

Een bestand dat in geen enkele lijst staat (maar wel wordt uitgevoerd), of een
pad dat niet bestaat, geeft een installatie die pas halverwege stukloopt. Dit
script toetst dat tegen de echte repo:

  - elke `pct push`-bron bestaat in de repo;
  - elk script dat met `bash "$KIT/..."` of `bash "$SCRIPT_DIR/..."` wordt
    uitgevoerd, bestaat in de repo en staat ook in de compleetheidlijst van
    install.sh;
  - geen bestandsnaam met een spatie (die lijmt twee namen tot één pad).

Draaien:  python3 scripts/check-kit.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy" / "proxmox"


def lees(pad: Path) -> str:
    return pad.read_text(encoding="utf-8")


def main() -> int:
    problemen: list[str] = []
    gecontroleerd = 0

    install = lees(DEPLOY / "install.sh")
    create = lees(DEPLOY / "create-llm-thuis-lxc.sh")

    # 1. Compleetheidlijst van install.sh
    blok = re.search(r"for f in (.*?); do\s*\n\s*\[ -f \"\$KIT/\$f\" \]", install, re.S)
    if not blok:
        problemen.append("install.sh: de compleetheidlijst (for f in ...) niet gevonden")
        lijst_bestanden: list[str] = []
    else:
        lijst_bestanden = blok.group(1).replace("\\", " ").split()
        for naam in lijst_bestanden:
            gecontroleerd += 1
            if not (REPO / naam).is_file():
                problemen.append(f"install.sh noemt {naam}, maar dat bestaat niet in de repo")

    # 2. pct push-bronnen in create-llm-thuis-lxc.sh
    for bron in re.findall(r'pct push "\$CTID"\s+"([^"]+)"', create):
        gecontroleerd += 1
        schoon = bron.replace("$KIT_DIR/", "").replace("$SCRIPT_DIR/", "deploy/proxmox/")
        if "$" in schoon:
            problemen.append(f"create: pad met een variabele die ik niet kan narekenen: {bron}")
            continue
        if " " in schoon:
            problemen.append(f"create: bestandsnaam met een spatie (lijmt paden): {schoon}")
            continue
        if not (REPO / schoon).is_file():
            problemen.append(f"create pusht {schoon}, maar dat bestaat niet in de repo")

    # 3. Uitgevoerde scripts moeten bestaan EN in de compleetheidlijst staan
    for script in re.findall(r'(?:exec )?bash "\$KIT/([\w./-]+)"', install):
        gecontroleerd += 1
        if not (REPO / script).is_file():
            problemen.append(f"install.sh voert {script} uit, maar dat bestaat niet")
        elif script not in lijst_bestanden and script.startswith("deploy/"):
            problemen.append(f"install.sh voert {script} uit, maar het staat niet in de compleetheidlijst")
    for script in re.findall(r'bash "\$(?:KIT_DIR)/?([\w./-]+)"', create):
        gecontroleerd += 1
        if not (REPO / script).is_file():
            problemen.append(f"create voert {script} uit, maar dat bestaat niet")

    # 4. Elk script moet zelf de container in (via push) of in de kit staan:
    #    scripts die create uitvoert, moeten ook gepusht worden.
    gepusht = {p.replace("$KIT_DIR/", "").replace("$SCRIPT_DIR/", "deploy/proxmox/")
               for p in re.findall(r'pct push "\$CTID"\s+"([^"]+)"', create)}
    if "deploy/proxmox/provision-llm-thuis.sh" not in gepusht:
        problemen.append("create pusht het inrichtscript niet de container in")
    if "deploy/proxmox/check-llm-thuis.sh" not in gepusht:
        problemen.append("create pusht het controlescript niet de container in")
    for naam in ("bord/bord.py", "assets/merk.png"):
        if naam not in gepusht:
            problemen.append(f"create pusht {naam} niet de container in")

    print(f"{gecontroleerd} paden gecontroleerd in de kit.")
    if problemen:
        print("\nProblemen:")
        for p in problemen:
            print(f"  - {p}")
        return 1
    print("Alles wat de kit ophaalt, pusht en uitvoert bestaat en staat in de lijsten.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
