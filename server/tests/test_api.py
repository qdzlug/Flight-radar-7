"""API level tests for caching, stale serving and upstream failure handling.

The upstream fetch is monkeypatched so these run offline and deterministically.

Run with:  python -m pytest server/tests -q
"""

from __future__ import annotations

import dataclasses
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import main as api  # noqa: E402
from app.statevector import ICAO24, LATITUDE, LONGITUDE  # noqa: E402

BOX = "lamin=13.10&lamax=13.30&lomin=77.60&lomax=77.80"


def sample_payload(row_count=3):
    return {
        "time": int(time.time()),
        "states": [
            [f"{i:06x}", "TEST", "India", 0, 0, 77.7, 13.2, 9000.0, False,
             220.0, 90.0, 0.0, None, 9500.0, "1234", False, 0, 4, "A320", "VT-SCH"]
            for i in range(row_count)
        ],
        "meta": {"rows": row_count},
    }


@pytest.fixture
def client(monkeypatch):
    with TestClient(api.app) as test_client:
        runtime = test_client.app.state.runtime
        # Short TTLs keep the timing assertions fast and predictable. The cache
        # is rebuilt too, because it captured the TTL at construction time.
        settings = dataclasses.replace(
            runtime.settings,
            cache_ttl_s=0.4,
            stale_grace_s=60.0,
        )
        test_client.app.state.runtime = dataclasses.replace(
            runtime,
            settings=settings,
            cache=api.MergeCache(settings.cache_ttl_s, settings.cache_max_entries),
        )
        yield test_client


class FakeFetcher:
    """Stands in for build_payload; can be told to succeed or fail."""

    def __init__(self) -> None:
        self.calls = 0
        self.result = sample_payload()
        self.fails = False

    async def __call__(self, runtime, box):
        self.calls += 1
        if self.fails:
            return None
        return self.result


@pytest.fixture
def fetcher(monkeypatch):
    fake = FakeFetcher()
    monkeypatch.setattr(api, "build_payload", fake)
    return fake


def test_returns_merged_states(client, fetcher):
    response = client.get(f"/states/all?{BOX}")

    assert response.status_code == 200
    assert response.headers["x-merge-cache"] == "miss"
    assert len(response.json()["states"]) == 3
    assert fetcher.calls == 1


def test_second_request_within_ttl_is_cached(client, fetcher):
    client.get(f"/states/all?{BOX}")
    response = client.get(f"/states/all?{BOX}")

    assert response.headers["x-merge-cache"] == "hit"
    assert response.json()["states"]
    assert fetcher.calls == 1


def test_cache_expires_and_refetches(client, fetcher):
    client.get(f"/states/all?{BOX}")
    time.sleep(0.5)
    response = client.get(f"/states/all?{BOX}")

    assert response.headers["x-merge-cache"] == "miss"
    assert fetcher.calls == 2


def test_nearby_boxes_share_a_cache_entry(client, fetcher):
    # Rounded to 0.01 deg (~1 km), so a slightly different box hits the entry.
    client.get(f"/states/all?{BOX}")
    response = client.get("/states/all?lamin=13.1004&lamax=13.3002&lomin=77.6001&lomax=77.8003")

    assert response.headers["x-merge-cache"] == "hit"
    assert fetcher.calls == 1


def test_different_boxes_are_cached_separately(client, fetcher):
    client.get(f"/states/all?{BOX}")
    response = client.get("/states/all?lamin=20.10&lamax=20.30&lomin=70.60&lomax=70.80")

    assert response.headers["x-merge-cache"] == "miss"
    assert fetcher.calls == 2


def test_serves_stale_data_when_every_source_fails(client, fetcher):
    client.get(f"/states/all?{BOX}")

    fetcher.fails = True
    time.sleep(0.5)
    response = client.get(f"/states/all?{BOX}")

    assert response.status_code == 200
    assert response.headers["x-merge-cache"] == "stale"
    assert len(response.json()["states"]) == 3


def test_returns_502_on_cold_cache_total_failure(client, fetcher):
    fetcher.fails = True
    response = client.get(f"/states/all?{BOX}")

    assert response.status_code == 502
    assert "every upstream source failed" in response.json()["error"]


def test_stale_data_expires(client, fetcher):
    client.get(f"/states/all?{BOX}")

    fetcher.fails = True
    # Move the stored entry's timestamp past the stale grace period.
    runtime = client.app.state.runtime
    for entry in runtime.cache._entries.values():
        entry.stored_at = time.time() - 3600

    response = client.get(f"/states/all?{BOX}")
    assert response.status_code == 502


def test_root_serves_the_index_without_parameters(client, fetcher):
    body = client.get("/").json()

    assert body["states_endpoint"] == "/states/all"
    assert "opensky" in body["sources"]
    assert fetcher.calls == 0


def test_root_serves_states_when_given_a_box(client, fetcher):
    response = client.get(f"/?{BOX}")

    assert response.status_code == 200
    assert len(response.json()["states"]) == 3
    assert fetcher.calls == 1


def test_states_alias(client, fetcher):
    response = client.get(f"/states?{BOX}")

    assert response.status_code == 200
    assert len(response.json()["states"]) == 3


def test_missing_parameters_are_rejected(client, fetcher):
    response = client.get("/states/all?lamin=13.10&lamax=13.30")

    assert response.status_code == 400
    assert "lomin" in response.json()["error"]
    assert fetcher.calls == 0


def test_inverted_box_is_rejected(client, fetcher):
    response = client.get("/states/all?lamin=13.30&lamax=13.10&lomin=77.60&lomax=77.80")

    assert response.status_code == 400
    assert fetcher.calls == 0


def test_health(client, fetcher):
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert "opensky" in body["sources"]
    assert body["cache"]["entries"] >= 0
