"""Upstream data sources.

Every source returns a ``SourceResult`` so the merge step can tell a source
that legitimately had no traffic apart from a source that actually failed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx

from .config import Settings
from .geo import BoundingBox
from .statevector import Row, from_readb, normalise_opensky_row

log = logging.getLogger("merge.sources")

TOKEN_EXPIRY_MARGIN_S = 30.0


@dataclass
class SourceResult:
    name: str
    rows: list[Row] = field(default_factory=list)
    error: str | None = None
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None

    def as_meta(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "rows": len(self.rows),
            "error": self.error,
            "duration_s": round(self.duration_s, 2),
        }


class Source:
    name = "source"

    async def fetch(self, client: httpx.AsyncClient, box: BoundingBox) -> SourceResult:
        raise NotImplementedError


class OpenSkyTokenManager:
    """Fetches and caches OAuth2 client-credentials tokens.

    OpenSky access tokens are valid for 30 minutes. A lock keeps concurrent
    requests from stampeding the token endpoint when several devices poll the
    merge service at once.
    """

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        """Drop the cached token, forcing the next get() to fetch a new one."""
        self._token = None
        self._expires_at = 0.0

    async def get(self) -> str | None:
        if not self._settings.has_opensky_credentials:
            return None

        async with self._lock:
            now = time.time()
            if self._token and now < self._expires_at:
                return self._token

            payload = {
                "grant_type": "client_credentials",
                "client_id": self._settings.opensky_client_id,
                "client_secret": self._settings.opensky_client_secret,
            }
            try:
                response = await self._client.post(
                    self._settings.opensky_token_url,
                    data=payload,
                    timeout=self._settings.open_sky_timeout_s,
                )
                response.raise_for_status()
                body = response.json()
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                log.warning("OpenSky token request failed: %s", exc)
                return None

            token = body.get("access_token")
            if not isinstance(token, str) or not token:
                log.warning("OpenSky token response had no access_token")
                return None

            expires_in = body.get("expires_in")
            lifetime = float(expires_in) if isinstance(expires_in, (int, float)) else 1800.0

            self._token = token
            self._expires_at = time.time() + max(60.0, lifetime - TOKEN_EXPIRY_MARGIN_S)
            log.info("OpenSky access token acquired (valid for %.0fs)", lifetime)
            return token


class OpenSkySource(Source):
    name = "opensky"

    def __init__(self, settings: Settings, tokens: OpenSkyTokenManager) -> None:
        self._settings = settings
        self._tokens = tokens

    async def fetch(self, client: httpx.AsyncClient, box: BoundingBox) -> SourceResult:
        started = time.monotonic()
        result = SourceResult(name=self.name)

        params: dict[str, Any] = {
            "lamin": f"{box.lamin:.6f}",
            "lamax": f"{box.lamax:.6f}",
            "lomin": f"{box.lomin:.6f}",
            "lomax": f"{box.lomax:.6f}",
        }
        if self._settings.opensky_extended:
            params["extended"] = 1

        headers: dict[str, str] = {"User-Agent": "flight-radar-merge/1.0"}
        token = await self._tokens.get()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            response = await client.get(
                f"{self._settings.opensky_base_url}/states/all",
                params=params,
                headers=headers,
                timeout=self._settings.open_sky_timeout_s,
            )

            if response.status_code in (401, 403) and token:
                # The cached token was rejected; drop it and retry once.
                self._tokens.invalidate()
                token = await self._tokens.get()
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                    response = await client.get(
                        f"{self._settings.opensky_base_url}/states/all",
                        params=params,
                        headers=headers,
                        timeout=self._settings.open_sky_timeout_s,
                    )

            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            result.error = _describe(exc)
            result.duration_s = time.monotonic() - started
            return result

        raw_states = body.get("states") if isinstance(body, dict) else None
        if raw_states is None:
            result.error = "response contained no 'states' key"
            result.duration_s = time.monotonic() - started
            return result

        for raw in raw_states:
            row = normalise_opensky_row(raw)
            if row is not None:
                result.rows.append(row)

        result.duration_s = time.monotonic() - started
        return result


class ReadbSource(Source):
    """Base for the readb/tar1090 v2 JSON APIs.

    Subclasses only have to say which URL to call and where the aircraft array
    sits in the response document.
    """

    array_keys: tuple[str, ...] = ("aircraft", "ac")

    def __init__(self, name: str, base_url: str, timeout_s: float = 8.0) -> None:
        self.name = name
        self._base_url = base_url
        self._timeout_s = timeout_s

    def url(self, box: BoundingBox) -> str:
        raise NotImplementedError

    async def fetch(self, client: httpx.AsyncClient, box: BoundingBox) -> SourceResult:
        started = time.monotonic()
        result = SourceResult(name=self.name)

        try:
            response = await client.get(
                self.url(box),
                headers={"User-Agent": "flight-radar-merge/1.0"},
                timeout=self._timeout_s,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            result.error = _describe(exc)
            result.duration_s = time.monotonic() - started
            return result

        now = float(body.get("now") or time.time()) if isinstance(body, dict) else time.time()
        result.rows = [
            row
            for row in (
                from_readb(item, now)
                for item in self._extract(body)
            )
            if row is not None
        ]
        result.duration_s = time.monotonic() - started
        return result

    def _extract(self, body: Any) -> list[Any]:
        if isinstance(body, list):
            return body
        if not isinstance(body, dict):
            return []
        for key in self.array_keys:
            value = body.get(key)
            if isinstance(value, list):
                return value
        return []


class AdsbLolSource(ReadbSource):
    def __init__(self, base_url: str, timeout_s: float = 8.0) -> None:
        super().__init__("adsb.lol", base_url, timeout_s)

    def url(self, box: BoundingBox) -> str:
        return (
            f"{self._base_url}/point/{box.center_lat:.4f}/"
            f"{box.center_lon:.4f}/{box.query_radius_nm}"
        )


class AdsbFiSource(ReadbSource):
    def __init__(self, base_url: str, timeout_s: float = 8.0) -> None:
        super().__init__("adsb.fi", base_url, timeout_s)

    def url(self, box: BoundingBox) -> str:
        return (
            f"{self._base_url}/lat/{box.center_lat:.4f}/"
            f"lon/{box.center_lon:.4f}/dist/{box.query_radius_nm}"
        )


class AdsbImSource(ReadbSource):
    """adsb.im v2 API — same response shape as adsb.lol, different host."""

    def __init__(self, base_url: str, timeout_s: float = 8.0) -> None:
        super().__init__("adsb.im", base_url, timeout_s)

    def url(self, box: BoundingBox) -> str:
        return (
            f"{self._base_url}/point/{box.center_lat:.4f}/"
            f"{box.center_lon:.4f}/{box.query_radius_nm}"
        )


class CustomReadbSource(ReadbSource):
    """A user supplied readb v2 endpoint, e.g. ``custom=http://pi:8080/v2``."""

    def __init__(self, name: str, base_url: str, timeout_s: float = 8.0) -> None:
        super().__init__(name, base_url, timeout_s)

    def url(self, box: BoundingBox) -> str:
        return (
            f"{self._base_url}/point/{box.center_lat:.4f}/"
            f"{box.center_lon:.4f}/{box.query_radius_nm}"
        )


