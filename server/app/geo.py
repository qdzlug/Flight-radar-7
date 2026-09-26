from __future__ import annotations

import math
from dataclasses import dataclass

KM_PER_NM = 1.852
EARTH_RADIUS_KM = 6371.0

# adsb.lol and adsb.fi both cap their radius query at 250 nautical miles.
MAX_QUERY_RADIUS_NM = 250


class InvalidBox(ValueError):
    """Raised when the requested bounding box is missing or out of range."""


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class BoundingBox:
    lamin: float
    lamax: float
    lomin: float
    lomax: float

    @classmethod
    def from_query(
        cls,
        lamin: float | None,
        lamax: float | None,
        lomin: float | None,
        lomax: float | None,
    ) -> "BoundingBox":
        if None in (lamin, lamax, lomin, lomax):
            raise InvalidBox(
                "lamin, lamax, lomin and lomax are all required, "
                "e.g. /states/all?lamin=13.1&lamax=13.3&lomin=77.6&lomax=77.8"
            )

        assert lamin is not None and lamax is not None
        assert lomin is not None and lomax is not None

        if not (-90.0 <= lamin <= 90.0 and -90.0 <= lamax <= 90.0):
            raise InvalidBox("lamin/lamax must be within -90..90")
        if not (-180.0 <= lomin <= 180.0 and -180.0 <= lomax <= 180.0):
            raise InvalidBox("lomin/lomax must be within -180..180")
        if lamin >= lamax:
            raise InvalidBox("lamin must be smaller than lamax")
        if lomin >= lomax:
            raise InvalidBox("lomin must be smaller than lomax")

        return cls(lamin=lamin, lamax=lamax, lomin=lomin, lomax=lomax)

    @property
    def center_lat(self) -> float:
        return (self.lamin + self.lamax) / 2.0

    @property
    def center_lon(self) -> float:
        return (self.lomin + self.lomax) / 2.0

    @property
    def radius_km(self) -> float:
        """Distance from the centre to the furthest corner of the box."""
        return max(
            haversine_km(
                self.center_lat,
                self.center_lon,
                corner_lat,
                corner_lon,
            )
            for corner_lat in (self.lamin, self.lamax)
            for corner_lon in (self.lomin, self.lomax)
        )

    @property
    def query_radius_nm(self) -> int:
        radius_nm = self.radius_km / KM_PER_NM
        return int(min(MAX_QUERY_RADIUS_NM, max(1, math.ceil(radius_nm))))

    def contains(self, lat: float | None, lon: float | None) -> bool:
        if lat is None or lon is None:
            return False
        return self.lamin <= lat <= self.lamax and self.lomin <= lon <= self.lomax

    def cache_key(self) -> tuple[float, float, float, float]:
        """Round to ~1 km so near-identical boxes share a cache entry."""
        return (
            round(self.lamin, 2),
            round(self.lamax, 2),
            round(self.lomin, 2),
            round(self.lomax, 2),
        )
