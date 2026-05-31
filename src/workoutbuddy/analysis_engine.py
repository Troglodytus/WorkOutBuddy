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

    Only local modes are supported:
    - deterministic local analysis
    - local Ollama
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


def _maybe_series(df: pd.DataFrame, col: str) -> pd.Series:
    if col in df.columns:
        return _safe_num(df[col])
    return pd.Series(dtype=float)


def _last_valid(series: pd.Series) -> Optional[float]:
    s = _safe_num(series).dropna()
    return float(s.iloc[0]) if len(s) else None


def _trend(series: pd.Series) -> Optional[float]:
    s = _safe_num(series).dropna()
    if len(s) < 4:
        return None
    y = s.iloc[::-1].to_numpy(dtype=float)  # chronological
    x = list(range(len(y)))
    try:
        import numpy as np
        return float(np.polyfit(x, y, 1)[0])
    except Exception:
        return None


def _norm_sport(x: Any) -> str:
    import re
    return re.sub(r"[^a-z0-9]", "", str(x or "").lower())


def _is_run_row(row: pd.Series) -> bool:
    return _norm_sport(row.get("sport_type")) in {"run", "running", "trailrun", "virtualrun"}


def _zone_seconds(row: pd.Series) -> Dict[str, float]:
    return {z: _safe_float(row.get(z), 0.0) or 0.0 for z in ["z1_s", "z2_s", "z3_s", "z4_s", "z5_s"]}


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        x = float(value)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


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
        "body_weight_kg_effective", "bmi", "easy_zone_fraction", "hard_zone_fraction",
        "z1_s", "z2_s", "z3_s", "z4_s", "z5_s",
    ]:
        if col in df.columns:
            s = _safe_num(df[col])
            out[col] = {
                "latest": _last_valid(df[col]),
                "median": float(s.median()) if s.notna().any() else None,
                "mean": float(s.mean()) if s.notna().any() else None,
                "trend_per_workout": _trend(df[col]),
            }

    out["total_distance_km"] = float(_maybe_series(df, "distance_km").fillna(0).sum())
    out["total_time_h"] = float(_maybe_series(df, "moving_time_min").fillna(0).sum() / 60.0)
    out["total_elevation_gain_m"] = float(_maybe_series(df, "elevation_gain_m").fillna(0).sum())

    out["split_summary"] = _split_summary(df)
    out["training_distribution"] = _training_distribution(df)
    out["run_specific"] = _run_specific_analysis(df)
    out["weakness_hypotheses"] = _weakness_hypotheses(out)

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


def _training_distribution(df: pd.DataFrame) -> Dict[str, Any]:
    z_totals = {z: 0.0 for z in ["z1_s", "z2_s", "z3_s", "z4_s", "z5_s"]}
    for _, row in df.iterrows():
        zs = _zone_seconds(row)
        for z, v in zs.items():
            z_totals[z] += v
    total = sum(z_totals.values())
    easy = z_totals["z1_s"] + z_totals["z2_s"]
    moderate = z_totals["z3_s"]
    hard = z_totals["z4_s"] + z_totals["z5_s"]

    def pct(v: float) -> Optional[float]:
        return round(100.0 * v / total, 1) if total > 0 else None

    return {
        "zone_minutes": {k.replace("_s", "_min"): round(v / 60.0, 1) for k, v in z_totals.items()},
        "zone_percent": {
            "z1": pct(z_totals["z1_s"]),
            "z2": pct(z_totals["z2_s"]),
            "z3": pct(z_totals["z3_s"]),
            "z4": pct(z_totals["z4_s"]),
            "z5": pct(z_totals["z5_s"]),
            "easy_z1_z2": pct(easy),
            "moderate_z3": pct(moderate),
            "hard_z4_z5": pct(hard),
        },
        "interpretation_hint": (
            "Endurance base is usually built by enough Z1-Z2 volume. "
            "Too much Z3-Z5 relative to easy volume can make HR/VO2 trends look worse due to fatigue."
        ),
    }


