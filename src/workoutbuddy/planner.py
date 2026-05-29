from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import median

import numpy as np
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .recommendation import RecommendationEngine


RACE_DISTANCES_KM = {
    "10k": 10.0,
    "hm": 21.0975,
    "m": 42.195,
}

GOAL_LABELS = {
    "10k": "10 km",
    "hm": "Half marathon",
    "m": "Marathon",
}

RUN_TYPES = {"run", "running", "trailrun", "trail_run", "virtualrun", "virtual_run"}
RIDE_TYPES = {"ride", "cycling", "bike", "biking", "virtualride", "virtual_ride", "gravelride", "mountainbikeride", "ebikeride"}
STRENGTH_TYPES = {"strength", "strengthtraining", "strength_training", "workout", "bodyweightstrength", "bodyweight_strength", "gym", "weights"}

RIDE_PREFERRED_FAMILIES = {"recovery_aerobic", "cross_training_ride", "endurance_ride", "bike_intervals"}
QUALITY_FAMILIES = {"quality", "tempo", "bike_intervals"}
STRENGTH_FAMILIES = {"strength_prehab", "strength_general"}
AEROBIC_FAMILIES = {"recovery_aerobic", "easy_aerobic", "steady_progression", "cross_training_ride", "endurance_ride", "long_run"}

AGGRESSION_LABELS = {
    1: "Easy / conservative",
    2: "Moderately easy",
    3: "Balanced",
    4: "Ambitious",
    5: "Hard / fastest safe progress",
}


@dataclass
class RaceGoal:
    key: str
    active: bool = False
    target_minutes: Optional[float] = None


@dataclass
class TrainingGoals:
    goals: Dict[str, RaceGoal] = field(default_factory=dict)

    @classmethod
    def default(cls) -> "TrainingGoals":
        return cls(
            goals={
                "10k": RaceGoal("10k", True, 55.0),
                "hm": RaceGoal("hm", True, 120.0),
                "m": RaceGoal("m", False, None),
            }
        )

    def active_keys(self) -> List[str]:
        return [k for k, g in self.goals.items() if g.active]

    def is_active(self, key: str) -> bool:
        g = self.goals.get(key)
        return bool(g and g.active)

    def target_minutes(self, key: str) -> Optional[float]:
        g = self.goals.get(key)
        return g.target_minutes if g and g.active else None

    def to_dict(self) -> Dict[str, Any]:
        return {k: {"active": g.active, "target_minutes": g.target_minutes} for k, g in self.goals.items()}

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "TrainingGoals":
        base = cls.default()
        if not isinstance(data, dict):
            return base
        for key in ["10k", "hm", "m"]:
            raw = data.get(key, {}) or {}
            if key not in base.goals:
                base.goals[key] = RaceGoal(key)
            base.goals[key].active = bool(raw.get("active", base.goals[key].active))
            tm = raw.get("target_minutes", base.goals[key].target_minutes)
            try:
                base.goals[key].target_minutes = float(tm) if tm not in (None, "") else None
            except Exception:
                pass
        return base


class RacePredictor:
    """Small robust race-time estimator from recent runs plus manual VO2max/VDOT.

    The latest 30 workouts are not necessarily races, so the output should be read
    as a conservative projection. It uses the best Riegel projection from recent
    runs and a VO2max/VDOT fallback, then reports the more useful estimate.
    """

    def project(self, activities: List[Dict[str, Any]], vo2max: float) -> Dict[str, Dict[str, Any]]:
        runs = [a for a in activities if _is_run(a.get("sport_type"))]
        effective_vo2 = _recent_apple_vo2max(activities) or vo2max
        out: Dict[str, Dict[str, Any]] = {}
        for key, dist_km in RACE_DISTANCES_KM.items():
            riegel = self._best_riegel_projection(runs, dist_km)
            vdot = self._vdot_time_from_vo2max(effective_vo2, dist_km)
            candidates = []
            if riegel is not None:
                candidates.append((riegel, "recent runs"))
            if vdot is not None:
                # Manual Apple Watch VO2max / profile VO2max is often close to a
                # VDOT-like race estimate, but for a low-volume runner it can be
                # optimistic. Add a small buffer.
                src = "activity Apple VO2max/VDOT" if _recent_apple_vo2max(activities) else "profile VO2max/VDOT"
                candidates.append((vdot * 1.04, src))
            if not candidates:
                out[key] = {"minutes": None, "text": "insufficient data", "source": "none"}
                continue
            # Use the faster plausible source, but do not let a single short easy run
            # produce absurdly optimistic long-distance times.
            minutes, source = min(candidates, key=lambda x: x[0])
            out[key] = {"minutes": round(minutes, 1), "text": _time_text(minutes), "source": source}
        return out

    def _best_riegel_projection(self, runs: List[Dict[str, Any]], target_km: float) -> Optional[float]:
        estimates: List[float] = []
        for a in runs:
            dist = _distance_km(a)
            dur = _duration_min(a)
            if dist < 2.0 or dur < 10.0:
                continue
            pace = dur / dist
            if pace < 3.0 or pace > 12.0:
                continue
            # Require a longer base run for long-race projections. This avoids deriving
            # a marathon from one short 3 km jog.
            if target_km >= 42 and dist < 10.0:
                continue
            if target_km >= 21 and dist < 5.0:
                continue
            # For easy training runs, Riegel will usually be conservative. That is acceptable.
            estimate = dur * ((target_km / dist) ** 1.06)
            estimates.append(estimate)
        if not estimates:
            return None
        estimates.sort()
        # Take the best of the recent projections, but if there are many, use the
        # average of the best two/three to reduce one-GPS-error sensitivity.
        n = min(3, len(estimates))
        return sum(estimates[:n]) / n

    def _vdot_time_from_vo2max(self, vo2max: float, distance_km: float) -> Optional[float]:
        if vo2max <= 20:
            return None
        # Daniels-style relationship. We solve for time t where vdot ~= vo2max.
        lo = max(4.0, distance_km * 2.4)  # extremely fast lower bound
        hi = max(45.0, distance_km * 12.0)  # very slow upper bound
        for _ in range(80):
            mid = (lo + hi) / 2.0
            vdot = _vdot_for_time(distance_km, mid)
            if vdot > vo2max:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0


