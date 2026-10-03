from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .config import Settings, load_settings
from .enrichment import EnrichmentService, build_enrichment_service
from .geo import BoundingBox, InvalidBox
from .merge import merge
from .sources import Source, SourceResult, build_sources

log = logging.getLogger("merge.api")


@dataclass
class CacheEntry:
    payload: dict[str, Any]
    stored_at: float

    def age(self) -> float:
        return time.time() - self.stored_at


class MergeCache:
    """Small TTL cache keyed by the rounded bounding box.

    A radar polls every few seconds and there is no reason to hit OpenSky once
    per device per poll, so identical requests inside the TTL share one fetch.
    Per-key locks stop a cold cache from stampeding the upstream APIs.
    """

    def __init__(self, ttl_s: float, max_entries: int) -> None:
        self._ttl_s = ttl_s
        self._max_entries = max(1, max_entries)
        self._entries: dict[Any, CacheEntry] = {}
        self._locks: dict[Any, asyncio.Lock] = {}
        self._guard = asyncio.Lock()
        self.hits = 0
        self.misses = 0

    async def lock_for(self, key: Any) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    def get(self, key: Any) -> CacheEntry | None:
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        if entry.age() > self._ttl_s:
            self.misses += 1
            return None
        self.hits += 1
        return entry

    def get_stale(self, key: Any, grace_s: float) -> CacheEntry | None:
        entry = self._entries.get(key)
        if entry is None or entry.age() > grace_s:
            return None
        return entry

    def put(self, key: Any, payload: dict[str, Any]) -> None:
        if len(self._entries) >= self._max_entries:
            oldest = min(self._entries, key=lambda k: self._entries[k].stored_at)
            self._entries.pop(oldest, None)
        self._entries[key] = CacheEntry(payload=payload, stored_at=time.time())

    def stats(self) -> dict[str, Any]:
        return {
            "entries": len(self._entries),
            "hits": self.hits,
            "misses": self.misses,
            "ttl_s": self._ttl_s,
        }


@dataclass
class Runtime:
    settings: Settings
    client: httpx.AsyncClient
    sources: list[Source]
    cache: MergeCache
    enrichment: EnrichmentService
    started_at: float


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    _configure_logging(settings.log_level)

    limits = httpx.Limits(max_connections=16, max_keepalive_connections=8)
    client = httpx.AsyncClient(
        limits=limits,
        follow_redirects=True,
        timeout=httpx.Timeout(15.0),
    )

    sources = build_sources(settings, client)
    enrichment = build_enrichment_service(settings, client)
    runtime = Runtime(
        settings=settings,
        client=client,
        sources=sources,
        cache=MergeCache(settings.cache_ttl_s, settings.cache_max_entries),
        enrichment=enrichment,
        started_at=time.time(),
    )
    app.state.runtime = runtime

    log.info(
        "merge service ready: sources=%s opensky_auth=%s enrichment=%s max_states=%d",
        [source.name for source in sources],
        settings.has_opensky_credentials,
        enrichment.provider.name,
        settings.max_states,
    )

    try:
        yield
    finally:
        await enrichment.close()
        await client.aclose()


