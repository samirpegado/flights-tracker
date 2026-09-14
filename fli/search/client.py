"""HTTP client with Chrome impersonation and Google Flights session warmup."""

from __future__ import annotations

import base64
import os
from typing import Any
from urllib.parse import quote

from curl_cffi import requests
from ratelimit import limits, sleep_and_retry
from tenacity import retry, stop_after_attempt, wait_exponential

client = None

GOOGLE_HOST = "https://www.google.com.br"
BRAZIL_LOCATION = "Natal,Rio Grande do Norte,Brazil"
CHROME_IMPERSONATE = "chrome131"
CONSENT_COOKIE = (
    "CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpd2ViXzIwMjQxMTEzLjA1X3AwGgJlbiACGgYI4LL5pwY"
)


def brazil_uule() -> str:
    """Encode a Brazilian canonical location for Google's `uule` parameter."""
    encoded = base64.b64encode(BRAZIL_LOCATION.encode("utf-8")).decode("ascii").rstrip("=")
    return "w+CAIQICI" + encoded


def brazil_search_qs() -> str:
    """Query string that asks Google Flights for the Brazilian market."""
    return f"hl=pt-BR&gl=BR&curr=BRL&uule={quote(brazil_uule(), safe='')}"


FLIGHTS_HOME = f"{GOOGLE_HOST}/travel/flights?{brazil_search_qs()}"
FLIGHTS_SEARCH = f"{GOOGLE_HOST}/travel/flights/search"
SHOPPING_RPC_URL = (
    f"{GOOGLE_HOST}/_/FlightsFrontendUi/data/travel.frontend.flights."
    f"FlightsFrontendService/GetShoppingResults?{brazil_search_qs()}&currency=BRL"
)
CALENDAR_RPC_URL = (
    f"{GOOGLE_HOST}/_/FlightsFrontendUi/data/travel.frontend.flights."
    f"FlightsFrontendService/GetCalendarGraph?{brazil_search_qs()}&currency=BRL"
)


def brazil_cookies() -> list[dict[str, str]]:
    """Cookies that signal Brazil + consent on both Google hosts."""
    pref = "ID=0000000000000000:FF=0:LD=pt-BR:NW=1:TM=0:LM=0:CR=BR:S=BRL"
    uule = brazil_uule()
    cookies: list[dict[str, str]] = []
    for domain in (".google.com.br", ".google.com"):
        cookies.extend(
            [
                {"name": "SOCS", "value": CONSENT_COOKIE, "domain": domain, "path": "/"},
                {"name": "CONSENT", "value": "YES+", "domain": domain, "path": "/"},
                {"name": "PREF", "value": pref, "domain": domain, "path": "/"},
                {"name": "UULE", "value": uule, "domain": domain, "path": "/"},
            ]
        )
    return cookies


class Client:
    """HTTP client with impersonation, rate limiting, retry and session warmup."""

    DEFAULT_HEADERS = {
        "accept": "*/*",
        "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
        "origin": GOOGLE_HOST,
        "referer": FLIGHTS_HOME,
        "x-same-domain": "1",
    }

    def __init__(self):
        """Initialize a Chrome-like session and warm it against Google Flights."""
        self._client = requests.Session(impersonate=CHROME_IMPERSONATE)
        self._client.headers.update(self.DEFAULT_HEADERS)
        proxy_url = os.getenv("PROXY_URL")
        if proxy_url:
            self._client.proxies = {"http": proxy_url, "https": proxy_url}
            print(f"Proxy configurado: {proxy_url.split('@')[-1]}")
        self._warmed = False
        self.warmup()

    def __del__(self):
        """Close the underlying session."""
        if hasattr(self, "_client"):
            self._client.close()

    def warmup(self) -> None:
        """Visit Google Flights once so the session carries NID/SOCS cookies."""
        if self._warmed:
            return
        try:
            for cookie in brazil_cookies():
                self._client.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie["domain"],
                )
            self._client.get(
                FLIGHTS_HOME,
                impersonate=CHROME_IMPERSONATE,
                allow_redirects=True,
                timeout=30,
            )
            self._warmed = True
        except Exception as exc:
            print(f"Falha no warmup do Google Flights: {exc}")

    @sleep_and_retry
    @limits(calls=10, period=1)
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(), reraise=True)
    def get(self, url: str, **kwargs: Any) -> requests.Response:
        """Make a rate-limited GET request with automatic retries."""
        self.warmup()
        kwargs.setdefault("impersonate", CHROME_IMPERSONATE)
        kwargs.setdefault("timeout", 30)
        try:
            response = self._client.get(url, **kwargs)
            response.raise_for_status()
            return response
        except Exception as e:
            raise Exception(f"GET request failed: {str(e)}") from e

    @sleep_and_retry
    @limits(calls=10, period=1)
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(), reraise=True)
    def post(self, url: str, **kwargs: Any) -> requests.Response:
        """Make a rate-limited POST request with automatic retries."""
        self.warmup()
        kwargs.setdefault("impersonate", CHROME_IMPERSONATE)
        kwargs.setdefault("timeout", 30)
        try:
            response = self._client.post(url, **kwargs)
            response.raise_for_status()
            return response
        except Exception as e:
            raise Exception(f"POST request failed: {str(e)}") from e


def get_client() -> Client:
    """Get or create a shared HTTP client instance."""
    global client
    if not client:
        client = Client()
    return client


def reset_client() -> None:
    """Drop the singleton so the next call builds a fresh warmed session."""
    global client
    client = None
