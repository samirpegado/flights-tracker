"""Playwright fallback when Google's RPC returns an empty shopping payload."""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from fli.models import Airline, Airport, FlightLeg, FlightResult

if TYPE_CHECKING:
    from fli.models import FlightSearchFilters

FLIGHTS_SEARCH = "https://www.google.com/travel/flights/search"

AIRLINE_ALIASES = (
    ("TAP Air Portugal", "TP"),
    ("Tap Air Portugal", "TP"),
    ("TAP Portugal", "TP"),
    ("LATAM Airlines", "LA"),
    ("Air Europa", "UX"),
    ("Air France", "AF"),
    ("British Airways", "BA"),
    ("Lufthansa", "LH"),
    ("Iberia", "IB"),
    ("Swiss", "LX"),
    ("LATAM", "LA"),
    ("Azul", "AD"),
    ("KLM", "KL"),
    ("Gol", "G3"),
    ("GOL", "G3"),
)

CARD_RE = re.compile(
    r"(?P<dep>\d{1,2}:\d{2})\s*[–\-]\s*(?P<arr>\d{1,2}:\d{2})(?P<plus>\+\d+)?"
    r"\s*(?P<carriers>.*?)"
    r"(?P<hours>\d+)\s*h(?:oras?)?\s*(?:(?P<mins>\d+)\s*min)?"
    r"\s*[A-Z]{3}\s*[–\-]\s*[A-Z]{3}"
    r"\s*(?P<stopstext>Sem escalas|\d+\s*paradas?)"
    r".{0,500}?"
    r"R\$\s*(?P<price>[\d\.]+)",
    re.IGNORECASE | re.DOTALL,
)


def build_one_way_tfs(origin: str, destination: str, date: str, adults: int = 1) -> str:
    """Encode a one-way Google Flights `tfs` query parameter.

    Layout reverse-engineered from public /travel/flights/search URLs:
    date nested as field 1/string, airports as field 13/14 with
    ``(1, airport_code)``.
    """

    def ld(field: int, data: bytes) -> bytes:
        return bytes([(field << 3) | 2, len(data)]) + data

    def varint(field: int, value: int) -> bytes:
        return bytes([(field << 3) | 0, value])

    def airport(code: str) -> bytes:
        inner = varint(1, 1) + ld(2, code.encode("ascii"))
        return inner

    date_and_airports = (
        ld(2, date.encode("ascii"))
        + ld(13, airport(origin))
        + ld(14, airport(destination))
    )
    payload = (
        varint(1, 28)
        + varint(2, 2)
        + ld(3, date_and_airports)
        + varint(8, max(1, adults))
        + varint(9, 1)
    )
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _airline_code(carriers: str) -> str | None:
    cleaned = re.split(r"Operado por", carriers, maxsplit=1, flags=re.I)[0]
    for label, code in AIRLINE_ALIASES:
        if re.search(re.escape(label), cleaned, re.I):
            return code
    return None


def parse_visible_flights(
    text: str, origin: str, destination: str, date: str
) -> list[FlightResult]:
    """Extract flight cards from the visible Google Flights page text."""
    origin_ap = getattr(Airport, origin)
    dest_ap = getattr(Airport, destination)
    travel_day = datetime.strptime(date, "%Y-%m-%d")
    results: list[FlightResult] = []
    seen: set[tuple] = set()

    for match in CARD_RE.finditer(text):
        price_raw = match.group("price").replace(".", "")
        try:
            price = float(price_raw)
        except ValueError:
            continue
        if price < 100:
            continue
        airline_code = _airline_code(match.group("carriers"))
        if not airline_code:
            continue
        try:
            airline = getattr(Airline, airline_code)
        except AttributeError:
            continue

        duration = int(match.group("hours")) * 60 + int(match.group("mins") or 0)
        stopstext = match.group("stopstext")
        if re.search(r"Sem escalas", stopstext, re.I):
            stops = 0
        else:
            stops_m = re.search(r"(\d+)", stopstext)
            stops = int(stops_m.group(1)) if stops_m else 1

        dep_h, dep_min = (int(part) for part in match.group("dep").split(":"))
        arr_h, arr_min = (int(part) for part in match.group("arr").split(":"))
        departure = travel_day.replace(hour=dep_h, minute=dep_min)
        arrival = travel_day.replace(hour=arr_h, minute=arr_min)
        plus = match.group("plus")
        if plus:
            arrival += timedelta(days=int(plus[1:]))
        elif arrival <= departure:
            arrival += timedelta(days=1)

        key = (airline_code, match.group("dep"), match.group("arr"), price, stops)
        if key in seen:
            continue
        seen.add(key)
        results.append(
            FlightResult(
                price=price,
                currency="BRL",
                duration=duration,
                stops=stops,
                legs=[
                    FlightLeg(
                        airline=airline,
                        flight_number="",
                        departure_airport=origin_ap,
                        arrival_airport=dest_ap,
                        departure_datetime=departure,
                        arrival_datetime=arrival,
                        duration=duration,
                    )
                ],
            )
        )
    return results


