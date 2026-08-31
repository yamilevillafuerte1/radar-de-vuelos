"""Consulta Travelpayouts y guarda el histórico diario de precios."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "radar.db"
ENV_PATH = ROOT / ".env"
FLIGHT_API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
LIMA_TIMEZONE = timezone(timedelta(hours=-5))


class RadarError(RuntimeError):
    """Error controlado del agente de vuelos."""


class NoFaresError(RadarError):
    """La API respondió correctamente, pero no tiene tarifas para una ruta."""


def load_env(path: Path = ENV_PATH) -> None:
    """Carga un archivo .env sencillo sin reemplazar variables ya definidas."""
    if not path.exists():
        raise RadarError(f"No existe el archivo de variables: {path}")

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def require_env(name: str) -> str:
    load_env()
    value = os.environ.get(name, "").strip()
    if not value:
        raise RadarError(f"La variable {name} no está configurada en .env")
    return value


def connect_db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db() -> None:
    with connect_db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS rutas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                origen TEXT NOT NULL,
                destino TEXT NOT NULL,
                activa INTEGER NOT NULL DEFAULT 1 CHECK (activa IN (0, 1)),
                precio_objetivo REAL NOT NULL CHECK (precio_objetivo > 0),
                fecha_salida TEXT,
                UNIQUE (origen, destino)
            );

            CREATE TABLE IF NOT EXISTS precios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ruta_id INTEGER NOT NULL,
                precio REAL NOT NULL CHECK (precio >= 0),
                moneda TEXT NOT NULL,
                aerolinea TEXT NOT NULL,
                fecha_vuelo TEXT NOT NULL,
                fecha_consulta TEXT NOT NULL,
                escalas INTEGER,
                numero_vuelo TEXT,
                duracion INTEGER,
                enlace TEXT,
                FOREIGN KEY (ruta_id) REFERENCES rutas(id) ON DELETE CASCADE,
                UNIQUE (ruta_id, fecha_consulta)
            );
            """
        )
        route_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(rutas)").fetchall()
        }
        if "fecha_salida" not in route_columns:
            connection.execute("ALTER TABLE rutas ADD COLUMN fecha_salida TEXT")

        price_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(precios)").fetchall()
        }
        migrations = {
            "escalas": "INTEGER",
            "numero_vuelo": "TEXT",
            "duracion": "INTEGER",
            "enlace": "TEXT",
        }
        for column, column_type in migrations.items():
            if column not in price_columns:
                connection.execute(
                    f"ALTER TABLE precios ADD COLUMN {column} {column_type}"
                )


def normalize_iata(value: str) -> str:
    code = value.strip().upper()
    if len(code) != 3 or not code.isalpha() or not code.isascii():
        raise RadarError(f"Código IATA inválido: {value!r}")
    return code


