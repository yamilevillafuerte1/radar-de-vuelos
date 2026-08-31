"""Alertas y comandos de Telegram para RADAR DE VUELOS."""

from __future__ import annotations

import argparse
import time
from typing import Any

import requests

import radar


def telegram_call(method: str, *, data: dict[str, Any] | None = None) -> Any:
    token = radar.require_env("TELEGRAM_TOKEN")
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        response = requests.post(url, data=data or {}, timeout=35)
    except requests.RequestException as error:
        # No incluir la excepción: requests puede incorporar la URL con el token.
        raise radar.RadarError("No se pudo conectar con Telegram") from error
    if response.status_code != 200:
        raise radar.RadarError(f"Telegram respondió HTTP {response.status_code}")
    try:
        payload = response.json()
    except requests.exceptions.JSONDecodeError as error:
        raise radar.RadarError("Telegram no devolvió JSON válido") from error
    if not isinstance(payload, dict) or payload.get("ok") is not True or "result" not in payload:
        description = payload.get("description", "esquema inesperado") if isinstance(payload, dict) else "esquema inesperado"
        raise radar.RadarError(f"Telegram rechazó la operación: {description}")
    return payload["result"]


def send_message(text: str, chat_id: str | None = None) -> Any:
    target = chat_id or radar.require_env("TELEGRAM_CHAT_ID")
    return telegram_call("sendMessage", data={"chat_id": target, "text": text})


def notify_opportunity(quote: dict[str, Any]) -> None:
    stops = "directo" if quote["transfers"] == 0 else f"{quote['transfers']} escala(s)"
    text = (
        "✈️ OPORTUNIDAD DETECTADA\n"
        f"{quote['origin']} → {quote['destination']}\n"
        f"Precio: {quote['currency']} {quote['price']:.2f}\n"
        f"Objetivo: USD {quote['target_price']:.2f}\n"
        f"Aerolínea: {quote['airline']} · {stops}\n"
        f"Salida: {quote['departure_at']}\n"
        f"Si decides viajar: /calendario {quote['price_id']}"
    )
    send_message(text)


def help_text() -> str:
    return (
        "RADAR DE VUELOS\n\n"
        "/rutas — ver rutas e históricos recientes\n"
        "/agregar LIM CUZ 2026-10-02 50 — guardar ruta y fecha\n"
        "/activar ID — activar una ruta\n"
        "/pausar ID — pausar una ruta\n"
        "/buscar — consultar ahora las rutas activas\n"
        "/calendario ID_PRECIO — proponer evento en Google Calendar"
    )


def format_routes() -> str:
    routes = radar.list_routes()
    if not routes:
        return "Todavía no hay rutas configuradas."
    lines = ["Rutas configuradas:"]
    for route in routes:
        state = "activa" if route["activa"] else "pausada"
        latest = (
            f" · último {route['ultima_moneda']} {route['ultimo_precio']:.2f}"
            if route["ultimo_precio"] is not None
            else " · sin consultas"
        )
        date_text = route["fecha_salida"] or "fecha flexible"
        calendar_hint = (
            f" · /calendario {route['ultimo_precio_id']}"
            if route["ultimo_precio_id"] is not None
            else ""
        )
        lines.append(
            f"#{route['id']} {route['origen']}→{route['destino']} · {date_text} · objetivo USD {route['precio_objetivo']:.2f} · {state}{latest}{calendar_hint}"
        )
    return "\n".join(lines)


def handle_command(text: str) -> str:
    parts = text.strip().split()
    command = parts[0].split("@", 1)[0].lower() if parts else ""
    if command in {"/start", "/help"}:
        return help_text()
    if command == "/rutas":
        return format_routes()
    if command == "/agregar":
        if len(parts) != 5:
            return "Uso: /agregar ORIGEN DESTINO AAAA-MM-DD PRECIO_OBJETIVO"
        route = radar.add_route(parts[1], parts[2], float(parts[4]), parts[3])
        return f"Ruta #{route['id']} {route['origen']}→{route['destino']} guardada."
    if command in {"/activar", "/pausar"}:
        if len(parts) != 2 or not parts[1].isdigit():
            return f"Uso: {command} ID"
        route = radar.set_route_active(int(parts[1]), command == "/activar")
        return f"Ruta #{route['id']} {'activada' if route['activa'] else 'pausada'}."
    if command == "/buscar":
        results = radar.scan_all(notify=False)
        lines = ["Consulta completada:"]
        for quote in results:
            if quote["status"] == "no_fares":
                lines.append(f"{quote['origin']}→{quote['destination']}: sin tarifas")
                continue
            marker = " 🎯" if quote["opportunity"] else ""
            lines.append(
                f"{quote['origin']}→{quote['destination']}: {quote['currency']} {quote['price']:.2f}{marker}"
            )
        return "\n".join(lines)
    if command == "/calendario":
        if len(parts) != 2 or not parts[1].isdigit():
            return "Uso: /calendario ID_PRECIO"
        proposal = radar.calendar_proposal(int(parts[1]))
        return (
            "Propuesta preparada. Revisa los datos y pulsa Guardar en Google Calendar:\n"
            f"{proposal['calendar_url']}"
        )
    return help_text()


def run_polling() -> None:
    expected_chat = radar.require_env("TELEGRAM_CHAT_ID")
    offset = 0
    print("Bot de Telegram en ejecución. Presiona Ctrl+C para detenerlo.")
    while True:
        try:
            updates = telegram_call(
                "getUpdates", data={"offset": offset, "timeout": 25, "allowed_updates": '["message"]'}
            )
            if not isinstance(updates, list):
                raise radar.RadarError("Telegram cambió el esquema de getUpdates")
            for update in updates:
                if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
                    raise radar.RadarError("Telegram devolvió una actualización inválida")
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                text = message.get("text")
                if str(chat.get("id")) != expected_chat or not isinstance(text, str):
                    continue
                try:
                    reply = handle_command(text)
                except (radar.RadarError, ValueError) as error:
                    reply = f"Error: {error}"
                send_message(reply, str(chat["id"]))
        except radar.RadarError as error:
            print(f"Error: {error}")
            time.sleep(5)


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot Telegram de RADAR DE VUELOS")
    parser.add_argument("command", nargs="?", choices=("run", "test"), default="run")
    args = parser.parse_args()
    if args.command == "test":
        result = telegram_call("getMe")
        print(f"Telegram OK: @{result.get('username', 'sin_usuario')}")
    else:
        run_polling()


if __name__ == "__main__":
    main()
