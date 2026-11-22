#!/usr/bin/env python3
"""Proeven voor de lokale stand: bord én gateway op dezelfde pc.

Er staat een nagemaakt model (Ollama-achtig) tegenover, zodat de doorgeefroute,
de sleutelcontrole en het budget echt worden uitgeoefend.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HIER = Path(__file__).resolve().parent
sys.path.insert(0, str(HIER.parent))

from llmthuis_bureau import lokaal  # noqa: E402

ANTWOORD = "Hallo! Alles werkt lokaal."
TOKENS = 12


class ModelHandler(BaseHTTPRequestHandler):
    """Bootst een lokaal model na (de OpenAI-route van Ollama)."""

    protocol_version = "HTTP/1.1"
    aanroepen: list[dict] = []

    def log_message(self, format, *args):  # noqa: A002
        pass

    def _json(self, gegevens: dict, code: int = 200) -> None:
        brok = json.dumps(gegevens).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(brok)))
        self.end_headers()
        self.wfile.write(brok)

    def do_GET(self):  # noqa: N802
        if self.path.endswith("/models"):
            return self._json({"object": "list", "data": [
                {"id": "lokaal-proefmodel", "object": "model"},
                {"id": "nog-een-model", "object": "model"}]})
        return self._json({"error": "onbekend"}, 404)

    def do_POST(self):  # noqa: N802
        lengte = int(self.headers.get("Content-Length") or 0)
        lichaam = json.loads(self.rfile.read(lengte) or b"{}")
        type(self).aanroepen.append({"pad": self.path, "lichaam": lichaam,
                                     "sleutel": self.headers.get("Authorization")})
        if self.path.endswith("/chat/completions"):
            return self._json({
                "id": "proef", "model": lichaam.get("model"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": ANTWOORD}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 8, "total_tokens": TOKENS}})
        return self._json({"error": "onbekend"}, 404)


class ProefLokaal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model_server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
        cls.model_server.daemon_threads = True
        threading.Thread(target=cls.model_server.serve_forever, daemon=True).start()
        cls.upstream = f"http://127.0.0.1:{cls.model_server.server_address[1]}/v1"

        cls.map_ = Path(tempfile.mkdtemp(prefix="llm-thuis-lokaal-"))
        cls.bord = lokaal.LokaalBord(poort=0, upstream=cls.upstream, gegevensmap=cls.map_,
                                     wachtwoord="proef-wachtwoord")
        cls.adres = cls.bord.start()

    @classmethod
    def tearDownClass(cls):
        cls.bord.stop()
        cls.model_server.shutdown()

    # -- klein grut --------------------------------------------------------- #
    def _vraag(self, pad: str, methode: str = "GET", lichaam=None, sleutel: str | None = None,
               opening=None):
        gegevens = json.dumps(lichaam).encode() if lichaam is not None else None
        verzoek = urllib.request.Request(self.adres + pad, data=gegevens, method=methode,
                                         headers={"Content-Type": "application/json"})
        if sleutel:
            verzoek.add_header("Authorization", f"Bearer {sleutel}")
        opener = opening or urllib.request.build_opener()
        try:
            with opener.open(verzoek, timeout=30) as antwoord:
                return antwoord.status, antwoord.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as fout:
            return fout.code, fout.read().decode("utf-8", "replace")

    def test_pagina_wordt_geserveerd_in_dezelfde_huisstijl(self):
        code, html = self._vraag("/")
        self.assertEqual(code, 200)
        self.assertIn("LLM-thuis", html)
        self.assertIn("--merk:linear-gradient", html)          # huisstijl uit bord.py
        self.assertIn("Je model draait <b>op deze pc</b>", html)  # lokale uitleg
        self.assertNotIn("LiteLLM", html)                      # geen verwijzing meer
        self.assertNotIn("__HUISSTIJL__", html)

    def test_gezondheid(self):
        code, tekst = self._vraag("/gezondheid")
        self.assertEqual(code, 200)
        self.assertIn("ok", tekst)

    def test_status_toont_de_modellen_van_je_eigen_machine(self):
        code, ruw = self._vraag("/api/status")
        self.assertEqual(code, 200)
        gegevens = json.loads(ruw)
        self.assertTrue(gegevens["gateway_levend"])
        self.assertTrue(gegevens["lokaal"])
        namen = [m["naam"] for m in gegevens["modellen"]]
        self.assertEqual(namen, ["lokaal-proefmodel", "nog-een-model"])
        self.assertTrue(all(m["herkomst"] == "eigen" for m in gegevens["modellen"]))
        self.assertTrue(gegevens["basis_url"].endswith("/v1/"))

    def test_model_testen_gaat_echt_naar_het_model(self):
        code, ruw = self._vraag("/api/test", "POST", {"model": "lokaal-proefmodel"})
        self.assertEqual(code, 200)
        uitslag = json.loads(ruw)
        self.assertTrue(uitslag["gelukt"])
        self.assertEqual(uitslag["antwoord"], ANTWOORD)
        self.assertEqual(uitslag["tokens"], TOKENS)
        self.assertTrue(ModelHandler.aanroepen[-1]["lichaam"]["model"] == "lokaal-proefmodel")

    def test_sleutels_alleen_na_inloggen(self):
        code, _ = self._vraag("/api/sleutels")
        self.assertEqual(code, 401)
        code, _ = self._vraag("/api/inloggen", "POST", {"wachtwoord": "fout"})
        self.assertEqual(code, 401)

    def test_sleutel_maken_en_gebruiken(self):
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        code, _ = self._vraag("/api/inloggen", "POST", {"wachtwoord": "proef-wachtwoord"},
                              opening=opener)
        self.assertEqual(code, 200)
        code, ruw = self._vraag("/api/sleutels", opening=opener)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(ruw)["sleutels"], [])

        code, ruw = self._vraag("/api/sleutels", "POST", {"naam": "thuisapp", "budget": "5"},
                                opening=opener)
        self.assertEqual(code, 200)
        nieuw = json.loads(ruw)
        self.assertTrue(nieuw["sleutel"].startswith("sk-thuis-"))

        code, ruw = self._vraag("/api/sleutels", opening=opener)
        rijen = json.loads(ruw)["sleutels"]
        self.assertEqual(rijen[0]["naam"], "thuisapp")
        self.assertEqual(rijen[0]["budget"], 5.0)

        # Vanaf nu is er een sleutel: zonder sleutel is de deur dicht.
        code, ruw = self._vraag("/v1/chat/completions", "POST",
                                {"model": "lokaal-proefmodel", "messages": []})
        self.assertEqual(code, 401)
        self.assertIn("geen sleutel", ruw)
        code, ruw = self._vraag("/v1/chat/completions", "POST",
                                {"model": "lokaal-proefmodel", "messages": []}, sleutel="sk-fout")
        self.assertEqual(code, 401)
        code, ruw = self._vraag("/v1/chat/completions", "POST",
                                {"model": "lokaal-proefmodel", "messages": []},
                                sleutel=nieuw["sleutel"])
        self.assertEqual(code, 200)
        self.assertIn(ANTWOORD, ruw)

    def test_budget_houdt_tegen_als_je_een_prijs_instelt(self):
        map_ = Path(tempfile.mkdtemp(prefix="llm-thuis-budget-"))
        bord = lokaal.LokaalBord(poort=0, upstream=self.upstream, gegevensmap=map_,
                                 wachtwoord="proef", prijs_per_1k=1000.0)
        adres = bord.start()
        try:
            sleutel = bord.maak_sleutel({"naam": "krappe-sleutel", "budget": "1"})
            verzoek = urllib.request.Request(
                adres + "/v1/chat/completions", data=b"{}", method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {sleutel['sleutel']}"})
            with urllib.request.urlopen(verzoek, timeout=30) as antwoord:
                self.assertEqual(antwoord.status, 200)
            # 12 tokens bij een prijs van 1000 euro per 1000 tokens = 12 euro: budget op.
            try:
                urllib.request.urlopen(verzoek, timeout=30)
                self.fail("de tweede aanroep had geweigerd moeten worden")
            except urllib.error.HTTPError as fout:
                self.assertEqual(fout.code, 429)
                self.assertIn("budget", fout.read().decode())
            self.assertEqual(bord.sleutels.alles()[0]["tokens"], 12)
        finally:
            bord.stop()

    def test_verbruik_blijft_nul_zonder_prijs(self):
        # Een model op je eigen machine kost niets: het budget loopt niet vol.
        sleutel = self.bord.maak_sleutel({"naam": "gratis", "budget": "1"})
        code, _ = self._vraag("/v1/chat/completions", "POST",
                              {"model": "lokaal-proefmodel", "messages": []},
                              sleutel=sleutel["sleutel"])
        self.assertEqual(code, 200)
        # Het verbruik wordt bijgeschreven nadat het antwoord bij de client is: even wachten.
        rij = self._wacht_op_rij("gratis")
        self.assertEqual(rij["tokens"], TOKENS)
        self.assertEqual(rij["verbruik"], 0.0)

    def _wacht_op_rij(self, naam: str, tijd: float = 3.0) -> dict:
        einde = time.monotonic() + tijd
        rij: dict | None = None
        while time.monotonic() < einde:
            gevonden = [r for r in self.bord.sleutels.alles() if r["naam"] == naam]
            if gevonden and gevonden[0]["tokens"]:
                rij = gevonden[0]
                break
            time.sleep(0.05)
        if rij is None:
            gevonden = [r for r in self.bord.sleutels.alles() if r["naam"] == naam]
            self.assertTrue(gevonden, f"sleutel '{naam}' staat niet in het bestand")
            rij = gevonden[0]
        return rij


class ProefWeggegooidBestand(unittest.TestCase):
    """Een verdwenen sleutelbestand mag het beheer niet stil laten stuklopen."""

    def test_bestand_weggooien_onder_een_draaiend_bord(self):
        map_ = Path(tempfile.mkdtemp(prefix="llm-thuis-weg-"))
        bord = lokaal.LokaalBord(poort=0, upstream="http://127.0.0.1:1/v1", gegevensmap=map_,
                                 wachtwoord="proef")
        adres = bord.start()
        try:
            (map_ / "sleutels.sqlite3").unlink()
            bord.sleutels.maak("nieuw", 1.0)
            self.assertEqual([r["naam"] for r in bord.sleutels.alles()], ["nieuw"])

            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
            verzoek = urllib.request.Request(
                adres + "/api/inloggen", data=json.dumps({"wachtwoord": "proef"}).encode(),
                method="POST", headers={"Content-Type": "application/json"})
            with opener.open(verzoek, timeout=20) as antwoord:
                self.assertEqual(antwoord.status, 200)
            with opener.open(adres + "/api/sleutels", timeout=20) as antwoord:
                gegevens = json.loads(antwoord.read())
            self.assertEqual([r["naam"] for r in gegevens["sleutels"]], ["nieuw"])
        finally:
            bord.stop()


class ProefSleutelboek(unittest.TestCase):
    """Het sleutelbestand zelf, zonder server ertussen."""

    def setUp(self):
        self.map_ = Path(tempfile.mkdtemp(prefix="llm-thuis-sleutelboek-"))
        self.boek = lokaal.Sleutelboek(self.map_ / "sleutels.sqlite3", prijs_per_1k=1.0)

    def test_maken_zoeken_en_tellen(self):
        self.assertEqual(self.boek.aantal(), 0)
        nieuw = self.boek.maak("app", 3.0)
        self.assertEqual(self.boek.aantal(), 1)
        gevonden = self.boek.zoek(nieuw["sleutel"])
        self.assertEqual(gevonden["naam"], "app")
        self.assertEqual(self.boek.keur(nieuw["sleutel"]), (0, ""))
        self.boek.tel(nieuw["sleutel"], 1000)
        rij = self.boek.alles()[0]
        self.assertEqual(rij["tokens"], 1000)
        self.assertAlmostEqual(rij["verbruik"], 1.0)

    def test_naam_is_verplicht(self):
        with self.assertRaises(lokaal.LokaalFout):
            self.boek.maak("   ")

    def test_budget_op_geeft_429(self):
        nieuw = self.boek.maak("krap", 1.0)
        self.boek.tel(nieuw["sleutel"], 1000)
        code, melding = self.boek.keur(nieuw["sleutel"])
        self.assertEqual(code, 429)
        self.assertIn("budget", melding)

    def test_onbekende_sleutel_geeft_401(self):
        code, melding = self.boek.keur("sk-bestaat-niet")
        self.assertEqual(code, 401)
        self.assertIn("niet bekend", melding)


if __name__ == "__main__":
    unittest.main(verbosity=2)
