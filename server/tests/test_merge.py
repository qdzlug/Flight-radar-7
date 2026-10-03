"""Unit tests for the merge logic and source normalisation.

Run with:  python -m pytest server/tests -q
"""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.geo import BoundingBox, InvalidBox, haversine_km  # noqa: E402
from app.merge import merge  # noqa: E402
from app.sources import SourceResult  # noqa: E402
from app.statevector import (  # noqa: E402
    BARO_ALTITUDE,
    CATEGORY,
    CALLSIGN,
    FIELD_COUNT,
    GEO_ALTITUDE,
    ICAO24,
    LATITUDE,
    LONGITUDE,
    ON_GROUND,
    POSITION_SOURCE,
    REGISTRATION,
    TYPECODE,
    VELOCITY,
    VERTICAL_RATE,
    from_readb,
    normalise_opensky_row,
)

BANGALORE = BoundingBox(lamin=13.10, lamax=13.30, lomin=77.60, lomax=77.80)


def make_opensky_row(icao24="abc123", overrides=None):
    """Build a full OpenSky row; ``overrides`` is keyed by the index constants."""
    row = [
        icao24,  # 0 icao24
        "TEST123 ",  # 1 callsign
        "India",  # 2 origin_country
        int(time.time()),  # 3 time_position
        int(time.time()),  # 4 last_contact
        77.70,  # 5 longitude
        13.20,  # 6 latitude
        9000.0,  # 7 baro_altitude
        False,  # 8 on_ground
        220.0,  # 9 velocity
        90.0,  # 10 true_track
        -1.5,  # 11 vertical_rate
        None,  # 12 sensors
        9500.0,  # 13 geo_altitude
        "1234",  # 14 squawk
        False,  # 15 spi
        0,  # 16 position_source
        None,  # 17 category
        None,  # 18 typecode
        None,  # 19 registration
    ]
    for index, value in (overrides or {}).items():
        row[index] = value
    return row


class TestBoundingBox:
    def test_requires_all_four_bounds(self):
        with pytest.raises(InvalidBox):
            BoundingBox.from_query(13.1, 13.3, 77.6, None)

    def test_rejects_inverted_bounds(self):
        with pytest.raises(InvalidBox):
            BoundingBox.from_query(13.3, 13.1, 77.6, 77.8)
        with pytest.raises(InvalidBox):
            BoundingBox.from_query(13.1, 13.3, 77.8, 77.6)

    def test_rejects_out_of_range_coordinates(self):
        with pytest.raises(InvalidBox):
            BoundingBox.from_query(-91.0, 13.3, 77.6, 77.8)
        with pytest.raises(InvalidBox):
            BoundingBox.from_query(13.1, 13.3, 77.6, 181.0)

    def test_contains(self):
        assert BANGALORE.contains(13.20, 77.70)
        assert not BANGALORE.contains(12.00, 77.70)
        assert not BANGALORE.contains(13.20, 78.90)
        assert not BANGALORE.contains(None, 77.70)

    def test_query_radius_covers_the_corners(self):
        # A 0.2 deg box is roughly 22 km per side, so the corner-to-centre
        # distance must exceed the half width.
        assert 10.0 < BANGALORE.radius_km < 25.0
        assert BANGALORE.query_radius_nm >= 7

    def test_query_radius_is_capped(self):
        huge = BoundingBox(lamin=-80.0, lamax=80.0, lomin=-170.0, lomax=170.0)
        assert huge.query_radius_nm == 250

    def test_haversine(self):
        assert haversine_km(13.20, 77.70, 13.20, 77.70) == pytest.approx(0.0)
        assert haversine_km(0.0, 0.0, 0.0, 1.0) == pytest.approx(111.19, abs=0.5)