app = FastAPI(
    title="Flight Radar merge service",
    description=(
        "Merges OpenSky Network state vectors with ADS-B sources and returns a "
        "single OpenSky-compatible /states/all response for the ESP32 flight "
        "radar firmware."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def _runtime(request: Request) -> Runtime:
    return request.app.state.runtime


async def _collect(runtime: Runtime, box: BoundingBox) -> list[SourceResult]:
    """Fetch every source concurrently, never raising."""
    # Per-request httpx timeouts are read timeouts, so a slow trickling body
    # can outlast them. Each fetch also gets a hard overall deadline.
    deadline = max(runtime.settings.open_sky_timeout_s, runtime.settings.adsb_timeout_s) + 3.0

    async def bounded(source: Source) -> SourceResult:
        started = time.monotonic()
        try:
            async with asyncio.timeout(deadline):
                return await source.fetch(runtime.client, box)
        except TimeoutError:
            return SourceResult(
                name=source.name,
                error=f"timeout: exceeded {deadline:.0f}s overall deadline",
                duration_s=time.monotonic() - started,
            )

    gathered = await asyncio.gather(
        *(bounded(source) for source in runtime.sources),
        return_exceptions=True,
    )

    results: list[SourceResult] = []
    for source, outcome in zip(runtime.sources, gathered):
        if isinstance(outcome, BaseException):
            log.warning("Source %s raised: %s", source.name, outcome)
            results.append(SourceResult(name=source.name, error=str(outcome)))
        else:
            results.append(outcome)
    return results


async def build_payload(runtime: Runtime, box: BoundingBox) -> dict[str, Any] | None:
    """Fetch, merge and return a fresh payload, or None if every source failed."""
    results = await _collect(runtime, box)

    if not any(result.ok for result in results):
        return None

    rows, stats = merge(
        results,
        box,
        max_states=runtime.settings.max_states,
        require_position=runtime.settings.require_position,
        drop_on_ground=runtime.settings.drop_on_ground,
    )

    await runtime.enrichment.enrich_rows(rows)

    return {
        "time": int(time.time()),
        "states": rows,
        "meta": stats.as_meta(),
    }


async def states_response(
    request: Request,
    lamin: float | None,
    lamax: float | None,
    lomin: float | None,
    lomax: float | None,
) -> JSONResponse:
    runtime = _runtime(request)

    try:
        box = BoundingBox.from_query(lamin, lamax, lomin, lomax)
    except InvalidBox as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    key = box.cache_key()
    cache = runtime.cache

    entry = cache.get(key)
    if entry is not None:
        return JSONResponse(
            entry.payload,
            headers={"X-Merge-Cache": "hit", "X-Merge-Age": f"{entry.age():.1f}"},
        )

    lock = await cache.lock_for(key)
    async with lock:
        # Another waiter may have refreshed the entry while we queued.
        entry = cache.get(key)
        if entry is not None:
            return JSONResponse(
                entry.payload,
                headers={"X-Merge-Cache": "hit", "X-Merge-Age": f"{entry.age():.1f}"},
            )

        payload = await build_payload(runtime, box)

        if payload is None:
            stale = cache.get_stale(key, runtime.settings.stale_grace_s)
            if stale is not None:
                log.warning("All sources failed, serving stale data (%.0fs old)", stale.age())
                return JSONResponse(
                    stale.payload,
                    headers={
                        "X-Merge-Cache": "stale",
                        "X-Merge-Age": f"{stale.age():.1f}",
                    },
                )
            log.error("All sources failed for box %s", key)
            return JSONResponse(
                {"error": "every upstream source failed", "box": key},
                status_code=502,
            )

        cache.put(key, payload)

    return JSONResponse(payload, headers={"X-Merge-Cache": "miss"})


@app.get("/states/all", summary="Merged OpenSky-compatible state vectors")
@app.get("/states", include_in_schema=False)
async def get_states(
    request: Request,
    lamin: float | None = Query(default=None),
    lamax: float | None = Query(default=None),
    lomin: float | None = Query(default=None),
    lomax: float | None = Query(default=None),
) -> JSONResponse:
    return await states_response(request, lamin, lamax, lomin, lomax)


@app.get("/", summary="Service index")
async def index(
    request: Request,
    lamin: float | None = Query(default=None),
    lamax: float | None = Query(default=None),
    lomin: float | None = Query(default=None),
    lomax: float | None = Query(default=None),
) -> JSONResponse:
    """Serve state vectors too, so ``http://radar:8000`` works as a data URL."""
    if None not in (lamin, lamax, lomin, lomax):
        return await states_response(request, lamin, lamax, lomin, lomax)

    runtime = _runtime(request)
    return JSONResponse(
        {
            "service": "flight-radar-merge",
            "states_endpoint": "/states/all",
            "parameters": ["lamin", "lamax", "lomin", "lomax"],
            "example": "/states/all?lamin=13.1&lamax=13.3&lomin=77.6&lomax=77.8",
            "sources": [source.name for source in runtime.sources],
            "enrichment_provider": runtime.enrichment.provider.name,
            "health": "/health",
            "docs": "/docs",
        }
    )


@app.get("/health", summary="Liveness and source configuration")
async def health(request: Request) -> JSONResponse:
    runtime = _runtime(request)
    return JSONResponse(
        {
            "status": "ok",
            "uptime_s": round(time.time() - runtime.started_at, 1),
            "sources": [source.name for source in runtime.sources],
            "opensky_credentials": runtime.settings.has_opensky_credentials,
            "max_states": runtime.settings.max_states,
            "cache": runtime.cache.stats(),
            "enrichment": await runtime.enrichment.health(),
        }
    )