def normalize_departure_date(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as error:
        raise RadarError("La fecha debe usar el formato AAAA-MM-DD") from error
    if parsed < datetime.now(LIMA_TIMEZONE).date():
        raise RadarError("La fecha de salida no puede estar en el pasado")
    return parsed.isoformat()


def add_route(
    origin: str,
    destination: str,
    target_price: float,
    departure_date: str | None = None,
) -> dict[str, Any]:
    init_db()
    origin = normalize_iata(origin)
    destination = normalize_iata(destination)
    if origin == destination:
        raise RadarError("El origen y el destino deben ser diferentes")
    if target_price <= 0:
        raise RadarError("El precio objetivo debe ser mayor que cero")
    departure_date = normalize_departure_date(departure_date)

    with connect_db() as connection:
        connection.execute(
            """
            INSERT INTO rutas (origen, destino, activa, precio_objetivo, fecha_salida)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(origen, destino) DO UPDATE SET
                activa = 1,
                precio_objetivo = excluded.precio_objetivo,
                fecha_salida = excluded.fecha_salida
            """,
            (origin, destination, float(target_price), departure_date),
        )
        row = connection.execute(
            "SELECT * FROM rutas WHERE origen = ? AND destino = ?",
            (origin, destination),
        ).fetchone()
    return dict(row)


def set_route_active(route_id: int, active: bool) -> dict[str, Any]:
    init_db()
    with connect_db() as connection:
        cursor = connection.execute(
            "UPDATE rutas SET activa = ? WHERE id = ?", (int(active), route_id)
        )
        if cursor.rowcount == 0:
            raise RadarError(f"No existe la ruta {route_id}")
        row = connection.execute("SELECT * FROM rutas WHERE id = ?", (route_id,)).fetchone()
    return dict(row)


def list_routes(active_only: bool = False) -> list[dict[str, Any]]:
    init_db()
    where = "WHERE r.activa = 1" if active_only else ""
    query = f"""
        SELECT r.*,
               p.id AS ultimo_precio_id,
               p.precio AS ultimo_precio,
               p.moneda AS ultima_moneda,
               p.fecha_vuelo AS ultimo_vuelo,
               p.fecha_consulta AS ultima_consulta,
               p.numero_vuelo AS ultimo_numero_vuelo,
               p.escalas AS ultimas_escalas
        FROM rutas r
        LEFT JOIN precios p ON p.id = (
            SELECT p2.id FROM precios p2
            WHERE p2.ruta_id = r.id
            ORDER BY p2.fecha_consulta DESC, p2.id DESC
            LIMIT 1
        )
        {where}
        ORDER BY r.id
    """
    with connect_db() as connection:
        return [dict(row) for row in connection.execute(query).fetchall()]


def list_prices(limit: int = 100, route_id: int | None = None) -> list[dict[str, Any]]:
    init_db()
    limit = max(1, min(int(limit), 1000))
    parameters: list[Any] = []
    where = ""
    if route_id is not None:
        where = "WHERE p.ruta_id = ?"
        parameters.append(route_id)
    parameters.append(limit)
    query = f"""
        SELECT p.*, r.origen, r.destino, r.precio_objetivo
        FROM precios p
        JOIN rutas r ON r.id = p.ruta_id
        {where}
        ORDER BY p.fecha_consulta DESC, p.id DESC
        LIMIT ?
    """
    with connect_db() as connection:
        prices = [dict(row) for row in connection.execute(query, parameters).fetchall()]
    for price in prices:
        price["calendar_url"] = build_calendar_url(price)
    return prices


def _validate_quote(record: Any, origin: str, destination: str) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise RadarError("Travelpayouts cambió el esquema: cada tarifa debe ser un objeto")

    required = {
        "origin": str,
        "destination": str,
        "price": (int, float),
        "airline": str,
        "departure_at": str,
        "transfers": int,
    }
    for field, expected_type in required.items():
        value = record.get(field)
        if isinstance(value, bool) or not isinstance(value, expected_type):
            raise RadarError(
                f"Travelpayouts cambió el esquema: campo {field!r} ausente o inválido"
            )

    if record["origin"] != origin or record["destination"] != destination:
        raise RadarError("Travelpayouts devolvió una ruta diferente de la solicitada")
    try:
        datetime.fromisoformat(record["departure_at"].replace("Z", "+00:00"))
    except ValueError as error:
        raise RadarError("Travelpayouts devolvió departure_at con formato inválido") from error
    return record


def fetch_cheapest_price(
    origin: str, destination: str, departure_date: str | None = None
) -> dict[str, Any]:
    origin = normalize_iata(origin)
    destination = normalize_iata(destination)
    token = require_env("FLIGHT_API_TOKEN")
    params = {
        "origin": origin,
        "destination": destination,
        "currency": "USD",
        "one_way": "true",
        "direct": "false",
        "sorting": "price",
        "unique": "false",
        "limit": 30,
        "page": 1,
    }
    departure_date = normalize_departure_date(departure_date)
    if departure_date:
        params["departure_at"] = departure_date
    try:
        response = requests.get(
            FLIGHT_API_URL,
            params=params,
            headers={"X-Access-Token": token, "Accept": "application/json"},
            timeout=30,
        )
    except requests.RequestException as error:
        raise RadarError(f"No se pudo conectar con Travelpayouts: {error}") from error

    if response.status_code != 200:
        raise RadarError(f"Travelpayouts respondió HTTP {response.status_code}: {response.text[:300]}")
    try:
        payload = response.json()
    except requests.exceptions.JSONDecodeError as error:
        raise RadarError("Travelpayouts no devolvió JSON válido") from error

    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise RadarError("Travelpayouts cambió el esquema o informó un error")
    if not isinstance(payload.get("currency"), str):
        raise RadarError("Travelpayouts cambió el esquema: falta currency")
    data = payload.get("data")
    if not isinstance(data, list):
        raise RadarError("Travelpayouts cambió el esquema: data ya no es una lista")
    if not data:
        raise NoFaresError(f"Travelpayouts no devolvió tarifas para {origin} → {destination}")

    quotes = [_validate_quote(item, origin, destination) for item in data]
    if departure_date:
        quotes = [
            item for item in quotes
            if datetime.fromisoformat(item["departure_at"].replace("Z", "+00:00")).date().isoformat()
            == departure_date
        ]
        if not quotes:
            raise NoFaresError(
                f"Travelpayouts no devolvió tarifas para {origin} → {destination} el {departure_date}"
            )
    cheapest = min(quotes, key=lambda item: float(item["price"]))
    return {
        "origin": origin,
        "destination": destination,
        "price": float(cheapest["price"]),
        "currency": payload["currency"].upper(),
        "airline": cheapest["airline"],
        "departure_at": cheapest["departure_at"],
        "transfers": cheapest["transfers"],
        "flight_number": cheapest.get("flight_number"),
        "duration": cheapest.get("duration"),
        "link": cheapest.get("link"),
    }


def save_quote(route: dict[str, Any], quote: dict[str, Any]) -> tuple[bool, int]:
    consultation_date = datetime.now(LIMA_TIMEZONE).date().isoformat()
    with connect_db() as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO precios
                (ruta_id, precio, moneda, aerolinea, fecha_vuelo, fecha_consulta,
                 escalas, numero_vuelo, duracion, enlace)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                route["id"],
                quote["price"],
                quote["currency"],
                quote["airline"],
                quote["departure_at"],
                consultation_date,
                quote["transfers"],
                quote.get("flight_number"),
                quote.get("duration"),
                quote.get("link"),
            ),
        )
        inserted = cursor.rowcount == 1
        if inserted:
            price_id = int(cursor.lastrowid)
        else:
            row = connection.execute(
                "SELECT id FROM precios WHERE ruta_id = ? AND fecha_consulta = ?",
                (route["id"], consultation_date),
            ).fetchone()
            if row is None:
                raise RadarError("No se pudo recuperar la observación diaria")
            price_id = int(row["id"])
    return inserted, price_id