class Tar1090Source(Source):
    """A local dump1090/tar1090 receiver, used as an ADS-B receiver input."""

    name = "tar1090"
    max_stale_s = 60.0

    def __init__(self, base_url: str, timeout_s: float = 8.0) -> None:
        self._base_url = base_url
        self._timeout_s = timeout_s

    async def fetch(self, client: httpx.AsyncClient, box: BoundingBox) -> SourceResult:
        started = time.monotonic()
        result = SourceResult(name=self.name)

        try:
            response = await client.get(
                f"{self._base_url}/data/aircraft.json",
                headers={"User-Agent": "flight-radar-merge/1.0"},
                timeout=self._timeout_s,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            result.error = _describe(exc)
            result.duration_s = time.monotonic() - started
            return result

        # aircraft.json is keyed by transponder address; some builds return a
        # bare array instead.
        if isinstance(body, dict):
            items: Iterable[Any] = body.values()
        elif isinstance(body, list):
            items = body
        else:
            items = []

        now = time.time()
        for item in items:
            row = from_readb(item, now)
            if row is None:
                continue
            seen = item.get("seen") if isinstance(item, dict) else None
            if isinstance(seen, (int, float)) and now - seen > self.max_stale_s:
                continue
            result.rows.append(row)

        result.duration_s = time.monotonic() - started
        return result


def build_sources(
    settings: Settings,
    client: httpx.AsyncClient,
) -> list[Source]:
    """Instantiate the enabled sources in merge priority order.

    OpenSky runs first because it wins every field conflict. The ADS-B sources
    then only fill gaps and add aircraft OpenSky has not seen at all.
    """
    sources: list[Source] = [
        OpenSkySource(settings, OpenSkyTokenManager(settings, client)),
    ]

    timeout = settings.adsb_timeout_s

    for spec in settings.adsb_sources:
        if spec == "adsb.lol":
            sources.append(AdsbLolSource(settings.adsb_lol_url, timeout))
        elif spec == "adsb.fi":
            sources.append(AdsbFiSource(settings.adsb_fi_url, timeout))
        elif spec == "adsb.im":
            if settings.adsb_im_url:
                sources.append(AdsbImSource(settings.adsb_im_url, timeout))
            else:
                log.warning("ADSB_SOURCES lists adsb.im but ADSB_IM_URL is empty; skipping")
        elif spec == "tar1090":
            if settings.tar1090_url:
                sources.append(Tar1090Source(settings.tar1090_url, timeout))
            else:
                log.warning("ADSB_SOURCES lists tar1090 but TAR1090_URL is empty; skipping")
        elif spec.startswith("custom="):
            sources.append(
                CustomReadbSource(f"custom:{spec[7:]}", spec[7:].rstrip("/"), timeout)
            )
        else:
            log.warning("Unknown entry in ADSB_SOURCES: %s", spec)

    return sources


def _describe(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} from {exc.request.url}"
    if isinstance(exc, httpx.TimeoutException):
        return f"timeout after {type(exc).__name__}"
    return f"{type(exc).__name__}: {exc}"
