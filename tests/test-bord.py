#!/usr/bin/env python3
"""Proeven voor het bord (LLM-thuis): statuspagina, testknop, sleutelbeheer en
het doorsturen van /v1/.

Draaien:  python3 tests/test-bord.py        (of: python3 -m unittest discover tests)

Alles draait tegen een nagebootste gateway (tests/stub_gateway.py) op een vrije
poort, dus geen echte gateway en geen kosten."""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HIER = Path(__file__).resolve().parent
REPO = HIER.parent
sys.path.insert(0, str(HIER))

import stub_gateway  # noqa: E402  (na sys.path-aanpassing)

WACHTWOORD = "proef-wachtwoord-123"
MODEL = "eigen-llm"


class Proef(unittest.TestCase):
    """Basis: stub-gateway + bord, met tijdelijke sleutel- en beheerbestanden."""

    @classmethod
    def setUpClass(cls):
        cls.stub, _ = stub_gateway.start()
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)

        cls.sleutelbestand = tmp / "sleutels.env"
        cls.beheerbestand = tmp / "beheer.env"
        cls.modellenbestand = tmp / "modellen.json"
        cls.sleutelbestand.write_text(
            f"THUIS_MASTER_KEY={stub_gateway.MASTERSLEUTEL}\n", encoding="utf-8")
        cls.modellenbestand.write_text(json.dumps({
            "eigen-llm": {"herkomst": "eigen", "omschrijving": "eigen machine"},
            "slimme-cloud": {"herkomst": "cloud", "omschrijving": "cloud"},
        }), encoding="utf-8")

        os.environ.update({
            "THUIS_GATEWAY": cls.stub.url,
            "THUIS_SLEUTELBESTAND": str(cls.sleutelbestand),
            "THUIS_BEHEERBESTAND": str(cls.beheerbestand),
            "THUIS_MODELLENBESTAND": str(cls.modellenbestand),
            "THUIS_MERK": str(REPO / "assets" / "merk.png"),
            "THUIS_TEST_TIMEOUT": "30",
        })
        cls.bord = cls._laad_bord()
        cls.beheerbestand.write_text(
            f"THUIS_BEHEER_HASH={cls.bord.maak_afdruk(WACHTWOORD)}\n", encoding="utf-8")

        cls.server = cls.bord.maak_server(0, "127.0.0.1")
        cls.poort = cls.server.server_address[1]
        cls.draad = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.draad.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.stub.shutdown()
        cls.stub.server_close()
        cls.tmp.cleanup()

    @staticmethod
    def _laad_bord():
        pad = REPO / "bord" / "bord.py"
        spec = importlib.util.spec_from_file_location("bord", pad)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    # -- hulpmiddelen -------------------------------------------------------
    def verzoek(self, pad: str, methode: str = "GET", lichaam: dict | None = None,
                koek: str | None = None, ruw: bytes | None = None,
                sleutel: str | None = None):
        data = ruw if ruw is not None else (
            json.dumps(lichaam).encode() if lichaam is not None else None)
        verzoek = urllib.request.Request(f"http://127.0.0.1:{self.poort}{pad}", data=data,
                                        method=methode)
        if lichaam is not None or ruw is not None:
            verzoek.add_header("Content-Type", "application/json")
        if koek:
            verzoek.add_header("Cookie", koek)
        if sleutel:
            verzoek.add_header("Authorization", f"Bearer {sleutel}")
        try:
            with urllib.request.urlopen(verzoek, timeout=30) as antwoord:
                return antwoord.status, antwoord.read(), dict(antwoord.headers)
        except urllib.error.HTTPError as fout:
            return fout.code, fout.read(), dict(fout.headers)

    def log_in(self) -> str:
        code, _, koppen = self.verzoek("/api/inloggen", "POST", {"wachtwoord": WACHTWOORD})
        self.assertEqual(code, 200)
        koek = koppen.get("Set-Cookie") or ""
        self.assertIn("HttpOnly", koek)
        return koek.split(";")[0]