def _parse_rpc_text(text: str) -> Any | None:
    stripped = text.lstrip(")]}'")
    try:
        parsed = json.loads(stripped)
        payload = parsed[0][2]
        if not payload:
            return None
        return json.loads(payload)
    except Exception:
        return None


def search_shopping_in_browser(filters: FlightSearchFilters) -> list | None:
    """Open Google Flights in Chromium and reuse GetShoppingResults from the page."""
    origin = filters.flight_segments[0].departure_airport[0][0].name
    destination = filters.flight_segments[0].arrival_airport[0][0].name
    date = filters.flight_segments[0].travel_date
    adults = getattr(getattr(filters, "passenger_info", None), "adults", 1) or 1
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(
            _search_shopping_sync, origin, destination, date, adults
        ).result(timeout=90)


def _search_shopping_sync(
    origin: str, destination: str, date: str, adults: int = 1
) -> list | None:
    """Run Playwright off the FastAPI event loop."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright não instalado; pulando fallback do browser.")
        return None

    tfs = build_one_way_tfs(origin, destination, date, adults)
    url = f"{FLIGHTS_SEARCH}?hl=pt-BR&gl=BR&curr=BRL&tfs={tfs}"
    payloads: list[str] = []
    visible = ""

    def on_response(response) -> None:
        if "GetShoppingResults" not in response.url:
            return
        if response.status != 200:
            return
        try:
            payloads.append(response.text())
        except Exception:
            return

    try:
        with sync_playwright() as playwright:
            headed = os.getenv("GOOGLE_FLIGHTS_HEADED", "").lower() in {"1", "true", "yes"}
            launch_kwargs = {"headless": not headed}
            try:
                browser = playwright.chromium.launch(channel="chrome", **launch_kwargs)
            except Exception:
                browser = playwright.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                locale="pt-BR",
                timezone_id="America/Recife",
                extra_http_headers={"accept-language": "pt-BR,pt;q=0.9"},
            )
            page = context.new_page()
            page.on("response", on_response)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_function(
                    "() => (document.body?.innerText || '').includes('R$')",
                    timeout=25000,
                )
            except Exception:
                page.wait_for_timeout(8000)
            page.wait_for_timeout(2500)
            for raw in payloads:
                inner = _parse_rpc_text(raw)
                if inner:
                    browser.close()
                    return inner
            try:
                visible = page.inner_text("body")
            except Exception:
                visible = ""
            if os.getenv("GOOGLE_FLIGHTS_DUMP", "").lower() in {"1", "true", "yes"}:
                dump_path = os.path.join(os.path.dirname(__file__), "..", "_last_google_flights.txt")
                try:
                    with open(dump_path, "w", encoding="utf-8") as handle:
                        handle.write(visible[:20000])
                except Exception:
                    pass
            parsed_flights = parse_visible_flights(visible, origin, destination, date)
            browser.close()
            if parsed_flights:
                return {"__dom_flights__": parsed_flights}
    except Exception as exc:
        print(f"Fallback Playwright falhou: {exc}")
        return None

    print(
        f"Fallback Playwright sem payload útil "
        f"({len(payloads)} respostas, texto tem R$={'R$' in visible})."
    )
    return None


def flights_from_shopping_payload(inner: list, parse_flight) -> list:
    """Turn a GetShoppingResults inner payload into FlightResult objects."""
    flights_data = [
        item
        for i in [2, 3]
        if isinstance(inner, list) and len(inner) > i and isinstance(inner[i], list)
        for item in inner[i][0]
    ]
    return [parse_flight(flight) for flight in flights_data]