def _run_specific_analysis(df: pd.DataFrame) -> Dict[str, Any]:
    runs = df[df.apply(_is_run_row, axis=1)].copy()
    if runs.empty:
        return {"run_count": 0}

    recent = runs.head(max(3, min(10, len(runs))))
    older = runs.iloc[max(3, min(10, len(runs))):]
    out: Dict[str, Any] = {
        "run_count": int(len(runs)),
        "recent_run_distance_km": float(_maybe_series(recent, "distance_km").fillna(0).sum()),
        "recent_median_pace_min_km": _maybe_float(_maybe_series(recent, "avg_pace_min_per_km").median()),
        "recent_median_hr": _maybe_float(_maybe_series(recent, "avg_hr").median()),
        "recent_median_easy_fraction": _maybe_float(_maybe_series(recent, "easy_zone_fraction").median()),
        "recent_median_hard_fraction": _maybe_float(_maybe_series(recent, "hard_zone_fraction").median()),
        "recent_median_drift": _maybe_float(_maybe_series(recent, "hr_efficiency_drift_pct").median()),
    }
    if not older.empty:
        out.update({
            "older_median_pace_min_km": _maybe_float(_maybe_series(older, "avg_pace_min_per_km").median()),
            "older_median_hr": _maybe_float(_maybe_series(older, "avg_hr").median()),
            "older_median_easy_fraction": _maybe_float(_maybe_series(older, "easy_zone_fraction").median()),
            "older_median_hard_fraction": _maybe_float(_maybe_series(older, "hard_zone_fraction").median()),
            "older_median_drift": _maybe_float(_maybe_series(older, "hr_efficiency_drift_pct").median()),
        })
    return out


def _weakness_hypotheses(out: Dict[str, Any]) -> List[str]:
    hyps: List[str] = []
    dist = out.get("training_distribution", {}).get("zone_percent", {})
    easy_pct = dist.get("easy_z1_z2")
    hard_pct = dist.get("hard_z4_z5")
    z3_pct = dist.get("moderate_z3")
    run = out.get("run_specific", {})
    drift = run.get("recent_median_drift")
    vo2_tr = out.get("own_vo2max_estimate", {}).get("trend_per_workout")
    score_tr = out.get("combined_fitness_score", {}).get("trend_per_workout")

    if easy_pct is not None and easy_pct < 70:
        hyps.append("Likely aerobic-base limiter: easy Z1-Z2 share is below a typical endurance-building target.")
    if z3_pct is not None and z3_pct > 20:
        hyps.append("Possible grey-zone issue: substantial Z3 time may create fatigue without enough easy volume or quality specificity.")
    if hard_pct is not None and hard_pct > 12:
        hyps.append("Hard-intensity load may be high relative to total volume; recovery may limit improvement.")
    if drift is not None and drift < -6:
        hyps.append("Fatigue resistance may be a weakness: recent HR/efficiency drift is unfavorable.")
    if vo2_tr is not None and vo2_tr < 0:
        hyps.append("VO2 estimate is not improving; check heat, routes, easy-volume consistency, and recovery.")
    if score_tr is not None and score_tr < 0:
        hyps.append("Combined fitness score is trending down; treat this as a signal to reduce intensity and rebuild consistency for 1-2 weeks.")
    if not hyps:
        hyps.append("No single obvious limiter detected; improvement may depend on consistency and comparing similar routes/conditions.")
    return hyps