class TestPagina(Proef):
    def test_pagina_komt_heel_uit_de_server(self):
        code, body, koppen = self.verzoek("/")
        tekst = body.decode()
        self.assertEqual(code, 200)
        self.assertIn("text/html", koppen.get("Content-Type", ""))
        self.assertTrue(tekst.rstrip().endswith("</html>"))
        # De huisstijl is écht ingevuld (geen letterlijke plaatshouder) en de
        # merkgradient staat erin.
        self.assertNotIn("__HUISSTIJL__", tekst)
        self.assertIn("--merk:linear-gradient", tekst)
        self.assertIn('id="model-rijen"', tekst)
        # Elk id maar één keer: een dubbel id geeft de gebeurtenisluisteraar aan
        # het verkeerde element (stil kapot).
        ids = re.findall(r'id="([^"]+)"', tekst)
        dubbel = sorted({i for i in ids if ids.count(i) > 1})
        self.assertEqual(dubbel, [])

    def test_geen_backslashes_in_de_javascript(self):
        tekst = self.verzoek("/")[1].decode()
        script = re.search(r"<script>(.*?)</script>", tekst, re.S).group(1)
        self.assertNotIn("\\", script, "backslash in de JS: wordt een echte newline")

    def test_javascript_is_syntaxfoutvrij(self):
        tekst = self.verzoek("/")[1].decode()
        script = re.search(r"<script>(.*?)</script>", tekst, re.S).group(1)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as tijdelijk:
            tijdelijk.write(script)
            pad = tijdelijk.name
        try:
            uit = subprocess.run(["node", "--check", pad], capture_output=True, text=True)
            self.assertEqual(uit.returncode, 0, uit.stderr)
        finally:
            os.unlink(pad)

    def test_gezondheid_met_eigen_kopregel(self):
        code, body, koppen = self.verzoek("/gezondheid")
        self.assertEqual(code, 200)
        self.assertEqual(body.strip(), b"ok")
        self.assertEqual(koppen.get("x-llm-thuis"), "bord")

    def test_merk_png_en_onbekende_route(self):
        code, body, koppen = self.verzoek("/merk.png")
        self.assertEqual(code, 200)
        self.assertEqual(koppen.get("Content-Type"), "image/png")
        self.assertTrue(body.startswith(b"\x89PNG"))
        self.assertEqual(self.verzoek("/bestaat-niet")[0], 404)


class TestStatusEnTest(Proef):
    def test_status_toont_de_modellen_van_de_gateway(self):
        code, body, _ = self.verzoek("/api/status")
        self.assertEqual(code, 200)
        gegevens = json.loads(body)
        self.assertTrue(gegevens["gateway_levend"])
        namen = [m["naam"] for m in gegevens["modellen"]]
        self.assertEqual(namen, ["eigen-llm", "slimme-cloud"])
        self.assertEqual(gegevens["modellen"][0]["herkomst"], "eigen")
        self.assertTrue(gegevens["sleutels_aan"])
        self.assertTrue(gegevens["basis_url"].endswith("/v1/"))

    def test_testknop_roept_het_model_echt_aan(self):
        voor = len(self.stub.aanroepen)
        code, body, _ = self.verzoek("/api/test", "POST", {"model": MODEL})
        self.assertEqual(code, 200)
        uitslag = json.loads(body)
        self.assertTrue(uitslag["gelukt"], uitslag)
        self.assertEqual(uitslag["antwoord"], stub_gateway.ANTWOORD)
        self.assertGreaterEqual(uitslag["ms"], 0)
        self.assertEqual(uitslag["tokens"], 15)
        nieuw = self.stub.aanroepen[voor:]
        self.assertTrue(any(a["pad"] == "/v1/chat/completions" for a in nieuw))
        self.assertTrue(all(a["auth"] == f"Bearer {stub_gateway.MASTERSLEUTEL}" for a in nieuw))
        # De uitslag is daarna op de pagina te zien.
        gegevens = json.loads(self.verzoek("/api/status")[1])
        laatste = [m for m in gegevens["modellen"] if m["naam"] == MODEL][0]["laatste_test"]
        self.assertTrue(laatste["gelukt"])

    def test_test_zonder_model_geeft_uitleg(self):
        code, body, _ = self.verzoek("/api/test", "POST", {})
        self.assertEqual(code, 400)
        self.assertIn("model", json.loads(body)["error"])

    def test_test_meldt_een_onbekend_model_als_fout(self):
        code, body, _ = self.verzoek("/api/test", "POST", {"model": "bestaat-niet"})
        self.assertIn(code, (200, 502))
        uitslag = json.loads(body)
        if code == 502:
            self.assertFalse(uitslag["gelukt"])
            self.assertIn("401", uitslag["error"])


