from __future__ import annotations

import os
from dataclasses import dataclass

TRUE_VALUES = {"1", "true", "yes", "on"}


def _raw(name: str) -> str:
    value = os.environ.get(name)
    return "" if value is None else value.strip()


def _str(name: str, default: str = "") -> str:
    return _raw(name) or default


def _bool(name: str, default: bool) -> bool:
    raw = _raw(name)
    if not raw:
        return default
    return raw.lower() in TRUE_VALUES


def _int(name: str, default: int) -> int:
    raw = _raw(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = _raw(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _list(name: str, default: list[str]) -> list[str]:
    raw = _raw(name)
    if not raw:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _seconds(name: str, default: float) -> float:
    return max(0.1, _float(name, default))


@dataclass(frozen=True)
class Settings:
    opensky_client_id: str
    opensky_client_secret: str
    opensky_base_url: str
    opensky_token_url: str
    opensky_extended: bool

    adsb_sources: list[str]
    adsb_lol_url: str
    adsb_fi_url: str
    adsb_im_url: str
    tar1090_url: str

    cache_ttl_s: float
    cache_max_entries: int
    stale_grace_s: float
    max_states: int
    require_position: bool
    drop_on_ground: bool
    open_sky_timeout_s: float
    adsb_timeout_s: float

    enrichment_provider: str
    enrichment_cache_path: str
    enrichment_ttl_s: float
    enrichment_negative_ttl_s: float
    enrichment_stale_grace_s: float
    enrichment_timeout_s: float
    enrichment_max_requests_hour: int
    enrichment_max_requests_day: int
    flightaware_api_key: str
    flightaware_base_url: str

    host: str
    port: int
    log_level: str

    @property
    def has_opensky_credentials(self) -> bool:
        return bool(self.opensky_client_id and self.opensky_client_secret)

    @property
    def has_flightaware_credentials(self) -> bool:
        return bool(self.flightaware_api_key)


def load_settings() -> Settings:
    return Settings(
        opensky_client_id=_str("OPENSKY_CLIENT_ID"),
        opensky_client_secret=_str("OPENSKY_CLIENT_SECRET"),
        opensky_base_url=_str(
            "OPENSKY_BASE_URL",
            "https://opensky-network.org/api",
        ).rstrip("/"),
        opensky_token_url=_str(
            "OPENSKY_TOKEN_URL",
            "https://auth.opensky-network.org/auth/realms/"
            "opensky-network/protocol/openid-connect/token",
        ),
        opensky_extended=_bool("OPENSKY_EXTENDED", False),
        adsb_sources=_list("ADSB_SOURCES", ["adsb.lol", "adsb.fi"]),
        adsb_lol_url=_str("ADSB_LOL_URL", "https://api.adsb.lol/v2").rstrip("/"),
        adsb_fi_url=_str(
            "ADSB_FI_URL",
            "https://opendata.adsb.fi/api/v2",
        ).rstrip("/"),
        adsb_im_url=_str("ADSB_IM_URL").rstrip("/"),
        tar1090_url=_str("TAR1090_URL").rstrip("/"),
        cache_ttl_s=_seconds("CACHE_TTL_S", 20.0),
        cache_max_entries=_int("CACHE_MAX_ENTRIES", 64),
        stale_grace_s=_seconds("STALE_GRACE_S", 120.0),
        max_states=max(1, _int("MAX_STATES", 150)),
        require_position=_bool("REQUIRE_POSITION", True),
        drop_on_ground=_bool("DROP_ON_GROUND", False),
        open_sky_timeout_s=_seconds("OPENSKY_TIMEOUT_S", 12.0),
        adsb_timeout_s=_seconds("ADSB_TIMEOUT_S", 8.0),
        enrichment_provider=_str("ENRICHMENT_PROVIDER", "none").lower(),
        enrichment_cache_path=_str(
            "ENRICHMENT_CACHE_PATH",
            "./data/enrichment.sqlite3",
        ),
        enrichment_ttl_s=_seconds("ENRICHMENT_TTL_S", 600.0),
        enrichment_negative_ttl_s=_seconds("ENRICHMENT_NEGATIVE_TTL_S", 900.0),
        enrichment_stale_grace_s=_seconds("ENRICHMENT_STALE_GRACE_S", 86400.0),
        enrichment_timeout_s=_seconds("ENRICHMENT_TIMEOUT_S", 2.0),
        enrichment_max_requests_hour=max(
            0,
            _int("ENRICHMENT_MAX_REQUESTS_PER_HOUR", 30),
        ),
        enrichment_max_requests_day=max(
            0,
            _int("ENRICHMENT_MAX_REQUESTS_PER_DAY", 200),
        ),
        flightaware_api_key=_str("FLIGHTAWARE_API_KEY"),
        flightaware_base_url=_str(
            "FLIGHTAWARE_BASE_URL",
            "https://aeroapi.flightaware.com/aeroapi",
        ).rstrip("/"),
        host=_str("HOST", "0.0.0.0"),
        port=_int("PORT", 8000),
        log_level=_str("LOG_LEVEL", "info").lower(),
    )
