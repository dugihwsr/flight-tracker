"""Runtime configuration, read once from environment variables (see .env.example)."""
import os


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str) -> list[str]:
    return [p.strip().lower() for p in os.getenv(name, default).split(",") if p.strip()]


class Settings:
    demo_mode: bool = _bool("DEMO_MODE", False)

    # Ordered provider chains; first provider that answers wins, later ones fill gaps.
    track_providers: list[str] = _list("TRACK_PROVIDERS", "adsb,opensky")
    region_providers: list[str] = _list("REGION_PROVIDERS", "adsb,opensky")

    # readsb-style API (adsb.lol, airplanes.live, adsb.fi all share the v2 shape)
    adsb_api_base: str = os.getenv("ADSB_API_BASE", "https://api.adsb.lol").rstrip("/")

    opensky_client_id: str = os.getenv("OPENSKY_CLIENT_ID", "")
    opensky_client_secret: str = os.getenv("OPENSKY_CLIENT_SECRET", "")
    opensky_global_ttl: int = int(os.getenv("OPENSKY_GLOBAL_TTL", "300"))

    route_api_base: str = os.getenv("ROUTE_API_BASE", "https://api.adsbdb.com").rstrip("/")

    aviationstack_api_key: str = os.getenv("AVIATIONSTACK_API_KEY", "")
    # Free AviationStack plans are HTTP-only and ~100 requests/month, so refresh sparingly.
    aviationstack_base: str = os.getenv("AVIATIONSTACK_BASE", "http://api.aviationstack.com/v1").rstrip("/")
    schedule_ttl: int = int(os.getenv("SCHEDULE_TTL", "21600"))

    poll_interval: int = int(os.getenv("POLL_INTERVAL", "20"))
    region_interval: int = int(os.getenv("REGION_INTERVAL", "30"))
    max_tracked: int = int(os.getenv("MAX_TRACKED", "10"))
    max_region_flights: int = int(os.getenv("MAX_REGION_FLIGHTS", "2500"))
    lost_after: int = int(os.getenv("LOST_AFTER", "300"))

    data_dir: str = os.getenv("DATA_DIR", "/data")
    frontend_dir: str = os.getenv(
        "FRONTEND_DIR",
        os.path.join(os.path.dirname(__file__), "..", "..", "frontend"),
    )
    http_timeout: float = float(os.getenv("HTTP_TIMEOUT", "12"))
    user_agent: str = os.getenv("USER_AGENT", "flighttracker/1.0 (self-hosted)")


settings = Settings()
