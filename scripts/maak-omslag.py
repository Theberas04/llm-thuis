#!/usr/bin/env python3
"""Maakt de omslagafbeelding (docs/img/hero.png) uit de twee schermafbeeldingen.

Draai dit NA scripts/maak-beelden.py: de omslag gebruikt bord.png (bureaublad) en
bord-telefoon.png (telefoon). De pagina wordt in Chromium op 1600x840 gezet en
vastgelegd, zodat de kaarten en de schaduw echt gerenderd zijn.
"""

from __future__ import annotations

import base64
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


def data_url(pad: pathlib.Path) -> str:
    return "data:image/png;base64," + base64.b64encode(pad.read_bytes()).decode()


def main() -> int:
    groot = BEELDEN / "bord.png"
    telefoon = BEELDEN / "bord-telefoon.png"
    for pad in (groot, telefoon):
        if not pad.exists():
            print(f"Ontbreekt: {pad}. Draai eerst scripts/maak-beelden.py")
            return 2

    html = f"""<!doctype html><html lang="nl"><head><meta charset="utf-8">
<style>
  *{{box-sizing:border-box;margin:0}}
  html,body{{width:1600px;height:840px}}
  body{{font-family:"Inter",system-ui,sans-serif;color:#fff;
    background:radial-gradient(900px 500px at 88% -10%,rgba(95,240,232,.35),transparent 62%),
               radial-gradient(800px 460px at -6% 110%,rgba(120,80,255,.28),transparent 60%),
               linear-gradient(118deg,#0b1a2b 0%,#123a5c 55%,#0e2a45 100%);
    display:flex;align-items:center;gap:44px;padding:54px 56px}}
  .tekst{{width:520px;flex:none}}
  .logo{{width:56px;height:56px;border-radius:14px;background:linear-gradient(118deg,#5ff0e8,#2ea3f2 55%,#1e73be);
    display:flex;align-items:center;justify-content:center;font-weight:800;font-size:24px;margin-bottom:20px}}
  h1{{font-size:52px;line-height:1.05;letter-spacing:-.03em;margin-bottom:14px}}
  h1 span{{background:linear-gradient(118deg,#5ff0e8,#7fd0ff 55%,#9ad4ff);-webkit-background-clip:text;
    -webkit-text-fill-color:transparent}}
  p{{font-size:19px;line-height:1.5;color:#c9dcf0;margin-bottom:22px}}
  ul{{list-style:none;font-size:16px;color:#e3eefa;display:grid;gap:9px}}
  li::before{{content:"→";color:#5ff0e8;margin-right:9px}}
  .beelden{{position:relative;flex:1;height:100%}}
  .bord{{position:absolute;top:26px;left:0;width:760px;border-radius:14px;border:1px solid rgba(255,255,255,.16);
    box-shadow:0 30px 70px -30px rgba(0,0,0,.85);overflow:hidden}}
  .bord img{{width:100%;display:block}}
  .telefoon{{position:absolute;right:0;bottom:-10px;width:250px;border-radius:22px;
    border:6px solid #0a1524;box-shadow:0 30px 60px -26px rgba(0,0,0,.9);overflow:hidden;background:#0a1524}}
  .telefoon img{{width:100%;display:block}}
</style></head><body>
  <div class="tekst">
    <div class="logo">LT</div>
    <h1>Eén adres voor <span>al je modellen</span></h1>
    <p>Je eigen LLM én de cloud achter één OpenAI-compatibel adres, op je eigen machine.
       Sleutels met een budget, verbruik in de hand, en niets dat per verzoek meerekent.</p>
    <ul>
      <li>OpenRouter-idee, in je eigen netwerk</li>
      <li>Eén regel op je Proxmox-host &middot; geen Docker</li>
      <li>Eigen model vooraan, cloud erbij als je wilt</li>
    </ul>
  </div>
  <div class="beelden">
    <div class="bord"><img src="{data_url(groot)}" alt=""></div>
    <div class="telefoon"><img src="{data_url(telefoon)}" alt=""></div>
  </div>
</body></html>"""
    tijdelijk = pathlib.Path("/tmp/llm-thuis-omslag.html")
    tijdelijk.write_text(html, encoding="utf-8")

    profiel = "/tmp/llm-thuis-omslag-profiel"
    subprocess.run(["rm", "-rf", profiel], check=False)
    ch = str(sorted(pathlib.Path(os.path.expanduser("~/.hermes/tools")).glob(
        "chromium-*/chrome-linux64/chrome"))[-1])
    proces = subprocess.Popen([ch, "--headless=new", "--no-sandbox", "--disable-gpu",
                               "--disable-dev-shm-usage", "--hide-scrollbars",
                               "--window-size=1600,840", "--force-device-scale-factor=1",
                               "--remote-debugging-port=0", f"--user-data-dir={profiel}",
                               "--no-first-run", "about:blank"],
                              env=OMGEVING, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    poort = None
    for _ in range(60):
        bestand = pathlib.Path(profiel) / "DevToolsActivePort"
        if bestand.exists():
            poort = int(bestand.read_text().splitlines()[0])
            break
        time.sleep(0.3)
    if poort is None:
        proces.kill()
        print("Chromium gaf geen debug-poort.")
        return 1
    try:
        b = None
        for _ in range(40):
            try:
                for tab in tabs(poort):
                    if tab.get("type") == "page":
                        b = CDP(tab["webSocketDebuggerUrl"])
                        break
            except Exception:  # noqa: BLE001
                pass
            if b:
                break
            time.sleep(0.3)
        b.call("Page.enable")
        b.call("Page.navigate", url=tijdelijk.as_uri())
        b.wacht(4)
        # Beide afbeeldingen moeten geladen zijn voor we vastleggen.
        for _ in range(30):
            klaar = b.js("Array.from(document.images).every(i => i.complete && i.naturalWidth > 0)")
            if klaar:
                break
            b.wacht(0.5)
        leeg = b.js("Array.from(document.images).filter(i => !i.naturalWidth).length")
        if leeg:
            print(f"Let op: {leeg} afbeelding(en) niet geladen")
        b.schermafbeelding(str(BEELDEN / "hero.png"))
    finally:
        proces.terminate()
    print("Omslag gemaakt:", BEELDEN / "hero.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
