#!/usr/bin/env python3
"""Schrijft version.json bij een gebouwd pakket.

De app haalt dit bestand op bij zijn bijwerkcontrole:
https://github.com/<repo>/releases/latest/download/version.json
Het bevat de versie, de bestandsnaam van het pakket en de vingerafdruk (sha256),
zodat de app nooit iets uitpakt wat niet heel is binnengekomen.

Gebruik: maak-manifest.py <pakket.zip> [<los-exe-bestand>] [<doel-version.json>]
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

HIER = Path(__file__).resolve().parent
BUREAU = HIER.parent
REPO = BUREAU.parent


def sha256(pad: Path) -> str:
    hash_ = hashlib.sha256()
    with open(pad, "rb") as bestand:
        for brok in iter(lambda: bestand.read(1 << 20), b""):
            hash_.update(brok)
    return hash_.hexdigest()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    pakket = Path(sys.argv[1]).resolve()
    los = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None
    doel = Path(sys.argv[3]).resolve() if len(sys.argv) > 3 else Path("version.json").resolve()

    if not pakket.exists():
        print(f"Pakket niet gevonden: {pakket}")
        return 1
    versie = (BUREAU / "version.txt").read_text(encoding="utf-8").strip()
    if not versie:
        print("bureau/version.txt is leeg")
        return 1

    opslag = os.environ.get("GITHUB_REPOSITORY") or "Theberas04/llm-thuis"
    gegevens = {
        "versie": versie,
        "bestand": pakket.name,
        "sha256": sha256(pakket),
        "grootte": pakket.stat().st_size,
        "url": f"https://github.com/{opslag}/releases/latest/download/{pakket.name}",
    }
    if los and los.exists():
        gegevens["los_bestand"] = los.name
        gegevens["los_sha256"] = sha256(los)
        gegevens["los_grootte"] = los.stat().st_size

    doel.write_text(json.dumps(gegevens, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"version.json geschreven voor versie {versie}: {pakket.name} "
          f"({gegevens['grootte']} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
