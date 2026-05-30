from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, List, Optional

import pandas as pd
import requests


DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen3:8b"


def analyze_recent_trainings(
    db: Any,
    limit: int = 35,
    use_ollama: bool = False,
    model: Optional[str] = None,
    timeout_s: int = 180,
    llm_backend: str = "deterministic",
) -> Dict[str, Any]:
    """Analyze recent training metrics and optionally ask local Ollama.

    This version deliberately supports only:
    - deterministic local analysis
    - local Ollama

    No online/OpenAI API path is included.

    Parameters
    ----------
    llm_backend:
        "deterministic" / "none":
            no LLM; only deterministic local analysis.
        "ollama" / "local_ollama":
            local Ollama using the official Python package when available,
            with HTTP fallback.

    Backwards compatibility:
        If use_ollama=True, llm_backend is forced to "ollama".
    """
    if use_ollama:
        llm_backend = "ollama"

    backend = (llm_backend or "deterministic").strip().lower()
    if backend in {"none", "off"}:
        backend = "deterministic"
    if backend in {"local", "local ollama", "local_ollama"}:
        backend = "ollama"
    if backend not in {"deterministic", "ollama"}:
        backend = "deterministic"

    df = db.read_activities_dataframe()
    if df is None or df.empty:
        return {"summary_markdown": "No activities available yet.", "deterministic": {}, "llm": None, "backend": backend}

    df = df.copy()
    df["_dt"] = pd.to_datetime(df.get("start_date_local"), errors="coerce", utc=True)
    df = df.dropna(subset=["_dt"]).sort_values("_dt", ascending=False).head(int(limit)).copy()
    if df.empty:
        return {"summary_markdown": "No recent activities with parseable dates.", "deterministic": {}, "llm": None, "backend": backend}

    det = _deterministic_analysis(df)
    md = _format_deterministic_markdown(det)

    llm_text = None
    if backend == "ollama":
        try:
            prompt = build_llm_prompt(det, df)
            llm_model = model or os.environ.get("WORKOUTBUDDY_OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
            llm_text = call_ollama_chat(prompt, model=llm_model, timeout_s=timeout_s)
            if llm_text:
                md += f"\n\n## Local Ollama interpretation ({llm_model})\n\n" + llm_text.strip()
        except Exception as e:
            llm_text = f"Ollama analysis failed: {e}"
            md += f"\n\n## Local Ollama interpretation\n\n{llm_text}"

    return {"summary_markdown": md, "deterministic": det, "llm": llm_text, "backend": backend}


def _safe_num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def _last_valid(series: pd.Series) -> Optional[float]:
    s = _safe_num(series).dropna()
    return float(s.iloc[0]) if len(s) else None


def _trend(series: pd.Series) -> Optional[float]:
    s = _safe_num(series).dropna()
    if len(s) < 4:
        return None
    # df is newest first; reverse to chronological.
    y = s.iloc[::-1].to_numpy(dtype=float)
    x = list(range(len(y)))
    try:
        import numpy as np
        return float(np.polyfit(x, y, 1)[0])
    except Exception:
        return None


def _deterministic_analysis(df: pd.DataFrame) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    out["count"] = int(len(df))
    out["date_newest"] = str(df["_dt"].max())
    out["date_oldest"] = str(df["_dt"].min())
    out["sport_counts"] = df.get("sport_type", pd.Series(dtype=str)).fillna("Unknown").astype(str).value_counts().to_dict()

    for col in [
        "distance_km", "moving_time_min", "avg_hr", "max_hr", "avg_pace_min_per_km",
        "apple_vo2max", "own_vo2max_estimate", "estimated_vo2max",
        "combined_fitness_score", "fitness_score_ma7", "fitness_score_ma14", "fitness_score_ma35",
        "hr_efficiency_drift_pct", "grade_adjusted_hr_efficiency_drift_pct",
        "power_hr_efficiency", "power_hr_efficiency_drift_pct",
        "weather_temp_c", "weather_apparent_temp_c", "weather_temp_deviation_from_15_c",
        "elevation_gain_m", "effective_power_w", "run_equivalent_power_w", "bike_equivalent_power_w",
        "body_weight_kg_effective", "bmi",
    ]:
        if col in df.columns:
            s = _safe_num(df[col])
            out[col] = {
                "latest": _last_valid(df[col]),
                "median": float(s.median()) if s.notna().any() else None,
                "mean": float(s.mean()) if s.notna().any() else None,
                "trend_per_workout": _trend(df[col]),
            }

    if "distance_km" in df.columns:
        out["total_distance_km"] = float(_safe_num(df["distance_km"]).fillna(0).sum())
    if "moving_time_min" in df.columns:
        out["total_time_h"] = float(_safe_num(df["moving_time_min"]).fillna(0).sum() / 60.0)
    if "elevation_gain_m" in df.columns:
        out["total_elevation_gain_m"] = float(_safe_num(df["elevation_gain_m"]).fillna(0).sum())

    out["split_summary"] = _split_summary(df)

    flags: List[str] = []
    vo2_tr = out.get("own_vo2max_estimate", {}).get("trend_per_workout") or out.get("estimated_vo2max", {}).get("trend_per_workout")
    apple_tr = out.get("apple_vo2max", {}).get("trend_per_workout")
    score_tr = out.get("combined_fitness_score", {}).get("trend_per_workout")
    drift_latest = out.get("hr_efficiency_drift_pct", {}).get("latest")
    power_drift_latest = out.get("power_hr_efficiency_drift_pct", {}).get("latest")
    hr_tr = out.get("avg_hr", {}).get("trend_per_workout")
    temp_latest = out.get("weather_temp_c", {}).get("latest")

    if vo2_tr is not None and vo2_tr < -0.08:
        flags.append("Own VO2max estimate is trending down over recent workouts.")
    if apple_tr is not None and apple_tr < -0.05:
        flags.append("Manual Apple VO2max values are trending down.")
    if score_tr is not None and score_tr < -0.15:
        flags.append("Combined fitness score is trending down.")
    if drift_latest is not None and drift_latest < -8:
        flags.append("Latest HR efficiency drift looks unfavorable; fatigue/heat/hills may be involved.")
    if power_drift_latest is not None and power_drift_latest < -8:
        flags.append("Latest power/HR efficiency drift looks unfavorable.")
    if hr_tr is not None and hr_tr > 0.4:
        flags.append("Average HR is rising across recent sessions; compare against pace, heat, route and fatigue.")
    if temp_latest is not None and abs(temp_latest - 15.0) >= 8:
        flags.append("Latest temperature deviates strongly from 15 °C; HR/performance may be weather-affected.")

    out["flags"] = flags
    return out


def _split_summary(df: pd.DataFrame) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for n in [7, 14, 35]:
        part = df.head(n).copy()
        if part.empty:
            continue
        result[f"last_{n}"] = {
            "activities": int(len(part)),
            "distance_km": float(_safe_num(part.get("distance_km", pd.Series(dtype=float))).fillna(0).sum()),
            "time_h": float(_safe_num(part.get("moving_time_min", pd.Series(dtype=float))).fillna(0).sum() / 60.0),
            "median_avg_hr": _maybe_float(_safe_num(part.get("avg_hr", pd.Series(dtype=float))).median()),
            "median_own_vo2max": _maybe_float(_safe_num(part.get("own_vo2max_estimate", pd.Series(dtype=float))).median()),
            "median_fitness_score": _maybe_float(_safe_num(part.get("combined_fitness_score", pd.Series(dtype=float))).median()),
        }
    return result


def _maybe_float(x: Any) -> Optional[float]:
    try:
        if x is None or not math.isfinite(float(x)):
            return None
        return float(x)
    except Exception:
        return None


def _fmt(v: Any, d: int = 1) -> str:
    try:
        if v is None or not math.isfinite(float(v)):
            return "—"
        return f"{float(v):.{d}f}"
    except Exception:
        return "—"


def _format_deterministic_markdown(det: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("## Training analysis: last %s activities" % det.get("count", "—"))
    lines.append("")
    lines.append(f"Period: {det.get('date_oldest', '—')} → {det.get('date_newest', '—')}")
    if det.get("sport_counts"):
        lines.append("Sports: " + ", ".join(f"{k}: {v}" for k, v in det["sport_counts"].items()))
    lines.append(
        f"Total volume: {_fmt(det.get('total_distance_km'), 1)} km, "
        f"{_fmt(det.get('total_time_h'), 1)} h, "
        f"{_fmt(det.get('total_elevation_gain_m'), 0)} m elevation"
    )
    lines.append("")
    lines.append("### Key trends")
    for col, label in [
        ("apple_vo2max", "Apple VO2max"),
        ("own_vo2max_estimate", "Own VO2max estimate"),
        ("estimated_vo2max", "Own/estimated VO2max"),
        ("combined_fitness_score", "Combined fitness score"),
        ("fitness_score_ma14", "14-workout fitness MA"),
        ("fitness_score_ma35", "35-workout fitness MA"),
        ("avg_hr", "Average HR"),
        ("avg_pace_min_per_km", "Average pace"),
        ("hr_efficiency_drift_pct", "HR efficiency drift"),
        ("power_hr_efficiency", "Power/HR efficiency"),
        ("power_hr_efficiency_drift_pct", "Power/HR drift"),
        ("weather_temp_c", "Temperature"),
        ("effective_power_w", "Effective power"),
        ("bmi", "BMI"),
    ]:
        item = det.get(col)
        if isinstance(item, dict):
            lines.append(
                f"- {label}: latest {_fmt(item.get('latest'), 2)}, "
                f"median {_fmt(item.get('median'), 2)}, "
                f"trend/workout {_fmt(item.get('trend_per_workout'), 3)}"
            )

    flags = det.get("flags") or []
    lines.append("")
    lines.append("### Interpretation")
    if flags:
        for f in flags:
            lines.append(f"- {f}")
    else:
        lines.append("- No strong negative trend detected by the deterministic checks.")
    lines.append("- Treat this as trend support, not medical advice. Single runs can look worse because of heat, fatigue, hills, sleep or route differences.")
    lines.append("")
    lines.append("### Recommendations from deterministic checks")
    lines.append("- Keep most running easy; use hard sessions sparingly when the fitness score or VO2 trend is falling.")
    lines.append("- Compare runs by similar route/temperature whenever possible; otherwise HR can look worse even if fitness is unchanged.")
    lines.append("- Use the combined score and moving averages rather than one isolated VO2max value.")
    return "\n".join(lines)


def build_llm_prompt(det: Dict[str, Any], df: pd.DataFrame) -> str:
    """Build the user message sent to the LLM.

    THIS is the main place to adjust what the LLM is asked to do.

    The actual local Ollama call uses:
        system message: defined in call_ollama_chat(...)
        user message:   this prompt string
    """
    compact_rows = _compact_activity_rows(df.head(35))
    payload = {
        "cue": {
            "task": "Analyze recent training trend and give practical recommendations.",
            "athlete_context": {
                "goal": "Improve overall fitness with running as main training mode; ergo/strength/hike as support.",
                "notes": [
                    "Temperature deviation from 15 C can affect HR and perceived performance.",
                    "Running and ergometer power are not directly equivalent; use effective/equivalent power fields when present.",
                    "Do not overinterpret one workout. Look for trends and confounders.",
                    "The user is concerned that he may be getting worse; evaluate whether this is real or explained by fatigue, route, heat, hills, or training distribution.",
                ],
            },
        },
        "deterministic_summary": det,
        "activities_newest_first": compact_rows,
    }
    return (
        "You are a conservative endurance-training analyst. Use only the data below. "
        "The user wants to know whether he is improving or getting worse and what to do next.\n\n"
        "Return Markdown with exactly these sections:\n"
        "## Cue\n"
        "- Briefly summarize the relevant data context you used.\n"
        "## Analyze\n"
        "- Explain trends in VO2max, HR vs pace/power, drift, weather, sport mix, volume, and fatigue signs.\n"
        "- Clearly distinguish actual worsening from confounders such as heat, hills, route, weight, sleep/fatigue, or sport type.\n"
        "## Recommendations for improving\n"
        "- Give concrete next-7-to-14-day training advice.\n"
        "- Include intensity distribution, recovery, strength/ergo/hike use, and what metrics to watch.\n"
        "- Be cautious and do not provide medical advice.\n\n"
        "DATA JSON:\n"
        + json.dumps(payload, default=_json_default, allow_nan=False, indent=2)
    )


def _compact_activity_rows(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """Select which activity columns are sent to the LLM.

    Add/remove column names here if you want to change the data payload.
    """
    keep_cols = [
        "start_date_local", "sport_type", "name", "distance_km", "moving_time_min",
        "elevation_gain_m", "avg_hr", "max_hr", "avg_pace_min_per_km",
        "apple_vo2max", "own_vo2max_estimate", "estimated_vo2max",
        "combined_fitness_score", "fitness_score_ma7", "fitness_score_ma14", "fitness_score_ma35",
        "hr_efficiency_drift_pct", "grade_adjusted_hr_efficiency_drift_pct",
        "weather_temp_c", "weather_apparent_temp_c", "weather_temp_deviation_from_15_c",
        "effective_power_w", "run_equivalent_power_w", "bike_equivalent_power_w",
        "power_hr_efficiency", "power_hr_efficiency_drift_pct",
        "body_weight_kg_effective", "bmi",
    ]
    rows: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        item = {}
        for c in keep_cols:
            if c in row.index:
                v = row.get(c)
                if pd.isna(v):
                    continue
                item[c] = _json_default(v)
        rows.append(item)
    return rows


def _json_default(v: Any) -> Any:
    try:
        if isinstance(v, pd.Timestamp):
            return v.isoformat()
        if hasattr(v, "item"):
            v = v.item()
        if isinstance(v, float):
            if not math.isfinite(v):
                return None
            return round(v, 4)
        if isinstance(v, (int, str, bool)) or v is None:
            return v
        return str(v)
    except Exception:
        return str(v)


def call_ollama_chat(prompt: str, model: str = DEFAULT_OLLAMA_MODEL, timeout_s: int = 180) -> str:
    """Call local Ollama.

    Uses the official `ollama` Python package if installed. Falls back to the
    local HTTP API if the package is unavailable.
    """
    host = os.environ.get("WORKOUTBUDDY_OLLAMA_HOST", DEFAULT_OLLAMA_HOST)
    system = (
        "You are a careful endurance training analyst. "
        "Analyze the data and give feedback on previous performance and fitness trajectories, particularly on runs. Recommend strategies to improve performance and endurance."
        "Comment on the overall trend direction longitudinally. Is there improvement or negative trend for fitness? How to improve the trend practically?"
        "Use the supplied workout data. Be practical, concise, and cautious. "
        "Add sport type specific insights."
        "Do not invent values."
    )

    try:
        from ollama import Client  # type: ignore

        client = Client(host=host, timeout=timeout_s)
        response = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            options={"temperature": 0.2, "num_ctx": 8192},
        )
        msg = getattr(response, "message", None)
        if msg is not None:
            content = getattr(msg, "content", None)
            if content:
                return str(content)
        if isinstance(response, dict):
            return str(((response.get("message") or {}).get("content")) or response.get("response") or "")
        return str(response)
    except ImportError:
        url = host.rstrip("/") + "/api/chat"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": {"temperature": 0.2, "num_ctx": 8192},
        }
        resp = requests.post(url, json=payload, timeout=timeout_s)
        resp.raise_for_status()
        data = resp.json()
        return str(((data.get("message") or {}).get("content")) or data.get("response") or "")
