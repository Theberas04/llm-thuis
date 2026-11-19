#!/usr/bin/env python3
"""Nagebootste gateway voor de proeven: net genoeg van LiteLLM's API om het bord
te toetsen zonder een echte gateway (en zonder kosten).

Houdt elke aanroep bij in server.aanroepen, zodat een proef kan controleren dat
het bord écht iets doorstuurde (en met welke sleutel)."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MASTERSLEUTEL = "sk-moeder-proef"
ANTWOORD = "Hallo! Alles werkt."


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: A002
        pass

    def _json(self, obj, code: int = 200, extra: dict | None = None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for naam, waarde in (extra or {}).items():
            self.send_header(naam, waarde)
        self.end_headers()
        self.wfile.write(body)

    def _tekst(self, tekst: str, code: int = 200, soort: str = "application/json"):
        body = tekst.encode()
        self.send_response(code)
        self.send_header("Content-Type", soort)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bewaar(self, lichaam: bytes):
        self.server.aanroepen.append({  # type: ignore[attr-defined]
            "pad": self.path,
            "methode": self.command,
            "auth": self.headers.get("Authorization"),
            "lichaam": lichaam.decode("utf-8", "replace") if lichaam else "",
            "tijd": time.time(),
        })

    def _auth_ok(self) -> bool:
        return (self.headers.get("Authorization") or "") == f"Bearer {MASTERSLEUTEL}"

    def do_GET(self):  # noqa: N802
        self._bewaar(b"")
        pad = self.path.split("?")[0]
        if pad == "/health/liveliness":
            return self._tekst('"I am alive!"')
        if pad == "/v1/models":
            return self._json({"data": [{"id": "eigen-llm", "object": "model"},
                                        {"id": "slimme-cloud", "object": "model"}]})
        if pad == "/key/list":
            if not self._auth_ok():
                return self._json({"detail": "invalid key"}, 401)
            return self._json({"keys": [{"key_name": "thuisapp", "max_budget": 5,
                                         "spend": 0.12, "created_at": "2026-11-19"}]})
        return self._json({"detail": "onbekende route"}, 404)

    def do_POST(self):  # noqa: N802
        lengte = int(self.headers.get("Content-Length") or 0)
        lichaam = self.rfile.read(lengte) if lengte else b""
        self._bewaar(lichaam)
        pad = self.path.split("?")[0]
        try:
            verzoek = json.loads(lichaam.decode() or "{}")
        except ValueError:
            verzoek = {}
        if not self._auth_ok():
            return self._json({"detail": "invalid key"}, 401)
        if pad == "/v1/chat/completions":
            if verzoek.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for stuk in ('{"choices":[{"delta":{"content":"Hallo"}}]}',
                             '{"choices":[{"delta":{"content":" daar"}}]}',
                             "[DONE]"):
                    brok = f"data: {stuk}\n\n".encode()
                    self.wfile.write(f"{len(brok):X}\r\n".encode() + brok + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                return
            return self._json({
                "id": "chatcmpl-proef",
                "model": verzoek.get("model"),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": ANTWOORD}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            })
        if pad == "/key/generate":
            if not verzoek.get("key_alias"):
                return self._json({"detail": "key_alias ontbreekt"}, 400)
            return self._json({"key": "sk-nieuwe-sleutel-1234", "key_alias": verzoek["key_alias"],
                               "max_budget": verzoek.get("max_budget")})
        return self._json({"detail": "onbekende route"}, 404)


class StubGateway(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, poort: int = 0):
        super().__init__(("127.0.0.1", poort), StubHandler)
        self.aanroepen: list[dict] = []

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


def start(poort: int = 0) -> tuple[StubGateway, threading.Thread]:
    server = StubGateway(poort)
    draad = threading.Thread(target=server.serve_forever, daemon=True)
    draad.start()
    return server, draad
