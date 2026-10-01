"""OpenSky-compatible state vector rows.

Every source is normalised into the positional array layout used by the
OpenSky ``/states/all`` response so the ESP32 firmware can parse a single
format regardless of where the data came from.

Layout (index -> field), matching the OpenSky REST documentation plus optional
local and commercial enrichment fields:

     0 icao24            hex string, lower case
     1 callsign          8 chars, stripped, or null
     2 origin_country    or null
     3 time_position     unix seconds, or null
     4 last_contact      unix seconds, or null
     5 longitude         WGS-84 degrees, or null
     6 latitude          WGS-84 degrees, or null
     7 baro_altitude     metres, or null
     8 on_ground         bool
     9 velocity          metres/second, or null
    10 true_track        degrees clockwise from north, or null
    11 vertical_rate     metres/second, or null
    12 sensors           null (no filtering is done per source)
    13 geo_altitude      metres, or null
    14 squawk            or null
    15 spi               bool
    16 position_source   0 ADS-B, 1 ASTERIX, 2 MLAT, 3 FLARM
    17 category          OpenSky integer category, or null
    18 typecode          e.g. "A388", or null
    19 registration      e.g. "N123UA", or null
    20 flight_number     provider-normalised number, or null
    21 departure_airport ICAO code, or null
    22 arrival_airport   ICAO code, or null
    23 flight_status     provider-normalised status, or null
    24 estimated_arrival unix seconds, or null
    25 airline           airline/operator name, or null
    26 enrichment_source provider name, or null
    27 enrichment_time   unix seconds, or null
    28 enrichment_stale  bool
"""

from __future__ import annotations

import re
from typing import Any, Iterable

BASE_FIELD_COUNT = 20
FIELD_COUNT = 29

ICAO24 = 0
CALLSIGN = 1
ORIGIN_COUNTRY = 2
TIME_POSITION = 3
LAST_CONTACT = 4
LONGITUDE = 5
LATITUDE = 6
BARO_ALTITUDE = 7
ON_GROUND = 8
VELOCITY = 9
TRUE_TRACK = 10
VERTICAL_RATE = 11
SENSORS = 12
GEO_ALTITUDE = 13
SQUAWK = 14
SPI = 15
POSITION_SOURCE = 16
CATEGORY = 17
TYPECODE = 18
REGISTRATION = 19
FLIGHT_NUMBER = 20
DEPARTURE_AIRPORT = 21
ARRIVAL_AIRPORT = 22
FLIGHT_STATUS = 23
ESTIMATED_ARRIVAL = 24
AIRLINE = 25
ENRICHMENT_PROVIDER = 26
ENRICHMENT_UPDATED_AT = 27
ENRICHMENT_STALE = 28

POSITION_SOURCE_ADSB = 0
POSITION_SOURCE_MLAT = 2

# The readb/tar1090 family (adsb.lol, adsb.fi, dump1090) reports imperial
# units, OpenSky reports SI. Every conversion happens here so the firmware can
# keep treating the row as metres and metres/second.
FT_TO_M = 0.3048
KT_TO_MPS = 0.514444
FPM_TO_MPS = 0.00508

ICAO24_RE = re.compile(r"^[0-9a-f]{6}$")
TYPECODE_RE = re.compile(r"^[A-Za-z0-9]{2,4}$")

Row = list[Any]

# ADS-B emitter category letter/number -> OpenSky integer category. The two
# schemes are offset by one: "A1" is a light aircraft, which OpenSky calls 2.
# See the OpenSky REST API category table and the ADS-B emitter category table
# for the underlying definitions.
EMITTER_CATEGORY: dict[str, int] = {
    "A0": 1,
    "A1": 2,
    "A2": 3,
    "A3": 4,
    "A4": 5,
    "A5": 6,
    "A6": 7,
    "A7": 8,
    "B0": 1,
    "B1": 16,
    "B2": 17,
    "B3": 4,
    "B4": 5,
    "B5": 6,
    "B6": 7,
    "B7": 8,
    "C0": 1,
    "C1": 9,
    "C2": 10,
    "C3": 11,
    "C4": 12,
    "C6": 14,
    "C7": 15,
    "D0": 1,
    "D1": 16,
    "D2": 17,
}


def empty_row() -> Row:
    return [None] * FIELD_COUNT


def normalise_icao24(value: Any) -> str | None:
    """Return a lower-case hex transponder address, or None if unusable.

    A ``~`` prefix marks an address that OpenSky did not receive over ADS-B
    (TIS-B or MLAT derived), so it is not a real ICAO24 address and is dropped.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    if not ICAO24_RE.match(candidate):
        return None
    return candidate


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _first_number(*values: Any) -> float | int | None:
    for value in values:
        parsed = _number(value)
        if parsed is not None:
            return parsed
    return None


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def category_from_emitter(code: Any) -> int | None:
    """Translate an ADS-B emitter category such as ``"A5"`` to OpenSky's int."""
    if not isinstance(code, str):
        return None
    return EMITTER_CATEGORY.get(code.strip().upper())


def _readb_spi(value: Any) -> bool:
    """The readb family spells the special purpose indicator as "yes" or 1."""
    if isinstance(value, str):
        return value.strip().lower() in ("yes", "1", "true")
    return value is True or value == 1


