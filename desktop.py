from pathlib import Path
from datetime import datetime
import multiprocessing
import os
import sys
import traceback

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from workoutbuddy.web_app import desktop_main


def _runtime_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return ROOT


def _startup_log(message: str) -> None:
    try:
        log_dir = _runtime_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "startup.log").open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
    except Exception:
        pass


if __name__ == "__main__":
    multiprocessing.freeze_support()
    _startup_log(
        f"starting pid={os.getpid()} frozen={getattr(sys, 'frozen', False)} "
        f"cwd={Path.cwd()} exe={Path(sys.executable).resolve()}"
    )
    try:
        desktop_main()
        _startup_log("desktop_main returned")
    except Exception:
        _startup_log("startup failed:\n" + traceback.format_exc())
        raise
