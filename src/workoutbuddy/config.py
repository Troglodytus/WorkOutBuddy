from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _runtime_root() -> Path:
    """Return the project/data root in source and packaged executable modes."""
    explicit_root = os.getenv("WORKOUTBUDDY_ROOT", "").strip()
    if explicit_root:
        return Path(explicit_root).expanduser().resolve()
    if getattr(sys, "frozen", False):
        cwd_env = Path.cwd() / ".env"
        if cwd_env.exists():
            return Path.cwd().resolve()
        exe_dir = Path(sys.executable).resolve().parent
        if (exe_dir / ".env").exists():
            return exe_dir
        if os.getenv("WORKOUTBUDDY_ALLOW_PARENT_ENV", "").strip().lower() in {"1", "true", "yes"}:
            if (exe_dir.parent / ".env").exists():
                return exe_dir.parent
        return exe_dir
    return Path(__file__).resolve().parents[2]


def _load_dotenv_files(root: Path) -> list[Path]:
    candidates = [root / ".env", Path.cwd() / ".env"]
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend([
            exe_dir / ".env",
        ])
        if os.getenv("WORKOUTBUDDY_ALLOW_PARENT_ENV", "").strip().lower() in {"1", "true", "yes"}:
            candidates.extend([
                exe_dir.parent / ".env",
                Path.cwd().parent / ".env",
            ])

    loaded: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists():
            load_dotenv(resolved, override=False)
            loaded.append(resolved)
    return loaded


@dataclass(frozen=True)
class AppConfig:
    root_dir: Path
    app_mode: str
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
    def load(cls, app_mode: str = "web") -> "AppConfig":
        mode = app_mode.strip().lower()
        if mode not in {"web", "desktop"}:
            raise ValueError(f"Unsupported WorkOutBuddy app mode: {app_mode}")

        root = _runtime_root()
        loaded_env_files = _load_dotenv_files(root)

        client_id = os.getenv("STRAVA_CLIENT_ID", "").strip()
        client_secret = os.getenv("STRAVA_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            checked = ", ".join(str(p) for p in loaded_env_files) or str(root / ".env")
            raise RuntimeError(
                "Missing STRAVA_CLIENT_ID or STRAVA_CLIENT_SECRET. "
                f"Create a .env file from .env.example first. Checked: {checked}"
            )

        if mode == "desktop":
            web_host = os.getenv("WORKOUTBUDDY_DESKTOP_HOST", "127.0.0.1").strip() or "127.0.0.1"
            web_port = int(os.getenv("WORKOUTBUDDY_DESKTOP_PORT", os.getenv("WORKOUTBUDDY_WEB_PORT", "8080")))
            public_base_url = os.getenv(
                "WORKOUTBUDDY_DESKTOP_PUBLIC_BASE_URL",
                f"http://localhost:{web_port}",
            ).strip().rstrip("/")
        else:
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
            app_mode=mode,
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
