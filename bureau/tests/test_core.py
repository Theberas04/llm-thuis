#!/usr/bin/env python3
"""Proeven voor de kern van de LLM-thuis bureaubladapp.

Deze draaien op elke machine (ook in de bouwstraat op Linux) en gebruiken een
nagemaakt bord en een nagemaakte uitgave, zodat de client, het vergelijken van
versies en het klaarzetten van een bijwerking echt worden uitgeoefend.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HIER = Path(__file__).resolve().parent
sys.path.insert(0, str(HIER.parent))

import stub_bord  # noqa: E402
from llmthuis_bureau import core  # noqa: E402


class ProefVersies(unittest.TestCase):
    def test_delen(self):
        self.assertEqual(core.versie_delen("1.2.10"), (1, 2, 10))
        self.assertEqual(core.versie_delen("v2.0"), (2, 0))
        self.assertEqual(core.versie_delen(""), (0,))

    def test_nieuwer(self):
        self.assertTrue(core.is_nieuwer("1.0.1", "1.0.0"))
        self.assertTrue(core.is_nieuwer("1.10.0", "1.9.9"))
        self.assertTrue(core.is_nieuwer("2.0", "1.9.9"))
        self.assertFalse(core.is_nieuwer("1.0.0", "1.0.0"))
        self.assertFalse(core.is_nieuwer("0.9.9", "1.0.0"))


class ProefAdres(unittest.TestCase):
    def test_zonder_schema(self):
        self.assertEqual(core.normaliseer_basis("192.168.1.100"), "http://192.168.1.100")

    def test_schuine_streep_weg(self):
        self.assertEqual(core.normaliseer_basis("http://bord.lokaal/"), "http://bord.lokaal")

    def test_met_poort_en_pad(self):
        self.assertEqual(core.normaliseer_basis("http://192.168.1.100:80/bord/"),
                         "http://192.168.1.100:80")

    def test_leeg(self):
        with self.assertRaises(core.BordFout):
            core.normaliseer_basis("   ")

    def test_onzin(self):
        with self.assertRaises(core.BordFout):
            core.normaliseer_basis("ftp://")


class ProefBord(unittest.TestCase):
    """De client tegen het nagemaakte bord."""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.adres = stub_bord.start()
        cls.storing_server, cls.storing_adres = stub_bord.start(storing=True)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.storing_server.shutdown()

    def test_gezondheid_en_status(self):
        bord = core.Bord(self.adres)
        self.assertTrue(bord.gezondheid())
        gegevens = bord.status()
        self.assertEqual(gegevens["gateway_detail"], "proef")
        self.assertEqual([m["naam"] for m in gegevens["modellen"]],
                         ["eigen-llm", "slimme-cloud"])
        self.assertTrue(gegevens["sleutels_aan"])

    def test_model_testen(self):
        bord = core.Bord(self.adres)
        uitslag = bord.test_model("eigen-llm")
        self.assertTrue(uitslag["gelukt"])
        self.assertIn("Hallo", uitslag["antwoord"])
        self.assertEqual(uitslag["seconden"], 1.2)

    def test_sleutels_zonder_inloggen_wordt_geweigerd(self):
        bord = core.Bord(self.adres)
        with self.assertRaises(core.BordFout) as opvang:
            bord.sleutels()
        self.assertIn("ingelogd", str(opvang.exception))

    def test_inloggen_met_verkeerd_wachtwoord(self):
        bord = core.Bord(self.adres)
        with self.assertRaises(core.BordFout) as opvang:
            bord.inloggen("fout")
        self.assertIn("wachtwoord", str(opvang.exception))
        self.assertFalse(bord.ingelogd)

    def test_inloggen_en_sleutels_en_maken(self):
        bord = core.Bord(self.adres)
        bord.inloggen(stub_bord.WACHTWOORD)
        self.assertTrue(bord.ingelogd)
        rijen = bord.sleutels()
        self.assertEqual(rijen[0]["naam"], "thuisapp")
        self.assertEqual(rijen[0]["budget"], 5.0)
        nieuw = bord.maak_sleutel("proefsleutel", 2.5)
        self.assertEqual(nieuw["sleutel"], stub_bord.SLEUTEL)
        self.assertEqual(stub_bord.STAAT["aangemaakt"][-1],
                         {"naam": "proefsleutel", "budget": 2.5})
        bord.uitloggen()
        self.assertFalse(bord.ingelogd)

    def test_storing_geeft_de_melding_van_het_bord(self):
        bord = core.Bord(self.storing_adres)
        with self.assertRaises(core.BordFout) as opvang:
            bord.status()
        self.assertIn("de gateway ligt eruit", str(opvang.exception))

    def test_onbereikbaar_adres(self):
        bord = core.Bord("http://127.0.0.1:1", time_out=3)
        with self.assertRaises(core.BordFout) as opvang:
            bord.status()
        self.assertIn("geen verbinding", str(opvang.exception))


class ProefBijwerking(unittest.TestCase):
    """Het vergelijken van versies en het keuren van een pakket."""

    def _manifest(self, versie: str = "1.2.0") -> dict:
        return {"versie": versie, "bestand": f"LLM-thuis-bureau-{versie}-windows.zip",
                "sha256": "a" * 64, "grootte": 1234}

    def test_nieuwere_versie_gevonden(self):
        nieuw = core.controleer_bijwerking("1.0.0", manifest=self._manifest())
        self.assertIsNotNone(nieuw)
        self.assertEqual(nieuw.versie, "1.2.0")
        self.assertIn("windows.zip", nieuw.bestand)
        self.assertTrue(nieuw.url.startswith("https://github.com/Theberas04/llm-thuis/"))

    def test_bij_de_tijd(self):
        self.assertIsNone(core.controleer_bijwerking("1.2.0", manifest=self._manifest()))
        self.assertIsNone(core.controleer_bijwerking("1.3.0", manifest=self._manifest()))

    def test_onvolledig_manifest(self):
        for veld in ("versie", "bestand", "sha256"):
            gegevens = self._manifest()
            gegevens.pop(veld)
            with self.assertRaises(core.BordFout, msg=veld):
                core.Bijwerking.uit_manifest(gegevens)

    def test_eigen_adres_in_manifest(self):
        gegevens = self._manifest()
        gegevens["url"] = "https://voorbeeld.lokaal/pakket.zip"
        nieuw = core.Bijwerking.uit_manifest(gegevens)
        self.assertEqual(nieuw.url, "https://voorbeeld.lokaal/pakket.zip")


class ProefOphalen(unittest.TestCase):
    """Het ophalen van een pakket, met en zonder kloppende vingerafdruk."""

    @classmethod
    def setUpClass(cls):
        cls.map_ = Path(tempfile.mkdtemp(prefix="llm-thuis-uitgave-"))
        pakket = cls.map_ / "LLM-thuis-bureau-9.9.9-windows.zip"
        with zipfile.ZipFile(pakket, "w") as z:
            z.writestr("LLM-thuis.exe", b"nep programma")
            z.writestr("version.txt", "9.9.9\n")
        cls.pakket = pakket
        cls.sha = hashlib.sha256(pakket.read_bytes()).hexdigest()

        buiten = cls

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):  # noqa: A002
                pass

            def do_GET(self):  # noqa: N802
                brok = buiten.pakket.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(brok)))
                self.end_headers()
                self.wfile.write(brok)

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/pakket.zip"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _bijwerking(self, sha: str) -> core.Bijwerking:
        return core.Bijwerking(versie="9.9.9", bestand="LLM-thuis-bureau-9.9.9-windows.zip",
                               sha256=sha, url=self.url)

    def test_goede_vingerafdruk(self):
        doel = self.map_ / "binnen"
        voortgang: list[tuple[int, int]] = []
        pad = core.haal_bijwerking_op(self._bijwerking(self.sha), doel,
                                      voortgang=lambda g, t, s: voortgang.append((g, t)))
        self.assertTrue(pad.exists())
        self.assertEqual(core.sha256_van_bestand(pad), self.sha)
        self.assertTrue(voortgang, "de voortgang is nooit gemeld")
        self.assertEqual(core.controleer_pakket(pad, "LLM-thuis.exe"), [])

    def test_verkeerde_vingerafdruk_laat_niets_achter(self):
        doel = self.map_ / "afgekeurd"
        with self.assertRaises(core.BordFout) as opvang:
            core.haal_bijwerking_op(self._bijwerking("b" * 64), doel)
        self.assertIn("niets vervangen", str(opvang.exception))
        self.assertEqual(list(doel.glob("*.zip")), [],
                         "een afgekeurd pakket hoort niet te blijven staan")


class ProefPakketcontrole(unittest.TestCase):
    def test_goed_pakket(self):
        map_ = Path(tempfile.mkdtemp(prefix="llm-thuis-pakket-"))
        pad = map_ / "goed.zip"
        with zipfile.ZipFile(pad, "w") as z:
            z.writestr("LLM-thuis.exe", b"x")
            z.writestr("version.txt", "1.0.0\n")
            z.writestr("_internal/merken/merk.png", b"x")
        self.assertEqual(core.controleer_pakket(pad, "LLM-thuis.exe"), [])

    def test_mist_programma(self):
        map_ = Path(tempfile.mkdtemp(prefix="llm-thuis-pakket-"))
        pad = map_ / "leeg.zip"
        with zipfile.ZipFile(pad, "w") as z:
            z.writestr("version.txt", "1.0.0\n")
        fouten = core.controleer_pakket(pad, "LLM-thuis.exe")
        self.assertTrue(any("mist LLM-thuis.exe" in f for f in fouten))
        self.assertTrue(any("mist version.txt" in f for f in fouten) is False)

    def test_pad_buiten_de_map(self):
        map_ = Path(tempfile.mkdtemp(prefix="llm-thuis-pakket-"))
        pad = map_ / "kwaad.zip"
        with zipfile.ZipFile(pad, "w") as z:
            z.writestr("LLM-thuis.exe", b"x")
            z.writestr("version.txt", "1.0.0\n")
            z.writestr("../../ergens/boos.txt", b"x")
        fouten = core.controleer_pakket(pad, "LLM-thuis.exe")
        self.assertTrue(any("buiten de installatiemap" in f for f in fouten))

    def test_geen_zip(self):
        map_ = Path(tempfile.mkdtemp(prefix="llm-thuis-pakket-"))
        pad = map_ / "nep.zip"
        pad.write_bytes(b"dit is geen zip")
        self.assertTrue(core.controleer_pakket(pad, "LLM-thuis.exe"))


class ProefBijwerken(unittest.TestCase):
    """Het hulpbestand en de weigering om te bijwerken buiten Windows."""

    def test_hulpbestand_wacht_en_pakt_uit(self):
        tekst = core.maak_bijwerk_bat(1234, Path(r"C:\tmp\n.zip"),
                                      Path(r"C:\Apps\LLM-thuis"),
                                      Path(r"C:\Apps\LLM-thuis\LLM-thuis.exe"))
        for verwacht in ("set \"PID=1234\"", r"C:\tmp\n.zip", "set \"MAP=C:\\Apps\\LLM-thuis\"",
                         "tasklist", "Expand-Archive", 'start "" "%EXE%"',
                         "del \"%ZIP%\""):
            self.assertIn(verwacht, tekst, verwacht)

    def test_bijwerken_alleen_als_ingepakt_programma(self):
        # Op deze machine draait de app uit de broncode op Linux: dan hoort de app te
        # weigeren in plaats van een pakket over de broncode te schuiven.
        self.assertFalse(core.kan_bijwerken())
        with self.assertRaises(core.BordFout) as opvang:
            core.zet_bijwerking_door(core.Bijwerking("9.9.9", "x.zip", "a" * 64, "http://x/x.zip"))
        self.assertIn("Windows-programma", str(opvang.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
