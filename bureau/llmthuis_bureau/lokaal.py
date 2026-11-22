"""LLM-thuis lokaal: bord én gateway op je eigen pc.

Deze module zet dezelfde pagina neer als het bord in je netwerk — dezelfde huisstijl,
dezelfde routes — maar haalt de modellen rechtstreeks bij een lokaal model (Ollama
standaard) of een andere OpenAI-achtige dienst. Sleutels met budget en verbruik
bewaart hij in een SQLite-bestand naast de app.

Zo is er voor "alles op één pc" geen Docker, geen PostgreSQL, geen LiteLLM en geen
Proxmox nodig: alleen de standaardbibliotheek van Python, het model naar keuze, en
dit programma.
"""

from __future__ import annotations

import http.client
import contextlib
import importlib.util
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STANDAARD_UPSTREAM = "http://127.0.0.1:11434/v1"
STANDAARD_POORT = 8100
ZITTINGDUUR = 8 * 3600
TEST_TIME_OUT = 300
STROOM_TIME_OUT = 900
VRAAG = "Zeg in één korte zin hallo."


class LokaalFout(Exception):
    """Iets ging mis bij het lokale bord; de tekst is voor de gebruiker."""


# --------------------------------------------------------------------------- #
# De pagina en de huisstijl komen uit bord.py, zodat er één gezicht is
# --------------------------------------------------------------------------- #
def vind_bord_bestand() -> Path | None:
    """Zoekt bord.py: naast de app, of meegebundeld in het programma."""
    kandidaten = [
        Path(getattr(sys, "_MEIPASS", "") or ".") / "bord" / "bord.py",
        Path(__file__).resolve().parent.parent.parent / "bord" / "bord.py",
        Path(getattr(sys, "_MEIPASS", "") or ".") / "bord.py",
        Path(__file__).resolve().parent / "bord.py",
    ]
    for pad in kandidaten:
        if pad.exists():
            return pad
    return None


_BORD_MODULE = None


def bord_module():
    """Laadt bord.py één keer: de pagina, de huisstijl en de wachtwoordcontrole komen daaruit."""
    global _BORD_MODULE
    if _BORD_MODULE is not None:
        return _BORD_MODULE
    pad = vind_bord_bestand()
    if pad is None:
        raise LokaalFout("bord/bord.py niet gevonden; de pagina kan niet worden geladen")
    specificatie = importlib.util.spec_from_file_location("llm_thuis_bord_pagina", pad)
    if specificatie is None or specificatie.loader is None:
        raise LokaalFout(f"{pad} kan niet worden geladen")
    module = importlib.util.module_from_spec(specificatie)
    specificatie.loader.exec_module(module)
    _BORD_MODULE = module
    return module


def laad_pagina() -> str:
    """Leest de pagina uit bord.py en vult de huisstijl in."""
    module = bord_module()
    pagina = str(getattr(module, "PAGINA", ""))
    huisstijl = str(getattr(module, "HUISSTIJL", ""))
    if not pagina or not huisstijl:
        raise LokaalFout("bord.py bevat geen pagina of huisstijl")
    return pagina.replace("__HUISSTIJL__", huisstijl)


def pas_pagina_aan(pagina: str, upstream: str) -> str:
    """Vervangt de verwijzingen naar LiteLLM door wat hier waar is."""
    adres = upstream.rstrip("/")
    if adres.endswith("/v1"):
        adres = adres[:-3]
    blok = (
        "<ul class=\"stappen\">"
        "<li>Je model draait <b>op deze pc</b> of ergens in je eigen netwerk; dit bord haalt het "
        f"rechtstreeks bij <code>{adres}</code>.</li>"
        "<li>Deze pagina zet alles wat naar <code>/v1/…</code> gaat door naar je model, zodat één "
        "adres volstaat voor al je apps. Er zit geen partij tussen.</li>"
        "<li>Sleutels met een budget worden hier bewaard, in een bestand naast het programma.</li>"
        "</ul>"
    )
    return re.sub(r'<ul class="stappen">.*?</ul>', blok, pagina, count=1, flags=re.S)


