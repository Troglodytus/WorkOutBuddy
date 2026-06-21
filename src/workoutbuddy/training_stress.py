from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, Iterable, List, Mapping


CTL_TIME_CONSTANT_DAYS = 42.0
ATL_TIME_CONSTANT_DAYS = 7.0


def calculate_training_stress_history(
    daily_loads: Mapping[date, float],
    through_day: date | None = None,
) -> List[Dict[str, float | str]]:
    """Calculate daily fitness, fatigue, and form from activity load.

    CTL and ATL use the conventional impulse-response exponential smoothing
    with 42-day and 7-day time constants. TSB is end-of-day CTL minus ATL.
    Missing days carry a zero training load so fitness and fatigue decay.
    """
    end_day = through_day or date.today()
    start_day = min(daily_loads) if daily_loads else end_day
    if end_day < start_day:
        end_day = start_day

    ctl = 0.0
    atl = 0.0
    rows: List[Dict[str, float | str]] = []
    current = start_day
    while current <= end_day:
        load = max(0.0, float(daily_loads.get(current, 0.0) or 0.0))
        ctl += (load - ctl) / CTL_TIME_CONSTANT_DAYS
        atl += (load - atl) / ATL_TIME_CONSTANT_DAYS
        tsb = ctl - atl
        load_ratio = atl / ctl if ctl > 1e-9 else 0.0
        rows.append({
            "stress_date": current.isoformat(),
            "daily_load": load,
            "ctl": ctl,
            "atl": atl,
            "tsb": tsb,
            "load_ratio": load_ratio,
        })
        current += timedelta(days=1)
    return rows


def aggregate_loads(records: Iterable[Mapping[str, object]]) -> Dict[date, float]:
    """Aggregate activity load by the recorded local calendar date."""
    out: Dict[date, float] = {}
    for record in records:
        raw_date = str(record.get("start_date_local") or "").strip()
        try:
            day = date.fromisoformat(raw_date[:10])
            load = float(record.get("training_load_score") or 0.0)
        except (TypeError, ValueError):
            continue
        out[day] = out.get(day, 0.0) + max(0.0, load)
    return out