class WorkoutPlanner:
    """Goal-aware 14-day planner with manual overrides.

    The planner is deliberately not an LLM. It creates inspectable, conservative
    but progressive sessions. Manual overrides are treated as fixed workouts and
    then injected back into the planning context so later days update automatically.
    """

    def __init__(self, profile_vo2max: float = 41.0, aggressiveness: int = 3):
        self.profile_vo2max = float(profile_vo2max)
        self.aggressiveness = int(_clamp(float(aggressiveness or 3), 1, 5))
        self.recommender = RecommendationEngine(profile_vo2max=self.profile_vo2max)

    def plan_14_days(
        self,
        start_datetime: datetime,
        activities: List[Dict[str, Any]],
        goals: TrainingGoals,
        available_sport_types: Iterable[str],
        overrides: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        overrides = overrides or {}
        available = list(available_sport_types or [])
        recent = _clean_activities(activities, start_datetime + timedelta(days=15))
        context_rows = list(recent)
        effective_vo2 = _recent_apple_vo2max(recent[:30]) or self.profile_vo2max
        projected = RacePredictor().project(recent[:30], effective_vo2)
        baseline = self._baseline(recent, effective_vo2)
        target_weekly_run_km = self._target_weekly_run_km(baseline, goals)
        planned: List[Dict[str, Any]] = []

        for day_idx in range(14):
            day_dt = start_datetime + timedelta(days=day_idx)
            if day_idx > 0:
                # Keep the user's current time for day 0. Future sessions default to 18:00.
                day_dt = day_dt.replace(hour=18, minute=0, second=0, microsecond=0)
            key = day_dt.date().isoformat()

            if key in overrides:
                workout = self._normalise_override(key, day_dt, overrides[key])
                workout["locked"] = True
                workout["source"] = "manual_override"
            else:
                recent_for_day = _clean_activities(context_rows, day_dt)
                rec = self.recommender.recommend(day_dt, recent_for_day[:120], desired_sport_type=None)
                context_pressure = self._context_pressure(recent_for_day, day_dt)
                family = self._goal_pattern_family(day_idx, goals)
                family = self._downgrade_by_recovery(family, rec)
                family = self._downgrade_by_recent_context(family, context_pressure)
                sport = self._choose_sport_type(family, goals, available, recent_for_day, day_idx, context_pressure)
                workout = self._build_planned_workout(
                    day_dt=day_dt,
                    day_idx=day_idx,
                    family=family,
                    sport_type=sport,
                    goals=goals,
                    baseline=baseline,
                    target_weekly_run_km=target_weekly_run_km,
                    recent_for_day=recent_for_day,
                    recovery_rec=rec,
                )
                workout["source"] = "planner"
                workout["locked"] = False

            planned.append(workout)
            if not workout.get("no_workout"):
                context_rows.insert(0, self._workout_to_activity_row(workout))

        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "start_datetime": start_datetime.isoformat(timespec="seconds"),
            "profile_vo2max": self.profile_vo2max,
            "effective_vo2max": effective_vo2,
            "aggressiveness": self.aggressiveness,
            "aggressiveness_label": AGGRESSION_LABELS.get(self.aggressiveness, "Balanced"),
            "goals": goals.to_dict(),
            "projected_race_times": projected,
            "baseline": baseline,
            "target_weekly_run_km": round(target_weekly_run_km, 1),
            "planned_workouts": planned,
            "method": "goal_aware_progressive_14_day_planner_v5_multifactor_load_efficiency_vo2_impact_aggression",
        }

    def adapt_workout_to_sport_type(
        self,
        workout: Dict[str, Any],
        new_sport_type: str,
        day_dt: datetime,
        activities: List[Dict[str, Any]],
        goals: TrainingGoals,
        available_sport_types: Iterable[str],
    ) -> Dict[str, Any]:
        """Recalculate one planned workout after the user changes only its type.

        This is a semi-automatic override: the user can change e.g. Run ->
        VirtualRide, while distance/duration/zone/pace/wattage/notes are rebuilt
        to match the same training purpose. The saved override then enters the
        planning context, so subsequent days are recalculated around it.
        """
        available = list(available_sport_types or [])
        recent = _clean_activities(activities, day_dt + timedelta(days=1))
        effective_vo2 = _recent_apple_vo2max(recent[:30]) or self.profile_vo2max
        baseline = self._baseline(recent, effective_vo2)
        target_weekly_run_km = self._target_weekly_run_km(baseline, goals)

        old_family = str(workout.get("family") or "easy_aerobic")
        family = self._map_family_for_sport_type(old_family, new_sport_type)

        if family == "rest_or_mobility" or str(new_sport_type).lower() in {"none", "rest", "off"}:
            rebuilt = self._base(day_dt, "None", "rest_or_mobility", "Rest / mobility", 0, 0.0, "Rest", "", "", "No endurance workout. Optional 15-20 min walk or mobility.", True)
        else:
            rebuilt = self._build_planned_workout(
                day_dt=day_dt,
                day_idx=0,
                family=family,
                sport_type=new_sport_type,
                goals=goals,
                baseline=baseline,
                target_weekly_run_km=target_weekly_run_km,
                recent_for_day=recent,
                recovery_rec={},
            )

        rebuilt["date"] = str(workout.get("date") or day_dt.date().isoformat())
        rebuilt["weekday"] = day_dt.strftime("%a")
        rebuilt["start_time"] = str(workout.get("start_time") or day_dt.strftime("%H:%M"))
        rebuilt["locked"] = True
        rebuilt["source"] = "manual_type_change_auto_recalculated"
        rebuilt["auto_recalculated_from_family"] = old_family
        return rebuilt

    def _map_family_for_sport_type(self, family: str, sport_type: str) -> str:
        if str(sport_type).lower() in {"none", "rest", "off"}:
            return "rest_or_mobility"
        if _is_strength(sport_type):
            if family in {"recovery_aerobic", "easy_aerobic", "cross_training_ride", "rest_or_mobility"}:
                return "strength_prehab"
            return "strength_general"
        if _is_ride(sport_type):
            return {
                "quality": "bike_intervals",
                "bike_intervals": "bike_intervals",
                "tempo": "steady_progression",
                "steady_progression": "steady_progression",
                "long_run": "endurance_ride",
                "endurance_ride": "endurance_ride",
                "recovery_aerobic": "recovery_aerobic",
                "easy_aerobic": "cross_training_ride",
                "strength_prehab": "recovery_aerobic",
                "strength_general": "cross_training_ride",
                "rest_or_mobility": "recovery_aerobic",
            }.get(family, "cross_training_ride")
        if _is_run(sport_type):
            return {
                "cross_training_ride": "easy_aerobic",
                "endurance_ride": "long_run",
                "bike_intervals": "quality",
                "strength_prehab": "recovery_aerobic",
                "strength_general": "easy_aerobic",
                "rest_or_mobility": "recovery_aerobic",
            }.get(family, family if family not in STRENGTH_FAMILIES else "easy_aerobic")
        return family

    def _baseline(self, activities: List[Dict[str, Any]], effective_vo2max: Optional[float] = None) -> Dict[str, Any]:
        now = datetime.now()
        cut_7 = now - timedelta(days=7)
        cut_30 = now - timedelta(days=30)
        runs_30 = [a for a in activities if _is_run(a.get("sport_type")) and (_activity_dt(a) or datetime.min) >= cut_30]
        runs_7 = [a for a in activities if _is_run(a.get("sport_type")) and (_activity_dt(a) or datetime.min) >= cut_7]
        all_30 = [a for a in activities if (_activity_dt(a) or datetime.min) >= cut_30]
        vo2_for_pace = effective_vo2max or _recent_apple_vo2max(activities[:30]) or self.profile_vo2max
        easy_pace = _median_easy_pace(runs_30) or _fallback_easy_pace_from_vo2max(vo2_for_pace)
        run_km_30 = sum(_distance_km(a) for a in runs_30)
        run_km_7 = sum(_distance_km(a) for a in runs_7)
        longest = max([_distance_km(a) for a in runs_30] or [0.0])
        median_run = median([_distance_km(a) for a in runs_30 if _distance_km(a) > 1.0]) if any(_distance_km(a) > 1.0 for a in runs_30) else 5.0
        load_7 = sum(_training_load(a) for a in all_30 if (_activity_dt(a) or datetime.min) >= cut_7)
        load_30 = sum(_training_load(a) for a in all_30)
        apple_vo2 = _recent_apple_vo2max(activities[:30])
        apple_vo2_trend = _recent_apple_vo2max_trend(activities[:60])
        median_gap_drift = _median_recent_gap_drift(runs_30)
        median_km_gap_drift = _median_metric(runs_30, "km_gap_hr_efficiency_drift_pct", lo=-50, hi=50, limit=20)
        median_impact = _median_metric(runs_30, "avg_impact_bw", lo=0.5, hi=5.0, limit=20)
        median_impact_load = _median_metric(runs_30, "impact_load_index", lo=0, hi=1000, limit=20)
        easy_fraction = _median_metric(all_30, "easy_zone_fraction", lo=0, hi=1, limit=30)
        hard_fraction = _median_metric(all_30, "hard_zone_fraction", lo=0, hi=1, limit=30)
        load_30_weekly = load_30 / 30.0 * 7.0 if load_30 else 0.0
        load_ratio = (load_7 / load_30_weekly) if load_30_weekly > 0 else 1.0
        return {
            "run_km_7d": round(run_km_7, 1),
            "run_km_30d": round(run_km_30, 1),
            "run_km_30d_weekly": round(run_km_30 / 30.0 * 7.0, 1) if run_km_30 else 0.0,
            "run_count_30d": len(runs_30),
            "activity_count_30d": len(all_30),
            "longest_run_30d_km": round(longest, 1),
            "median_run_30d_km": round(float(median_run), 1),
            "recent_easy_pace_min_km": round(easy_pace, 2),
            "load_7d": round(load_7, 1),
            "load_30d_weekly": round(load_30_weekly, 1),
            "load_7d_vs_30d_weekly_ratio": round(load_ratio, 2),
            "apple_vo2max_recent": round(apple_vo2, 1) if apple_vo2 else None,
            "apple_vo2max_trend_per_month": round(apple_vo2_trend, 2) if apple_vo2_trend is not None else None,
            "effective_vo2max": round(vo2_for_pace, 1) if vo2_for_pace else None,
            "median_gap_hr_drift_30d_pct": round(median_gap_drift, 1) if median_gap_drift is not None else None,
            "median_km_gap_hr_drift_30d_pct": round(median_km_gap_drift, 1) if median_km_gap_drift is not None else None,
            "median_easy_zone_fraction_30d": round(easy_fraction, 2) if easy_fraction is not None else None,
            "median_hard_zone_fraction_30d": round(hard_fraction, 2) if hard_fraction is not None else None,
            "median_impact_bw_30d": round(median_impact, 2) if median_impact is not None else None,
            "median_impact_load_30d": round(median_impact_load, 1) if median_impact_load is not None else None,
        }

    def _target_weekly_run_km(self, baseline: Dict[str, Any], goals: TrainingGoals) -> float:
        weekly = float(baseline.get("run_km_30d_weekly") or baseline.get("run_km_7d") or 8.0)
        # Five-level progression dial. Level 1 prioritises continuity and injury risk;
        # level 5 tries to move toward the goal paces quickly while still capping jumps.
        pct_by_level = {1: 1.04, 2: 1.07, 3: 1.10, 4: 1.14, 5: 1.18}
        abs_by_level = {1: 1.0, 2: 1.5, 3: 2.0, 4: 3.0, 5: 4.0}
        cap_abs_by_level = {1: 3.0, 2: 5.0, 3: 7.0, 4: 9.0, 5: 11.0}
        pct = pct_by_level[self.aggressiveness]
        add = abs_by_level[self.aggressiveness]
        cap_add = cap_abs_by_level[self.aggressiveness]
        target = max(weekly * pct, weekly + add)
        if goals.is_active("10k"):
            target = max(target, min(weekly + cap_add * 0.65, max(12.0, weekly * (pct + 0.04))))
        if goals.is_active("hm"):
            target = max(target, min(weekly + cap_add * 0.80, max(16.0, weekly * (pct + 0.07))))
        if goals.is_active("m"):
            target = max(target, min(weekly + cap_add, max(20.0, weekly * (pct + 0.10))))
        # Risk modifiers from actual physiology/biomechanics. Bad aerobic drift,
        # high acute:chronic load, or high impact load slows progression; improving
        # Apple VO2max allows the chosen aggression level to express itself.
        drift = _safe_float(baseline.get("median_km_gap_hr_drift_30d_pct"))
        if drift is None:
            drift = _safe_float(baseline.get("median_gap_hr_drift_30d_pct"))
        load_ratio = _safe_float(baseline.get("load_7d_vs_30d_weekly_ratio")) or 1.0
        impact = _safe_float(baseline.get("median_impact_load_30d"))
        vo2_trend = _safe_float(baseline.get("apple_vo2max_trend_per_month"))
        risk_factor = 1.0
        if drift is not None and drift < -7.0:
            risk_factor *= 0.88
        elif drift is not None and drift < -4.0:
            risk_factor *= 0.94
        if load_ratio > 1.45:
            risk_factor *= 0.86
        elif load_ratio > 1.25:
            risk_factor *= 0.93
        if impact is not None and impact > 28:
            risk_factor *= 0.94
        if vo2_trend is not None and vo2_trend > 0.8 and load_ratio < 1.25:
            risk_factor *= 1.03
        capped = min(target, max(weekly + cap_add, weekly * (1.12 + 0.04 * self.aggressiveness)))
        return max(weekly * 0.95, capped * risk_factor)

    def _goal_pattern_family(self, day_idx: int, goals: TrainingGoals) -> str:
        hm = goals.is_active("hm")
        ten = goals.is_active("10k")
        mar = goals.is_active("m")
        # Running is the backbone of the two-week plan. Virtual rides are used
        # as occasional low-impact aerobic volume, and strength fills the plan
        # as prehab / running-specific strength rather than replacing the run focus.
        if self.aggressiveness <= 1:
            pattern = [
                "easy_aerobic", "strength_prehab", "recovery_aerobic", "cross_training_ride",
                "rest_or_mobility", "long_run" if (hm or mar) else "easy_aerobic", "rest_or_mobility",
                "easy_aerobic", "strength_prehab", "steady_progression" if (ten or hm) else "easy_aerobic",
                "cross_training_ride", "easy_aerobic", "rest_or_mobility", "long_run" if (hm or mar) else "easy_aerobic",
            ]
        elif self.aggressiveness >= 5:
            pattern = [
                "easy_aerobic", "strength_prehab", "quality" if ten else "steady_progression",
                "cross_training_ride", "easy_aerobic", "steady_progression", "long_run" if (hm or mar) else "steady_progression",
                "recovery_aerobic", "strength_general", "tempo" if (ten or hm) else "steady_progression",
                "cross_training_ride", "easy_aerobic", "quality" if ten else "steady_progression",
                "long_run" if (hm or mar) else "easy_aerobic",
            ]
        else:
            pattern = [
                "easy_aerobic",        # run
                "strength_prehab",     # fill/support: knee/core/calf/hip stability
                "quality" if ten else "steady_progression",  # run quality stimulus
                "cross_training_ride", # ergo volume without extra running impact
                "easy_aerobic",        # run
                "rest_or_mobility",
                "long_run" if (hm or mar) else "steady_progression", # run stamina
                "recovery_aerobic",    # usually run unless recovery suggests otherwise
                "strength_general",    # running-specific strength
                "tempo" if (ten or hm) else "steady_progression", # run threshold/steady
                "cross_training_ride", # second ergo support session
                "easy_aerobic",        # run
                "rest_or_mobility",
                "long_run" if (hm or mar) else "easy_aerobic", # run stamina
            ]
        return pattern[day_idx % len(pattern)]

    def _downgrade_by_recovery(self, planned_family: str, rec: Dict[str, Any]) -> str:
        score = _safe_float(rec.get("recovery_score_0_100"))
        rec_family = str(rec.get("workout_family") or "easy_aerobic")
        if score is None:
            return planned_family
        hard_floor = {1: 62, 2: 58, 3: 55, 4: 52, 5: 50}[self.aggressiveness]
        easy_floor = {1: 45, 2: 41, 3: 38, 4: 35, 5: 32}[self.aggressiveness]
        if planned_family in {"quality", "tempo", "long_run"} and score < hard_floor:
            return "easy_aerobic" if score >= easy_floor else "recovery_aerobic"
        if planned_family in {"steady_progression", "easy_aerobic"} and score < easy_floor:
            return "recovery_aerobic"
        if planned_family == "quality" and rec_family in {"rest_or_mobility", "recovery_aerobic"}:
            return "easy_aerobic"
        return planned_family

    def _context_pressure(self, recent: List[Dict[str, Any]], day_dt: datetime) -> Dict[str, Any]:
        """Summarise very recent real/planned training so manual edits affect later days.

        This is intentionally separate from the historical baseline: changing today's
        run to a 60-min virtual ride should not count as run volume, but it should
        count as aerobic stress and therefore influence tomorrow's/next session's
        intensity.
        """
        cut_36 = day_dt - timedelta(hours=36)
        cut_72 = day_dt - timedelta(hours=72)
        rows_36 = [a for a in recent if (_activity_dt(a) or datetime.min) >= cut_36]
        rows_72 = [a for a in recent if (_activity_dt(a) or datetime.min) >= cut_72]
        load_36 = sum(_training_load(a) for a in rows_36)
        load_72 = sum(_training_load(a) for a in rows_72)
        run_km_72 = sum(_distance_km(a) for a in rows_72 if _is_run(a.get("sport_type")))
        ride_min_72 = sum(_duration_min(a) for a in rows_72 if _is_ride(a.get("sport_type")))
        hard_min_72 = sum(((_safe_float(a.get("z4_s")) or 0.0) + (_safe_float(a.get("z5_s")) or 0.0)) / 60.0 for a in rows_72)
        impact_72 = sum((_safe_float(a.get("impact_load_index")) or 0.0) for a in rows_72 if _is_run(a.get("sport_type")))
        median_recent_drift = _median_metric(rows_72, "km_gap_hr_efficiency_drift_pct", lo=-50, hi=50, limit=8)
        last = recent[0] if recent else None
        return {
            "load_36h": round(load_36, 1),
            "load_72h": round(load_72, 1),
            "run_km_72h": round(run_km_72, 1),
            "ride_min_72h": round(ride_min_72, 1),
            "hard_min_72h": round(hard_min_72, 1),
            "impact_load_72h": round(impact_72, 1),
            "median_gap_drift_72h": round(median_recent_drift, 1) if median_recent_drift is not None else None,
            "last_sport_type": last.get("sport_type") if last else None,
            "last_family": last.get("family") if last else None,
            "last_duration_min": round(_duration_min(last), 1) if last else 0.0,
            "last_load": round(_training_load(last), 1) if last else 0.0,
            "last_is_planned": str(last.get("source") or "") == "planned" if last else False,
        }

    def _downgrade_by_recent_context(self, planned_family: str, pressure: Dict[str, Any]) -> str:
        load_36 = float(pressure.get("load_36h") or 0.0)
        load_72 = float(pressure.get("load_72h") or 0.0)
        hard_72 = float(pressure.get("hard_min_72h") or 0.0)
        last_load = float(pressure.get("last_load") or 0.0)
        last_family = str(pressure.get("last_family") or "")
        impact_72 = float(pressure.get("impact_load_72h") or 0.0)
        drift_72 = _safe_float(pressure.get("median_gap_drift_72h"))

        # Manual virtual rides now matter: a long/hard ride raises load and can push
        # the next quality day down to easy/recovery, while not pretending to be run km.
        if planned_family in {"quality", "tempo", "long_run"}:
            load36_limit = {1: 115, 2: 130, 3: 140, 4: 155, 5: 170}[self.aggressiveness]
            hard72_limit = {1: 18, 2: 23, 3: 28, 4: 34, 5: 40}[self.aggressiveness]
            load72_limit = {1: 210, 2: 235, 3: 260, 4: 290, 5: 320}[self.aggressiveness]
            if last_load >= load36_limit * 0.85 or load_36 >= load36_limit or hard_72 >= hard72_limit:
                return "easy_aerobic"
            if impact_72 >= 35 and planned_family in {"quality", "long_run"}:
                return "cross_training_ride" if planned_family == "quality" else "easy_aerobic"
            if drift_72 is not None and drift_72 < -8 and planned_family in {"quality", "tempo"}:
                return "easy_aerobic"
            if load_72 >= load72_limit and planned_family == "quality":
                return "steady_progression"
        if planned_family in {"quality", "tempo"} and last_family in {"quality", "tempo", "bike_intervals"}:
            return "easy_aerobic"
        if planned_family == "cross_training_ride" and load_36 >= 160:
            return "recovery_aerobic"
        return planned_family

    def _choose_sport_type(self, family: str, goals: TrainingGoals, available: List[str], recent: List[Dict[str, Any]], day_idx: int = 0, pressure: Optional[Dict[str, Any]] = None) -> str:
        pressure = pressure or {}
        run_type = _first_type(available, RUN_TYPES) or "Run"
        ride_type = _first_type(available, RIDE_TYPES)
        if not ride_type:
            ride_type = "VirtualRide" if "VirtualRide" in available else "Ride"
        strength_type = _first_type(available, STRENGTH_TYPES) or "Strength"

        if family == "rest_or_mobility":
            return run_type or ride_type or "Generic"

        if family in STRENGTH_FAMILIES:
            return strength_type

        # Explicit cross-training slots should be rides when any ride-like type exists.
        if family in {"cross_training_ride", "endurance_ride", "bike_intervals"} and ride_type:
            return ride_type

        # Recovery remains mostly run-focused, but can become a ride after recent run impact.
        if family == "recovery_aerobic" and ride_type:
            recent_two_runs = sum(1 for a in recent[:2] if _is_run(a.get("sport_type")))
            run_km_72 = float(pressure.get("run_km_72h") or 0.0)
            impact_72 = float(pressure.get("impact_load_72h") or 0.0)
            if recent_two_runs >= 2 or run_km_72 >= 16.0 or impact_72 >= 45:
                return ride_type

        # If recent run impact is already high, use a ride for an otherwise easy day.
        run_km_72 = float(pressure.get("run_km_72h") or 0.0)
        if family == "easy_aerobic" and ride_type and run_km_72 >= 18.0 and day_idx not in {0, 4, 7, 11}:
            return ride_type

        return run_type if run_type else (ride_type or "Generic")

    def _distance_multiplier(self) -> float:
        return {1: 0.88, 2: 0.95, 3: 1.00, 4: 1.08, 5: 1.15}[self.aggressiveness]

    def _build_planned_workout(
        self,
        day_dt: datetime,
        day_idx: int,
        family: str,
        sport_type: str,
        goals: TrainingGoals,
        baseline: Dict[str, Any],
        target_weekly_run_km: float,
        recent_for_day: List[Dict[str, Any]],
        recovery_rec: Dict[str, Any],
    ) -> Dict[str, Any]:
        easy_pace = float(baseline.get("recent_easy_pace_min_km") or _fallback_easy_pace_from_vo2max(self.profile_vo2max))
        ten_goal = goals.target_minutes("10k")
        hm_goal = goals.target_minutes("hm")
        race_pace_10 = ten_goal / 10.0 if ten_goal else None
        race_pace_hm = hm_goal / 21.0975 if hm_goal else None
        is_run = _is_run(sport_type)
        is_ride = _is_ride(sport_type)
        longest = float(baseline.get("longest_run_30d_km") or 5.0)
        median_run = float(baseline.get("median_run_30d_km") or 5.0)
        weekly_run = float(baseline.get("run_km_30d_weekly") or baseline.get("run_km_7d") or 8.0)
        distance_mult = self._distance_multiplier()

        if family == "rest_or_mobility":
            return self._base(day_dt, sport_type, family, "Rest / mobility", 0, 0.0, "None", "", "", "No endurance workout. Optional 15-20 min walk or mobility.", True)

        if _is_strength(sport_type) or family in STRENGTH_FAMILIES:
            return self._build_strength_workout(day_dt, family, sport_type, baseline, goals)

        # Ride / virtual-ride variants. These deliberately still influence training
        # load through planned HR-zone time, but they do not count as run distance.
        if is_ride:
            return self._build_ride_workout(day_dt, family, sport_type, baseline, goals)

        # Non-running and non-riding fallback.
        if not is_run:
            duration = 35 if family == "recovery_aerobic" else 45
            return self._base(day_dt, sport_type, family, "Easy aerobic workout", duration, 0.0, "Z1-Z2", "", "", "Keep it conversational. This counts as aerobic load, but not run-specific volume.")

        if family == "recovery_aerobic":
            dist = _clamp(round(max(3.0, min(5.5, median_run * 0.65)) * distance_mult, 1), 3.0, 6.5)
            dur = int(round(dist * (easy_pace + 0.5)))
            return self._base(day_dt, sport_type, family, "Recovery run", dur, dist, "Z1-low Z2", _pace_range(easy_pace + 0.3, easy_pace + 0.9), "", "Easy enough that HR remains controlled. Walk breaks are allowed.")

        if family == "easy_aerobic":
            dist = _clamp(round(max(4.5, min(8.0, median_run * 0.95)) * distance_mult, 1), 4.0, min(9.5, max(5.0, longest * 0.95 + 0.5)))
            dur = int(round(dist * easy_pace))
            return self._base(day_dt, sport_type, family, "Easy aerobic run", dur, dist, "Mostly Z2", _pace_range(easy_pace - 0.05, easy_pace + 0.45), "", "Build aerobic consistency. HR/effort overrides pace, especially on hills.")

        if family == "steady_progression":
            dist = _clamp(round(max(5.5, min(9.0, median_run * 1.05)) * distance_mult, 1), 5.0, min(11.0, max(6.0, longest + 0.5)))
            dur = int(round(dist * max(4.5, easy_pace - 0.1)))
            pace = _pace_range(easy_pace - 0.20, easy_pace + 0.25)
            return self._base(day_dt, sport_type, family, "Steady aerobic progression run", dur, dist, "Z2, optional low Z3 late", pace, "", "Slightly more productive than easy, but not threshold. Finish feeling you could continue.")

        if family == "tempo":
            return self._build_tempo_run(day_dt, sport_type, easy_pace, race_pace_hm, race_pace_10, median_run, longest, weekly_run)

        if family == "quality":
            return self._build_interval_run(day_dt, sport_type, easy_pace, race_pace_10, median_run, longest, weekly_run)

        if family == "long_run":
            # Long run progression is the main stamina builder. Keep cap moderate.
            goal_factor = 1.15 if goals.is_active("hm") else 1.05
            desired = max(median_run * 1.15, longest * 0.95)
            if day_idx >= 12:
                desired = max(desired, longest * goal_factor)
            max_cap = longest + 1.5 if longest >= 6 else longest + 1.0
            if goals.is_active("m"):
                max_cap = longest + 2.0
            dist = _clamp(round(desired * distance_mult, 1), 7.0, max(8.0, max_cap + (self.aggressiveness - 3) * 0.4))
            dur = int(round(dist * (easy_pace + 0.15)))
            return self._base(day_dt, sport_type, family, "Long easy run", dur, dist, "Z2; avoid Z4/Z5", _pace_range(easy_pace + 0.05, easy_pace + 0.65), "", "Main stamina session. Keep it easier than ego says; distance matters more than speed.")

        return self._base(day_dt, sport_type, family, "Easy workout", 45, 0.0, "Z2", "", "", "Generic aerobic work.")

    def _build_strength_workout(self, day_dt: datetime, family: str, sport_type: str, baseline: Dict[str, Any], goals: TrainingGoals) -> Dict[str, Any]:
        dur_mult = {1: 0.80, 2: 0.90, 3: 1.0, 4: 1.08, 5: 1.15}[self.aggressiveness]
        if family == "strength_prehab":
            dur = int(round(30 * dur_mult))
            notes = (
                "Running support strength: 2-3 rounds of calf raises, glute bridge or hip thrust, "
                "side plank, dead bug, split-squat pattern without pain, and light hamstring hinge. "
                "This should feel useful, not exhausting."
            )
            return self._base(day_dt, sport_type, "strength_prehab", "Core / prehab strength", dur, 0.0, "Easy-moderate; not HR-driven", "", "bodyweight/light resistance", notes)

        dur = int(round(40 * dur_mult))
        notes = (
            "Running-specific strength: squat or leg press pattern, Romanian deadlift/hinge, "
            "calf raises, step-ups or split squats, and anti-rotation core. Keep 2-3 reps in reserve; "
            "avoid knee pain and avoid heavy soreness before quality/long runs."
        )
        return self._base(day_dt, sport_type, "strength_general", "Running-specific strength", dur, 0.0, "Moderate; not HR-driven", "", "controlled strength work", notes)

    def _build_ride_workout(self, day_dt: datetime, family: str, sport_type: str, baseline: Dict[str, Any], goals: TrainingGoals) -> Dict[str, Any]:
        weekly_run = float(baseline.get("run_km_30d_weekly") or baseline.get("run_km_7d") or 8.0)
        stamina = "low" if weekly_run < 12 else ("moderate" if weekly_run < 24 else "higher")
        dur_mult = {1: 0.85, 2: 0.93, 3: 1.0, 4: 1.10, 5: 1.18}[self.aggressiveness]

        if family in {"recovery_aerobic"}:
            dur = int(round((35 if stamina == "low" else 40) * dur_mult))
            return self._base(day_dt, sport_type, family, "Recovery ride", dur, _virtual_ride_km(dur), "Z1-low Z2", "", "easy spin / low resistance", "Low-impact recovery. Keep HR mostly below Z2; cadence smooth, no surges.")

        if family in {"cross_training_ride", "easy_aerobic"}:
            dur = int(round((50 if stamina == "low" else 60) * dur_mult))
            return self._base(day_dt, sport_type, "cross_training_ride", "Aerobic cross-training ride", dur, _virtual_ride_km(dur), "Mostly Z2", "", "Z2 HR-guided / comfortable resistance", "Aerobic volume without extra running impact. This should feel easier than a hard run.")

        if family in {"endurance_ride", "long_run"}:
            dur = int(round((70 if stamina == "low" else (80 if stamina == "moderate" else 95)) * dur_mult))
            return self._base(day_dt, sport_type, "endurance_ride", "Endurance ride", dur, _virtual_ride_km(dur), "Z2; avoid Z4/Z5", "", "Z2 HR-guided / sustainable", "Long low-impact stamina stimulus. No sprinting; keep the load controlled.")

        if family in {"tempo", "steady_progression"}:
            dur = int(round((55 if stamina == "low" else 65) * dur_mult))
            notes = "12 min warm-up, 2 x 10 min steady Z3 with 5 min easy between, cool-down. Keep it controlled, not a race."
            return self._base(day_dt, sport_type, "steady_progression", "Steady aerobic ride", dur, _virtual_ride_km(dur), "Z2-Z3", "", "Z2/low Z3 HR-guided", notes)

        if family in {"quality", "bike_intervals"}:
            if stamina == "low":
                dur = 45
                notes = "10-12 min warm-up, 6 x 1 min high-cadence hard Z4 effort with 2 min very easy spin, cool-down."
            elif stamina == "moderate":
                dur = 55
                notes = "12-15 min warm-up, 5 x 2 min controlled hard Z4 effort with 3 min easy spin, cool-down."
            else:
                dur = 60
                notes = "15 min warm-up, 4 x 4 min strong but controlled Z4 effort with 4 min easy spin, cool-down."
            return self._base(day_dt, sport_type, "bike_intervals", "Bike interval session", dur, _virtual_ride_km(dur), "Z4 during work reps", "", "HR-guided Z4 if no power meter", notes)

        return self._base(day_dt, sport_type, family, "Easy ride", 45, _virtual_ride_km(45), "Z2", "", "Z2 HR-guided", "Aerobic ride.")

    def _build_interval_run(self, day_dt: datetime, sport_type: str, easy_pace: float, race_pace_10: Optional[float], median_run: float, longest: float, weekly_run: float) -> Dict[str, Any]:
        work_pace = race_pace_10 or max(3.8, easy_pace - 0.65)
        dm = self._distance_multiplier()
        if weekly_run < 12 or longest < 6.0:
            reps = {1: 4, 2: 5, 3: 6, 4: 6, 5: 7}[self.aggressiveness]
            dist = _clamp(round(max(5.2, median_run * 0.9) * dm, 1), 4.8, 7.0)
            dur = int(round(dist * easy_pace))
            notes = f"12 min warm-up, {reps} x 1 min controlled hard at ~10k effort with 2 min easy jog/walk, cool-down. Stop early if form deteriorates."
        elif weekly_run < 24 or longest < 9.0:
            reps = {1: 4, 2: 4, 3: 5, 4: 6, 5: 6}[self.aggressiveness]
            dist = _clamp(round(max(6.0, median_run * 1.0) * dm, 1), 5.5, 8.8)
            dur = int(round(dist * easy_pace))
            notes = f"12-15 min warm-up, {reps} x 2 min at around 10k effort/pace with 2 min easy jog, cool-down. Last rep should be controlled, not maximal."
        else:
            reps = {1: 4, 2: 5, 3: 5, 4: 6, 5: 6}[self.aggressiveness]
            rep_len = "600 m" if self.aggressiveness <= 2 else ("600-800 m" if self.aggressiveness <= 4 else "800 m")
            dist = _clamp(round(max(7.0, median_run * 1.05) * dm, 1), 6.5, 10.5)
            dur = int(round(dist * easy_pace))
            notes = f"15 min warm-up, {reps} x {rep_len} at around 10k pace with 2-3 min easy jog, cool-down. Keep the first two reps deliberately restrained."
        return self._base(day_dt, sport_type, "quality", "10k-oriented interval run", dur, dist, "Z4 during work reps", _pace_range(work_pace - 0.05, work_pace + 0.15), "", notes)

    def _build_tempo_run(self, day_dt: datetime, sport_type: str, easy_pace: float, race_pace_hm: Optional[float], race_pace_10: Optional[float], median_run: float, longest: float, weekly_run: float) -> Dict[str, Any]:
        tempo_pace = race_pace_hm or (race_pace_10 + 0.25 if race_pace_10 else easy_pace - 0.45)
        dm = self._distance_multiplier()
        if weekly_run < 12 or longest < 6.0:
            reps = 2 if self.aggressiveness <= 1 else 3
            dist = _clamp(round(max(5.0, median_run * 0.9) * dm, 1), 4.8, 7.0)
            notes = f"12 min warm-up, {reps} x 5 min steady Z3 with 3 min easy jog, cool-down. This is controlled stamina work, not a time trial."
        elif weekly_run < 24:
            dist = _clamp(round(max(6.5, median_run * 1.05) * dm, 1), 6.0, 10.0)
            work = "2 x 8 min" if self.aggressiveness <= 2 else ("2 x 10 min" if self.aggressiveness <= 4 else "3 x 8 min")
            notes = f"12-15 min warm-up, {work} controlled tempo Z3/low Z4 with 4 min easy jog, cool-down."
        else:
            dist = _clamp(round(max(8.0, median_run * 1.10) * dm, 1), 7.0, 12.0)
            dur_txt = "18-20 min" if self.aggressiveness <= 2 else ("20-25 min" if self.aggressiveness <= 4 else "25-30 min")
            notes = f"15 min warm-up, {dur_txt} continuous controlled tempo Z3/low Z4, cool-down. You should finish with one more gear available."
        dur = int(round(dist * easy_pace))
        return self._base(day_dt, sport_type, "tempo", "Controlled tempo run", dur, dist, "Z3 with short Z4 cap", _pace_range(tempo_pace - 0.05, tempo_pace + 0.20), "", notes)

    def _base(self, day_dt: datetime, sport_type: str, family: str, title: str, duration_min: int, distance_km: float, zone: str, pace: str, wattage: str, notes: str, no_workout: bool = False) -> Dict[str, Any]:
        return {
            "date": day_dt.date().isoformat(),
            "weekday": day_dt.strftime("%a"),
            "start_time": day_dt.strftime("%H:%M"),
            "sport_type": sport_type,
            "family": family,
            "title": title,
            "duration_min": int(duration_min),
            "distance_km": round(float(distance_km or 0.0), 1),
            "zone": zone,
            "pace": pace,
            "wattage": wattage,
            "notes": notes,
            "no_workout": bool(no_workout),
        }

    def _normalise_override(self, key: str, day_dt: datetime, raw: Dict[str, Any]) -> Dict[str, Any]:
        base = self._base(day_dt, str(raw.get("sport_type") or "Run"), str(raw.get("family") or "manual"), str(raw.get("title") or "Manual workout"), int(_safe_float(raw.get("duration_min")) or 0), float(_safe_float(raw.get("distance_km")) or 0.0), str(raw.get("zone") or ""), str(raw.get("pace") or ""), str(raw.get("wattage") or ""), str(raw.get("notes") or ""), bool(raw.get("no_workout")))
        base["date"] = key
        base["start_time"] = str(raw.get("start_time") or base["start_time"])
        if base["no_workout"]:
            base["title"] = raw.get("title") or "No workout"
            base["duration_min"] = 0
            base["distance_km"] = 0.0
        return base

    def _workout_to_activity_row(self, w: Dict[str, Any]) -> Dict[str, Any]:
        start = datetime.fromisoformat(f"{w['date']}T{w.get('start_time') or '18:00'}:00")
        dur_s = max(0.0, float(w.get("duration_min") or 0) * 60.0)
        dist_m = max(0.0, float(w.get("distance_km") or 0) * 1000.0)
        z1 = z2 = z3 = z4 = z5 = 0.0
        family = str(w.get("family") or "")
        if family in {"recovery_aerobic"}:
            z1 = dur_s * 0.55; z2 = dur_s * 0.45
        elif family in {"strength_prehab"}:
            z1 = dur_s * 0.65; z2 = dur_s * 0.35
        elif family in {"strength_general"}:
            z1 = dur_s * 0.35; z2 = dur_s * 0.55; z3 = dur_s * 0.10
        elif family in {"easy_aerobic", "cross_training_ride"}:
            z1 = dur_s * 0.20; z2 = dur_s * 0.75; z3 = dur_s * 0.05
        elif family in {"long_run", "endurance_ride"}:
            z1 = dur_s * 0.18; z2 = dur_s * 0.78; z3 = dur_s * 0.04
        elif family in {"steady_progression", "tempo"}:
            z1 = dur_s * 0.15; z2 = dur_s * 0.55; z3 = dur_s * 0.25; z4 = dur_s * 0.05
        elif family in {"quality", "bike_intervals"}:
            z1 = dur_s * 0.20; z2 = dur_s * 0.40; z3 = dur_s * 0.20; z4 = dur_s * 0.18; z5 = dur_s * 0.02
        else:
            z1 = dur_s * 0.30; z2 = dur_s * 0.70
        return {
            "activity_id": f"planned_{w['date']}",
            "source": "planned",
            "name": w.get("title") or "Planned workout",
            "sport_type": w.get("sport_type") or "Run",
            "family": w.get("family") or "manual",
            "start_date_local": start.isoformat(timespec="seconds"),
            "distance_m": dist_m,
            "moving_time_s": dur_s,
            "elapsed_time_s": dur_s,
            "avg_pace_min_km": (dur_s / 60.0) / (dist_m / 1000.0) if dist_m > 0 else None,
            "avg_hr": None,
            "max_hr": None,
            "z1_s": z1,
            "z2_s": z2,
            "z3_s": z3,
            "z4_s": z4,
            "z5_s": z5,
            "hr_efficiency_drift_pct": None,
        }