def vind_merk() -> Path | None:
    for pad in (
        Path(getattr(sys, "_MEIPASS", "") or ".") / "assets" / "merk.png",
        Path(__file__).resolve().parent.parent.parent / "assets" / "merk.png",
        Path(__file__).resolve().parent / "merk.png",
    ):
        if pad.exists():
            return pad
    return None


# --------------------------------------------------------------------------- #
# Sleutels met budget, in een SQLite-bestand
# --------------------------------------------------------------------------- #
class Sleutelboek:
    """Sleutels, budget en verbruik in één bestand — geen database-dienst nodig.

    Het verbruik wordt alleen in euro's geteld als er een prijs per 1000 tokens is
    ingesteld. Voor een model op je eigen machine is die prijs nul, en dan blijft er
    dus ook niets van je budget af: een lokaal model kost niets.
    """

    def __init__(self, pad: Path, prijs_per_1k: float = 0.0):
        self.pad = pad
        self.prijs_per_1k = float(prijs_per_1k or 0.0)
        self.pad.parent.mkdir(parents=True, exist_ok=True)
        self._maak_tabel()

    @contextlib.contextmanager
    def _verbind(self):
        """Verbinding die altijd weer sluit.

        'with sqlite3.connect(...)' legt alleen vast of terugdraait, het sluit de
        verbinding niet: die blijft dan het bestand vasthouden. Op Windows kan het
        bestand daardoor niet meer worden weggegooid of vervangen, dus sluiten we hem
        hier met de hand. Meteen ook de tabel aanmaken, zodat een weggegooid of nieuw
        bestand het sleutelbeheer niet stil laat stuklopen.
        """
        verbinding = sqlite3.connect(self.pad, timeout=10)
        verbinding.row_factory = sqlite3.Row
        try:
            verbinding.execute(
                "CREATE TABLE IF NOT EXISTS sleutels ("
                " sleutel TEXT PRIMARY KEY, naam TEXT NOT NULL, budget REAL,"
                " verbruik REAL NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0,"
                " aangemaakt TEXT NOT NULL)")
            with verbinding:
                yield verbinding
        finally:
            verbinding.close()

    def _maak_tabel(self) -> None:
        with self._verbind() as verbinding:
            verbinding.execute(
                "CREATE TABLE IF NOT EXISTS sleutels ("
                " sleutel TEXT PRIMARY KEY, naam TEXT NOT NULL, budget REAL,"
                " verbruik REAL NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0,"
                " aangemaakt TEXT NOT NULL)")

    def aantal(self) -> int:
        with self._verbind() as verbinding:
            rij = verbinding.execute("SELECT COUNT(*) AS n FROM sleutels").fetchone()
        return int(rij["n"])

    def alles(self) -> list[dict]:
        with self._verbind() as verbinding:
            rijen = verbinding.execute(
                "SELECT naam, budget, verbruik, tokens, aangemaakt FROM sleutels"
                " ORDER BY aangemaakt DESC").fetchall()
        return [{"naam": r["naam"], "budget": r["budget"], "verbruik": r["verbruik"],
                 "tokens": r["tokens"], "aangemaakt": r["aangemaakt"]} for r in rijen]

    def maak(self, naam: str, budget: float | None = None) -> dict:
        schoon = (naam or "").strip()
        if not schoon:
            raise LokaalFout("geef de sleutel een naam")
        sleutel = "sk-thuis-" + secrets.token_urlsafe(24)
        with self._verbind() as verbinding:
            verbinding.execute(
                "INSERT INTO sleutels (sleutel, naam, budget, aangemaakt) VALUES (?, ?, ?, ?)",
                (sleutel, schoon, budget, time.strftime("%Y-%m-%d")))
        return {"naam": schoon, "sleutel": sleutel, "budget": budget}

    def zoek(self, sleutel: str) -> dict | None:
        if not sleutel:
            return None
        with self._verbind() as verbinding:
            rij = verbinding.execute(
                "SELECT * FROM sleutels WHERE sleutel = ?", (sleutel,)).fetchone()
        return dict(rij) if rij else None

    def tel(self, sleutel: str, tokens: int) -> None:
        """Schrijft het verbruik van één aanroep bij de sleutel."""
        if not tokens or tokens < 0:
            return
        bedrag = (tokens / 1000.0) * self.prijs_per_1k
        with self._verbind() as verbinding:
            verbinding.execute(
                "UPDATE sleutels SET tokens = tokens + ?, verbruik = verbruik + ?"
                " WHERE sleutel = ?", (int(tokens), bedrag, sleutel))

    def keur(self, sleutel: str) -> tuple[int, str]:
        """Keurt een sleutel: (0, '') als hij mag, anders (foutcode, melding)."""
        rij = self.zoek(sleutel)
        if rij is None:
            return 401, "deze sleutel is hier niet bekend"
        budget = rij.get("budget")
        if budget is not None and float(rij.get("verbruik") or 0) >= float(budget):
            return 429, f"het budget van deze sleutel ({budget} euro) is op"
        return 0, ""