class TestReadbNormalisation:
    def test_converts_imperial_units_to_si(self):
        row = from_readb(
            {
                "hex": "06A0E7",
                "flight": "QTR945  ",
                "lat": 13.06,
                "lon": 77.46,
                "alt_baro": 40000,  # feet
                "alt_geom": 42225,  # feet
                "gs": 505.7,  # knots
                "track": 298.59,
                "baro_rate": 0,  # feet per minute
                "category": "A5",
                "t": "A359",
                "r": "A7-AMH",
                "squawk": "2202",
                "mlat": [],
                "spi": 0,
                "seen": 0.3,
                "seen_pos": 0.3,
            },
            now=1_700_000_000.0,
        )

        assert row is not None
        assert row[ICAO24] == "06a0e7"
        assert row[CALLSIGN] == "QTR945"
        assert row[BARO_ALTITUDE] == pytest.approx(40000 * 0.3048)
        assert row[GEO_ALTITUDE] == pytest.approx(42225 * 0.3048)
        assert row[VELOCITY] == pytest.approx(505.7 * 0.514444, rel=1e-3)
        assert row[VERTICAL_RATE] == pytest.approx(0.0)
        assert row[CATEGORY] == 6  # A5 = heavy
        assert row[TYPECODE] == "A359"
        assert row[REGISTRATION] == "A7-AMH"
        assert row[POSITION_SOURCE] == 0
        assert row[ON_GROUND] is False

    def test_ground_altitude_sets_on_ground(self):
        row = from_readb(
            {"hex": "abc123", "lat": 13.2, "lon": 77.7, "alt_baro": "ground"},
            now=1_700_000_000.0,
        )
        assert row is not None
        assert row[ON_GROUND] is True
        assert row[BARO_ALTITUDE] is None

    def test_mlat_flag_sets_position_source(self):
        row = from_readb(
            {"hex": "abc123", "mlat": ["1cdef"]},
            now=1_700_000_000.0,
        )
        assert row is not None
        assert row[POSITION_SOURCE] == 2

    def test_rejects_non_icao_addresses(self):
        assert from_readb({"hex": "~abc123"}, now=1.0) is None
        assert from_readb({"hex": "abc12"}, now=1.0) is None
        assert from_readb({"hex": "zzz999"}, now=1.0) is None

    def test_row_always_has_full_width(self):
        row = from_readb({"hex": "abc123"}, now=1.0)
        assert row is not None
        assert len(row) == FIELD_COUNT

    def test_skips_implausible_typecode(self):
        row = from_readb({"hex": "abc123", "t": "!!"}, now=1.0)
        assert row is not None
        assert row[TYPECODE] is None


class TestEmitterCategoryMap:
    def test_letters_map_to_opensky_integers(self):
        row = from_readb({"hex": "abc123", "category": "A0"}, now=1.0)
        assert row is not None and row[CATEGORY] == 1
        for letter, expected in (("A1", 2), ("A5", 6), ("A7", 8), ("C6", 14)):
            row = from_readb({"hex": "abc123", "category": letter}, now=1.0)
            assert row is not None and row[CATEGORY] == expected

    def test_surface_vehicles(self):
        row = from_readb({"hex": "abc123", "category": "B2"}, now=1.0)
        assert row is not None and row[CATEGORY] == 17

    def test_unknown_is_none(self):
        row = from_readb({"hex": "abc123", "category": "Z9"}, now=1.0)
        assert row is not None and row[CATEGORY] is None


class TestOpenSkyNormalisation:
    def test_pads_short_anonymous_rows(self):
        # An unauthenticated OpenSky request returns 17 fields, with no
        # category, typecode or registration.
        raw = ["abc123", "TEST123 ", "India", 1, 1, 77.7, 13.2, 9000.0,
               False, 220.0, 90.0, -1.5, None, 9500.0, "1234", False, 0]
        row = normalise_opensky_row(raw)
        assert row is not None
        assert len(row) == FIELD_COUNT
        assert row[CATEGORY] is None
        assert row[TYPECODE] is None
        assert row[CALLSIGN] == "TEST123"

    def test_strips_invalid_coordinates(self):
        row = normalise_opensky_row(
            make_opensky_row(overrides={LATITUDE: 999.0, LONGITUDE: None})
        )
        assert row is not None
        assert row[LATITUDE] is None
        assert row[LONGITUDE] is None

    def test_rejects_bad_rows(self):
        assert normalise_opensky_row(None) is None
        assert normalise_opensky_row("nope") is None
        assert normalise_opensky_row([]) is None
        assert normalise_opensky_row(["~abc123"]) is None


def result(name, rows, error=None):
    return SourceResult(name=name, rows=rows, error=error)


def opensky_result(rows):
    return result("opensky", [normalise_opensky_row(r) for r in rows])


def readb_result(name, aircraft, now=1_700_000_000.0):
    rows = [from_readb(a, now) for a in aircraft]
    return result(name, [r for r in rows if r is not None])