def _recent_apple_vo2max(activities: List[Dict[str, Any]]) -> Optional[float]:
    values: List[float] = []
    for a in activities[:30]:
        v = _safe_float(a.get("apple_vo2max"))
        if v is not None and 20 <= v <= 80:
            values.append(float(v))
            if len(values) >= 5:
                break
    if not values:
        return None
    return median(values)


def _median_recent_gap_drift(runs: List[Dict[str, Any]]) -> Optional[float]:
    values = []
    for a in runs[:20]:
        v = _safe_float(a.get("gap_hr_efficiency_drift_pct"))
        if v is None:
            v = _safe_float(a.get("hr_efficiency_drift_pct"))
        if v is not None and -50 <= v <= 50:
            values.append(v)
    return median(values) if values else None


def _recent_apple_vo2max_trend(activities: List[Dict[str, Any]]) -> Optional[float]:
    points: List[Tuple[datetime, float]] = []
    for a in activities:
        v = _safe_float(a.get("apple_vo2max"))
        dt = _activity_dt(a)
        if v is not None and 20 <= v <= 80 and dt is not None:
            points.append((dt, float(v)))
    if len(points) < 2:
        return None
    points.sort(key=lambda x: x[0])
    t0 = points[0][0]
    xs = np.array([(p[0] - t0).total_seconds() / 86400.0 for p in points], dtype=float)
    ys = np.array([p[1] for p in points], dtype=float)
    if float(np.nanmax(xs) - np.nanmin(xs)) < 3:
        return None
    # slope in VO2 units per 30 days
    slope = float(np.polyfit(xs, ys, 1)[0])
    return slope * 30.0