class TestSleutelbeheer(Proef):
    def test_zonder_inlog_geen_sleutels(self):
        self.assertEqual(self.verzoek("/api/sleutels")[0], 401)
        voor = len(self.stub.aanroepen)
        code, body, _ = self.verzoek("/api/sleutels", "POST", {"naam": "stiekem"})
        self.assertEqual(code, 401)
        self.assertEqual(len(self.stub.aanroepen), voor, "de gateway mocht niets zien")

    def test_verkeerd_wachtwoord_wordt_geweigerd(self):
        code, body, koppen = self.verzoek("/api/inloggen", "POST", {"wachtwoord": "fout"})
        self.assertEqual(code, 401)
        self.assertNotIn("Set-Cookie", koppen)

    def test_inloggen_lijst_sleutels_en_maakt_een_nieuwe(self):
        koek = self.log_in()
        code, body, _ = self.verzoek("/api/sleutels", koek=koek)
        self.assertEqual(code, 200)
        sleutels = json.loads(body)["sleutels"]
        # Het bord vertaalt de sleutelobjecten naar naam/budget/verbruik, zodat de
        # pagina geen lege rijen toont als de gateway een andere vorm teruggeeft.
        self.assertEqual(sleutels[0]["naam"], "thuisapp")
        self.assertEqual(sleutels[0]["budget"], 5.0)
        self.assertEqual(sleutels[0]["verbruik"], 0.12)
        self.assertEqual(sleutels[0]["aangemaakt"], "2026-11-19")

        voor = len(self.stub.aanroepen)
        code, body, _ = self.verzoek("/api/sleutels", "POST",
                                     {"naam": "thuisapp-nieuw", "budget": "5"}, koek=koek)
        self.assertEqual(code, 200)
        nieuw = json.loads(body)
        self.assertEqual(nieuw["sleutel"], "sk-nieuwe-sleutel-1234")
        self.assertEqual(nieuw["budget"], 5.0)
        oproep = [a for a in self.stub.aanroepen[voor:] if a["pad"] == "/key/generate"][0]
        verstuurd = json.loads(oproep["lichaam"])
        self.assertEqual(verstuurd["key_alias"], "thuisapp-nieuw")
        self.assertEqual(verstuurd["max_budget"], 5.0)
        self.assertEqual(oproep["auth"], f"Bearer {stub_gateway.MASTERSLEUTEL}")

    def test_sleutels_zonder_volledige_objecten_vallen_terug_op_key_info(self):
        """De echte gateway geeft op /key/list alleen hashes; dan moet het bord
        /key/info gebruiken in plaats van lege rijen te tonen."""
        self.stub.kaal = True
        try:
            voor = len(self.stub.aanroepen)
            koek = self.log_in()
            code, body, _ = self.verzoek("/api/sleutels", koek=koek)
            self.assertEqual(code, 200)
            sleutels = json.loads(body)["sleutels"]
            self.assertEqual(sleutels[0]["naam"], "thuisapp")
            self.assertEqual(sleutels[0]["budget"], 5.0)
            paden = [a["pad"] for a in self.stub.aanroepen[voor:]]
            self.assertTrue(any(p.startswith("/key/info") for p in paden), paden)
        finally:
            self.stub.kaal = False
            self.verzoek("/api/uitloggen", "POST", koek=koek)

    def test_budget_moet_een_getal_zijn(self):
        koek = self.log_in()
        code, body, _ = self.verzoek("/api/sleutels", "POST",
                                     {"naam": "x", "budget": "veel"}, koek=koek)
        self.assertEqual(code, 502)
        self.assertIn("getal", json.loads(body)["error"])
        code, body, _ = self.verzoek("/api/sleutels", "POST",
                                     {"naam": "x", "budget": "-3"}, koek=koek)
        self.assertIn("groter dan nul", json.loads(body)["error"])

    def test_uitloggen_maakt_de_zitting_ongeldig(self):
        koek = self.log_in()
        self.assertEqual(self.verzoek("/api/sleutels", koek=koek)[0], 200)
        self.assertEqual(self.verzoek("/api/uitloggen", "POST", koek=koek)[0], 200)
        self.assertEqual(self.verzoek("/api/sleutels", koek=koek)[0], 401)

    def test_zitting_verloopt(self):
        koek = self.log_in()
        token = koek.split("=", 1)[1]
        self.server.zittingen[token]["sinds"] -= self.bord.ZITTINGDUUR + 1
        self.assertEqual(self.verzoek("/api/sleutels", koek=koek)[0], 401)


