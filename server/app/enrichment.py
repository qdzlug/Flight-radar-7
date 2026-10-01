"""Optional, fail-open commercial flight enrichment."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence

import httpx

from .config import Settings
from .statevector import (
    AIRLINE,
    ARRIVAL_AIRPORT,
    CALLSIGN,
    DEPARTURE_AIRPORT,
    ENRICHMENT_PROVIDER,
    ENRICHMENT_STALE,
    ENRICHMENT_UPDATED_AT,
    ESTIMATED_ARRIVAL,
    FLIGHT_NUMBER,
    FLIGHT_STATUS,
    ICAO24,
    REGISTRATION,
    Row,
)

log = logging.getLogger("merge.enrichment")


@dataclass(frozen=True)
class AircraftIdentity:
    icao24: str
    callsign: str | None
    registration: str | None

    @property
    def lookup_ident(self) -> str | None:
        return self.callsign or self.registration

    def cache_key(self, provider: str, now: float) -> str | None:
        ident = self.lookup_ident
        if not ident:
            return None
        day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
        return f"{provider}:{ident.upper()}:{day}"


@dataclass(frozen=True)
class EnrichmentData:
    flight_number: str | None = None
    departure_airport: str | None = None
    arrival_airport: str | None = None
    flight_status: str | None = None
    estimated_arrival: int | None = None
    airline: str | None = None
    provider: str = "none"
    updated_at: int = 0
    provider_flight_id: str | None = None

    def apply(self, row: Row, *, stale: bool) -> None:
        row[FLIGHT_NUMBER] = self.flight_number
        row[DEPARTURE_AIRPORT] = self.departure_airport
        row[ARRIVAL_AIRPORT] = self.arrival_airport
        row[FLIGHT_STATUS] = self.flight_status
        row[ESTIMATED_ARRIVAL] = self.estimated_arrival
        row[AIRLINE] = self.airline
        row[ENRICHMENT_PROVIDER] = self.provider
        row[ENRICHMENT_UPDATED_AT] = self.updated_at
        row[ENRICHMENT_STALE] = stale


@dataclass(frozen=True)
class CacheRecord:
    data: EnrichmentData | None
    stale: bool
    negative: bool


class EnrichmentProvider(Protocol):
    name: str

    async def lookup(self, identity: AircraftIdentity) -> EnrichmentData | None: ...


class DisabledProvider:
    name = "none"

    async def lookup(self, identity: AircraftIdentity) -> EnrichmentData | None:
        return None


class FlightAwareProvider:
    name = "flightaware"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: str,
        base_url: str,
        timeout_s: float,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._base_url = base_url
        self._timeout_s = timeout_s

    async def lookup(self, identity: AircraftIdentity) -> EnrichmentData | None:
        ident = identity.lookup_ident
        if not ident:
            return None

        response = await self._client.get(
            f"{self._base_url}/flights/{ident}",
            params={"max_pages": 1},
            headers={"x-apikey": self._api_key},
            timeout=self._timeout_s,
        )
        response.raise_for_status()
        body = response.json()
        flights = body.get("flights") if isinstance(body, dict) else None
        flight = self._select_active(flights, identity)
        if flight is None:
            return None

        now = int(time.time())
        return EnrichmentData(
            flight_number=_text(
                flight.get("ident_iata")
                or flight.get("ident_icao")
                or flight.get("ident")
            ),
            departure_airport=_airport(flight.get("origin")),
            arrival_airport=_airport(flight.get("destination")),
            flight_status=_text(flight.get("status")),
            estimated_arrival=_timestamp(
                flight.get("estimated_on")
                or flight.get("scheduled_on")
                or flight.get("estimated_in")
            ),
            airline=_text(
                flight.get("operator_name")
                or flight.get("operator")
                or flight.get("operator_icao")
            ),
            provider=self.name,
            updated_at=now,
            provider_flight_id=_text(flight.get("fa_flight_id")),
        )

    @staticmethod
    def _select_active(
        flights: Any,
        identity: AircraftIdentity,
    ) -> dict[str, Any] | None:
        if not isinstance(flights, list):
            return None
        candidates = [flight for flight in flights if isinstance(flight, dict)]

        if identity.registration:
            registration = identity.registration.upper()
            matching = [
                flight
                for flight in candidates
                if _text(flight.get("registration"), upper=True) == registration
            ]
            if matching:
                candidates = matching

        active = [
            flight
            for flight in candidates
            if not flight.get("actual_on")
            and _text(flight.get("status"), upper=True) not in {"CANCELLED", "CANCELED"}
        ]
        return active[0] if len(active) == 1 else None


class SQLiteEnrichmentCache:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=2.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS enrichment_cache (
                    cache_key TEXT PRIMARY KEY,
                    payload TEXT,
                    negative INTEGER NOT NULL,
                    stored_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )

    async def get_many(
        self,
        keys: Sequence[str],
        *,
        now: float,
        stale_grace_s: float,
    ) -> dict[str, CacheRecord]:
        if not keys:
            return {}
        return await asyncio.to_thread(self._get_many, list(dict.fromkeys(keys)), now, stale_grace_s)

    def _get_many(
        self,
        keys: list[str],
        now: float,
        stale_grace_s: float,
    ) -> dict[str, CacheRecord]:
        placeholders = ",".join("?" for _ in keys)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM enrichment_cache WHERE cache_key IN ({placeholders})",
                keys,
            ).fetchall()

        records: dict[str, CacheRecord] = {}
        for row in rows:
            negative = bool(row["negative"])
            expired = now > float(row["expires_at"])
            if expired and (negative or now > float(row["expires_at"]) + stale_grace_s):
                continue
            payload = json.loads(row["payload"]) if row["payload"] else None
            records[row["cache_key"]] = CacheRecord(
                data=EnrichmentData(**payload) if payload else None,
                stale=expired,
                negative=negative,
            )
        return records

    async def put(
        self,
        key: str,
        data: EnrichmentData | None,
        *,
        ttl_s: float,
    ) -> None:
        await asyncio.to_thread(self._put, key, data, ttl_s)

    def _put(self, key: str, data: EnrichmentData | None, ttl_s: float) -> None:
        now = time.time()
        payload = json.dumps(asdict(data), separators=(",", ":")) if data else None
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO enrichment_cache(cache_key, payload, negative, stored_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    payload=excluded.payload,
                    negative=excluded.negative,
                    stored_at=excluded.stored_at,
                    expires_at=excluded.expires_at
                """,
                (key, payload, int(data is None), now, now + ttl_s),
            )

    async def count(self) -> int:
        return await asyncio.to_thread(self._count)

    def _count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) FROM enrichment_cache").fetchone()
        return int(row[0])