def _median_metric(activities: List[Dict[str, Any]], key: str, lo: float = -math.inf, hi: float = math.inf, limit: int = 30) -> Optional[float]:
    vals: List[float] = []
    for a in activities[:limit]:
        v = _safe_float(a.get(key))
        if v is not None and lo <= v <= hi:
            vals.append(float(v))
    return median(vals) if vals else None


def _vdot_for_time(distance_km: float, time_min: float) -> float:
    v = distance_km * 1000.0 / time_min  # m/min
    vo2 = -4.60 + 0.182258 * v + 0.000104 * v * v
    pct = 0.8 + 0.1894393 * math.exp(-0.012778 * time_min) + 0.2989558 * math.exp(-0.1932605 * time_min)
    return vo2 / pct


def _clean_activities(activities: List[Dict[str, Any]], before: datetime) -> List[Dict[str, Any]]:
    out = []
    for a in activities:
        dt = _activity_dt(a)
        if dt is not None and dt <= before:
            out.append(a)
    out.sort(key=lambda a: _activity_dt(a) or datetime.min, reverse=True)
    return out


def _activity_dt(a: Optional[Dict[str, Any]]) -> Optional[datetime]:
    if not a:
        return None
    raw = a.get("start_date_local")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except Exception:
        return None


def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None or x == "":
            return None
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def _duration_min(a: Optional[Dict[str, Any]]) -> float:
    if not a:
        return 0.0
    return max(0.0, (_safe_float(a.get("moving_time_s")) or _safe_float(a.get("elapsed_time_s")) or 0.0) / 60.0)


