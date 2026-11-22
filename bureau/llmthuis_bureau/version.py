"""De versie van de app, uit version.txt.

version.txt is de enige plek waar het versienummer staat: het bouwscript, de
workflow en de bijwerking lezen allemaal dit bestand, zodat ze niet uit elkaar
kunnen lopen.
"""

from __future__ import annotations

from pathlib import Path


def _lees_versie() -> str:
    kandidaten = [
        Path(__file__).resolve().parent.parent / "version.txt",  # naast de broncode
        Path(getattr(__import__("sys"), "_MEIPASS", "")) / "version.txt",  # in het pakket
        Path(__file__).resolve().parent / "version.txt",
    ]
    for pad in kandidaten:
        try:
            tekst = pad.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if tekst:
            return tekst
    return "0.0.0"


VERSIE = _lees_versie()
