"""Een nagemaakt bord voor de proeven van de bureaubladapp.

Het echte bord is een Linux-dienst; hier staat precies genoeg van dezelfde routes en
vormen om de client van de app te kunnen toetsen, inclusief het koekje van het
inloggen en de foutmeldingen die het bord geeft.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WACHTWOORD = "goed-wachtwoord"
SLEUTEL = "sk-proef-1234567890"
STAAT: dict = {"ingelogd": 0, "test_aanroepen": 0, "aangemaakt": []}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    storing = False

    def log_message(self, format, *args):  # noqa: A002 - stdlib-signatuur
        pass

    def _json(self, gegevens: dict, code: int = 200, extra: dict | None = None) -> None:
        brok = json.dumps(gegevens).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(brok)))
        for naam, waarde in (extra or {}).items():
            self.send_header(naam, waarde)
        self.end_headers()
        self.wfile.write(brok)

    def _tekst(self, tekst: str, code: int = 200) -> None:
        brok = tekst.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(brok)))
        self.end_headers()
        self.wfile.write(brok)

    def _lichaam(self) -> dict:
        lengte = int(self.headers.get("Content-Length") or 0)
        if not lengte:
            return {}
        try:
            return json.loads(self.rfile.read(lengte).decode("utf-8"))
        except ValueError:
            return {}

    def _beheerder(self) -> bool:
        return "thuis_zitting=proef" in (self.headers.get("Cookie") or "")

    def do_GET(self):  # noqa: N802
        pad = self.path.split("?", 1)[0]
        if type(self).storing and pad.startswith("/api/"):
            return self._json({"detail": "de gateway ligt eruit"}, 502)
        if pad == "/gezondheid":
            return self._tekst("ok\n")
        if pad == "/api/status":
            return self._json({
                "versie": "1.0.0-proef",
                "gateway": "http://gateway.lokaal:4000",
                "gateway_levend": True,
                "gateway_detail": "proef",
                "modellen": [
                    {"naam": "eigen-llm", "herkomst": "eigen", "omschrijving": "eigen model",
                     "laatste_test": None},
                    {"naam": "slimme-cloud", "herkomst": "cloud", "omschrijving": "",
                     "laatste_test": {"gelukt": True, "antwoord": "Hallo!", "tijd": "12:00:00"}},
                ],
                "sleutels_aan": True,
                "basis_url": "http://stub.lokaal/v1/",
                "ui_url": "http://stub.lokaal:4000/ui",
                "draait_sinds": 12,
            })
        if pad == "/api/sleutels":
            if not self._beheerder():
                return self._json({"detail": "niet ingelogd"}, 401)
            return self._json({"sleutels": [
                {"naam": "thuisapp", "budget": 5.0, "verbruik": 0.12,
                 "aangemaakt": "2026-11-19", "verlopen": None},
            ]})
        return self._json({"detail": "onbekende route"}, 404)

    def do_POST(self):  # noqa: N802
        pad = self.path.split("?", 1)[0]
        if pad == "/api/test":
            lichaam = self._lichaam()
            STAAT["test_aanroepen"] += 1
            return self._json({"model": lichaam.get("model"), "gelukt": True,
                               "antwoord": "Hallo! Alles werkt.", "ms": 1200, "tokens": 7,
                               "tijd": "12:00:00", "seconden": 1.2})
        if pad == "/api/inloggen":
            lichaam = self._lichaam()
            if lichaam.get("wachtwoord") != WACHTWOORD:
                return self._json({"detail": "dat wachtwoord klopt niet"}, 401)
            STAAT["ingelogd"] += 1
            return self._json({"ok": True}, extra={
                "Set-Cookie": "thuis_zitting=proef; Path=/; HttpOnly; SameSite=Lax"})
        if pad == "/api/uitloggen":
            return self._json({"ok": True}, extra={
                "Set-Cookie": "thuis_zitting=; Path=/; Max-Age=0"})
        if pad == "/api/sleutels":
            if not self._beheerder():
                return self._json({"detail": "niet ingelogd"}, 401)
            lichaam = self._lichaam()
            STAAT["aangemaakt"].append(lichaam)
            return self._json({"naam": lichaam.get("naam"), "sleutel": SLEUTEL,
                               "budget": lichaam.get("budget")})
        return self._json({"detail": "onbekende route"}, 404)


def start(storing: bool = False) -> tuple[ThreadingHTTPServer, str]:
    """Start het nagemaakte bord op een vrije poort; geeft de server en het adres.

    Elke server krijgt zijn eigen handler-klasse: een gedeelde klasse zou de
    storing-vlag van de ene proef op de andere laten overslaan.
    """
    eigen = type("Handler", (Handler,), {"storing": storing})
    server = ThreadingHTTPServer(("127.0.0.1", 0), eigen)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"