def _distance_km(a: Optional[Dict[str, Any]]) -> float:
    if not a:
        return 0.0
    return max(0.0, (_safe_float(a.get("distance_m")) or 0.0) / 1000.0)


def _norm_type(sport_type: Any) -> str:
    return str(sport_type or "").strip().replace(" ", "").replace("-", "").lower()


def _is_run(sport_type: Any) -> bool:
    return _norm_type(sport_type) in {x.replace("_", "") for x in RUN_TYPES}


def _is_ride(sport_type: Any) -> bool:
    return _norm_type(sport_type) in {x.replace("_", "") for x in RIDE_TYPES}


def _is_strength(sport_type: Any) -> bool:
    return _norm_type(sport_type) in {x.replace("_", "") for x in STRENGTH_TYPES}


def _first_type(available: Iterable[str], wanted: set[str]) -> Optional[str]:
    norm = {x.replace("_", "") for x in wanted}
    for t in available:
        if _norm_type(t) in norm:
            return str(t)
    return None


def _training_load(a: Dict[str, Any]) -> float:
    stored = _safe_float(a.get("training_load_score"))
    if stored is not None and stored > 0:
        return stored
    total = 0.0
    for key, weight in [("z1_s", 1.0), ("z2_s", 2.0), ("z3_s", 3.0), ("z4_s", 4.5), ("z5_s", 6.0)]:
        total += ((_safe_float(a.get(key)) or 0.0) / 60.0) * weight
    if total > 0:
        return total
    return _duration_min(a) * 1.8