class TestMerge:
    def test_adsb_fills_missing_opensky_fields(self):
        base = opensky_result([make_opensky_row(overrides={CATEGORY: None, TYPECODE: None})])
        extra = readb_result(
            "adsb.lol",
            [{"hex": "abc123", "t": "A320", "r": "VT-SCH", "category": "A3"}],
        )

        rows, stats = merge([base, extra], BANGALORE)

        assert len(rows) == 1
        assert rows[0][TYPECODE] == "A320"
        assert rows[0][REGISTRATION] == "VT-SCH"
        assert rows[0][CATEGORY] == 4  # A3 = large
        assert rows[0][CALLSIGN] == "TEST123"  # from OpenSky, not overwritten
        assert stats.enriched == 1
        assert stats.added_by_adsb == 0

    def test_opensky_position_wins_over_adsb(self):
        base = opensky_result([make_opensky_row()])
        extra = readb_result(
            "adsb.lol",
            [{"hex": "abc123", "lat": 12.0, "lon": 78.0, "gs": 500}],
        )

        rows, _ = merge([base, extra], BANGALORE)

        assert rows[0][LATITUDE] == pytest.approx(13.20)
        assert rows[0][LONGITUDE] == pytest.approx(77.70)

    def test_adsb_only_aircraft_is_appended(self):
        base = opensky_result([make_opensky_row()])
        extra = readb_result(
            "adsb.lol",
            [{"hex": "def456", "lat": 13.25, "lon": 77.65, "t": "B738"}],
        )

        rows, stats = merge([base, extra], BANGALORE)

        assert [r[ICAO24] for r in rows] == ["abc123", "def456"]
        assert rows[1][TYPECODE] == "B738"
        assert stats.added_by_adsb == 1
        assert stats.from_opensky == 1

    def test_first_adsb_source_wins_conflicts(self):
        base = opensky_result([])
        lol = readb_result("adsb.lol", [{"hex": "def456", "lat": 13.2, "lon": 77.7, "t": "A320"}])
        fi = readb_result("adsb.fi", [{"hex": "def456", "lat": 13.2, "lon": 77.7, "t": "B738"}])

        rows, _ = merge([base, lol, fi], BANGALORE)

        assert rows[0][TYPECODE] == "A320"

    def test_failed_source_is_ignored(self):
        base = opensky_result([make_opensky_row()])
        broken = result("adsb.fi", [], error="HTTP 503 from https://example")

        rows, stats = merge([base, broken], BANGALORE)

        assert len(rows) == 1
        assert stats.sources["adsb.fi"]["ok"] is False

    def test_rows_outside_the_box_are_dropped(self):
        base = opensky_result([make_opensky_row()])
        far = readb_result(
            "adsb.lol",
            [{"hex": "aaa111", "lat": 10.0, "lon": 77.7}],
        )

        rows, stats = merge([base, far], BANGALORE)

        assert [r[ICAO24] for r in rows] == ["abc123"]
        assert stats.dropped_outside_box == 1

    def test_rows_without_position_are_dropped_by_default(self):
        base = opensky_result([make_opensky_row(overrides={LATITUDE: None, LONGITUDE: None})])

        rows, stats = merge([base], BANGALORE)

        assert rows == []
        assert stats.dropped_no_position == 1

    def test_rows_without_position_are_kept_when_allowed(self):
        base = opensky_result([make_opensky_row(overrides={LATITUDE: None, LONGITUDE: None})])

        rows, _ = merge([base], BANGALORE, require_position=False)

        assert len(rows) == 1

    def test_on_ground_can_be_dropped(self):
        base = opensky_result([make_opensky_row(overrides={ON_GROUND: True})])

        rows, stats = merge([base], BANGALORE, drop_on_ground=True)

        assert rows == []
        assert stats.dropped_on_ground == 1

    def test_max_states_truncates_and_reports(self):
        rows_in = [
            make_opensky_row(icao24=f"{i:06x}", overrides={LATITUDE: 13.10 + i * 0.001})
            for i in range(20)
        ]
        base = opensky_result(rows_in)

        rows, stats = merge([base], BANGALORE, max_states=5)

        assert len(rows) == 5
        assert stats.truncated == 15

    def test_duplicate_icao_within_one_source_is_deduped(self):
        base = opensky_result([make_opensky_row(), make_opensky_row()])

        rows, _ = merge([base], BANGALORE)

        assert len(rows) == 1

    def test_no_sources(self):
        rows, stats = merge([], BANGALORE)
        assert rows == []
        assert stats.rows_out == 0


def test_failed_opensky_does_not_mislabel_fallback_rows():
    opensky = SourceResult(name="opensky", error="timeout")
    adsb = SourceResult(name="adsb.lol", rows=[make_opensky_row()])
    rows, stats = merge([opensky, adsb], BANGALORE)
    assert len(rows) == 1
    assert stats.from_opensky == 0
    assert stats.added_by_adsb == 1
