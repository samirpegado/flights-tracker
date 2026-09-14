"""HTTP client with Chrome impersonation and Google Flights session warmup."""

from __future__ import annotations

import os
from typing import Any

from curl_cffi import requests
from ratelimit import limits, sleep_and_retry
from tenacity import retry, stop_after_attempt, wait_exponential

client = None

FLIGHTS_HOME = "https://www.google.com/travel/flights?hl=pt-BR&gl=BR&curr=BRL"
CHROME_IMPERSONATE = "chrome131"
CONSENT_COOKIE = (
    "CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpd2ViXzIwMjQxMTEzLjA1X3AwGgJlbiACGgYI4LL5pwY"
)


class Client:
    """HTTP client with impersonation, rate limiting, retry and session warmup."""

    DEFAULT_HEADERS = {
        "accept": "*/*",
        "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
        "origin": "https://www.google.com",
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
            self._client.cookies.set("SOCS", CONSENT_COOKIE, domain=".google.com")
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
