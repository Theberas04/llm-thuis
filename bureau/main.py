#!/usr/bin/env python3
"""LLM-thuis — bureaubladapp voor je eigen LLM-gateway.

Start zonder argumenten het venster. Met --selftest controleert de app zichzelf en
schrijft het verslag naar het opgegeven bestand; dat is de proef die in de bouwstraat
op de ingepakte Windows-versie wordt losgelaten, want alleen die vertelt of het
programma werkt zoals het is afgeleverd.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback
import zipfile
from pathlib import Path

HIER = Path(__file__).resolve().parent
if str(HIER) not in sys.path:
    sys.path.insert(0, str(HIER))

from llmthuis_bureau import core  # noqa: E402
from llmthuis_bureau.version import VERSIE  # noqa: E402


class Verslag:
    """Schrijft elke stap meteen weg: een verslag dat pas aan het eind wordt
    opgebouwd is leeg precies wanneer het programma omvalt."""

    def __init__(self, pad: Path | None):
        self.pad = pad
        self.gegevens: dict = {
            "ok": False, "versie": VERSIE, "stappen": [], "fouten": [],
            "python": sys.version.split()[0], "platform": sys.platform,
            "ingepakt": bool(getattr(sys, "frozen", False)),
            "venster": False, "bijwerken_mogelijk": core.kan_bijwerken(),
        }
        self.schrijf()

    def schrijf(self) -> None:
        if not self.pad:
            return
        try:
            self.pad.write_text(json.dumps(self.gegevens, indent=2, ensure_ascii=False),
                                encoding="utf-8")
        except OSError:
            pass

    def stap(self, naam: str, **extra) -> None:
        self.gegevens["stappen"].append(naam)
        if extra:
            self.gegevens.setdefault("bijzonderheden", {}).update(extra)
        self.schrijf()

    def fout(self, tekst: str) -> None:
        self.gegevens["fouten"].append(tekst)
        self.schrijf()

    def klaar(self, ok: bool) -> None:
        self.gegevens["ok"] = ok
        self.schrijf()


def log(tekst: str) -> None:
    """Printen kan niet in een venster-app zonder console."""
    try:
        if sys.stdout:
            print(tekst, flush=True)
    except (ValueError, OSError, AttributeError):
        pass


def proef_kern(verslag: Verslag) -> None:
    """Toetst de rekenkundige kern: versies, adressen, pakketcontrole."""
    if core.normaliseer_basis("192.168.1.100") != "http://192.168.1.100":
        raise AssertionError("normaliseer_basis maakt geen adres van '192.168.1.100'")
    if core.normaliseer_basis("http://bord.lokaal/") != "http://bord.lokaal":
        raise AssertionError("normaliseer_basis laat een schuine streep staan")
    if not core.is_nieuwer("1.0.1", "1.0.0"):
        raise AssertionError("is_nieuwer ziet 1.0.1 niet als nieuwer dan 1.0.0")
    if core.is_nieuwer("1.0.0", "1.0.0"):
        raise AssertionError("is_nieuwer vindt dezelfde versie nieuwer")
    if not core.is_nieuwer("1.10.0", "1.9.9"):
        raise AssertionError("is_nieuwer vergelijkt niet als getallen")
    verslag.stap("kern")

    dicht = core.maak_bijwerk_bat(4242, Path(r"C:\tmp\nieuw.zip"),
                                  Path(r"C:\Apps\LLM-thuis"), Path(r"C:\Apps\LLM-thuis\LLM-thuis.exe"))
    for verwacht in ("4242", r"C:\tmp\nieuw.zip", r"C:\Apps\LLM-thuis", "Expand-Archive", "tasklist"):
        if verwacht not in dicht:
            raise AssertionError(f"het bijwerkscript mist '{verwacht}'")
    verslag.stap("bijwerkscript")

    # Een pakket in elkaar zetten en meteen weer keuren: zo is de controle zelf getoetst.
    map_ = Path(tempfile.mkdtemp(prefix="llm-thuis-proef-"))
    pakket = map_ / "proef.zip"
    with zipfile.ZipFile(pakket, "w") as z:
        z.writestr("LLM-thuis.exe", b"nep")
        z.writestr("version.txt", "9.9.9\n")
    fouten = core.controleer_pakket(pakket, "LLM-thuis.exe")
    if fouten:
        raise AssertionError(f"controleer_pakket keurt een goed pakket af: {fouten}")
    with zipfile.ZipFile(map_ / "kwaad.zip", "w") as z:
        z.writestr("../buiten.exe", b"nep")
        z.writestr("version.txt", "1.0.0\n")
    if not core.controleer_pakket(map_ / "kwaad.zip", "LLM-thuis.exe"):
        raise AssertionError("controleer_pakket laat paden buiten de installatiemap door")
    verslag.stap("pakketcontrole")


def proef_instellingen(verslag: Verslag) -> None:
    """Instellingen moeten in de map van de gebruiker landen, niet in de app zelf."""
    map_ = Path(tempfile.mkdtemp(prefix="llm-thuis-instellingen-"))
    os.environ["APPDATA"] = str(map_)
    core.bewaar_instellingen({"basis": "http://192.168.1.100"})
    terug = core.lees_instellingen()
    if terug.get("basis") != "http://192.168.1.100":
        raise AssertionError("de instellingen komen niet terug")
    if "192.168.1.100" not in (map_ / "LLM-thuis" / "instellingen.json").read_text(encoding="utf-8"):
        raise AssertionError("de instellingen staan niet in de map van de gebruiker")
    verslag.stap("instellingen")


def proef_venster(verslag: Verslag) -> None:
    """Bouwt het echte venster en breekt het weer af.

    In HEADLESS-modus komt er geen venster met een knop die op een klik wacht, dus
    dit kan in de bouwstraat zonder dat iemand erbij is.
    """
    os.environ["LLM_THUIS_HEADLESS"] = "1"
    import tkinter as tk
    from llmthuis_bureau.ui import Venster

    hoofd = tk.Tk()
    venster = Venster(hoofd, basis="http://192.168.1.100", headless=True)
    hoofd.update_idletasks()
    hoofd.update()
    if not venster.modelboom.winfo_exists():
        raise AssertionError("de modellenlijst is niet gebouwd")
    if not venster.sleutelboom.winfo_exists():
        raise AssertionError("de sleutellijst is niet gebouwd")
    # Een ronde door de pomp: precies het pad dat een echt antwoord ook neemt.
    venster.wachtrij.put(("proef", True, "ok", lambda _u: venster.stap("pomp")))
    hoofd.after(200, venster._pomp)
    einde = time.monotonic() + 3
    while time.monotonic() < einde:
        hoofd.update()
        time.sleep(0.05)
        if "pomp" in venster.stappen:
            break
    if "pomp" not in venster.stappen:
        raise AssertionError("de wachtrij van het venster wordt niet verwerkt")
    venster.sluit()
    verslag.gegevens["venster"] = True
    verslag.stap("venster")


def proef_lokaal(verslag: Verslag) -> None:
    """Toetst dat het ingepakte programma het bord op deze pc kan neerzetten.

    Dit is de kern van "alles op één pc": dezelfde pagina als de website, geserveerd
    door het programma zelf, met de sleutels in een bestand naast de app.
    """
    import urllib.request
    from llmthuis_bureau import lokaal

    bord = lokaal.LokaalBord(poort=0, upstream="http://127.0.0.1:1/v1",
                            gegevensmap=Path(tempfile.mkdtemp(prefix="llm-thuis-proef-lokaal-")),
                            wachtwoord="proef-wachtwoord")
    adres = bord.start()
    try:
        with urllib.request.urlopen(adres + "/api/status", timeout=20) as antwoord:
            gegevens = json.loads(antwoord.read().decode("utf-8"))
        if not gegevens.get("lokaal"):
            raise AssertionError("het lokale bord meldt zich niet als lokaal")
        with urllib.request.urlopen(adres + "/", timeout=20) as antwoord:
            html = antwoord.read().decode("utf-8", "replace")
        if "LLM-thuis" not in html:
            raise AssertionError("de pagina van het lokale bord is niet de bordpagina")
        if "LiteLLM" in html:
            raise AssertionError("de lokale pagina verwijst nog naar LiteLLM")
        if bord.sleutels.aantal() != 0:
            raise AssertionError("een vers bord hoort nog geen sleutels te hebben")
        verslag.gegevens.setdefault("bijzonderheden", {})["lokaal_adres"] = adres
    finally:
        bord.stop()
    verslag.stap("lokaal")


def doe_selftest(pad: Path | None) -> int:
    verslag = Verslag(pad)
    try:
        verslag.stap("start")
        verslag.stap(f"versie:{VERSIE}")
        proef_kern(verslag)
        proef_instellingen(verslag)
        proef_venster(verslag)
        proef_lokaal(verslag)
        proef_openbaar(verslag)
    except Exception as fout:  # noqa: BLE001 - een venster-app zonder console moet
        # zijn eigen fout opschrijven, anders leest een crash als "de bouw hangt".
        verslag.fout(f"{type(fout).__name__}: {fout}")
        verslag.fout(traceback.format_exc())
        verslag.klaar(False)
        log("zelftest mislukt:\n" + traceback.format_exc())
        return 1
    verslag.klaar(True)
    log(f"zelftest geslaagd, versie {VERSIE}: " + " -> ".join(verslag.gegevens["stappen"]))
    return 0


def proef_openbaar(verslag: Verslag) -> None:
    """Kijkt of de nieuwste uitgave van de repo te vinden is.

    Dit is een *zachte* controle: zonder internet hoort de app bruikbaar te blijven.
    """
    try:
        manifest = core.lees_manifest(time_out=15)
        nieuw = core.controleer_bijwerking(VERSIE, manifest=manifest)
    except core.BordFout as fout:
        verslag.stap("openbaar-onbekend")
        verslag.gegevens.setdefault("bijzonderheden", {})["openbaar"] = str(fout)
        log(f"de uitgavenlijst is niet op te halen ({fout}) — geen fout voor de app")
        return
    verslag.stap("openbaar")
    verslag.gegevens.setdefault("bijzonderheden", {})["nieuwste"] = manifest.get("versie")
    verslag.gegevens["bijzonderheden"]["bijwerking"] = bool(nieuw)
    log(f"nieuwste versie in de uitgave: {manifest.get('versie')}")


def main() -> int:
    argumenten = sys.argv[1:]
    if "--versie" in argumenten or "-v" in argumenten:
        log(VERSIE)
        return 0

    pad: Path | None = None
    if "--selftest" in argumenten:
        plek = argumenten.index("--selftest")
        if plek + 1 < len(argumenten) and not argumenten[plek + 1].startswith("--"):
            pad = Path(argumenten[plek + 1])
        os.environ["LLM_THUIS_HEADLESS"] = "1"
        return doe_selftest(pad)

    if "--lokaal" in argumenten:
        # Alleen het bord en het doorgeefluik, zonder venster. Handig op een pc of
        # server waar je het bord wilt laten lopen zonder programma op het scherm.
        from llmthuis_bureau import lokaal

        poort = lokaal.STANDAARD_POORT
        if "--poort" in argumenten:
            plek = argumenten.index("--poort")
            if plek + 1 < len(argumenten):
                poort = int(argumenten[plek + 1])
        upstream = lokaal.STANDAARD_UPSTREAM
        if "--model" in argumenten:
            plek = argumenten.index("--model")
            if plek + 1 < len(argumenten):
                upstream = argumenten[plek + 1]
        # Het beheerderswachtwoord mag uit de omgeving komen (THUIS_BEHEER_HASH) of uit
        # een beheer.env naast de app; zonder dat is alleen het beheer dicht.
        afdruk = os.environ.get("THUIS_BEHEER_HASH", "")
        if not afdruk:
            bestand = Path.cwd() / "beheer.env"
            try:
                for regel in bestand.read_text(encoding="utf-8").splitlines():
                    if regel.strip().startswith("THUIS_BEHEER_HASH="):
                        afdruk = regel.split("=", 1)[1].strip()
            except OSError:
                afdruk = ""
        bord = lokaal.LokaalBord(poort=poort, upstream=upstream, beheer_afdruk=afdruk)
        log(f"Lokaal bord op {bord.start()} — model op {bord.upstream} (stop met Ctrl+C)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            bord.stop()
        return 0

    if "--controleer-bijwerking" in argumenten:
        try:
            nieuw = core.controleer_bijwerking(VERSIE, manifest=core.lees_manifest())
        except core.BordFout as fout:
            log(f"bijwerken controleren lukte niet: {fout}")
            return 2
        if nieuw:
            log(f"nieuwe versie beschikbaar: {nieuw.versie} ({nieuw.bestand})")
            return 0
        log(f"je hebt de nieuwste versie ({VERSIE})")
        return 0

    # Gewoon starten: het venster.
    import tkinter as tk
    from llmthuis_bureau.ui import Venster

    basis = None
    if "--basis" in argumenten:
        plek = argumenten.index("--basis")
        if plek + 1 < len(argumenten):
            basis = argumenten[plek + 1]
    hoofd = tk.Tk()
    try:
        Venster(hoofd, basis=basis)
    except Exception:  # noqa: BLE001 - een fout bij het starten hoort zichtbaar te zijn
        traceback.print_exc()
        try:
            from tkinter import messagebox
            messagebox.showerror("LLM-thuis", "De app kon niet starten:\n\n" + traceback.format_exc())
        except Exception:  # noqa: BLE001
            pass
        return 1
    hoofd.mainloop()
    return 0


if __name__ == "__main__":
    code = main()
    # os._exit: een venster-app kan blijven hangen in het opruimen van de tolk.
    try:
        if sys.stdout:
            sys.stdout.flush()
    except (ValueError, OSError, AttributeError):
        pass
    os._exit(code)
