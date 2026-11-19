#!/usr/bin/env python3
"""Maakt de schermafbeeldingen voor de README (docs/img/).

Start zelf een Chromium (headless) en stuurt hem via het DevTools-protocol aan.
De pagina wordt bezocht onder een neutrale naam (llm-thuis.lokaal), zodat er geen
echt adres van de gebruiker in een openbare repo belandt.

Gebruik:
    python3 scripts/maak-beelden.py http://llm-thuis.lokaal:8099 WACHTWOORD

De gateway moet draaien (voor de testknop en het sleutelbeheer), en de omgeving van
het bord moet THUIS_BASIS_URL=http://llm-thuis.lokaal/v1/ zetten zodat de pagina het
nette adres toont.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, "/home/hermes/tools")
from cdp import CDP, tabs  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
BEELDEN = REPO / "docs" / "img"
OMGEVING = dict(os.environ)
OMGEVING["LD_LIBRARY_PATH"] = ":".join([
    "/home/hermes/.local/lib",
    os.path.expanduser("~/.local/chromelibs/root/usr/lib/x86_64-linux-gnu"),
    os.path.expanduser("~/.local/chromedeps/root/usr/lib/x86_64-linux-gnu"),
    OMGEVING.get("LD_LIBRARY_PATH", ""),
])
PROFIEL = "/tmp/llm-thuis-beeld-profiel"


def chromium_pad() -> str:
    kandidaten = sorted(pathlib.Path(os.path.expanduser("~/.hermes/tools")).glob("chromium-*/chrome-linux64/chrome"))
    if not kandidaten:
        raise SystemExit("Geen Chromium gevonden onder ~/.hermes/tools/")
    return str(kandidaten[-1])


def start_chromium(breedte=1440, hoogte=900):
    subprocess.run(["rm", "-rf", PROFIEL], check=False)
    proces = subprocess.Popen([
        chromium_pad(), "--headless=new", "--no-sandbox", "--disable-gpu",
        "--disable-dev-shm-usage", "--hide-scrollbars",
        f"--window-size={breedte},{hoogte}",
        "--remote-debugging-port=0", f"--user-data-dir={PROFIEL}",
        "--no-first-run", "--no-default-browser-check",
        '--host-resolver-rules=MAP llm-thuis.lokaal 127.0.0.1',
        "about:blank",
    ], env=OMGEVING, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    poort = None
    for _ in range(60):
        tijdelijk = pathlib.Path(PROFIEL) / "DevToolsActivePort"
        if tijdelijk.exists():
            poort = int(tijdelijk.read_text().splitlines()[0])
            break
        time.sleep(0.3)
    if poort is None:
        proces.kill()
        raise SystemExit("Chromium gaf geen debug-poort; startte hij wel?")
    return proces, poort


def verbind(poort: int) -> CDP:
    for _ in range(40):
        try:
            for tab in tabs(poort):
                if tab.get("type") == "page":
                    return CDP(tab["webSocketDebuggerUrl"])
        except Exception:  # noqa: BLE001 - browser nog niet klaar
            pass
        time.sleep(0.4)
    raise SystemExit("Kon geen tab vinden om mee te werken.")


def hoofdpagina(b: CDP, adres: str, wachttijd: float = 3.0):
    b.call("Page.enable")
    b.call("Runtime.enable")
    b.call("Emulation.setDeviceMetricsOverride", width=1440, height=900,
           deviceScaleFactor=1, mobile=False)
    b.call("Page.navigate", {"url": adres})
    b.call("Network.setCacheDisabled", {"cacheDisabled": True})
    tijd = time.time()
    while time.time() - tijd < 25:
        b.wacht(0.5)
        titel = b.js("document.title")
        if titel and "LLM-thuis" in str(titel):
            break
    b.wacht(wachttijd)
    # Pas verder als de modellenlijst echt gevuld is (niet "Laden…").
    for _ in range(40):
        aantal = b.js("document.querySelectorAll('#model-rijen tr').length")
        inhoud = b.js("document.querySelector('#model-rijen').textContent")
        if aantal and "Laden" not in str(inhoud):
            break
        b.wacht(0.5)
    fouten = []
    if b.js("document.querySelectorAll('#model-rijen tr').length") == 0:
        fouten.append("geen modelrijen")
    if b.js("document.querySelectorAll('img:not([src=\"\"])').length") == 0:
        fouten.append("geen afbeeldingen")
    leeg = b.js("Array.from(document.images).filter(i => !i.naturalWidth).length")
    if leeg:
        fouten.append(f"{leeg} afbeelding(en) zonder inhoud")
    print("  pagecontrole:", ", ".join(fouten) or "ok")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    adres, wachtwoord = sys.argv[1].rstrip("/"), sys.argv[2]
    BEELDEN.mkdir(parents=True, exist_ok=True)

    proces, poort = start_chromium()
    try:
        b = verbind(poort)
        print("1. Het bord (bureaublad)")
        hoofdpagina(b, adres + "/")
        b.schermafbeelding(str(BEELDEN / "bord.png"))

        print("2. Een model testen (echte aanroep)")
        b.js("(() => { const k = document.querySelector('button[data-test=\"eigen-llm\"]');"
             " if (k) { k.click(); return 'geklikt'; } return 'geen knop'; })()")
        for _ in range(120):
            b.wacht(0.5)
            tekst = str(b.js("document.querySelector('#test-uitslag').textContent"))
            if "antwoordde" in tekst or "mislukte" in tekst:
                break
        print("   uitslag:", str(b.js("document.querySelector('#test-uitslag').textContent"))[:160])
        b.schermafbeelding(str(BEELDEN / "bord-test.png"))

        print("3. Inloggen en een sleutel maken")
        b.js("(() => { const v = document.querySelector('#wachtwoord');"
             " Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(v, "
             + repr(wachtwoord) + ");"
             " v.dispatchEvent(new Event('input',{bubbles:true}));"
             " document.querySelector('#knop-inloggen').click(); return v.value; })()")
        b.wacht(2)
        ingelogd = b.js("!document.querySelector('#sleutel-open').hidden")
        print("   ingelogd:", ingelogd)
        if not ingelogd:
            print("   LET OP: inloggen mislukte; ik maak wel het gewone bord-beeld.")
        else:
            b.js("(() => { const n = document.querySelector('#sleutel-naam');"
                 " Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(n, 'thuisapp');"
                 " n.dispatchEvent(new Event('input',{bubbles:true}));"
                 " const g = document.querySelector('#sleutel-budget');"
                 " Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(g, '5');"
                 " g.dispatchEvent(new Event('input',{bubbles:true}));"
                 " document.querySelector('#knop-sleutel').click(); return 'geklikt'; })()")
            b.wacht(3)
            print("   sleuteluitslag:", str(b.js("document.querySelector('#sleutel-uitslag').textContent"))[:120])
            # Terug naar boven zodat de kop en de eerste kaarten in het beeld staan.
            b.js("window.scrollTo(0, document.querySelector('#sleutel-open').closest('.kaart').offsetTop - 90)")
            b.wacht(0.8)
            b.schermafbeelding(str(BEELDEN / "sleutels.png"))

        print("4. Telefoon (390x844)")
        b.call("Emulation.setDeviceMetricsOverride", width=390, height=844,
               deviceScaleFactor=2, mobile=True)
        b.js("window.scrollTo(0,0)")
        b.wacht(1.0)
        gemeten = b.js("window.innerWidth")
        print("   gemeten breedte:", gemeten)
        b.schermafbeelding(str(BEELDEN / "bord-telefoon.png"))
        b.js("window.scrollTo(0, document.querySelector('#sleutel-open').closest('.kaart').offsetTop - 90)")
        b.wacht(0.8)
        b.schermafbeelding(str(BEELDEN / "bord-telefoon-sleutels.png"))
        print("Klaar: beelden staan in", BEELDEN)
    finally:
        proces.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