def _median_easy_pace(runs: List[Dict[str, Any]]) -> Optional[float]:
    values: List[float] = []
    for a in runs:
        pace = _safe_float(a.get("avg_gap_pace_min_km")) or _safe_float(a.get("avg_pace_min_km"))
        if pace is None or pace < 3.0 or pace > 12.0:
            continue
        total = sum((_safe_float(a.get(k)) or 0.0) for k in ["z1_s", "z2_s", "z3_s", "z4_s", "z5_s"])
        easy = ((_safe_float(a.get("z1_s")) or 0.0) + (_safe_float(a.get("z2_s")) or 0.0)) / total if total > 0 else 0.0
        if easy >= 0.50 or (_safe_float(a.get("avg_hr")) or 999) < 150:
            values.append(float(pace))
    return median(values) if values else None


def _fallback_easy_pace_from_vo2max(vo2max: float) -> float:
    target_vo2 = max(16.0, vo2max * 0.60)
    speed_m_min = max(80.0, (target_vo2 - 3.5) / 0.2)
    return 1000.0 / speed_m_min


def _pace_range(lo: float, hi: float) -> str:
    return f"{_pace_text(lo)}-{_pace_text(hi)} min/km"


def _pace_text(min_per_km: float) -> str:
    if min_per_km <= 0 or not math.isfinite(min_per_km):
        return ""
    m = int(min_per_km)
    s = int(round((min_per_km - m) * 60))
    if s == 60:
        m += 1; s = 0
    return f"{m}:{s:02d}"


def _virtual_ride_km(duration_min: float, virtual_speed_kmh: float = 23.0) -> float:
    """Conservative virtual-ride distance estimate for planned ergo sessions.

    The exact distance depends on the trainer/app, but showing an estimated
    distance is more useful in the calendar than zero. Power/HR remain the
    primary targets.
    """
    try:
        return round(max(0.0, float(duration_min)) / 60.0 * virtual_speed_kmh, 1)
    except Exception:
        return 0.0


def _time_text(minutes: float) -> str:
    if minutes is None or not math.isfinite(minutes):
        return ""
    total_seconds = int(round(minutes * 60))
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