def _split_summary(df: pd.DataFrame) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for n in [7, 14, 35]:
        part = df.head(n).copy()
        if part.empty:
            continue
        result[f"last_{n}"] = {
            "activities": int(len(part)),
            "distance_km": float(_maybe_series(part, "distance_km").fillna(0).sum()),
            "time_h": float(_maybe_series(part, "moving_time_min").fillna(0).sum() / 60.0),
            "median_avg_hr": _maybe_float(_maybe_series(part, "avg_hr").median()),
            "median_own_vo2max": _maybe_float(_maybe_series(part, "own_vo2max_estimate").median()),
            "median_fitness_score": _maybe_float(_maybe_series(part, "combined_fitness_score").median()),
            "easy_zone_pct_median": _maybe_float(_maybe_series(part, "easy_zone_fraction").median() * 100.0),
            "hard_zone_pct_median": _maybe_float(_maybe_series(part, "hard_zone_fraction").median() * 100.0),
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

    zp = det.get("training_distribution", {}).get("zone_percent", {})
    if zp:
        lines.append("")
        lines.append("### Intensity distribution")
        lines.append(
            f"- Easy Z1-Z2: {_fmt(zp.get('easy_z1_z2'), 1)} %, "
            f"Z3: {_fmt(zp.get('moderate_z3'), 1)} %, "
            f"Hard Z4-Z5: {_fmt(zp.get('hard_z4_z5'), 1)} %"
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

    lines.append("")
    lines.append("### Weakness hypotheses")
    for h in det.get("weakness_hypotheses", []):
        lines.append(f"- {h}")

    flags = det.get("flags") or []
    lines.append("")
    lines.append("### Interpretation")
    if flags:
        for f in flags:
            lines.append(f"- {f}")
    else:
        lines.append("- No strong negative trend detected by the deterministic checks.")
    lines.append("- Treat this as trend support, not medical advice. Single runs can look worse because of heat, fatigue, hills, sleep or route differences.")
    return "\n".join(lines)


def build_llm_prompt(det: Dict[str, Any], df: pd.DataFrame) -> str:
    """Build the user message sent to the LLM.

    Edit this function if you want to change the task given to the LLM.
    Edit _compact_activity_rows(...) if you want to change the data columns.
    """
    compact_rows = _compact_activity_rows(df.head(35))
    payload = {
        "cue": {
            "task": "Analyze recent training trend, diagnose likely weaknesses, and give practical training recommendations.",
            "athlete_context": {
                "goal": "Improve overall fitness with running as main training mode; ergo/strength/hike as support.",
                "important_questions": [
                    "Is the user improving or declining longitudinally?",
                    "What is the most likely limiter: aerobic base/Z2 volume, too much Z3-Z5, fatigue/recovery, heat/route confounding, insufficient strength, or inconsistent volume?",
                    "How does Z2 performance compare with high-intensity Z4-Z5 performance?",
                    "Should the next block emphasize more Z2, less intensity, strength training, hills/hikes, or recovery?",
                ],
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
            "You are an endurance running coach and data analyst. "
            "Do not summarize workouts one by one. "
            "Your task is to diagnose the user's current main training limiter.\n\n"

            "First decide whether fitness is improving, stable, or declining. "
            "Then decide whether any decline is likely real or explained by fatigue, heat, route, hills, or inconsistent training.\n\n"

            "Rank the likely weaknesses:\n"
            "1. insufficient Z2/aerobic base\n"
            "2. too much Z3/Z4/Z5 intensity\n"
            "3. insufficient recovery\n"
            "4. poor fatigue resistance / HR drift\n"
            "5. heat sensitivity\n"
            "6. muscular durability / strength limitation\n\n"

            "Return Markdown with exactly these sections:\n"
            "## Fitness trend\n"
            "## Main weaknesses ranked\n"
            "## Evidence\n"
            "## Next 14 days\n"
            "## Metrics to watch\n\n"

            "DATA JSON:\n"
    + json.dumps(payload, default=_json_default, allow_nan=False, indent=2)
    )
    # return (
    #     "You are a conservative but useful endurance-training analyst. Use only the data below.\n\n"
    #     "The user does NOT want generic comments like 'one run was faster and one was warmer'. "
    #     "The user wants a diagnosis of weaknesses and an actionable training strategy.\n\n"
    #     "Return Markdown with exactly these sections:\n\n"
    #     "## Cue\n"
    #     "- State what data you used: number of activities, sports, period, major caveats.\n\n"
    #     "## Analyze\n"
    #     "- Decide whether fitness appears to be improving, stable, or declining.\n"
    #     "- Analyze running specifically first.\n"
    #     "- Analyze intensity distribution: Z1-Z2 vs Z3 vs Z4-Z5.\n"
    #     "- Discuss HR versus pace/power and drift/fatigue resistance.\n"
    #     "- Discuss weather/temperature and route/elevation as confounders.\n"
    #     "- Compare easy/Z2 performance with hard/Z4-Z5 performance if data is available.\n"
    #     "- Explicitly list likely weaknesses ranked from most likely to least likely.\n\n"
    #     "## Recommendations for improving\n"
    #     "- Give a concrete next 14-day strategy.\n"
    #     "- Say whether to add more Z2, reduce intensity, add strength, add hills/hikes, or rest.\n"
    #     "- Give practical targets: number of runs, long run, quality session, strength sessions, and recovery.\n"
    #     "- Give metric targets to monitor next: HR drift, Z2 pace/HR, VO2 estimate, temperature-normalized efficiency, and fitness score.\n"
    #     "- Be cautious and do not provide medical advice.\n\n"
    #     "DATA JSON:\n"
    #     + json.dumps(payload, default=_json_default, allow_nan=False, indent=2)
    # )


def _compact_activity_rows(df: pd.DataFrame) -> List[Dict[str, Any]]:
    keep_cols = [
        "start_date_local", "sport_type", "name", "distance_km", "moving_time_min",
        "elevation_gain_m", "avg_hr", "max_hr", "avg_pace_min_per_km",
        "apple_vo2max", "own_vo2max_estimate", "estimated_vo2max",
        "combined_fitness_score", "fitness_score_ma7", "fitness_score_ma14", "fitness_score_ma35",
        "hr_efficiency_drift_pct", "grade_adjusted_hr_efficiency_drift_pct",
        "easy_zone_fraction", "hard_zone_fraction", "z1_s", "z2_s", "z3_s", "z4_s", "z5_s",
        "weather_temp_c", "weather_apparent_temp_c", "weather_temp_deviation_from_15_c",
        "effective_power_w", "run_equivalent_power_w", "bike_equivalent_power_w",
        "power_hr_efficiency", "power_hr_efficiency_drift_pct",
        "body_weight_kg_effective", "bmi",
        "z1_s", "z2_s", "z3_s", "z4_s", "z5_s",
        "easy_zone_fraction",
        "hard_zone_fraction",
        "hr_efficiency_drift_pct",
        "grade_adjusted_hr_efficiency_drift_pct",
        "weather_temp_c",
        "weather_temp_deviation_from_15_c",
        "combined_fitness_score",
        "fitness_score_ma14",
        "fitness_score_ma35",
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
    host = os.environ.get("WORKOUTBUDDY_OLLAMA_HOST", DEFAULT_OLLAMA_HOST)
    # system = (
    #     "You are a careful endurance training analyst. "
    #     "Analyze previous performance and fitness trajectories, particularly runs. "
    #     "Do not give generic summaries. Diagnose likely weaknesses and give actionable recommendations. "
    #     "Focus on Z2/aerobic base, Z3/Z4/Z5 intensity balance, HR-vs-pace/power efficiency, drift/fatigue resistance, recovery, strength needs, weather/route confounding, and sport-specific insights. "
    #     "Use only the supplied workout data. Be practical, concise, and cautious. "
    #     "Do not invent values and do not give medical diagnosis."
    # )
    system = (
        "You are a precise endurance running coach and training data analyst. "
        "Use only the supplied workout data. "
        "Focus on diagnosing training limiters and giving actionable recommendations. "
        "Do not invent values. Do not give medical diagnosis."
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
