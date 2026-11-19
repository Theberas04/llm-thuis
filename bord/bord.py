#!/usr/bin/env python3
"""LLM-thuis — het bord: statuspagina, testknop en sleutelbeheer voor je eigen
LLM-gateway.

Draait naast de gateway (LiteLLM) op dezelfde machine en doet drie dingen:

  1. laten zien welke modellen er achter de gateway zitten, met een testknop die
     er écht één aanroept en het antwoord plus de tijd toont;
  2. virtuele sleutels maken met een budget per sleutel (beheerder-only);
  3. alles wat naar /v1/... gaat doorsturen naar de gateway, zodat een app aan
     één adres genoeg heeft: http://<jouw-ip>/v1

Deze laag is bewust alleen de standaardbibliotheek van Python: geen pip-install,
geen npm, geen framework. De gateway zelf is LiteLLM (MIT, https://litellm.ai).

Beheer (sleutels maken) zit achter één wachtwoord. Dat wachtwoord staat als
scrypt-afdruk in een bestand dat alleen root kan lezen; hetzelfde geldt voor de
moedersleutel van de gateway. Beide komen nooit in de pagina terecht.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import secrets
import socket
import sys
import time
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSIE = "1.0.0"
START = time.time()

# --------------------------------------------------------------------------- #
# Instellingen (omgevingsvariabelen, met verstandige standaardwaarden)
# --------------------------------------------------------------------------- #
POORT = int(os.environ.get("THUIS_POORT", "80"))
GASTHEER = os.environ.get("THUIS_GASTHEER", "0.0.0.0")
GATEWAY = os.environ.get("THUIS_GATEWAY", "http://127.0.0.1:4000")
SLEUTELBESTAND = Path(os.environ.get("THUIS_SLEUTELBESTAND", "/etc/llm-thuis/sleutels.env"))
BEHEERBESTAND = Path(os.environ.get("THUIS_BEHEERBESTAND", "/etc/llm-thuis/beheer.env"))
MODELLENBESTAND = Path(os.environ.get("THUIS_MODELLENBESTAND", "/etc/llm-thuis/modellen.json"))
MERKBESTAND = Path(os.environ.get("THUIS_MERK", str(Path(__file__).resolve().parent.parent / "assets" / "merk.png")))
ZITTINGDUUR = int(os.environ.get("THUIS_ZITTINGDUUR", str(12 * 3600)))
TEST_TIMEOUT = int(os.environ.get("THUIS_TEST_TIMEOUT", "180"))
# Optionele overschrijving van het adres dat de pagina toont (handig achter een
# omgekeerde proxy of met een eigen naam); leeg betekent: zelf samenstellen.
BASIS_URL = os.environ.get("THUIS_BASIS_URL", "").strip()
STROOM_TIMEOUT = int(os.environ.get("THUIS_STROOM_TIMEOUT", "1800"))

# Onderdelen van de gateway-URL uit elkaar halen (http://host:poort).
_g = urllib.parse.urlparse(GATEWAY)
GATEWAY_HOST = _g.hostname or "127.0.0.1"
GATEWAY_POORT = _g.port or (443 if _g.scheme == "https" else 80)


# --------------------------------------------------------------------------- #
# Sleutels en wachtwoord inlezen
# --------------------------------------------------------------------------- #
def lees_env_bestand(pad: Path) -> dict:
    """Lees KEY=WAARDE-regels; onleesbaar bestand betekent leeg (geen crash)."""
    uit: dict[str, str] = {}
    try:
        for regel in pad.read_text(encoding="utf-8").splitlines():
            regel = regel.strip()
            if not regel or regel.startswith("#") or "=" not in regel:
                continue
            naam, _, waarde = regel.partition("=")
            uit[naam.strip()] = waarde.strip().strip('"').strip("'")
    except OSError:
        pass
    return uit


def moedersleutel() -> str:
    """De sleutel waarmee dit bord bij de gateway komt.

    Eerst de omgeving (systemd leest het bestand als root en zet de waarde in de
    omgeving van de dienst), en pas daarna het bestand: zo hoeft de dienst het
    sleutelbestand zelf niet te kunnen lezen."""
    uit_omgeving = os.environ.get("THUIS_MASTER_KEY")
    if uit_omgeving:
        return uit_omgeving
    return lees_env_bestand(SLEUTELBESTAND).get("THUIS_MASTER_KEY", "")


def beheer_afdruk() -> str:
    uit_omgeving = os.environ.get("THUIS_BEHEER_HASH")
    if uit_omgeving:
        return uit_omgeving
    return lees_env_bestand(BEHEERBESTAND).get("THUIS_BEHEER_HASH", "")


def modellen_uit_bestand() -> dict:
    """Naam -> {herkomst, omschrijving}. Geschreven door het inrichtscript."""
    try:
        inhoud = json.loads(MODELLENBESTAND.read_text(encoding="utf-8"))
        return inhoud if isinstance(inhoud, dict) else {}
    except (OSError, ValueError):
        return {}


def wachtwoord_klopt(wachtwoord: str) -> bool:
    """Toets tegen de afdruk uit het beheerbestand."""
    return wachtwoord_klopt_met(beheer_afdruk(), wachtwoord)


def wachtwoord_klopt_met(afdruk: str, wachtwoord: str) -> bool:
    """Toets een wachtwoord tegen een scrypt-afdruk (scrypt$n$r$p$zout$afdruk)."""
    if not afdruk or not wachtwoord:
        return False
    delen = afdruk.split("$")
    if len(delen) != 6 or delen[0] != "scrypt":
        return False
    try:
        n, r, p = int(delen[1]), int(delen[2]), int(delen[3])
        zout = bytes.fromhex(delen[4])
        verwacht = bytes.fromhex(delen[5])
    except ValueError:
        return False
    berekend = hashlib.scrypt(wachtwoord.encode(), salt=zout, n=n, r=r, p=p,
                              maxmem=132 * n * r * 2, dklen=len(verwacht))
    return hmac.compare_digest(berekend, verwacht)


def maak_afdruk(wachtwoord: str) -> str:
    """Hulpmiddel voor het inrichtscript: scrypt-afdruk maken."""
    n, r, p = 16384, 8, 1
    zout = secrets.token_bytes(16)
    sleutel = hashlib.scrypt(wachtwoord.encode(), salt=zout, n=n, r=r, p=p,
                             maxmem=132 * n * r * 2, dklen=32)
    return f"scrypt${n}${r}${p}${zout.hex()}${sleutel.hex()}"


# --------------------------------------------------------------------------- #
# Praten met de gateway
# --------------------------------------------------------------------------- #
class GatewayFout(Exception):
    pass


def gateway_verbinding(timeout: int) -> http.client.HTTPConnection:
    if _g.scheme == "https":
        return http.client.HTTPSConnection(GATEWAY_HOST, GATEWAY_POORT, timeout=timeout)
    return http.client.HTTPConnection(GATEWAY_HOST, GATEWAY_POORT, timeout=timeout)


def gateway_json(pad: str, methode: str = "GET", lichaam: dict | None = None,
                 timeout: int = 30, sleutel: str | None = None):
    """Eén verzoek aan de gateway; gooit GatewayFout met uitleg bij problemen."""
    kop = {"Authorization": f"Bearer {sleutel or moedersleutel()}"}
    ruw = None
    if lichaam is not None:
        ruw = json.dumps(lichaam).encode()
        kop["Content-Type"] = "application/json"
    verbinding = gateway_verbinding(timeout)
    try:
        verbinding.request(methode, pad, body=ruw, headers=kop)
        antwoord = verbinding.getresponse()
        tekst = antwoord.read().decode("utf-8", "replace")
    except (OSError, http.client.HTTPException) as fout:
        raise GatewayFout(f"geen verbinding met de gateway op {GATEWAY} ({fout})") from fout
    finally:
        try:
            verbinding.close()
        except Exception:  # noqa: BLE001 - sluiten mag nooit de route breken
            pass
    if antwoord.status >= 400:
        kort = tekst.strip().replace("\n", " ")[:300]
        raise GatewayFout(f"de gateway antwoordde met {antwoord.status}: {kort}")
    try:
        return json.loads(tekst) if tekst.strip() else {}
    except ValueError as fout:
        raise GatewayFout("de gateway gaf geen leesbaar JSON-antwoord") from fout


def gateway_leeft() -> tuple[bool, str]:
    try:
        verbinding = gateway_verbinding(8)
        verbinding.request("GET", "/health/liveliness")
        antwoord = verbinding.getresponse()
        antwoord.read()
        verbinding.close()
        return antwoord.status == 200, f"HTTP {antwoord.status}"
    except (OSError, http.client.HTTPException) as fout:
        return False, str(fout)


# Laatste testuitslag per model, in het geheugen (een statuspagina hoeft geen
# geschiedenis; een digest of database zou hier alleen tijd kosten).
LAATSTE_TEST: dict[str, dict] = {}


def test_model(naam: str) -> dict:
    """Roep één model écht aan via de gateway en meet hoe lang het duurde."""
    begin = time.monotonic()
    uit = gateway_json(
        "/v1/chat/completions",
        "POST",
        {
            "model": naam,
            "messages": [{"role": "user", "content": "Zeg in één korte zin hallo."}],
            "max_tokens": 48,
            "temperature": 0,
        },
        timeout=TEST_TIMEOUT,
    )
    duur_ms = int((time.monotonic() - begin) * 1000)
    keuze = (uit.get("choices") or [{}])[0]
    bericht = keuze.get("message") or {}
    antwoord = str(bericht.get("content") or "").strip()
    verbruik = uit.get("usage") or {}
    uitslag = {
        "model": naam,
        "gelukt": True,
        "antwoord": antwoord,
        "ms": duur_ms,
        "tokens": verbruik.get("total_tokens"),
        "tijd": time.strftime("%H:%M:%S"),
        "seconden": round(duur_ms / 1000, 1),
    }
    LAATSTE_TEST[naam] = uitslag
    return uitslag


# --------------------------------------------------------------------------- #
# Huisstijl (letterlijk uit Werdens Monitor / de eigen LLM-stack)
# --------------------------------------------------------------------------- #
HUISSTIJL = """
:root{
  --bg:#f4f7fb; --pane:#ffffff; --pane2:#f7fafd; --pane3:#eef3fa;
  --line:#e3eaf4; --line2:#cfdaeb;
  --txt:#0f172a; --muted:#55657f; --muted2:#8296ae;
  --accent:#2ea3f2; --accent-d:#1e73be; --accent-soft:#e8f4fd;
  --ok:#059669; --ok-soft:#e6f6ef;
  --amber:#b45309; --amber-soft:#fdf1e2;
  --red:#dc2626; --red-soft:#fdeaea;
  --blue:#1e73be; --blue-soft:#e8f4fd;
  --merk:linear-gradient(118deg,#5ff0e8 0%,#2ea3f2 55%,#1e73be 100%);
  --r:16px; --r-klein:10px; --hoogte:40px;
  --schaduw:0 1px 2px rgba(15,23,42,.05),0 12px 28px -20px rgba(15,23,42,.3);
  --font:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
*{box-sizing:border-box}
[hidden]{display:none !important}
body{margin:0;min-height:100vh;color:var(--txt);font:15px/1.5 var(--font);
  -webkit-font-smoothing:antialiased;
  background:radial-gradient(1100px 520px at 90% -10%,rgba(95,240,232,.12),transparent 62%),
             radial-gradient(900px 480px at -8% -4%,rgba(74,76,248,.08),transparent 58%),var(--bg)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header.kop{position:sticky;top:0;z-index:30;background:rgba(255,255,255,.88);
  -webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);border-bottom:1px solid var(--line);
  padding:12px 26px;display:flex;align-items:center;gap:11px}
.merk{display:flex;align-items:center;gap:11px;min-width:0}
.merk .logo{width:44px;height:44px;flex:none}
.merk .logo img{width:100%;height:100%;object-fit:contain;display:block}
.merk .naam{font-weight:700;font-size:17px;letter-spacing:-.025em;white-space:nowrap}
.merk .sub{font-size:11.5px;color:var(--muted2);letter-spacing:.08em;text-transform:uppercase;
  white-space:nowrap}
.kop-rechts{margin-left:auto;display:flex;align-items:center;gap:8px;font-size:12px;
  color:var(--muted2);flex:none}
.chip{display:inline-flex;align-items:center;gap:6px;height:26px;padding:0 10px;border-radius:999px;
  background:var(--pane3);border:1px solid var(--line);font-size:12px;font-weight:600;color:var(--muted)}
.chip.aan{background:var(--ok-soft);border-color:#bfe6d4;color:var(--ok)}
.chip.uit{background:var(--red-soft);border-color:#f4c9c9;color:var(--red)}
.chip.eigen{background:var(--accent-soft);border-color:#c6e4fa;color:var(--accent-d)}
main{max-width:1080px;margin:0 auto;padding:22px 26px 60px;display:grid;gap:16px}
.raster{display:grid;gap:16px;grid-template-columns:1fr 1fr}
.kaart{background:var(--pane);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--schaduw)}
.kaart-kop{background:var(--pane2);border-bottom:1px solid var(--line);
  border-radius:var(--r) var(--r) 0 0;padding:14px 18px 12px;display:flex;align-items:center;gap:10px}
.kaart-kop h2{margin:0;font-size:12px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--muted);font-weight:700}
.kaart-kop .rechts{margin-left:auto;font-size:12px;color:var(--muted2)}
.kaart-body{padding:16px 18px}
.knop{display:inline-flex;align-items:center;justify-content:center;gap:7px;height:var(--hoogte);
  padding:0 14px;border-radius:var(--r-klein);background:var(--pane);border:1px solid var(--line2);
  color:var(--txt);font:inherit;font-weight:600;cursor:pointer;white-space:nowrap;flex:none}
.knop:hover{background:var(--pane2);border-color:#b6c7dd}
.knop.primair{background:var(--merk);border-color:transparent;color:#fff;font-weight:700}
.knop.klein{height:32px;padding:0 11px;font-size:13px}
input,select,textarea{font:inherit;color:var(--txt);background:var(--pane);border:1px solid var(--line2);
  border-radius:var(--r-klein);padding:0 12px;height:var(--hoogte);width:100%}
input:focus,select:focus,textarea:focus{outline:2px solid var(--accent-soft);border-color:var(--accent)}
code{font-family:var(--mono);font-size:12.5px;background:var(--pane3);border:1px solid var(--line);
  border-radius:6px;padding:1px 6px}
pre{margin:0;font-family:var(--mono);font-size:12.5px;background:var(--pane3);border:1px solid var(--line);
  border-radius:var(--r-klein);padding:12px 14px;overflow-x:auto}
.tabel{width:100%;border-collapse:collapse;font-size:14px}
.tabel th{text-align:left;font-size:11.5px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted2);font-weight:700;padding:0 10px 8px 0}
.tabel td{padding:10px 10px 10px 0;border-top:1px solid var(--line);vertical-align:middle}
.rij-kop{display:flex;align-items:center;gap:9px;min-width:0}
.rij-kop .naam{font-weight:600;font-family:var(--mono);font-size:13px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.grijs{color:var(--muted2)}
.ok{color:var(--ok)}.fout{color:var(--red)}
.uitslag{margin-top:10px;padding:11px 13px;border-radius:var(--r-klein);background:var(--pane3);
  border:1px solid var(--line);font-size:13.5px;display:grid;gap:5px}
.uitslag.goed{background:var(--ok-soft);border-color:#bfe6d4}
.uitslag.mis{background:var(--red-soft);border-color:#f4c9c9}
.uitslag .antwoord{font-family:var(--mono);font-size:12.5px;white-space:pre-wrap;word-break:break-word}
.veld{display:grid;gap:6px}
.veld label{font-size:11.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted2);font-weight:700}
.form-rij{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap}
.form-rij .veld{flex:1 1 160px}
.hint{font-size:13px;color:var(--muted);margin:0}
.stappen{display:grid;gap:10px;margin:0;padding-left:20px}
.stappen li{margin:0}
.sleutel{font-family:var(--mono);font-size:12.5px;background:#0f172a;color:#e8f0f8;border-radius:var(--r-klein);
  padding:11px 13px;overflow-x:auto;white-space:nowrap}
@media (max-width:820px){
  header.kop{padding:11px 16px;flex-wrap:wrap}
  .merk .sub{display:none}
  .kop-rechts{margin-left:0;width:100%;order:3;padding-top:2px}
  main{padding:16px 16px 50px}
  .raster{grid-template-columns:1fr}
  .rij-kop .naam{font-size:12.5px}
  .tabel .kolom-weg{display:none}
}
"""

# --------------------------------------------------------------------------- #
# De pagina (geen f-string: de JS staat vol accolades; de huisstijl komt er via
# __HUISSTIJL__ in, want een gewone string interpoleert niet).
# De JS is backslash-vrij geschreven: nieuwe regels met String.fromCharCode(10).
# --------------------------------------------------------------------------- #
PAGINA = """<!doctype html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LLM-thuis — één adres voor al je modellen</title>
<link rel="icon" href="/merk.png">
<style>
__HUISSTIJL__
</style></head><body>
<header class="kop">
  <span class="merk">
    <span class="logo"><img src="/merk.png" alt="Werdens Media" onerror="this.style.display='none'"></span>
    <span>
      <span class="naam">LLM-thuis</span><br>
      <span class="sub">één adres voor al je modellen</span>
    </span>
  </span>
  <span class="kop-rechts">
    <span class="chip" id="chip-gateway">gateway …</span>
    <span class="chip" id="chip-db">sleutelbeheer …</span>
  </span>
</header>
<main>
  <div class="kaart">
    <div class="kaart-kop"><h2>Zo gebruik je het</h2><span class="rechts" id="gw-adres">…</span></div>
    <div class="kaart-body">
      <p class="hint">Zet dit adres in je app of agent, met een sleutel uit de kaart hieronder.
      Alles loopt via deze machine: jouw prompts en jouw sleutels blijven thuis.</p>
      <pre id="voorbeeld">…</pre>
    </div>
  </div>

  <div class="kaart">
    <div class="kaart-kop"><h2>Modellen</h2>
      <span class="rechts"><button class="knop klein" id="knop-ververs">Verversen</button></span></div>
    <div class="kaart-body">
      <table class="tabel"><thead><tr>
        <th>Model</th><th class="kolom-weg">Herkomst</th><th class="kolom-weg">Laatste test</th><th></th>
      </tr></thead><tbody id="model-rijen"><tr><td colspan="4" class="grijs">Laden…</td></tr></tbody></table>
      <div id="test-uitslag"></div>
    </div>
  </div>

  <div class="kaart">
    <div class="kaart-kop"><h2>Sleutels</h2><span class="rechts" id="sleutel-status">alleen voor de beheerder</span></div>
    <div class="kaart-body">
      <div id="sleutel-dicht">
        <p class="hint">Sleutels maken is beheerderswerk: log in met het wachtwoord dat bij de
        installatie is gekozen. Kijken en testen mag iedereen op je netwerk.</p>
        <div class="form-rij">
          <div class="veld"><label for="wachtwoord">Beheerderswachtwoord</label>
            <input type="password" id="wachtwoord" autocomplete="current-password"></div>
          <button class="knop primair" id="knop-inloggen">Inloggen</button>
        </div>
        <div id="inlog-melding"></div>
      </div>
      <div id="sleutel-open" hidden>
        <div id="sleutel-lijst"></div>
        <div class="form-rij" style="margin-top:14px">
          <div class="veld"><label for="sleutel-naam">Naam van de app</label>
            <input type="text" id="sleutel-naam" placeholder="bijvoorbeeld: thuisapp"></div>
          <div class="veld"><label for="sleutel-budget">Budget in euro (leeg = geen limiet)</label>
            <input type="text" id="sleutel-budget" placeholder="5"></div>
          <button class="knop primair" id="knop-sleutel">Sleutel maken</button>
          <button class="knop" id="knop-uitloggen">Uitloggen</button>
        </div>
        <div id="sleutel-uitslag"></div>
      </div>
    </div>
  </div>

  <div class="kaart">
    <div class="kaart-kop"><h2>Onder de motorkap</h2></div>
    <div class="kaart-body">
      <ul class="stappen">
        <li>De gateway is <b>LiteLLM</b> (MIT) en luistert op poort 4000; zijn eigen dashboard staat op
          <a href="/ui" id="ui-link">/ui</a>.</li>
        <li>Deze pagina zet alles wat naar <code>/v1/…</code> gaat door naar de gateway, zodat één adres
          volstaat voor al je apps.</li>
        <li>Met een database erbij houdt de gateway per sleutel het verbruik en het budget bij.</li>
      </ul>
    </div>
  </div>
</main>
<script>
(function(){
  var tekst = function(sel){ var el = document.querySelector(sel); return el ? el.textContent : ''; };
  var escapeHtml = function(s){ return String(s).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); };
  var NL = String.fromCharCode(10);

  function melding(doel, soort, tekstIn){
    var el = document.querySelector(doel);
    if (!el) { return; }
    el.innerHTML = tekstIn ? '<div class="uitslag ' + soort + '">' + tekstIn + '</div>' : '';
  }

  function chip(id, aan, tekstIn){
    var el = document.querySelector(id);
    if (!el) { return; }
    el.textContent = tekstIn;
    el.className = 'chip ' + (aan === true ? 'aan' : aan === false ? 'uit' : '');
  }

  function toonStatus(s){
    chip('#chip-gateway', s.gateway_levend, s.gateway_levend ? 'gateway bereikbaar' : 'gateway onbereikbaar');
    chip('#chip-db', s.sleutels_aan === true ? true : (s.sleutels_aan === false ? false : null),
      s.sleutels_aan === true ? 'sleutelbeheer aan' : (s.sleutels_aan === false ? 'geen sleutelbeheer' : 'sleutelbeheer ?'));
    var adres = document.querySelector('#gw-adres');
    if (adres) { adres.textContent = s.basis_url; }
    var ui = document.querySelector('#ui-link');
    if (ui) { ui.setAttribute('href', s.ui_url); }
    var vb = document.querySelector('#voorbeeld');
    if (vb) {
      vb.textContent = 'curl ' + s.basis_url + 'chat/completions  ' + NL
        + '  -H "Authorization: Bearer JOUW-SLEUTEL"  ' + NL
        + '  -H "Content-Type: application/json"  ' + NL
        + '  -d { "model": "' + (s.modellen[0] ? s.modellen[0].naam : 'eigen-llm') + '",' + NL
        + '       "messages": [ { "role": "user", "content": "Hallo" } ] }';
    }
    var rijen = '';
    (s.modellen || []).forEach(function(m){
      var test = m.laatste_test;
      var herkomst = m.herkomst === 'eigen' ? 'eigen machine' : (m.herkomst || 'cloud');
      var laatste = test
        ? (test.gelukt ? '<span class="ok">' + escapeHtml(test.seconden + ' s') + '</span> <span class="grijs">'
            + escapeHtml(test.tijd) + '</span>' : '<span class="fout">mislukt</span>')
        : '<span class="grijs">nog niet getest</span>';
      rijen += '<tr>'
        + '<td><div class="rij-kop"><span class="naam">' + escapeHtml(m.naam) + '</span>'
        + (m.herkomst === 'eigen' ? '<span class="chip eigen">eigen</span>' : '') + '</div></td>'
        + '<td class="kolom-weg grijs">' + escapeHtml(herkomst) + '</td>'
        + '<td class="kolom-weg">' + laatste + '</td>'
        + '<td style="text-align:right"><button class="knop klein" data-test="' + escapeHtml(m.naam) + '">Test</button></td>'
        + '</tr>';
    });
    var lijf = document.querySelector('#model-rijen');
    if (lijf) {
      lijf.innerHTML = rijen || '<tr><td colspan="4" class="grijs">Nog geen modellen in de gateway.</td></tr>';
      Array.prototype.forEach.call(lijf.querySelectorAll('button[data-test]'), function(knop){
        knop.addEventListener('click', function(){ test(knop.getAttribute('data-test'), knop); });
      });
    }
  }

  function laadStatus(){
    fetch('/api/status').then(function(r){ return r.json(); }).then(toonStatus)
      .catch(function(f){ melding('#test-uitslag', 'mis', 'Status ophalen mislukte: ' + f.message); });
  }

  function test(naam, knop){
    knop.disabled = true;
    var oud = knop.textContent; knop.textContent = 'bezig…';
    melding('#test-uitslag', '', 'Bezig met <code>' + escapeHtml(naam) + '</code> — dit kan even duren op een klein model.');
    fetch('/api/test', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ model: naam }) })
      .then(function(r){ return r.json().then(function(j){ return { ok:r.ok, j:j }; }); })
      .then(function(u){
        knop.disabled = false; knop.textContent = oud;
        if (u.ok && u.j.gelukt) {
          melding('#test-uitslag', 'goed', '<b>' + escapeHtml(u.j.model) + '</b> antwoordde in '
            + escapeHtml(u.j.seconden) + ' s'
            + (u.j.tokens ? ' (' + escapeHtml(u.j.tokens) + ' tokens)' : '')
            + '<span class="antwoord">' + escapeHtml(u.j.antwoord) + '</span>');
        } else {
          melding('#test-uitslag', 'mis', 'Test mislukte: ' + escapeHtml(u.j.error || 'onbekende fout'));
        }
        laadStatus();
      })
      .catch(function(f){
        knop.disabled = false; knop.textContent = oud;
        melding('#test-uitslag', 'mis', 'Test mislukte: ' + escapeHtml(f.message));
      });
  }

  function laadSleutels(){
    fetch('/api/sleutels').then(function(r){
      if (r.status === 401) { throw new Error('niet ingelogd'); }
      return r.json();
    }).then(function(j){
      document.querySelector('#sleutel-dicht').hidden = true;
      document.querySelector('#sleutel-open').hidden = false;
      document.querySelector('#sleutel-status').textContent = 'beheerder ingelogd';
      var rijen = '';
      (j.sleutels || []).forEach(function(k){
        var verbruik = (k.verbruik === null || k.verbruik === undefined) ? '—' : (Math.round(k.verbruik * 100) / 100 + ' euro');
        var budget = (k.budget === null || k.budget === undefined) ? 'geen limiet' : (k.budget + ' euro');
        rijen += '<tr><td>' + escapeHtml(k.naam) + '</td><td class="grijs">' + escapeHtml(budget)
          + '</td><td class="grijs">' + escapeHtml(verbruik) + '</td>'
          + '<td class="grijs kolom-weg">' + escapeHtml(k.aangemaakt || '') + '</td></tr>';
      });
      document.querySelector('#sleutel-lijst').innerHTML = '<table class="tabel"><thead><tr><th>Sleutel</th>'
        + '<th>Budget</th><th>Verbruikt</th><th class="kolom-weg">Aangemaakt</th></tr></thead><tbody>'
        + (rijen || '<tr><td colspan="4" class="grijs">Nog geen sleutels.</td></tr>') + '</tbody></table>';
    }).catch(function(){
      document.querySelector('#sleutel-dicht').hidden = false;
      document.querySelector('#sleutel-open').hidden = true;
      document.querySelector('#sleutel-status').textContent = 'alleen voor de beheerder';
    });
  }

  var inlogKnop = document.querySelector('#knop-inloggen');
  if (inlogKnop) {
    inlogKnop.addEventListener('click', function(){
      var wachtwoord = document.querySelector('#wachtwoord').value;
      fetch('/api/inloggen', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ wachtwoord: wachtwoord }) })
        .then(function(r){ return r.json().then(function(j){ return { ok:r.ok, j:j }; }); })
        .then(function(u){
          if (u.ok) { document.querySelector('#wachtwoord').value = ''; melding('#inlog-melding','', ''); laadSleutels(); }
          else { melding('#inlog-melding', 'mis', escapeHtml(u.j.error || 'Inloggen mislukte.')); }
        })
        .catch(function(f){ melding('#inlog-melding', 'mis', 'Inloggen mislukte: ' + escapeHtml(f.message)); });
    });
    document.querySelector('#wachtwoord').addEventListener('keydown', function(e){
      if (e.key === 'Enter') { inlogKnop.click(); }
    });
  }

  var uitlogKnop = document.querySelector('#knop-uitloggen');
  if (uitlogKnop) {
    uitlogKnop.addEventListener('click', function(){
      fetch('/api/uitloggen', { method:'POST' }).then(function(){ laadSleutels(); });
    });
  }

  var sleutelKnop = document.querySelector('#knop-sleutel');
  if (sleutelKnop) {
    sleutelKnop.addEventListener('click', function(){
      var naam = document.querySelector('#sleutel-naam').value.trim();
      var budgetRuw = document.querySelector('#sleutel-budget').value.trim();
      if (!naam) { melding('#sleutel-uitslag', 'mis', 'Geef de sleutel een naam.'); return; }
      var lichaam = { naam: naam };
      if (budgetRuw) { lichaam.budget = budgetRuw; }
      fetch('/api/sleutels', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify(lichaam) })
        .then(function(r){ return r.json().then(function(j){ return { ok:r.ok, j:j }; }); })
        .then(function(u){
          if (u.ok) {
            melding('#sleutel-uitslag', 'goed', '<div>Nieuwe sleutel voor <b>' + escapeHtml(u.j.naam)
              + '</b>. Bewaar hem nu, je ziet hem hierna niet meer:</div>'
              + '<div class="sleutel">' + escapeHtml(u.j.sleutel) + '</div>');
            document.querySelector('#sleutel-naam').value = '';
            document.querySelector('#sleutel-budget').value = '';
            laadSleutels();
          } else {
            melding('#sleutel-uitslag', 'mis', escapeHtml(u.j.error || 'Sleutel maken mislukte.'));
          }
        })
        .catch(function(f){ melding('#sleutel-uitslag', 'mis', 'Sleutel maken mislukte: ' + escapeHtml(f.message)); });
    });
  }

  var ververs = document.querySelector('#knop-ververs');
  if (ververs) { ververs.addEventListener('click', laadStatus); }
  var uiEl = document.querySelector('#ui-link');
  if (uiEl) {
    uiEl.addEventListener('click', function(e){
      e.preventDefault();
      window.open(location.protocol + '//' + location.hostname + ':4000/ui', '_blank');
    });
  }
  laadStatus();
  laadSleutels();
})();
</script>
</body></html>
"""


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def status_json() -> dict:
    levend, detail = gateway_leeft()
    modellen: list[dict] = []
    herkomst = modellen_uit_bestand()
    if levend:
        try:
            lijst = gateway_json("/v1/models").get("data") or []
            for m in lijst:
                naam = str(m.get("id") or "")
                if not naam:
                    continue
                info = herkomst.get(naam) or {}
                modellen.append({
                    "naam": naam,
                    "herkomst": info.get("herkomst", "cloud"),
                    "omschrijving": info.get("omschrijving", ""),
                    "laatste_test": LAATSTE_TEST.get(naam),
                })
        except GatewayFout as fout:
            modellen = []
            detail = str(fout)
        # Modellen die in het bestand staan maar (nog) niet in de gateway zitten,
        # toch tonen: dan zie je meteen dat er iets niet doorkwam.
        bekend = {m["naam"] for m in modellen}
        for naam, info in herkomst.items():
            if naam not in bekend:
                modellen.append({
                    "naam": naam,
                    "herkomst": info.get("herkomst", "cloud"),
                    "omschrijving": info.get("omschrijving", ""),
                    "laatste_test": LAATSTE_TEST.get(naam),
                })
    gastheer = GASTHEER if GASTHEER != "0.0.0.0" else "deze-machine"
    basis_url = BASIS_URL or f"http://{gastheer}:{POORT}/v1/"
    if not basis_url.endswith("/"):
        basis_url += "/"
    return {
        "versie": VERSIE,
        "gateway": GATEWAY,
        "gateway_levend": levend,
        "gateway_detail": detail,
        "modellen": modellen,
        "sleutels_aan": _sleutels_aan(),
        "basis_url": basis_url,
        "ui_url": f"http://{gastheer}:4000/ui",
        "draait_sinds": int(time.time() - START),
    }


def _sleutels_aan() -> bool:
    """Vertelt de pagina of er sleutelbeheer (database) is: /key/list werkt of niet."""
    if not moedersleutel():
        return False
    try:
        gateway_json("/key/list", "GET", timeout=10)
        return True
    except GatewayFout:
        return False


def sleutels_json() -> dict:
    """Lijst van sleutels met naam, budget en verbruik.

    Let op: de gateway geeft op /key/list standaard alleen de *hashes* terug. Zonder
    return_full_object krijg je een lijst strings en toont de pagina allemaal lege
    rijen; met de vlag krijg je de objecten. Valt die route weg, dan halen we de
    gegevens per sleutel op via /key/info.
    """
    uit = gateway_json("/key/list?return_full_object=true", "GET", timeout=30)
    ruw = uit.get("keys") or []
    if not isinstance(ruw, list):
        return {"sleutels": []}
    uitgesplitst: list[dict] = []
    for item in ruw[:50]:
        if isinstance(item, dict):
            uitgesplitst.append(_sleutel_uit_item(item))
        elif isinstance(item, str):
            try:
                info = (gateway_json(f"/key/info?key={item}", "GET", timeout=20).get("info") or {})
            except GatewayFout:
                info = {}
            uitgesplitst.append(_sleutel_uit_item(info, terugval=item))
    return {"sleutels": uitgesplitst}


def _sleutel_uit_item(item: dict, terugval: str = "") -> dict:
    naam = str(item.get("key_alias") or item.get("key_name") or terugval or "zonder naam")
    budget = item.get("max_budget")
    verbruik = item.get("spend", item.get("total_spend"))
    aangemaakt = str(item.get("created_at") or item.get("updated_at") or "")[:10]
    return {
        "naam": naam,
        "budget": budget,
        "verbruik": verbruik,
        "aangemaakt": aangemaakt,
        "verlopen": item.get("expires"),
    }


def maak_sleutel(lichaam: dict) -> dict:
    naam = str(lichaam.get("naam") or "").strip()
    if not naam:
        raise GatewayFout("geef de sleutel een naam")
    verzoek: dict = {"key_alias": naam, "models": []}
    budget = lichaams_budget(lichaam.get("budget"))
    if budget is not None:
        verzoek["max_budget"] = budget
        verzoek["budget_duration"] = "30d"
    uit = gateway_json("/key/generate", "POST", verzoek, timeout=60)
    sleutel = uit.get("key")
    if not sleutel:
        raise GatewayFout("de gateway gaf geen sleutel terug")
    return {"naam": naam, "sleutel": sleutel, "budget": budget}


def lichaams_budget(ruw) -> float | None:
    if ruw in (None, "", "leeg"):
        return None
    try:
        bedrag = float(str(ruw).replace(",", "."))
    except ValueError as fout:
        raise GatewayFout("het budget moet een getal zijn, bijvoorbeeld 5") from fout
    if bedrag <= 0:
        raise GatewayFout("het budget moet groter dan nul zijn")
    return bedrag


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"llm-thuis-bord/{VERSIE}"

    # -- helpers -----------------------------------------------------------
    def log_message(self, format, *args):  # noqa: A002 - stdlib-signatuur
        sys.stderr.write("[bord] %s\n" % (format % args))

    def _stuur(self, code: int, soort: str, body: bytes, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", soort)
        self.send_header("Content-Length", str(len(body)))
        for naam, waarde in (extra or {}).items():
            self.send_header(naam, waarde)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200, extra: dict | None = None):
        self._stuur(code, "application/json; charset=utf-8",
                    json.dumps(obj).encode(), extra)

    def _html(self, tekst: str, code: int = 200):
        self._stuur(code, "text/html; charset=utf-8", tekst.encode())

    def _fout(self, melding: str, code: int = 400):
        self._json({"error": melding}, code)

    def _vraag(self, naam: str) -> str:
        query = urllib.parse.urlparse(self.path).query
        return (urllib.parse.parse_qs(query).get(naam) or [""])[0]

    def _lichaam(self) -> dict:
        lengte = int(self.headers.get("Content-Length") or 0)
        ruw = self.rfile.read(lengte) if lengte else b""
        if not ruw:
            return {}
        try:
            uit = json.loads(ruw.decode("utf-8", "replace"))
        except ValueError:
            return {}
        return uit if isinstance(uit, dict) else {}

    # -- beheerder: zitting in een HttpOnly-koekje --------------------------
    def _zittingen(self) -> dict:
        return getattr(self.server, "zittingen", {})

    def _is_beheerder(self) -> bool:
        ruw = self.headers.get("Cookie") or ""
        if not ruw:
            return False
        try:
            koek = SimpleCookie(ruw)
        except Exception:  # noqa: BLE001 - kapotte koek = niet ingelogd
            return False
        m = koek.get("thuis_zitting")
        if not m:
            return False
        gegevens = self._zittingen().get(m.value)
        if not gegevens:
            return False
        if time.time() - gegevens.get("sinds", 0) > ZITTINGDUUR:
            self._zittingen().pop(m.value, None)
            return False
        return gegevens.get("beheerder") is True

    def _eist_beheerder(self) -> bool:
        if self._is_beheerder():
            return True
        self._fout("log eerst in als beheerder", 401)
        return False

    # -- logo ---------------------------------------------------------------
    def _merk(self):
        try:
            body = MERKBESTAND.read_bytes()
            self._stuur(200, "image/png", body, {"Cache-Control": "public, max-age=300"})
        except OSError:
            # Neutrale 1x1-png als terugval; nooit cachen (anders blijft de lege
            # afbeelding dagenlang in de browser hangen).
            leeg = bytes.fromhex(
                "89504e470d0a1a0a0000000d494844520000000100000001080600000"
                "01f15c4890000000a49444154789c6360000002000154a24f5f000000"
                "0049454e44ae426082")
            self._stuur(200, "image/png", leeg, {"Cache-Control": "no-store"})

    # -- doorsturen naar de gateway ----------------------------------------
    def _stuur_door(self, methode: str):
        """Alles onder /v1/ gaat naar de gateway, inclusief streaming antwoorden.

        Zo hoeft een app maar één adres te kennen. De sleutel van de client gaat
        mee; deze laag voegt niets toe en bewaart niets."""
        doel = self.path
        lengte = int(self.headers.get("Content-Length") or 0)
        lichaam = self.rfile.read(lengte) if lengte else None
        kop = {}
        for naam, waarde in self.headers.items():
            if naam.lower() in ("host", "connection", "content-length", "authorization"):
                continue
            kop[naam] = waarde
        if self.headers.get("Authorization"):
            kop["Authorization"] = self.headers["Authorization"]
        if lichaam is not None:
            kop["Content-Length"] = str(len(lichaam))
        verbinding = gateway_verbinding(STROOM_TIMEOUT)
        try:
            verbinding.request(methode, doel, body=lichaam, headers=kop)
            antwoord = verbinding.getresponse()
        except (OSError, http.client.HTTPException) as fout:
            return self._fout(f"geen verbinding met de gateway op {GATEWAY} ({fout})", 502)
        self.send_response(antwoord.status)
        lengte_heen = antwoord.getheader("Content-Length")
        for naam, waarde in antwoord.getheaders():
            if naam.lower() in ("content-length", "transfer-encoding", "connection", "keep-alive"):
                continue
            self.send_header(naam, waarde)
        if lengte_heen is not None:
            self.send_header("Content-Length", lengte_heen)
        else:
            # Zonder lengte van bovenaf moet deze laag zelf chunk-framing doen:
            # alleen de kop 'Transfer-Encoding: chunked' zetten en de bytes ruw
            # doorsturen levert een kapot antwoord op.
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            while True:
                brok = antwoord.read(4096)
                if not brok:
                    break
                if lengte_heen is None:
                    self.wfile.write(f"{len(brok):X}\r\n".encode() + brok + b"\r\n")
                else:
                    self.wfile.write(brok)
                self.wfile.flush()
            if lengte_heen is None:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Client hing op: geen 500 van maken, de gateway ruimt zelf op.
            pass
        finally:
            try:
                verbinding.close()
            except Exception:  # noqa: BLE001
                pass

    # -- GET ----------------------------------------------------------------
    def do_GET(self):  # noqa: N802 - stdlib-naam
        pad = urllib.parse.urlparse(self.path).path
        if pad in ("/logo.png", "/merk.png"):
            return self._merk()
        if pad == "/gezondheid":
            return self._stuur(200, "text/plain; charset=utf-8", b"ok\n", {"x-llm-thuis": "bord"})
        if pad in ("/", "/index.html"):
            return self._html(PAGINA.replace("__HUISSTIJL__", HUISSTIJL))
        if pad == "/api/status":
            try:
                return self._json(status_json())
            except GatewayFout as fout:
                return self._fout(str(fout), 502)
        if pad == "/api/sleutels":
            if not self._eist_beheerder():
                return
            try:
                return self._json(sleutels_json())
            except GatewayFout as fout:
                return self._fout(str(fout), 502)
        if pad.startswith("/v1/"):
            return self._stuur_door("GET")
        return self._fout("onbekende route", 404)

    # -- POST ---------------------------------------------------------------
    def do_POST(self):  # noqa: N802 - stdlib-naam
        pad = urllib.parse.urlparse(self.path).path
        if pad == "/api/test":
            lichaam = self._lichaam()
            naam = str(lichaam.get("model") or "").strip()
            if not naam:
                return self._fout("geen model opgegeven")
            try:
                return self._json(test_model(naam))
            except GatewayFout as fout:
                LAATSTE_TEST[naam] = {"model": naam, "gelukt": False, "antwoord": str(fout),
                                      "tijd": time.strftime("%H:%M:%S")}
                return self._json({"model": naam, "gelukt": False, "error": str(fout)}, 502)
        if pad == "/api/inloggen":
            lichaam = self._lichaam()
            if not beheer_afdruk():
                return self._fout("er is nog geen beheerderswachtwoord ingesteld", 503)
            if not wachtwoord_klopt(str(lichaam.get("wachtwoord") or "")):
                return self._fout("dat wachtwoord klopt niet", 401)
            token = secrets.token_urlsafe(32)
            self._zittingen()[token] = {"beheerder": True, "sinds": time.time()}
            return self._json({"ok": True}, extra={
                "Set-Cookie": f"thuis_zitting={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={ZITTINGDUUR}"})
        if pad == "/api/uitloggen":
            token = ""
            ruw = self.headers.get("Cookie") or ""
            try:
                m = SimpleCookie(ruw).get("thuis_zitting")
                token = m.value if m else ""
            except Exception:  # noqa: BLE001
                token = ""
            self._zittingen().pop(token, None)
            return self._json({"ok": True}, extra={
                "Set-Cookie": "thuis_zitting=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"})
        if pad == "/api/sleutels":
            if not self._eist_beheerder():
                return
            try:
                return self._json(maak_sleutel(self._lichaam()))
            except GatewayFout as fout:
                return self._fout(str(fout), 502)
        if pad.startswith("/v1/"):
            return self._stuur_door("POST")
        return self._fout("onbekende route", 404)


def maak_server(poort: int = POORT, gastheer: str = GASTHEER) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((gastheer, poort), Handler)
    server.zittingen = {}  # type: ignore[attr-defined]
    server.daemon_threads = True
    return server


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--afdruk":
        # Hulpmiddel voor het inrichtscript: scrypt-afdruk maken van een wachtwoord.
        wachtwoord = sys.argv[2] if len(sys.argv) > 2 else ""
        if not wachtwoord:
            sys.stderr.write("gebruik: bord.py --afdruk <wachtwoord>\n")
            return 2
        print(maak_afdruk(wachtwoord))
        return 0
    poort = int(os.environ.get("THUIS_POORT", str(POORT)))
    gastheer = os.environ.get("THUIS_GASTHEER", GASTHEER)
    server = maak_server(poort, gastheer)
    sys.stderr.write(f"[bord] LLM-thuis {VERSIE} luistert op {gastheer}:{poort}, "
                     f"gateway {GATEWAY}\n")
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # Nooit srv.shutdown() in een signaalafhandelaar: die wacht op zijn eigen
        # lus. Gewoon stoppen en direct afsluiten.
        try:
            server.server_close()
        finally:
            os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
