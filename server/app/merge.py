"""Merge OpenSky state vectors with ADS-B sources."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .geo import BoundingBox
from .sources import SourceResult
from .statevector import (
    FIELD_COUNT,
    ICAO24,
    LATITUDE,
    LONGITUDE,
    ON_GROUND,
    Row,
    fill_missing,
    has_position,
)

log = logging.getLogger("merge.merge")


@dataclass
class MergeStats:
    rows_out: int = 0
    from_opensky: int = 0
    added_by_adsb: int = 0
    enriched: int = 0
    dropped_no_position: int = 0
    dropped_outside_box: int = 0
    dropped_on_ground: int = 0
    truncated: int = 0
    sources: dict[str, Any] = field(default_factory=dict)

    def as_meta(self) -> dict[str, Any]:
        return {
            "rows": self.rows_out,
            "from_opensky": self.from_opensky,
            "added_by_adsb": self.added_by_adsb,
            "enriched": self.enriched,
            "dropped": {
                "no_position": self.dropped_no_position,
                "outside_box": self.dropped_outside_box,
                "on_ground": self.dropped_on_ground,
            },
            "truncated": self.truncated,
            "sources": self.sources,
        }


def merge(
    results: Sequence[SourceResult],
    box: BoundingBox,
    *,
    max_states: int = 150,
    require_position: bool = True,
    drop_on_ground: bool = False,
) -> tuple[list[Row], MergeStats]:
    """Combine source results into one OpenSky-compatible list of state vectors.

    ``results`` is ordered by source priority. The first result is the base and
    keeps every value it has; later results only fill nulls and append aircraft
    that the base did not report.
    """
    stats = MergeStats(sources={result.name: result.as_meta() for result in results})

    if not results:
        return [], stats

    order: list[str] = []
    rows: dict[str, Row] = {}

    base = results[0]
    for row in base.rows:
        icao24 = row[ICAO24]
        if icao24 in rows:
            continue
        rows[icao24] = list(row)
        order.append(icao24)
    stats.from_opensky = len(order)

    for result in results[1:]:
        if not result.ok:
            continue
        for row in result.rows:
            icao24 = row[ICAO24]
            existing = rows.get(icao24)
            if existing is None:
                rows[icao24] = list(row)
                order.append(icao24)
                stats.added_by_adsb += 1
                continue
            before = list(existing)
            fill_missing(existing, row)
            if existing != before:
                stats.enriched += 1

    merged: list[Row] = []
    for position, icao24 in enumerate(order):
        if len(merged) >= max_states:
            stats.truncated = len(order) - position
            break

        row = rows[icao24]

        if not has_position(row):
            stats.dropped_no_position += 1
            if require_position:
                continue
        elif not box.contains(row[LATITUDE], row[LONGITUDE]):
            # The ADS-B sources are queried by radius, so their results cover
            # the box corners too. The radar only draws inside the configured
            # range, which is the inscribed circle, so the box never hides an
            # aircraft the firmware would have drawn.
            stats.dropped_outside_box += 1
            continue

        if drop_on_ground and row[ON_GROUND]:
            stats.dropped_on_ground += 1
            continue

        merged.append(row)

    stats.rows_out = len(merged)
    if stats.truncated:
        log.warning(
            "Truncated merged output to %d rows (%d dropped, MAX_STATES)",
            max_states,
            stats.truncated,
        )
    return merged, stats