# --------------------------------------------------------------------------- #
# Het lokale bord
# --------------------------------------------------------------------------- #
class LokaalBord:
    """Het bord plus de doorgeefluik naar je eigen model, op deze pc."""

    def __init__(self, poort: int = STANDAARD_POORT, upstream: str = STANDAARD_UPSTREAM,
                 gegevensmap: Path | None = None, beheer_afdruk: str = "",
                 prijs_per_1k: float = 0.0, wachtwoord: str = ""):
        self.poort = int(poort)
        self.upstream = upstream.rstrip("/")
        if not self.upstream.endswith("/v1"):
            self.upstream += "/v1"
        self.gegevensmap = Path(gegevensmap) if gegevensmap else Path.cwd()
        self.beheer_afdruk = beheer_afdruk
        self.wachtwoord = wachtwoord
        self.sleutels = Sleutelboek(self.gegevensmap / "sleutels.sqlite3", prijs_per_1k)
        self.pagina = pas_pagina_aan(laad_pagina(), self.upstream)
        self.merk = vind_merk()
        self.zittingen: dict[str, dict] = {}
        self.laatste_test: dict[str, dict] = {}
        self.begonnen = time.time()
        self.server: ThreadingHTTPServer | None = None
        self.draad: threading.Thread | None = None

    # -- levensloop --------------------------------------------------------- #
    def start(self) -> str:
        """Start het bord op 127.0.0.1 en geeft het adres terug."""
        if self.server is not None:
            return self.basis_url
        self.server = ThreadingHTTPServer(("127.0.0.1", self.poort), Handler)
        self.server.daemon_threads = True
        self.server.lokaal = self  # type: ignore[attr-defined]
        self.poort = int(self.server.server_address[1])
        self.draad = threading.Thread(target=self.server.serve_forever,
                                      name="llm-thuis-lokaal", daemon=True)
        self.draad.start()
        return self.basis_url

    def stop(self) -> None:
        if self.server is not None:
            try:
                self.server.shutdown()
                self.server.server_close()
            finally:
                self.server = None
                self.draad = None

    @property
    def basis_url(self) -> str:
        return f"http://127.0.0.1:{self.poort}"

    # -- praten met het model ---------------------------------------------- #
    def _model_verzoek(self, pad: str, methode: str = "GET", lichaam: dict | None = None,
                       time_out: int = 60) -> tuple[int, bytes, dict]:
        adres = self.upstream + pad
        gegevens = json.dumps(lichaam).encode() if lichaam is not None else None
        verzoek = urllib.request.Request(
            adres, data=gegevens, method=methode,
            headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(verzoek, timeout=time_out) as antwoord:
                return antwoord.status, antwoord.read(), dict(antwoord.headers)
        except urllib.error.HTTPError as fout:
            return fout.code, fout.read(), dict(fout.headers or {})
        except (urllib.error.URLError, TimeoutError, OSError) as fout:
            raise LokaalFout(
                f"geen verbinding met je model op {self.upstream} ({fout})") from fout

    def modellen(self) -> tuple[bool, str, list[dict]]:
        try:
            code, ruw, _ = self._model_verzoek("/models", time_out=8)
        except LokaalFout as fout:
            return False, str(fout), []
        if code >= 400:
            return False, f"je model gaf foutcode {code}", []
        try:
            gegevens = json.loads(ruw.decode("utf-8", "replace"))
        except ValueError:
            return False, "je model gaf geen leesbaar antwoord", []
        rijen = gegevens.get("data") if isinstance(gegevens, dict) else None
        if not isinstance(rijen, list):
            return False, "je model gaf geen modellenlijst", []
        uit = []
        for rij in rijen:
            naam = str((rij or {}).get("id") or "").strip()
            if naam:
                uit.append({"naam": naam, "herkomst": "eigen",
                            "omschrijving": "draait lokaal", "laatste_test": self.laatste_test.get(naam)})
        return True, f"{self.upstream}", uit

    def test_model(self, naam: str) -> dict:
        begin = time.monotonic()
        code, ruw, _ = self._model_verzoek(
            "/chat/completions", "POST",
            {"model": naam, "messages": [{"role": "user", "content": VRAAG}],
             "max_tokens": 48, "temperature": 0},
            time_out=TEST_TIME_OUT)
        if code >= 400:
            raise LokaalFout(f"je model gaf foutcode {code}: {ruw[:200].decode('utf-8', 'replace')}")
        try:
            gegevens = json.loads(ruw.decode("utf-8", "replace"))
        except ValueError as fout:
            raise LokaalFout("je model gaf geen leesbaar antwoord") from fout
        keuze = (gegevens.get("choices") or [{}])[0]
        antwoord = str((keuze.get("message") or {}).get("content") or "").strip()
        verbruik = gegevens.get("usage") or {}
        seconden = round(time.monotonic() - begin, 1)
        uitslag = {"model": naam, "gelukt": True, "antwoord": antwoord, "seconden": seconden,
                   "ms": int(seconden * 1000), "tokens": verbruik.get("total_tokens"),
                   "tijd": time.strftime("%H:%M:%S")}
        self.laatste_test[naam] = uitslag
        return uitslag

    def status(self) -> dict:
        levend, detail, modellen = self.modellen()
        return {
            "versie": "lokaal",
            "gateway": self.upstream,
            "gateway_levend": levend,
            "gateway_detail": detail,
            "modellen": modellen,
            "sleutels_aan": True,
            "basis_url": f"{self.basis_url}/v1/",
            "ui_url": "",
            "draait_sinds": int(time.time() - self.begonnen),
            "lokaal": True,
        }

    # -- beheerder --------------------------------------------------------- #
    def wachtwoord_klopt(self, wachtwoord: str) -> bool:
        """Vergelijkt met de scrypt-afdruk (of met het ingestelde wachtwoord)."""
        if self.wachtwoord:
            return secrets.compare_digest(str(wachtwoord), str(self.wachtwoord))
        if not self.beheer_afdruk:
            return False
        try:
            return bool(bord_module().wachtwoord_klopt_met(self.beheer_afdruk, str(wachtwoord)))
        except Exception:  # noqa: BLE001 - geen afdruk = geen beheer
            return False

    # -- sleutels ---------------------------------------------------------- #
    def maak_sleutel(self, lichaam: dict) -> dict:
        ruw = lichaam.get("budget")
        budget = None
        if ruw not in (None, "", "leeg"):
            try:
                budget = float(str(ruw).replace(",", "."))
            except ValueError as fout:
                raise LokaalFout("het budget moet een getal zijn, bijvoorbeeld 5") from fout
            if budget <= 0:
                raise LokaalFout("het budget moet groter dan nul zijn")
        return self.sleutels.maak(str(lichaam.get("naam") or ""), budget)

    # -- doorsturen -------------------------------------------------------- #
    def stuur_door(self, handler: BaseHTTPRequestHandler, methode: str) -> None:
        lokaal = self
        pad = handler.path
        lengte = int(handler.headers.get("Content-Length") or 0)
        lichaam = handler.rfile.read(lengte) if lengte else None

        sleutel = ""
        kop = handler.headers.get("Authorization") or ""
        if kop.lower().startswith("bearer "):
            sleutel = kop[7:].strip()

        # Zonder sleutels is het bord open: dat is het eerste gebruik op je eigen pc.
        if lokaal.sleutels.aantal() > 0:
            code, waarom = lokaal.sleutels.keur(sleutel)
            if code:
                melding = "er is geen sleutel meegegeven" if not sleutel else waarom
                handler._json({"error": melding}, code)
                return

        doel = urllib.parse.urlsplit(lokaal.upstream)
        verbinding = http.client.HTTPConnection(doel.hostname, doel.port or 80,
                                               timeout=STROOM_TIME_OUT)
        doorgeef = {}
        for naam, waarde in handler.headers.items():
            if naam.lower() in ("host", "connection", "content-length", "authorization",
                                "accept-encoding"):
                continue
            doorgeef[naam] = waarde
        if lichaam is not None:
            doorgeef["Content-Length"] = str(len(lichaam))
        pad_heen = doel.path.rstrip("/") + pad
        try:
            verbinding.request(methode, pad_heen, body=lichaam, headers=doorgeef)
            antwoord = verbinding.getresponse()
        except (OSError, http.client.HTTPException) as fout:
            handler._json({"error": f"geen verbinding met je model op {lokaal.upstream} ({fout})"}, 502)
            return

        lengte_heen = antwoord.getheader("Content-Length")
        handler.send_response(antwoord.status)
        for naam, waarde in antwoord.getheaders():
            if naam.lower() in ("content-length", "transfer-encoding", "connection", "keep-alive"):
                continue
            handler.send_header(naam, waarde)
        if lengte_heen is not None:
            handler.send_header("Content-Length", lengte_heen)
        else:
            handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()

        brokken: list[bytes] = []
        try:
            while True:
                brok = antwoord.read(4096)
                if not brok:
                    break
                if lengte_heen is None:
                    handler.wfile.write(f"{len(brok):X}\r\n".encode() + brok + b"\r\n")
                else:
                    handler.wfile.write(brok)
                handler.wfile.flush()
                if len(brokken) * 4096 < 4_000_000:  # alleen de eerste brokken bewaren
                    brokken.append(brok)
            if lengte_heen is None:
                handler.wfile.write(b"0\r\n\r\n")
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                verbinding.close()
            except Exception:  # noqa: BLE001
                pass

        if sleutel:
            lokaal.sleutels.tel(sleutel, _tokens_uit(b"".join(brokken)))


def _tokens_uit(ruw: bytes) -> int:
    """Haalt het aantal tokens uit een antwoord (ook uit de laatste brok van een stroom)."""
    if not ruw:
        return 0
    tekst = ruw.decode("utf-8", "replace")
    totaal = 0
    for stuk in tekst.split("data: "):
        stuk = stuk.strip()
        if not stuk or stuk == "[DONE]":
            continue
        try:
            gegevens = json.loads(stuk)
        except ValueError:
            continue
        if isinstance(gegevens, dict):
            verbruik = gegevens.get("usage") or {}
            if isinstance(verbruik, dict) and verbruik.get("total_tokens"):
                totaal = int(verbruik["total_tokens"])
    return totaal


# --------------------------------------------------------------------------- #
# De routes (dezelfde als het bord, zodat de pagina onveranderd werkt)
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "llm-thuis-lokaal/1.0"

    def log_message(self, format, *args):  # noqa: A002 - stdlib-signatuur
        pass

    @property
    def lokaal(self) -> LokaalBord:
        return getattr(self.server, "lokaal")

    def _stuur(self, code: int, soort: str, body: bytes, extra: dict | None = None) -> None:
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

    def _json(self, gegevens, code: int = 200, extra: dict | None = None) -> None:
        self._stuur(code, "application/json; charset=utf-8",
                    json.dumps(gegevens, ensure_ascii=False).encode("utf-8"), extra)

    def _fout(self, melding: str, code: int = 400) -> None:
        self._json({"error": melding}, code)

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

    def _beheerder(self) -> bool:
        ruw = self.headers.get("Cookie") or ""
        if not ruw:
            return False
        try:
            koek = SimpleCookie(ruw)
        except Exception:  # noqa: BLE001
            return False
        m = koek.get("thuis_zitting")
        if not m:
            return False
        gegevens = self.lokaal.zittingen.get(m.value)
        if not gegevens:
            return False
        if time.time() - gegevens.get("sinds", 0) > ZITTINGDUUR:
            self.lokaal.zittingen.pop(m.value, None)
            return False
        return True

    def _eist_beheerder(self) -> bool:
        if self._beheerder():
            return True
        self._fout("log eerst in als beheerder", 401)
        return False

    def do_GET(self):  # noqa: N802
        pad = urllib.parse.urlparse(self.path).path
        if pad in ("/", "/index.html"):
            return self._stuur(200, "text/html; charset=utf-8", self.lokaal.pagina.encode("utf-8"))
        if pad in ("/merk.png", "/logo.png"):
            if self.lokaal.merk:
                return self._stuur(200, "image/png", self.lokaal.merk.read_bytes(),
                                   {"Cache-Control": "public, max-age=300"})
            return self._fout("geen merk", 404)
        if pad == "/gezondheid":
            return self._stuur(200, "text/plain; charset=utf-8", b"ok\n")
        if pad == "/api/status":
            return self._json(self.lokaal.status())
        if pad == "/api/sleutels":
            if not self._eist_beheerder():
                return
            try:
                return self._json({"sleutels": self.lokaal.sleutels.alles()})
            except sqlite3.Error as fout:
                return self._fout(f"het sleutelbestand is niet te lezen ({fout})", 500)
        if pad.startswith("/v1/"):
            return self.lokaal.stuur_door(self, "GET")
        return self._fout("onbekende route", 404)

    def do_POST(self):  # noqa: N802
        pad = urllib.parse.urlparse(self.path).path
        if pad == "/api/test":
            naam = str(self._lichaam().get("model") or "").strip()
            if not naam:
                return self._fout("geen model opgegeven")
            try:
                return self._json(self.lokaal.test_model(naam))
            except LokaalFout as fout:
                return self._fout(str(fout), 502)
        if pad == "/api/inloggen":
            if not self.lokaal.beheer_afdruk and not self.lokaal.wachtwoord:
                return self._fout("er is nog geen beheerderswachtwoord ingesteld", 503)
            if not self.lokaal.wachtwoord_klopt(str(self._lichaam().get("wachtwoord") or "")):
                return self._fout("dat wachtwoord klopt niet", 401)
            token = secrets.token_urlsafe(32)
            self.lokaal.zittingen[token] = {"beheerder": True, "sinds": time.time()}
            return self._json({"ok": True}, extra={
                "Set-Cookie": f"thuis_zitting={token}; Path=/; HttpOnly; SameSite=Lax;"
                              f" Max-Age={ZITTINGDUUR}"})
        if pad == "/api/uitloggen":
            self.lokaal.zittingen.clear()
            return self._json({"ok": True}, extra={
                "Set-Cookie": "thuis_zitting=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"})
        if pad == "/api/sleutels":
            if not self._eist_beheerder():
                return
            try:
                return self._json(self.lokaal.maak_sleutel(self._lichaam()))
            except LokaalFout as fout:
                return self._fout(str(fout), 502)
            except sqlite3.Error as fout:
                return self._fout(f"het sleutelbestand is niet te schrijven ({fout})", 500)
        if pad.startswith("/v1/"):
            return self.lokaal.stuur_door(self, "POST")
        return self._fout("onbekende route", 404)