def build_calendar_url(price: dict[str, Any]) -> str:
    departure_value = price.get("fecha_vuelo") or price.get("departure_at")
    if not isinstance(departure_value, str):
        raise RadarError("No hay fecha de vuelo para proponer el evento")
    try:
        start = datetime.fromisoformat(departure_value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RadarError("La fecha del vuelo no es válida para Calendar") from error
    duration = price.get("duracion") or price.get("duration") or 120
    end = start + timedelta(minutes=max(1, int(duration)))
    to_utc = lambda value: value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    origin = str(price.get("origen") or price.get("origin") or "")
    destination = str(price.get("destino") or price.get("destination") or "")
    airline = str(price.get("aerolinea") or price.get("airline") or "")
    flight_number = str(price.get("numero_vuelo") or price.get("flight_number") or "")
    currency = str(price.get("moneda") or price.get("currency") or "USD")
    amount = price.get("precio") if price.get("precio") is not None else price.get("price")
    transfers = price.get("escalas") if price.get("escalas") is not None else price.get("transfers")
    details = [f"Precio detectado: {currency} {float(amount):.2f}"] if amount is not None else []
    if airline:
        details.append(f"Aerolínea: {airline} {flight_number}".strip())
    if transfers is not None:
        details.append("Vuelo directo" if int(transfers) == 0 else f"Escalas: {transfers}")
    link = price.get("enlace") or price.get("link")
    if isinstance(link, str) and link:
        details.append(f"Oferta: https://www.aviasales.com{link}")

    flight_label = " ".join(part for part in (airline, flight_number) if part)
    title_suffix = f" ({flight_label})" if flight_label else ""
    query = urlencode(
        {
            "action": "TEMPLATE",
            "text": f"Vuelo {origin} → {destination}{title_suffix}",
            "dates": f"{to_utc(start)}/{to_utc(end)}",
            "details": "\n".join(details),
            "location": f"{origin} → {destination}",
        }
    )
    return f"https://calendar.google.com/calendar/render?{query}"


def calendar_proposal(price_id: int) -> dict[str, Any]:
    init_db()
    with connect_db() as connection:
        row = connection.execute(
            """
            SELECT p.*, r.origen, r.destino
            FROM precios p JOIN rutas r ON r.id = p.ruta_id
            WHERE p.id = ?
            """,
            (price_id,),
        ).fetchone()
    if row is None:
        raise RadarError(f"No existe la observación de precio {price_id}")
    price = dict(row)
    return {"price_id": price_id, "calendar_url": build_calendar_url(price)}


def scan_route(route: dict[str, Any], notify: bool = True) -> dict[str, Any]:
    quote = fetch_cheapest_price(
        route["origen"], route["destino"], route.get("fecha_salida")
    )
    inserted, price_id = save_quote(route, quote)
    opportunity = quote["price"] <= float(route["precio_objetivo"])
    result = {
        **quote,
        "status": "ok",
        "price_id": price_id,
        "route_id": route["id"],
        "target_price": float(route["precio_objetivo"]),
        "inserted": inserted,
        "opportunity": opportunity,
    }
    result["calendar_url"] = build_calendar_url(result)
    if notify and inserted and opportunity:
        from bot import notify_opportunity

        notify_opportunity(result)
    return result


def scan_all(notify: bool = True) -> list[dict[str, Any]]:
    routes = list_routes(active_only=True)
    if not routes:
        raise RadarError("No hay rutas activas para consultar")
    results: list[dict[str, Any]] = []
    for route in routes:
        try:
            results.append(scan_route(route, notify=notify))
        except NoFaresError as error:
            results.append(
                {
                    "status": "no_fares",
                    "route_id": route["id"],
                    "origin": route["origen"],
                    "destination": route["destino"],
                    "inserted": False,
                    "opportunity": False,
                    "error": str(error),
                }
            )
    return results


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="RADAR DE VUELOS")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="Crear la base de datos")

    add_parser = subparsers.add_parser("add", help="Agregar o actualizar una ruta")
    add_parser.add_argument("origin")
    add_parser.add_argument("destination")
    add_parser.add_argument("target_price", type=float)
    add_parser.add_argument("departure_date", nargs="?")

    subparsers.add_parser("routes", help="Listar las rutas")
    subparsers.add_parser("scan", help="Consultar todas las rutas activas")

    state_parser = subparsers.add_parser("state", help="Activar o pausar una ruta")
    state_parser.add_argument("route_id", type=int)
    state_parser.add_argument("value", choices=("on", "off"))

    args = parser.parse_args()
    try:
        if args.command == "init":
            init_db()
            _print_json({"ok": True, "database": str(DB_PATH)})
        elif args.command == "add":
            _print_json(
                add_route(
                    args.origin,
                    args.destination,
                    args.target_price,
                    args.departure_date,
                )
            )
        elif args.command == "routes":
            _print_json(list_routes())
        elif args.command == "scan":
            _print_json(scan_all())
        elif args.command == "state":
            _print_json(set_route_active(args.route_id, args.value == "on"))
    except RadarError as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