class TestDoorsturen(Proef):
    def test_v1_wordt_doorgestuurd(self):
        code, body, koppen = self.verzoek(
            "/v1/chat/completions", "POST",
            {"model": MODEL, "messages": [{"role": "user", "content": "hoi"}]},
            sleutel=stub_gateway.MASTERSLEUTEL)
        self.assertEqual(code, 200)
        antwoord = json.loads(body)
        self.assertEqual(antwoord["choices"][0]["message"]["content"], stub_gateway.ANTWOORD)

    def test_streamen_komt_heel_aan(self):
        """Een streamend antwoord mag niet in stukken breken door de chunk-framing."""
        verbinding = http.client.HTTPConnection("127.0.0.1", self.poort, timeout=30)
        lichaam = json.dumps({"model": MODEL, "stream": True,
                              "messages": [{"role": "user", "content": "hoi"}]}).encode()
        verbinding.request("POST", "/v1/chat/completions", body=lichaam,
                           headers={"Content-Type": "application/json",
                                    "Authorization": f"Bearer {stub_gateway.MASTERSLEUTEL}",
                                    "Accept": "text/event-stream"})
        antwoord = verbinding.getresponse()
        tekst = antwoord.read().decode()
        verbinding.close()
        self.assertEqual(antwoord.status, 200)
        self.assertIn("Hallo", tekst)
        self.assertIn("daar", tekst)
        self.assertIn("[DONE]", tekst)

    def test_aanroep_zonder_sleutel_geeft_de_fout_van_de_gateway_door(self):
        verbinding = http.client.HTTPConnection("127.0.0.1", self.poort, timeout=30)
        lichaam = json.dumps({"model": MODEL}).encode()
        verbinding.request("POST", "/v1/chat/completions", body=lichaam,
                           headers={"Content-Type": "application/json"})
        antwoord = verbinding.getresponse()
        tekst = antwoord.read().decode()
        verbinding.close()
        self.assertEqual(antwoord.status, 401)
        self.assertIn("invalid key", tekst)


class TestWachtwoord(Proef):
    def test_scrypt_afdruk_rond_uit(self):
        afdruk = self.bord.maak_afdruk("geheim-1234")
        self.assertTrue(afdruk.startswith("scrypt$"))
        self.assertTrue(self.bord.wachtwoord_klopt_met(afdruk, "geheim-1234"))
        self.assertFalse(self.bord.wachtwoord_klopt_met(afdruk, "geheim-1235"))
        self.assertFalse(self.bord.wachtwoord_klopt_met("onzin", "geheim-1234"))
        self.assertFalse(self.bord.wachtwoord_klopt_met("", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
