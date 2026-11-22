"""Kern van de LLM-thuis bureaubladapp.

Alle logica staat hier en dit bestand raakt tkinter niet aan, zodat het op elke
machine te toetsen is: de client die met het bord praat, het vergelijken van
versies, en het voorbereiden en uitvoeren van een bijwerking. De vensters zelf
staan in ui.py.

Er gaat geen wachtwoord de schijf op: het beheerderswachtwoord wordt alleen naar
het bord gestuurd en de zitting blijft in het koekje van deze sessie.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from http.cookiejar import CookieJar
from pathlib import Path

from .version import VERSIE

# Waar de app zijn bijwerking vandaan haalt. De repo is openbaar, dus er is geen
# sleutel nodig; de nieuwste uitgave (release) van de repo levert deze twee bestanden.
REPO = "Theberas04/llm-thuis"
MANIFEST_NAAM = "version.json"

TIME_OUT = 20  # seconden voor gewone verzoeken


class BordFout(Exception):
    """Iets ging mis in het gesprek met het bord; de tekst is voor de gebruiker."""


# --------------------------------------------------------------------------- #
# Instellingen (adres van het bord), in de map van de gebruiker
# --------------------------------------------------------------------------- #
def gegevensmap() -> Path:
    """De map waarin de app zijn instellingen en bijwerkingen bewaart."""
    basis = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME")
    if basis:
        map_ = Path(basis) / "LLM-thuis"
    else:
        map_ = Path.home() / ".config" / "llm-thuis"
    map_.mkdir(parents=True, exist_ok=True)
    return map_


def lees_instellingen() -> dict:
    pad = gegevensmap() / "instellingen.json"
    try:
        inhoud = json.loads(pad.read_text(encoding="utf-8"))
        return inhoud if isinstance(inhoud, dict) else {}
    except (OSError, ValueError):
        return {}


def bewaar_instellingen(gegevens: dict) -> None:
    pad = gegevensmap() / "instellingen.json"
    try:
        pad.write_text(json.dumps(gegevens, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # instellingen zijn gemak, geen functie: nooit een fout naar de gebruiker


# --------------------------------------------------------------------------- #
# Het bord: status, modellen testen, sleutels beheren
# --------------------------------------------------------------------------- #
def normaliseer_basis(ruw: str) -> str:
    """Maakt van wat de gebruiker typt een bruikbaar adres.

    '192.168.1.100' wordt 'http://192.168.1.100', 'http://x/' wordt 'http://x', en
    een vergeten poort blijft weg (het bord luistert op 80).
    """
    tekst = (ruw or "").strip()
    if not tekst:
        raise BordFout("vul eerst het adres van je bord in, bijvoorbeeld 192.168.1.100")
    if "//" not in tekst:
        tekst = "http://" + tekst
    onderdelen = urllib.parse.urlsplit(tekst)
    if onderdelen.scheme not in ("http", "https") or not onderdelen.netloc:
        raise BordFout(f"'{ruw}' is geen bruikbaar adres")
    return urllib.parse.urlunsplit((onderdelen.scheme, onderdelen.netloc, "", "", "")).rstrip("/")


class Bord:
    """Client voor de pagina-API van het bord (/api/status, /api/test, /api/sleutels)."""

    def __init__(self, basis: str, time_out: int = TIME_OUT):
        self.basis = normaliseer_basis(basis)
        self.time_out = time_out
        # Eigen koekjespot: de zitting van het inloggen hoort hier, niet in de browser.
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        self._ingelogd = False

    @property
    def ingelogd(self) -> bool:
        return self._ingelogd

    def _verzoek(self, pad: str, methode: str = "GET", lichaam: dict | None = None,
                 time_out: int | None = None) -> tuple[int, dict]:
        url = self.basis + pad
        gegevens = None
        kop = {"Accept": "application/json"}
        if lichaam is not None:
            gegevens = json.dumps(lichaam).encode("utf-8")
            kop["Content-Type"] = "application/json; charset=utf-8"
        verzoek = urllib.request.Request(url, data=gegevens, headers=kop, method=methode)
        try:
            with self._opener.open(verzoek, timeout=time_out or self.time_out) as antwoord:
                ruw = antwoord.read().decode("utf-8", "replace")
                code = antwoord.status
        except urllib.error.HTTPError as fout:
            ruw = fout.read().decode("utf-8", "replace")
            code = fout.code
        except urllib.error.URLError as fout:
            raise BordFout(f"geen verbinding met {self.basis} ({fout.reason})") from fout
        except TimeoutError as fout:
            raise BordFout(f"{self.basis} antwoordde niet op tijd") from fout
        try:
            inhoud = json.loads(ruw) if ruw.strip() else {}
        except ValueError:
            inhoud = {"ruw": ruw[:400]}
        if not isinstance(inhoud, dict):
            inhoud = {"ruw": inhoud}
        if code >= 400:
            melding = str(inhoud.get("detail") or inhoud.get("error") or inhoud.get("ruw")
                          or f"het bord gaf foutcode {code}")
            if code == 401:
                self._ingelogd = False
                if melding.startswith("het bord gaf foutcode") or not melding.strip():
                    melding = "je bent niet (meer) ingelogd als beheerder"
            raise BordFout(melding)
        return code, inhoud

    def gezondheid(self) -> bool:
        try:
            self._verzoek("/gezondheid", time_out=6)
            return True
        except BordFout:
            return False

    def status(self) -> dict:
        _, inhoud = self._verzoek("/api/status")
        return inhoud

    def test_model(self, model: str, time_out: int = 240) -> dict:
        _, inhoud = self._verzoek("/api/test", "POST", {"model": model}, time_out=time_out)
        return inhoud

    def inloggen(self, wachtwoord: str) -> None:
        self._verzoek("/api/inloggen", "POST", {"wachtwoord": wachtwoord})
        self._ingelogd = True

    def uitloggen(self) -> None:
        try:
            self._verzoek("/api/uitloggen", "POST")
        except BordFout:
            pass
        self._ingelogd = False

    def sleutels(self) -> list[dict]:
        _, inhoud = self._verzoek("/api/sleutels", time_out=45)
        rijen = inhoud.get("sleutels")
        return [r for r in rijen if isinstance(r, dict)] if isinstance(rijen, list) else []

    def maak_sleutel(self, naam: str, budget: float | None = None) -> dict:
        lichaam: dict = {"naam": naam.strip()}
        if budget is not None:
            lichaam["budget"] = budget
        _, inhoud = self._verzoek("/api/sleutels", "POST", lichaam, time_out=90)
        return inhoud


# --------------------------------------------------------------------------- #
# Versies en bijwerken
# --------------------------------------------------------------------------- #
def versie_delen(tekst: str) -> tuple[int, ...]:
    """'1.2.10' -> (1, 2, 10). Onbekende stukken tellen als 0."""
    delen: list[int] = []
    for stuk in str(tekst or "").strip().lstrip("v").split("."):
        cijfers = "".join(t for t in stuk if t.isdigit())
        delen.append(int(cijfers) if cijfers else 0)
    return tuple(delen or [0])


def is_nieuwer(kandidaat: str, huidig: str) -> bool:
    nieuw, oud = versie_delen(kandidaat), versie_delen(huidig)
    lengte = max(len(nieuw), len(oud))
    return nieuw + (0,) * (lengte - len(nieuw)) > oud + (0,) * (lengte - len(oud))


@dataclass
class Bijwerking:
    """Wat de uitgave vertelt over de nieuwe versie."""

    versie: str
    bestand: str
    sha256: str
    url: str
    grootte: int | None = None

    @classmethod
    def uit_manifest(cls, gegevens: dict) -> "Bijwerking":
        versie = str(gegevens.get("versie") or "").strip()
        bestand = str(gegevens.get("bestand") or "").strip()
        controle = str(gegevens.get("sha256") or "").strip().lower()
        if not versie or not bestand or not controle:
            raise BordFout("het updatebestand is onvolledig (versie, bestand of sha256 ontbreekt)")
        url = str(gegevens.get("url") or "").strip() or manifest_basis() + "/" + urllib.parse.quote(bestand)
        grootte = gegevens.get("grootte")
        return cls(versie=versie, bestand=bestand, sha256=controle, url=url,
                   grootte=int(grootte) if str(grootte or "").isdigit() else None)


def manifest_basis() -> str:
    return f"https://github.com/{REPO}/releases/latest/download"


def lees_manifest(url: str | None = None, time_out: int = 20) -> dict:
    """Haalt version.json uit de nieuwste uitgave van de repo."""
    doel = url or f"{manifest_basis()}/{MANIFEST_NAAM}"
    verzoek = urllib.request.Request(doel, headers={
        "Accept": "application/json",
        "User-Agent": f"LLM-thuis-bureau/{VERSIE}",
        # De cache mag dit bestand niet vasthouden: anders zie je een oude versie.
        "Cache-Control": "no-cache",
    })
    try:
        with urllib.request.urlopen(verzoek, timeout=time_out) as antwoord:
            ruw = antwoord.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as fout:
        if fout.code == 404:
            raise BordFout(f"de nieuwste uitgave van {REPO} heeft geen {MANIFEST_NAAM}") from fout
        raise BordFout(f"de uitgavenlijst is niet op te halen (foutcode {fout.code})") from fout
    except urllib.error.URLError as fout:
        raise BordFout(f"geen verbinding met github.com ({fout.reason})") from fout
    try:
        gegevens = json.loads(ruw)
    except ValueError as fout:
        raise BordFout("het updatebestand is geen geldige JSON") from fout
    if not isinstance(gegevens, dict):
        raise BordFout("het updatebestand heeft een onverwachte vorm")
    return gegevens


def controleer_bijwerking(huidige: str = VERSIE, manifest: dict | None = None,
                          haal_op=lees_manifest) -> Bijwerking | None:
    """Geeft de nieuwe versie terug, of None als je al bij bent."""
    gegevens = manifest if manifest is not None else haal_op()
    nieuw = Bijwerking.uit_manifest(gegevens)
    return nieuw if is_nieuwer(nieuw.versie, huidige) else None


def sha256_van_bestand(pad: Path, blokgrootte: int = 1 << 20) -> str:
    hash_ = hashlib.sha256()
    with open(pad, "rb") as bestand:
        while True:
            brok = bestand.read(blokgrootte)
            if not brok:
                break
            hash_.update(brok)
    return hash_.hexdigest()


def haal_bijwerking_op(bijwerking: Bijwerking, doelmap: Path,
                       voortgang=None, time_out: int = 30) -> Path:
    """Haalt de nieuwe versie op en controleert de vingerafdruk (sha256).

    Klopt de vingerafdruk niet, dan verdwijnt het bestand weer en komt er niets in
    de plaats: een half binnengehaalde bijwerking mag nooit worden uitgepakt.
    """
    doelmap.mkdir(parents=True, exist_ok=True)
    doel = doelmap / bijwerking.bestand
    verzoek = urllib.request.Request(bijwerking.url, headers={
        "User-Agent": f"LLM-thuis-bureau/{VERSIE}", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(verzoek, timeout=time_out) as antwoord, open(doel, "wb") as uit:
            totaal = int(antwoord.headers.get("Content-Length") or 0)
            gedaan = 0
            begin = time.monotonic()
            while True:
                brok = antwoord.read(1 << 16)
                if not brok:
                    break
                uit.write(brok)
                gedaan += len(brok)
                if voortgang:
                    seconden = max(time.monotonic() - begin, 0.001)
                    voortgang(gedaan, totaal, gedaan / seconden)
    except (urllib.error.URLError, OSError) as fout:
        doel.unlink(missing_ok=True)
        raise BordFout(f"de nieuwe versie kwam niet binnen ({fout})") from fout

    gevonden = sha256_van_bestand(doel)
    if gevonden != bijwerking.sha256:
        doel.unlink(missing_ok=True)
        raise BordFout(f"de nieuwe versie is niet heel binnen: verwacht {bijwerking.sha256[:12]}…, "
                       f"gevonden {gevonden[:12]}… — er is niets vervangen")
    return doel


def kan_bijwerken() -> bool:
    """Alleen een ingepakte Windows-app kan zichzelf bijwerken."""
    return os.name == "nt" and bool(getattr(sys, "frozen", False))


def installatiemap() -> Path:
    """De map waarin de app zelf staat (dist-map bij een ingepakte app)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def maak_bijwerk_bat(proces_id: int, zip_pad: Path, installatie_map: Path, exe_pad: Path) -> str:
    """De tekst van het hulpbestand dat de bijwerking doorzet.

    Windows houdt de draaiende .exe vast, dus de app kan zichzelf niet vervangen.
    Dit script wacht tot het proces weg is, pakt de ZIP uit over de installatiemap,
    ruimt de ZIP op en start de nieuwe versie.
    """
    return f"""@echo off
rem LLM-thuis bijwerken: wacht op het einde van het programma, pak uit, start opnieuw.
setlocal
set "PID={proces_id}"
set "ZIP={zip_pad}"
set "MAP={installatie_map}"
set "EXE={exe_pad}"
:lus
tasklist /FI "PID eq %PID%" 2>nul | findstr /C:"%PID%" >nul
if not errorlevel 1 (
  timeout /t 1 /nobreak >nul
  goto lus
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%ZIP%' -DestinationPath '%MAP%' -Force"
if errorlevel 1 (
  echo De bijwerking kon niet worden uitgepakt. Pak %ZIP% zelf uit in %MAP%.
  pause
  exit /b 1
)
del "%ZIP%" >nul 2>&1
start "" "%EXE%"
exit /b 0
"""