class VolatileEnrichmentCache:
    """In-memory fallback used when persistent cache initialization fails."""

    def __init__(self) -> None:
        self._records: dict[str, tuple[EnrichmentData | None, float]] = {}

    async def get_many(
        self,
        keys: Sequence[str],
        *,
        now: float,
        stale_grace_s: float,
    ) -> dict[str, CacheRecord]:
        records: dict[str, CacheRecord] = {}
        for key in keys:
            stored = self._records.get(key)
            if stored is None:
                continue
            data, expires_at = stored
            expired = now > expires_at
            if expired and (data is None or now > expires_at + stale_grace_s):
                continue
            records[key] = CacheRecord(
                data=data,
                stale=expired,
                negative=data is None,
            )
        return records

    async def put(
        self,
        key: str,
        data: EnrichmentData | None,
        *,
        ttl_s: float,
    ) -> None:
        self._records[key] = (data, time.time() + ttl_s)

    async def count(self) -> int:
        return len(self._records)


class EnrichmentService:
    """Adds cached data immediately and schedules provider lookups in the background."""

    def __init__(
        self,
        settings: Settings,
        provider: EnrichmentProvider,
        cache: SQLiteEnrichmentCache | VolatileEnrichmentCache,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.cache = cache
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._deferred_until: dict[str, float] = {}
        self._hour_requests: deque[float] = deque()
        self._day_requests: deque[float] = deque()
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self.counters = {
            "cache_hits": 0,
            "stale_hits": 0,
            "negative_hits": 0,
            "scheduled": 0,
            "provider_success": 0,
            "provider_empty": 0,
            "provider_failures": 0,
            "budget_skips": 0,
            "cache_errors": 0,
        }

    @property
    def enabled(self) -> bool:
        return self.provider.name != "none"

    async def enrich_rows(self, rows: list[Row]) -> None:
        if not self.enabled:
            return

        now = time.time()
        keyed: list[tuple[Row, str, AircraftIdentity]] = []
        for row in rows:
            identity = _identity(row)
            key = identity.cache_key(self.provider.name, now)
            if key:
                keyed.append((row, key, identity))

        try:
            records = await self.cache.get_many(
                [key for _, key, _ in keyed],
                now=now,
                stale_grace_s=self.settings.enrichment_stale_grace_s,
            )
        except Exception as exc:  # noqa: BLE001 - enrichment must fail open
            self.counters["cache_errors"] += 1
            log.warning("Enrichment cache read failed: %s", exc)
            records = {}

        for row, key, identity in keyed:
            record = records.get(key)
            if record and record.data:
                record.data.apply(row, stale=record.stale)
                counter = "stale_hits" if record.stale else "cache_hits"
                self.counters[counter] += 1
            elif record and record.negative:
                self.counters["negative_hits"] += 1

            if self.enabled and (record is None or record.stale):
                self._schedule(key, identity)

    def _schedule(self, key: str, identity: AircraftIdentity) -> None:
        if key in self._tasks or time.time() < self._deferred_until.get(key, 0.0):
            return
        task = asyncio.create_task(self._lookup(key, identity))
        self._tasks[key] = task
        self.counters["scheduled"] += 1
        task.add_done_callback(lambda _: self._tasks.pop(key, None))

    async def _lookup(self, key: str, identity: AircraftIdentity) -> None:
        if not self._request_allowed():
            self.counters["budget_skips"] += 1
            self._deferred_until[key] = time.time() + 60.0
            return

        self._record_request()
        try:
            data = await asyncio.wait_for(
                self.provider.lookup(identity),
                timeout=self.settings.enrichment_timeout_s,
            )
            ttl = (
                self.settings.enrichment_ttl_s
                if data
                else self.settings.enrichment_negative_ttl_s
            )
            await self.cache.put(key, data, ttl_s=ttl)
            self._consecutive_failures = 0
            counter = "provider_success" if data else "provider_empty"
            self.counters[counter] += 1
        except Exception as exc:  # noqa: BLE001 - enrichment must fail open
            self.counters["provider_failures"] += 1
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                self._circuit_open_until = time.time() + 60.0
            self._deferred_until[key] = time.time() + 60.0
            log.warning("%s enrichment failed for %s: %s", self.provider.name, key, exc)

    def _request_allowed(self) -> bool:
        now = time.time()
        if now < self._circuit_open_until:
            return False
        self._prune_requests(now)
        hour_limit = self.settings.enrichment_max_requests_hour
        day_limit = self.settings.enrichment_max_requests_day
        return not (
            (hour_limit and len(self._hour_requests) >= hour_limit)
            or (day_limit and len(self._day_requests) >= day_limit)
        )

    def _record_request(self) -> None:
        now = time.time()
        self._hour_requests.append(now)
        self._day_requests.append(now)

    def _prune_requests(self, now: float) -> None:
        while self._hour_requests and self._hour_requests[0] < now - 3600:
            self._hour_requests.popleft()
        while self._day_requests and self._day_requests[0] < now - 86400:
            self._day_requests.popleft()

    async def health(self) -> dict[str, Any]:
        self._prune_requests(time.time())
        try:
            entries = await self.cache.count()
        except Exception:  # noqa: BLE001 - health still reports provider state
            entries = None
        return {
            "provider": self.provider.name,
            "enabled": self.enabled,
            "cache_entries": entries,
            "in_flight": len(self._tasks),
            "circuit_open": time.time() < self._circuit_open_until,
            "requests_last_hour": len(self._hour_requests),
            "requests_last_day": len(self._day_requests),
            **self.counters,
        }

    async def close(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)


def build_enrichment_service(
    settings: Settings,
    client: httpx.AsyncClient,
) -> EnrichmentService:
    requested = settings.enrichment_provider
    if requested == "flightaware" and settings.has_flightaware_credentials:
        provider: EnrichmentProvider = FlightAwareProvider(
            client,
            api_key=settings.flightaware_api_key,
            base_url=settings.flightaware_base_url,
            timeout_s=settings.enrichment_timeout_s,
        )
    else:
        if requested not in {"none", "flightaware"}:
            log.warning("Unknown enrichment provider '%s'; using local-only mode", requested)
        elif requested == "flightaware" and not settings.has_flightaware_credentials:
            log.warning("FlightAware enrichment requested without FLIGHTAWARE_API_KEY")
        provider = DisabledProvider()

    if provider.name == "none":
        cache: SQLiteEnrichmentCache | VolatileEnrichmentCache = VolatileEnrichmentCache()
    else:
        try:
            cache = SQLiteEnrichmentCache(settings.enrichment_cache_path)
        except Exception as exc:  # noqa: BLE001 - base service must still start
            log.warning("Persistent enrichment cache unavailable, using memory: %s", exc)
            cache = VolatileEnrichmentCache()
    return EnrichmentService(settings, provider, cache)


def _identity(row: Row) -> AircraftIdentity:
    return AircraftIdentity(
        icao24=str(row[ICAO24]),
        callsign=_text(row[CALLSIGN], upper=True),
        registration=_text(row[REGISTRATION], upper=True),
    )


def _text(value: Any, *, upper: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned.upper() if upper else cleaned


def _airport(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    return _text(value.get("code_icao") or value.get("code"), upper=True)


def _timestamp(value: Any) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(parsed.timestamp())