def normalise_opensky_row(raw: Any) -> Row | None:
    """Coerce one OpenSky state vector into a full 20 field row."""
    if not isinstance(raw, (list, tuple)) or len(raw) <= ICAO24:
        return None

    icao24 = normalise_icao24(raw[ICAO24])
    if icao24 is None:
        return None

    row = empty_row()
    row[ICAO24] = icao24

    # The remaining indices are copied positionally. OpenSky may return short
    # rows (an unauthenticated request omits the category), so anything past the
    # end of the source row stays null.
    for index in range(1, min(len(raw), BASE_FIELD_COUNT)):
        row[index] = raw[index]

    row[CALLSIGN] = _text(row[CALLSIGN])
    row[ORIGIN_COUNTRY] = _text(row[ORIGIN_COUNTRY])
    row[ON_GROUND] = bool(row[ON_GROUND])
    row[SPI] = bool(row[SPI])

    latitude = _number(row[LATITUDE])
    longitude = _number(row[LONGITUDE])
    if latitude is not None and not -90.0 <= latitude <= 90.0:
        latitude = None
    if longitude is not None and not -180.0 <= longitude <= 180.0:
        longitude = None
    row[LATITUDE] = latitude
    row[LONGITUDE] = longitude

    if not isinstance(row[CATEGORY], int) or isinstance(row[CATEGORY], bool):
        row[CATEGORY] = None

    row[TYPECODE] = _text(row[TYPECODE])
    row[REGISTRATION] = _text(row[REGISTRATION])
    row[ENRICHMENT_STALE] = False

    return row


def from_readb(aircraft: Any, now: float) -> Row | None:
    """Convert one readb/tar1090 style aircraft dict into a 20 field row.

    Used for adsb.lol, adsb.fi and a local tar1090/dump1090 receiver. Units
    are converted from feet/knots/feet-per-minute to metres, m/s and m/s.
    """
    if not isinstance(aircraft, dict):
        return None

    icao24 = normalise_icao24(aircraft.get("hex"))
    if icao24 is None:
        return None

    row = empty_row()
    row[ICAO24] = icao24
    row[CALLSIGN] = _text(aircraft.get("flight"))
    row[ON_GROUND] = False
    row[SPI] = _readb_spi(aircraft.get("spi"))

    seen = _number(aircraft.get("seen"))
    row[LAST_CONTACT] = int(now - seen) if seen is not None else int(now)

    latitude = _number(aircraft.get("lat"))
    longitude = _number(aircraft.get("lon"))
    if latitude is not None and not -90.0 <= latitude <= 90.0:
        latitude = None
    if longitude is not None and not -180.0 <= longitude <= 180.0:
        longitude = None
    row[LATITUDE] = latitude
    row[LONGITUDE] = longitude

    if latitude is not None and longitude is not None:
        seen_pos = _number(aircraft.get("seen_pos"))
        row[TIME_POSITION] = (
            int(now - seen_pos) if seen_pos is not None else int(now)
        )

    # alt_baro is "ground" for surface reports, otherwise feet.
    baro = aircraft.get("alt_baro")
    if isinstance(baro, str) and baro.strip().lower() == "ground":
        row[ON_GROUND] = True
    else:
        baro_value = _number(baro)
        if baro_value is not None:
            row[BARO_ALTITUDE] = baro_value * FT_TO_M

    geo = _number(aircraft.get("alt_geom"))
    if geo is not None:
        row[GEO_ALTITUDE] = geo * FT_TO_M

    ground_speed = _number(aircraft.get("gs"))
    if ground_speed is not None:
        row[VELOCITY] = ground_speed * KT_TO_MPS

    track = _number(aircraft.get("track"))
    if track is not None:
        row[TRUE_TRACK] = track

    vertical_rate = _first_number(aircraft.get("baro_rate"), aircraft.get("geom_rate"))
    if vertical_rate is not None:
        row[VERTICAL_RATE] = vertical_rate * FPM_TO_MPS

    squawk = _text(aircraft.get("squawk"))
    if squawk:
        row[SQUAWK] = squawk

    mlat = aircraft.get("mlat")
    row[POSITION_SOURCE] = (
        POSITION_SOURCE_MLAT if isinstance(mlat, list) and mlat else POSITION_SOURCE_ADSB
    )

    row[CATEGORY] = category_from_emitter(aircraft.get("category"))

    typecode = _text(aircraft.get("t"))
    row[TYPECODE] = typecode if typecode and TYPECODE_RE.match(typecode) else None

    row[REGISTRATION] = _text(aircraft.get("r"))
    row[ENRICHMENT_STALE] = False

    return row


def has_position(row: Row) -> bool:
    return row[LATITUDE] is not None and row[LONGITUDE] is not None


def fill_missing(target: Row, source: Row) -> None:
    """Copy null fields of ``target`` from ``source``.

    OpenSky is treated as authoritative: it is only ever filled in, never
    overwritten, so a source that is a few seconds behind cannot drag a live
    position backwards.
    """
    for index in range(FIELD_COUNT):
        if target[index] is None and source[index] is not None:
            target[index] = source[index]