def zet_bijwerking_door(bijwerking: Bijwerking, doelmap: Path | None = None,
                        start_hulp=subprocess.Popen) -> dict:
    """Haalt op, controleert, schrijft het hulpbestand en start het losgekoppeld.

    De app moet hierna zelf afsluiten; het hulpbestand wacht daarop.
    """
    if not kan_bijwerken():
        raise BordFout("deze versie draait niet als Windows-programma, dus bijwerken doet "
                       "de app niet zelf. Haal de nieuwe versie van de uitgavenpagina.")
    map_ = doelmap or (gegevensmap() / "bijwerking")
    zip_pad = haal_bijwerking_op(bijwerking, map_)
    exe_pad = Path(sys.executable).resolve()
    installatie = installatiemap()
    hulp = map_ / "bijwerken.bat"
    hulp.write_text(maak_bijwerk_bat(os.getpid(), zip_pad, installatie, exe_pad),
                    encoding="utf-8")
    try:
        start_hulp(["cmd.exe", "/c", str(hulp)], close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    except OSError as fout:
        raise BordFout(f"het hulpbestand kon niet starten ({fout})") from fout
    return {"versie": bijwerking.versie, "zip": str(zip_pad), "hulp": str(hulp)}


# --------------------------------------------------------------------------- #
# Controle van de inhoud van een bijwerkpakket (ook zonder Windows te toetsen)
# --------------------------------------------------------------------------- #
def controleer_pakket(zip_pad: Path, exe_naam: str) -> list[str]:
    """Leest het pakket na en noemt wat er mis is (leeg = goed)."""
    fouten: list[str] = []
    try:
        with zipfile.ZipFile(zip_pad) as pakket:
            kapot = pakket.testzip()
            if kapot:
                fouten.append(f"beschadigd bestand in het pakket: {kapot}")
            namen = [n.replace("\\", "/") for n in pakket.namelist()]
    except (OSError, zipfile.BadZipFile) as fout:
        return [f"het pakket is geen leesbare ZIP ({fout})"]
    if not any(n.split("/")[-1] == exe_naam for n in namen):
        fouten.append(f"het pakket mist {exe_naam}")
    if not any(n.split("/")[-1] == "version.txt" for n in namen):
        fouten.append("het pakket mist version.txt")
    if any(n.startswith("/") or ".." in n.split("/") for n in namen):
        fouten.append("het pakket bevat paden die buiten de installatiemap vallen")
    return fouten
