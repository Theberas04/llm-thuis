#!/usr/bin/env python3
"""Maakt packaging/merk.ico van assets/merk.png.

Het merk (162x120) wordt op een vierkant doek gecentreerd met de huisstijlkleur als
achtergrond, en daarna als Windows-pictogram weggeschreven in alle maten die Windows
vraagt. Dit draait in de bouwstraat (daar is Pillow) en lokaal; het pictogram wordt
niet in de repo bewaard.
"""

from __future__ import annotations

import sys
from pathlib import Path

HIER = Path(__file__).resolve().parent
BUREAU = HIER.parent
REPO = BUREAU.parent
BRON = REPO / "assets" / "merk.png"
DOEL = BUREAU / "packaging" / "merk.ico"
MATEN = [16, 24, 32, 48, 64, 128, 256]


def main() -> int:
    try:
        from PIL import Image
    except ImportError:
        print("Pillow ontbreekt; het pictogram wordt overgeslagen (de bouw gaat door).")
        return 0
    if not BRON.exists():
        print(f"Geen merk gevonden op {BRON}; pictogram overgeslagen.")
        return 0

    # Pillow ≥ 9.1 heeft de filter in Resampling staan; oudere versies in Image.
    filter_ = getattr(Image, "LANCZOS", None)
    if filter_ is None:  # Pillow >= 9.1
        filter_ = getattr(Image.Resampling, "LANCZOS")

    with Image.open(BRON) as bron:
        merk = bron.convert("RGBA")
    zijde = max(merk.size)
    marge = int(zijde * 0.10)
    doek = Image.new("RGBA", (zijde + 2 * marge, zijde + 2 * marge), (255, 255, 255, 0))
    doek.paste(merk, ((doek.width - merk.width) // 2, (doek.height - merk.height) // 2), merk)
    vierkant = doek.resize((256, 256), filter_)

    DOEL.parent.mkdir(parents=True, exist_ok=True)
    vierkant.save(DOEL, format="ICO", sizes=[(m, m) for m in MATEN])
    print(f"Pictogram geschreven: {DOEL} ({', '.join(str(m) for m in MATEN)} px)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
