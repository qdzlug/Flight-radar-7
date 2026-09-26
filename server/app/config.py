from __future__ import annotations

import os
from dataclasses import dataclass, field

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
    tar1090_url: str

    cache_ttl_s: float
    cache_max_entries: int
    stale_grace_s: float
    max_states: int
    require_position: bool
    drop_on_ground: bool
    open_sky_timeout_s: float
    adsb_timeout_s: float

    host: str
    port: int
    log_level: str

    @property
    def has_opensky_credentials(self) -> bool:
        return bool(self.opensky_client_id and self.opensky_client_secret)


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
        tar1090_url=_str("TAR1090_URL").rstrip("/"),
        cache_ttl_s=_seconds("CACHE_TTL_S", 5.0),
        cache_max_entries=_int("CACHE_MAX_ENTRIES", 64),
        stale_grace_s=_seconds("STALE_GRACE_S", 120.0),
        max_states=max(1, _int("MAX_STATES", 150)),
        require_position=_bool("REQUIRE_POSITION", True),
        drop_on_ground=_bool("DROP_ON_GROUND", False),
        open_sky_timeout_s=_seconds("OPENSKY_TIMEOUT_S", 12.0),
        adsb_timeout_s=_seconds("ADSB_TIMEOUT_S", 8.0),
        host=_str("HOST", "0.0.0.0"),
        port=_int("PORT", 8000),
        log_level=_str("LOG_LEVEL", "info").lower(),
    )
