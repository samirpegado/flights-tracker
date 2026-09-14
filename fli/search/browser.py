"""Playwright fallback when Google's RPC returns an empty shopping payload."""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from fli.models import Airline, Airport, FlightLeg, FlightResult
from fli.search.client import FLIGHTS_HOME, FLIGHTS_SEARCH, brazil_cookies, brazil_search_qs

if TYPE_CHECKING:
    from fli.models import FlightSearchFilters

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
    r"(?P<dep>\d{1,2}:\d{2})\s*(?:AM|PM)?\s*[–\-]\s*(?P<arr>\d{1,2}:\d{2})\s*(?:AM|PM)?(?P<plus>\+\d+)?"
    r"\s*(?P<carriers>.*?)"
    r"(?P<hours>\d+)\s*(?:hrs?|h(?:oras?)?|std\.?)\s*(?:(?P<mins>\d+)\s*(?:mins?|min\.?)?)?"
    r"\s*[A-Z]{3}\s*[–\-]\s*[A-Z]{3}"
    r"\s*(?P<stopstext>Sem escalas|Non-?stop|Direkt(?:flug)?|Ohne Zwischenstopp|"
    r"\d+\s*(?:paradas?|stops?|Zwischenstopps?))"
    r".{0,500}?"
    r"(?P<currencysym>R\$|€|EUR|US\$|\$)\s*(?P<price>[\d\.\,]+)",
    re.IGNORECASE | re.DOTALL,
)

CONSENT_BUTTONS = (
    "Aceitar tudo",
    "Accept all",
    "I agree",
    "Alle akzeptieren",
    "Einverstanden",
    "Aceptar todo",
)

CURRENCY_FROM_SYMBOL = {
    "R$": "BRL",
    "€": "EUR",
    "EUR": "EUR",
    "US$": "USD",
    "$": "USD",
}


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
    cleaned = re.split(r"Operado por|Operated by|Betrieben von", carriers, maxsplit=1, flags=re.I)[0]
    for label, code in AIRLINE_ALIASES:
        if re.search(re.escape(label), cleaned, re.I):
            return code
    return None


def _parse_price(raw: str) -> float:
    text = raw.strip()
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            return float(text.replace(".", "").replace(",", "."))
        return float(text.replace(",", ""))
    if "," in text:
        decimals = text.split(",")[-1]
        if len(decimals) == 2:
            return float(text.replace(".", "").replace(",", "."))
        return float(text.replace(",", ""))
    if "." in text:
        decimals = text.split(".")[-1]
        if len(decimals) == 3:
            return float(text.replace(".", ""))
        return float(text)
    return float(text)


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
        try:
            price = _parse_price(match.group("price"))
        except ValueError:
            continue
        if price < 50:
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
        if re.search(r"Sem escalas|Non-?stop|Direkt|Ohne Zwischenstopp", stopstext, re.I):
            stops = 0
        else:
            stops_m = re.search(r"(\d+)", stopstext)
            stops = int(stops_m.group(1)) if stops_m else 1
        currency = CURRENCY_FROM_SYMBOL.get(match.group("currencysym").upper(), "BRL")
        if match.group("currencysym") in {"R$"}:
            currency = "BRL"
        elif match.group("currencysym") in {"€", "EUR"}:
            currency = "EUR"

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
                currency=currency,
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


def _playwright_proxy() -> dict[str, str] | None:
    raw = os.getenv("PROXY_URL") or ""
    if not raw.strip():
        return None
    parsed = urlparse(raw)
    if not parsed.hostname:
        return None
    scheme = parsed.scheme or "http"
    port = parsed.port or (443 if scheme == "https" else 80)
    proxy: dict[str, str] = {"server": f"{scheme}://{parsed.hostname}:{port}"}
    if parsed.username:
        proxy["username"] = parsed.username
        proxy["password"] = parsed.password or ""
    return proxy


def _chromium_args() -> list[str]:
    return [
        "--disable-blink-features=AutomationControlled",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--window-size=1400,900",
    ]


def _dismiss_consent(page) -> None:
    """Click through the EU Google consent interstitial if it appears."""
    for frame in [page, *page.frames]:
        try:
            btn = frame.locator("#L2AGLb")
            if btn.count():
                btn.first.click(timeout=1500)
                page.wait_for_timeout(800)
                return
        except Exception:
            pass
        for label in CONSENT_BUTTONS:
            try:
                frame.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.I)).first.click(
                    timeout=1200
                )
                page.wait_for_timeout(800)
                return
            except Exception:
                continue


def _page_snippet(visible: str, title: str) -> str:
    collapsed = re.sub(r"\s+", " ", visible or "").strip()
    return f"title={title!r} chars={len(visible or '')} snippet={collapsed[:220]!r}"


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
        ).result(timeout=120)


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
    url = f"{FLIGHTS_SEARCH}?{brazil_search_qs()}&tfs={tfs}"
    payloads: list[str] = []
    visible = ""
    title = ""

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
            launch_kwargs: dict[str, Any] = {
                "headless": not headed,
                "args": _chromium_args(),
            }
            proxy = _playwright_proxy()
            if proxy:
                launch_kwargs["proxy"] = proxy
                print("Playwright usando PROXY_URL.")
            in_docker = os.path.exists("/.dockerenv")
            browser = None
            if not in_docker:
                try:
                    browser = playwright.chromium.launch(channel="chrome", **launch_kwargs)
                except Exception:
                    browser = None
            if browser is None:
                browser = playwright.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                locale="pt-BR",
                timezone_id="America/Recife",
                geolocation={"latitude": -5.7945, "longitude": -35.211},
                permissions=["geolocation"],
                viewport={"width": 1400, "height": 900},
                extra_http_headers={"accept-language": "pt-BR,pt;q=0.9,en;q=0.8"},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            context.add_cookies(brazil_cookies())
            page = context.new_page()
            page.on("response", on_response)
            page.goto(FLIGHTS_HOME, wait_until="domcontentloaded", timeout=45000)
            _dismiss_consent(page)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            _dismiss_consent(page)
            try:
                page.wait_for_function(
                    """() => {
                        const t = document.body?.innerText || '';
                        return t.includes('R$') || t.includes('€')
                            || /resultados encontrados/i.test(t)
                            || /results found/i.test(t)
                            || /ergebnisse/i.test(t);
                    }""",
                    timeout=28000,
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
                title = page.title()
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
            print(f"Fallback Playwright página: {_page_snippet(visible, title)}")
            browser.close()
            if parsed_flights:
                currencies = {flight.currency for flight in parsed_flights}
                if currencies - {"BRL"}:
                    print(
                        "Google serviu preços fora de BRL "
                        f"({', '.join(sorted(currencies))}). "
                        "Na Hetzner use PROXY_URL com IP do Brasil."
                    )
                return {"__dom_flights__": parsed_flights}
    except Exception as exc:
        print(f"Fallback Playwright falhou: {exc}")
        return None

    print(
        f"Fallback Playwright sem payload útil "
        f"({len(payloads)} respostas, texto tem R$={'R$' in visible}, "
        f"tem €={'€' in visible})."
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
