from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class AppConfig:
    root_dir: Path
    client_id: str
    client_secret: str
    redirect_port: int
    redirect_uri: str
    db_path: Path
    token_path: Path
    raw_dir: Path
    streams_dir: Path
    maps_dir: Path
    exports_dir: Path
    upload_dir: Path
    web_host: str
    web_port: int
    public_base_url: str

    @classmethod
    def load(cls) -> "AppConfig":
        # Project root = two parents above src/workoutbuddy/config.py
        root = Path(__file__).resolve().parents[2]
        load_dotenv(root / ".env")

        client_id = os.getenv("STRAVA_CLIENT_ID", "").strip()
        client_secret = os.getenv("STRAVA_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise RuntimeError(
                "Missing STRAVA_CLIENT_ID or STRAVA_CLIENT_SECRET. "
                "Create a .env file from .env.example first."
            )

        web_host = os.getenv("WORKOUTBUDDY_WEB_HOST", "0.0.0.0").strip() or "0.0.0.0"
        web_port = int(os.getenv("WORKOUTBUDDY_WEB_PORT", "8080"))
        public_base_url = os.getenv("WORKOUTBUDDY_PUBLIC_BASE_URL", f"http://localhost:{web_port}").strip().rstrip("/")

        # Kept for compatibility with older desktop code; the web app uses /strava/callback.
        redirect_port = int(os.getenv("WORKOUTBUDDY_REDIRECT_PORT", str(web_port)))
        redirect_uri = f"{public_base_url}/strava/callback"

        db_path = root / os.getenv("WORKOUTBUDDY_DB", "data/workoutbuddy.sqlite")
        data_dir = db_path.parent
        token_path = root / ".secrets" / "token.json"
        raw_dir = data_dir / "raw_activities"
        streams_dir = data_dir / "streams"
        maps_dir = data_dir / "maps"
        exports_dir = data_dir / "exports"
        upload_dir = data_dir / "uploaded_tcx"

        for p in [data_dir, token_path.parent, raw_dir, streams_dir, maps_dir, exports_dir, upload_dir]:
            p.mkdir(parents=True, exist_ok=True)

        return cls(
            root_dir=root,
            client_id=client_id,
            client_secret=client_secret,
            redirect_port=redirect_port,
            redirect_uri=redirect_uri,
            db_path=db_path,
            token_path=token_path,
            raw_dir=raw_dir,
            streams_dir=streams_dir,
            maps_dir=maps_dir,
            exports_dir=exports_dir,
            upload_dir=upload_dir,
            web_host=web_host,
            web_port=web_port,
            public_base_url=public_base_url,
        )
