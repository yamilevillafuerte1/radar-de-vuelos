"""Servidor web sin frameworks para RADAR DE VUELOS."""

from __future__ import annotations

import argparse
import json
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import radar


ROOT = Path(__file__).resolve().parent
INDEX_PATH = ROOT / "index.html"


class RadarHandler(BaseHTTPRequestHandler):
    server_version = "RadarVuelos/1.0"

    def _json(self, payload: object, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise radar.RadarError("Content-Length inválido") from error
        if length <= 0 or length > 100_000:
            raise radar.RadarError("Cuerpo JSON vacío o demasiado grande")
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise radar.RadarError("JSON inválido") from error
        if not isinstance(payload, dict):
            raise radar.RadarError("El cuerpo debe ser un objeto JSON")
        return payload

    def _error(self, error: Exception, status: int = HTTPStatus.BAD_REQUEST) -> None:
        self._json({"ok": False, "error": str(error)}, status)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path in {"/", "/index.html"}:
                body = INDEX_PATH.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif parsed.path == "/api/rutas":
                self._json({"ok": True, "data": radar.list_routes()})
            elif parsed.path == "/api/precios":
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", ["100"])[0])
                route_value = query.get("ruta_id", [None])[0]
                route_id = int(route_value) if route_value is not None else None
                self._json({"ok": True, "data": radar.list_prices(limit, route_id)})
            elif parsed.path == "/api/resumen":
                self._json(
                    {
                        "ok": True,
                        "data": {
                            "rutas": radar.list_routes(),
                            "precios": radar.list_prices(100),
                        },
                    }
                )
            else:
                self._error(radar.RadarError("Recurso no encontrado"), HTTPStatus.NOT_FOUND)
        except (radar.RadarError, ValueError) as error:
            self._error(error)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/rutas":
                payload = self._read_json()
                route = radar.add_route(
                    str(payload.get("origen", "")),
                    str(payload.get("destino", "")),
                    float(payload.get("precio_objetivo", 0)),
                    str(payload.get("fecha_salida", "")),
                )
                self._json({"ok": True, "data": route}, HTTPStatus.CREATED)
            elif parsed.path == "/api/escanear":
                results = radar.scan_all()
                self._json({"ok": True, "data": results})
            elif parsed.path.startswith("/api/rutas/") and parsed.path.endswith("/estado"):
                route_id = int(parsed.path.split("/")[3])
                payload = self._read_json()
                if not isinstance(payload.get("activa"), bool):
                    raise radar.RadarError("activa debe ser true o false")
                route = radar.set_route_active(route_id, payload["activa"])
                self._json({"ok": True, "data": route})
            else:
                self._error(radar.RadarError("Recurso no encontrado"), HTTPStatus.NOT_FOUND)
        except (radar.RadarError, ValueError, TypeError) as error:
            self._error(error)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")


def automatic_scanner(stop_event: threading.Event, interval_minutes: int) -> None:
    while not stop_event.wait(interval_minutes * 60):
        try:
            results = radar.scan_all(notify=True)
            completed = sum(result["status"] == "ok" for result in results)
            print(f"Escaneo automático completado: {completed} ruta(s) con precio.")
        except radar.RadarError as error:
            print(f"Escaneo automático detenido: {error}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Panel web de RADAR DE VUELOS")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    radar.init_db()
    radar.load_env()
    try:
        interval_minutes = int(os.environ.get("SCAN_INTERVAL_MINUTES", "60"))
    except ValueError as error:
        raise radar.RadarError("SCAN_INTERVAL_MINUTES debe ser un número entero") from error
    if interval_minutes < 1:
        raise radar.RadarError("SCAN_INTERVAL_MINUTES debe ser al menos 1")

    stop_event = threading.Event()
    scanner = threading.Thread(
        target=automatic_scanner,
        args=(stop_event, interval_minutes),
        daemon=True,
    )
    scanner.start()
    server = ThreadingHTTPServer((args.host, args.port), RadarHandler)
    print(f"RADAR DE VUELOS disponible en http://{args.host}:{args.port}")
    print(f"Escaneo automático cada {interval_minutes} minuto(s).")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")
    finally:
        stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
