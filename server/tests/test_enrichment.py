"""Offline tests for optional commercial flight enrichment."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import load_settings  # noqa: E402
from app.enrichment import (  # noqa: E402
    AircraftIdentity,
    DisabledProvider,
    EnrichmentData,
    EnrichmentService,
    FlightAwareProvider,
    SQLiteEnrichmentCache,
    VolatileEnrichmentCache,
    build_enrichment_service,
)
from app.statevector import (  # noqa: E402
    ARRIVAL_AIRPORT,
    CALLSIGN,
    DEPARTURE_AIRPORT,
    ENRICHMENT_PROVIDER,
    ENRICHMENT_STALE,
    FIELD_COUNT,
    FLIGHT_NUMBER,
    FLIGHT_STATUS,
    ICAO24,
    empty_row,
)


def settings(tmp_path, **overrides):
    base = load_settings()
    values = {
        "enrichment_cache_path": str(tmp_path / "enrichment.sqlite3"),
        "enrichment_provider": "none",
    }
    values.update(overrides)
    return dataclasses.replace(base, **values)


def aircraft_row(callsign="UAL123"):
    row = empty_row()
    row[ICAO24] = "abc123"
    row[CALLSIGN] = callsign
    return row


def test_enrichment_fields_are_additive():
    row = aircraft_row()
    assert len(row) == FIELD_COUNT == 29
    assert row[:20][ICAO24] == "abc123"

    data = EnrichmentData(
        flight_number="UA123",
        departure_airport="KDEN",
        arrival_airport="KORD",
        flight_status="En Route",
        provider="flightaware",
        updated_at=1_700_000_000,
    )
    data.apply(row, stale=False)

    assert row[FLIGHT_NUMBER] == "UA123"
    assert row[DEPARTURE_AIRPORT] == "KDEN"
    assert row[ARRIVAL_AIRPORT] == "KORD"
    assert row[FLIGHT_STATUS] == "En Route"
    assert row[ENRICHMENT_PROVIDER] == "flightaware"
    assert row[ENRICHMENT_STALE] is False


def test_sqlite_cache_fresh_stale_and_negative(tmp_path):
    async def scenario():
        cache = SQLiteEnrichmentCache(str(tmp_path / "cache.sqlite3"))
        data = EnrichmentData(flight_number="UA123", provider="test", updated_at=1)
        await cache.put("fresh", data, ttl_s=60)
        await cache.put("stale", data, ttl_s=0.01)
        await cache.put("negative", None, ttl_s=60)
        await asyncio.sleep(0.02)

        records = await cache.get_many(
            ["fresh", "stale", "negative"],
            now=time.time(),
            stale_grace_s=60,
        )
        assert records["fresh"].stale is False
        assert records["stale"].stale is True
        assert records["negative"].negative is True

    asyncio.run(scenario())


def test_disabled_provider_never_schedules_lookup(tmp_path):
    async def scenario():
        service = EnrichmentService(
            settings(tmp_path),
            DisabledProvider(),
            SQLiteEnrichmentCache(str(tmp_path / "cache.sqlite3")),
        )
        row = aircraft_row()
        await service.enrich_rows([row])
        assert service.enabled is False
        assert service.counters["scheduled"] == 0
        assert row[FLIGHT_NUMBER] is None

    asyncio.run(scenario())


class FakeProvider:
    name = "fake"

    def __init__(self, *, fails=False):
        self.calls = 0
        self.fails = fails

    async def lookup(self, identity):
        self.calls += 1
        await asyncio.sleep(0)
        if self.fails:
            raise RuntimeError("provider unavailable")
        return EnrichmentData(
            flight_number="UA123",
            departure_airport="KDEN",
            arrival_airport="KORD",
            provider=self.name,
            updated_at=int(time.time()),
        )


def test_background_lookup_is_deduplicated_and_applied_later(tmp_path):
    async def scenario():
        provider = FakeProvider()
        service = EnrichmentService(
            settings(tmp_path),
            provider,
            SQLiteEnrichmentCache(str(tmp_path / "cache.sqlite3")),
        )

        first = aircraft_row()
        await service.enrich_rows([first])
        await service.enrich_rows([aircraft_row()])
        assert first[FLIGHT_NUMBER] is None
        assert service.counters["scheduled"] == 1

        await asyncio.gather(*service._tasks.values())
        second = aircraft_row()
        await service.enrich_rows([second])
        assert provider.calls == 1
        assert second[FLIGHT_NUMBER] == "UA123"
        assert second[DEPARTURE_AIRPORT] == "KDEN"

    asyncio.run(scenario())


def test_provider_failure_leaves_base_row_untouched(tmp_path):
    async def scenario():
        provider = FakeProvider(fails=True)
        service = EnrichmentService(
            settings(tmp_path),
            provider,
            SQLiteEnrichmentCache(str(tmp_path / "cache.sqlite3")),
        )
        row = aircraft_row()
        await service.enrich_rows([row])
        await asyncio.gather(*service._tasks.values())
        assert row[FLIGHT_NUMBER] is None
        assert service.counters["provider_failures"] == 1

    asyncio.run(scenario())


def test_hourly_budget_prevents_provider_call(tmp_path):
    async def scenario():
        provider = FakeProvider()
        limited = settings(tmp_path, enrichment_max_requests_hour=0)
        # A zero limit means unlimited; set one and pre-consume it.
        limited = dataclasses.replace(limited, enrichment_max_requests_hour=1)
        service = EnrichmentService(
            limited,
            provider,
            SQLiteEnrichmentCache(str(tmp_path / "cache.sqlite3")),
        )
        service._record_request()
        await service.enrich_rows([aircraft_row()])
        await asyncio.gather(*service._tasks.values())
        assert provider.calls == 0
        assert service.counters["budget_skips"] == 1

    asyncio.run(scenario())


def test_flightaware_normalises_single_active_flight():
    async def scenario():
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["x-apikey"] == "secret"
            return httpx.Response(
                200,
                json={
                    "flights": [
                        {
                            "fa_flight_id": "UAL123-1",
                            "ident_iata": "UA123",
                            "registration": "N12345",
                            "status": "En Route",
                            "estimated_on": "2026-10-01T20:30:00Z",
                            "origin": {"code_icao": "KDEN"},
                            "destination": {"code_icao": "KORD"},
                            "operator_name": "United Airlines",
                            "actual_on": None,
                        }
                    ]
                },
                request=request,
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = FlightAwareProvider(
                client,
                api_key="secret",
                base_url="https://example.test/aeroapi",
                timeout_s=1,
            )
            data = await provider.lookup(
                AircraftIdentity("abc123", "UAL123", "N12345")
            )

        assert data is not None
        assert data.flight_number == "UA123"
        assert data.departure_airport == "KDEN"
        assert data.arrival_airport == "KORD"
        assert data.airline == "United Airlines"

    asyncio.run(scenario())


def test_flightaware_rejects_ambiguous_active_results():
    flights = [
        {"fa_flight_id": "one", "status": "En Route", "actual_on": None},
        {"fa_flight_id": "two", "status": "En Route", "actual_on": None},
    ]
    identity = AircraftIdentity("abc123", "UAL123", None)
    assert FlightAwareProvider._select_active(flights, identity) is None


def test_missing_flightaware_key_falls_back_to_disabled(tmp_path):
    async def scenario():
        configured = settings(
            tmp_path,
            enrichment_provider="flightaware",
            flightaware_api_key="",
        )
        async with httpx.AsyncClient() as client:
            service = build_enrichment_service(configured, client)
        assert service.provider.name == "none"
        assert isinstance(service.cache, VolatileEnrichmentCache)

    asyncio.run(scenario())


def test_unusable_sqlite_path_falls_back_to_memory(tmp_path):
    async def scenario():
        invalid_path = tmp_path / "is-a-directory"
        invalid_path.mkdir()
        configured = settings(
            tmp_path,
            enrichment_cache_path=str(invalid_path),
            enrichment_provider="flightaware",
            flightaware_api_key="secret",
        )
        async with httpx.AsyncClient() as client:
            service = build_enrichment_service(configured, client)
        assert isinstance(service.cache, VolatileEnrichmentCache)

    asyncio.run(scenario())
