from __future__ import annotations

import json
import math
import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Tuple


RUN_TYPES = {"run", "running", "trailrun", "trail_run", "virtualrun", "virtual_run"}
RIDE_TYPES = {
    "ride",
    "biking",
    "bike",
    "cycling",
    "virtualride",
    "virtual_ride",
    "mountainbikeride",
    "mountain_bike_ride",
    "gravelride",
    "gravel_ride",
    "ebikeride",
    "e_bike_ride",
}

# User-specific HR zones from the TCX import stage.
HR_Z1_MAX = 134.0
HR_Z2_MAX = 146.0
HR_Z3_MAX = 156.0
HR_Z4_MAX = 167.0

# Zone-weighted session-load units. This is intentionally simple and transparent:
# minutes in zone * zone weight. It is not a true physiological TRIMP model.
ZONE_WEIGHTS = {
    "z1_s": 1.0,
    "z2_s": 2.0,
    "z3_s": 3.0,
    "z4_s": 4.5,
    "z5_s": 6.0,
}

AGGRESSIVENESS = {
    "rest_or_mobility": 0,
    "recovery_aerobic": 1,
    "easy_aerobic": 2,
    "steady_progression": 3,
    "quality": 4,
}


@dataclass
class TrainingContext:
    target_datetime: datetime
    considered_count: int
    profile_vo2max: float
    profile_birthdate: str
    profile_gender: str
    profile_age_years: Optional[float]
    profile_weight_kg: Optional[float]
    profile_height_cm: Optional[float]
    profile_bmi: Optional[float]

    last_activity: Optional[Dict[str, Any]]
    last_activity_hours_since_start: Optional[float]
    last_activity_hours_since_end: Optional[float]
    last_activity_load: float
    last_activity_hard_fraction: float
    last_activity_stress_label: str
    last_activity_temp_c: Optional[float]
    last_activity_temp_deviation_from_15_c: Optional[float]

    last_run: Optional[Dict[str, Any]]
    last_run_hours_since_end: Optional[float]
    last_run_distance_km: Optional[float]
    last_run_duration_min: Optional[float]
    last_run_hr_efficiency_drift_pct: Optional[float]
    last_run_hard_fraction: Optional[float]
    last_run_load: Optional[float]

    load_7d: float
    load_14d: float
    load_30d: float
    load_30d_weekly_equivalent: float
    load_ratio_7d_vs_30d_weekly: Optional[float]
    hard_minutes_7d: float
    hard_minutes_30d: float
    last_hard_hours_since_end: Optional[float]
    activities_7d: int

    run_km_7d: float
    run_km_30d: float
    run_km_30d_weekly_equivalent: float
    run_minutes_7d: float
    ride_minutes_7d: float

    median_run_distance_30d_km: Optional[float]
    median_run_duration_30d_min: Optional[float]
    longest_run_30d_km: Optional[float]
    recent_easy_gap_pace_min_km: Optional[float]
    recent_easy_pace_min_km: Optional[float]

    available_sport_types: List[str]
    sport_type_counts: Dict[str, int]


class RecommendationEngine:
    """
    Deterministic, progressive-overload-oriented workout recommender.

    Version 2 is intentionally less conservative than v1. It distinguishes a true
    hard workout from a normal endurance run with a short HR spike, uses time since
    the estimated END of the previous workout, and uses an ensemble of three simple
    rule sets: safety, balanced, and progression.
    """

    def __init__(
        self,
        profile_vo2max: Optional[float] = None,
        birthdate: str = "1993-06-16",
        gender: str = "Male",
        weight_kg: Optional[float] = None,
        height_cm: Optional[float] = None,
    ):
        if profile_vo2max is None:
            profile_vo2max = _env_float("WORKOUTBUDDY_VO2MAX", 41.0)
        self.profile_vo2max = float(profile_vo2max)
        self.birthdate = str(birthdate or "1993-06-16")
        self.gender = str(gender or "Male")
        self.weight_kg = _safe_float(weight_kg)
        self.height_cm = _safe_float(height_cm)
        self.bmi = _calc_bmi(self.weight_kg, self.height_cm)
        self.age_years = _age_years_at(self.birthdate, datetime.now().isoformat())

    def recommend(
        self,
        target_datetime: datetime,
        last_activities: List[Dict[str, Any]],
        desired_sport_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        activities = _clean_and_sort_activities(last_activities, target_datetime)
        context = self._build_context(target_datetime, activities)

        if not activities:
            return self._no_data_recommendation(target_datetime, desired_sport_type)

        recovery_score, recovery_reasons = self._estimate_recovery_score(context)
        candidate_votes = self._candidate_votes(context, recovery_score)
        workout_family = self._select_family_from_votes(candidate_votes, context, recovery_score)
        selected_type, type_reasons = self._choose_sport_type(context, desired_sport_type, workout_family)
        workout = self._build_workout(selected_type, workout_family, recovery_score, context)

        reasons: List[str] = []
        reasons.extend(recovery_reasons)
        reasons.extend(self._vote_reasons(candidate_votes, workout_family))
        reasons.extend(type_reasons)
        reasons.extend(self._context_reasons(context))

        return {
            "status": "ok",
            "target_datetime": target_datetime.isoformat(timespec="seconds"),
            "desired_sport_type": desired_sport_type or "Auto / none",
            "selected_sport_type": selected_type,
            "workout_family": workout_family,
            "recovery_score_0_100": round(recovery_score, 1),
            "recommendation": workout,
            "candidate_votes": candidate_votes,
            "reasons": reasons,
            "context": _context_to_dict(context),
            "llm_ready_summary": self._llm_ready_summary(context, recovery_score, workout_family, selected_type, workout),
            "method": "deterministic_progressive_ensemble_v2",
            "disclaimer": "Algorithmic training suggestion, not medical advice. Reduce or skip the workout if you have pain, illness, abnormal fatigue, dizziness, chest symptoms, or unusual cardiovascular symptoms.",
        }

    def _build_context(self, target_datetime: datetime, activities: List[Dict[str, Any]]) -> TrainingContext:
        last_activity = activities[0] if activities else None
        last_activity_hours_start = _hours_between_activity_start_and_target(last_activity, target_datetime)
        last_activity_hours_end = _hours_between_activity_end_and_target(last_activity, target_datetime)
        last_activity_load = _training_load(last_activity) if last_activity else 0.0
        last_activity_hard_fraction = _hard_fraction(last_activity) if last_activity else 0.0
        last_activity_stress_label = _activity_stress_label(last_activity) if last_activity else "unknown"
        last_activity_temp = _activity_temp_c(last_activity) if last_activity else None
        last_activity_temp_dev = abs(last_activity_temp - 15.0) if last_activity_temp is not None else None

        runs = [a for a in activities if _is_run(a.get("sport_type"))]
        last_run = runs[0] if runs else None
        last_run_hours_end = _hours_between_activity_end_and_target(last_run, target_datetime)
        last_run_drift = _safe_float(last_run.get("hr_efficiency_drift_pct")) if last_run else None
        last_run_hard_fraction = _hard_fraction(last_run) if last_run else None
        last_run_load = _training_load(last_run) if last_run else None
        last_run_distance = _distance_km(last_run) if last_run else None
        last_run_duration = _duration_min(last_run) if last_run else None

        cutoff_7d = target_datetime - timedelta(days=7)
        cutoff_14d = target_datetime - timedelta(days=14)
        cutoff_30d = target_datetime - timedelta(days=30)
        activities_7d = [a for a in activities if _activity_dt(a) and _activity_dt(a) >= cutoff_7d]
        activities_14d = [a for a in activities if _activity_dt(a) and _activity_dt(a) >= cutoff_14d]
        activities_30d = [a for a in activities if _activity_dt(a) and _activity_dt(a) >= cutoff_30d]
        runs_30d = [a for a in activities_30d if _is_run(a.get("sport_type"))]

        load_7d = sum(_training_load(a) for a in activities_7d)
        load_14d = sum(_training_load(a) for a in activities_14d)
        load_30d = sum(_training_load(a) for a in activities_30d)
        load_30d_weekly_equivalent = load_30d / 30.0 * 7.0 if load_30d > 0 else 0.0
        load_ratio = None
        if load_30d_weekly_equivalent > 1e-6:
            load_ratio = load_7d / load_30d_weekly_equivalent

        hard_minutes_7d = sum(_hard_minutes(a) for a in activities_7d)
        hard_minutes_30d = sum(_hard_minutes(a) for a in activities_30d)
        last_hard_hours_end = None
        for a in activities:
            if _is_hard_activity(a):
                last_hard_hours_end = _hours_between_activity_end_and_target(a, target_datetime)
                break

        run_km_7d = sum(_distance_km(a) for a in activities_7d if _is_run(a.get("sport_type")))
        run_km_30d = sum(_distance_km(a) for a in activities_30d if _is_run(a.get("sport_type")))
        run_km_30d_weekly = run_km_30d / 30.0 * 7.0 if run_km_30d > 0 else 0.0
        run_minutes_7d = sum(_duration_min(a) for a in activities_7d if _is_run(a.get("sport_type")))
        ride_minutes_7d = sum(_duration_min(a) for a in activities_7d if _is_ride(a.get("sport_type")))

        run_distances = [_distance_km(a) for a in runs_30d if _distance_km(a) > 0.5]
        run_durations = [_duration_min(a) for a in runs_30d if _duration_min(a) > 10.0]
        median_run_distance = median(run_distances) if run_distances else None
        median_run_duration = median(run_durations) if run_durations else None
        longest_run_30d = max(run_distances) if run_distances else None

        recent_easy_gap = _median_easy_run_pace(runs_30d, use_gap=True)
        recent_easy_pace = _median_easy_run_pace(runs_30d, use_gap=False)

        available_sport_types = sorted({str(a.get("sport_type") or "Unknown") for a in activities if a.get("sport_type")})
        sport_type_counts = dict(Counter(str(a.get("sport_type") or "Unknown") for a in activities if a.get("sport_type")))

        return TrainingContext(
            target_datetime=target_datetime,
            considered_count=len(activities),
            profile_vo2max=self.profile_vo2max,
            profile_birthdate=self.birthdate,
            profile_gender=self.gender,
            profile_age_years=self.age_years,
            profile_weight_kg=self.weight_kg,
            profile_height_cm=self.height_cm,
            profile_bmi=self.bmi,
            last_activity=last_activity,
            last_activity_hours_since_start=last_activity_hours_start,
            last_activity_hours_since_end=last_activity_hours_end,
            last_activity_load=last_activity_load,
            last_activity_hard_fraction=last_activity_hard_fraction,
            last_activity_stress_label=last_activity_stress_label,
            last_activity_temp_c=last_activity_temp,
            last_activity_temp_deviation_from_15_c=last_activity_temp_dev,
            last_run=last_run,
            last_run_hours_since_end=last_run_hours_end,
            last_run_distance_km=last_run_distance,
            last_run_duration_min=last_run_duration,
            last_run_hr_efficiency_drift_pct=last_run_drift,
            last_run_hard_fraction=last_run_hard_fraction,
            last_run_load=last_run_load,
            load_7d=load_7d,
            load_14d=load_14d,
            load_30d=load_30d,
            load_30d_weekly_equivalent=load_30d_weekly_equivalent,
            load_ratio_7d_vs_30d_weekly=load_ratio,
            hard_minutes_7d=hard_minutes_7d,
            hard_minutes_30d=hard_minutes_30d,
            last_hard_hours_since_end=last_hard_hours_end,
            activities_7d=len(activities_7d),
            run_km_7d=run_km_7d,
            run_km_30d=run_km_30d,
            run_km_30d_weekly_equivalent=run_km_30d_weekly,
            run_minutes_7d=run_minutes_7d,
            ride_minutes_7d=ride_minutes_7d,
            median_run_distance_30d_km=median_run_distance,
            median_run_duration_30d_min=median_run_duration,
            longest_run_30d_km=longest_run_30d,
            recent_easy_gap_pace_min_km=recent_easy_gap,
            recent_easy_pace_min_km=recent_easy_pace,
            available_sport_types=available_sport_types,
            sport_type_counts=sport_type_counts,
        )

    def _estimate_recovery_score(self, c: TrainingContext) -> Tuple[float, List[str]]:
        score = 66.0
        reasons: List[str] = []

        h = c.last_activity_hours_since_end
        if h is None:
            reasons.append("No previous activity end time could be parsed; assuming moderate recovery.")
        elif h < 8:
            score -= 42
            reasons.append(f"Previous workout ended only {h:.1f} h ago; this is usually too soon for another endurance session.")
        elif h < 16:
            score -= 25
            reasons.append(f"Previous workout ended {h:.1f} h ago; active recovery only if you feel good.")
        elif h < 24:
            score -= 10
            reasons.append(f"Previous workout ended {h:.1f} h ago; avoid intensity but light aerobic work can be reasonable.")
        elif h < 36:
            # This is where v1 was too strict. 24-36h after a normal 10k should not imply rest.
            if c.last_activity_stress_label in {"very_hard", "hard"}:
                score -= 6
                reasons.append(f"Previous workout ended {h:.1f} h ago and was classified as {c.last_activity_stress_label}; keep the next session easy.")
            else:
                score += 3
                reasons.append(f"Previous workout ended {h:.1f} h ago and was not classified as hard; an easy workout is normally acceptable.")
        elif h < 60:
            score += 8
            reasons.append(f"Previous workout ended {h:.1f} h ago; recovery time is adequate for aerobic work.")
        elif h < 96:
            score += 5
            reasons.append(f"Previous workout ended {h:.1f} h ago; a normal aerobic or controlled quality session may be possible.")
        else:
            score -= 2
            reasons.append(f"Previous workout ended {h:.1f} h ago; avoid a sudden jump, but do not default to rest.")

        # Previous-workout strain: based on load and actual zone distribution, not just max HR.
        if c.last_activity_stress_label == "very_hard":
            score -= 14
            reasons.append("The previous workout had very high zone-weighted load or hard-zone fraction.")
        elif c.last_activity_stress_label == "hard":
            score -= 7
            reasons.append("The previous workout was classified as hard; avoid another hard session.")
        elif c.last_activity_stress_label == "moderate":
            score -= 1
            reasons.append("The previous workout was moderate, not a recovery-only red flag.")
        elif c.last_activity_stress_label == "easy":
            score += 4
            reasons.append("The previous workout looked easy/aerobic by HR-zone distribution.")

        # Temperature affects HR and perceived effort. We use 15 °C as a neutral
        # reference and penalise large deviations only modestly, because weather is
        # a confounder rather than a direct injury signal.
        if c.last_activity_temp_c is not None:
            dev = abs(c.last_activity_temp_c - 15.0)
            if dev >= 18.0:
                score -= 8
                reasons.append(f"Last workout temperature was {c.last_activity_temp_c:.1f} °C, far from 15 °C; HR/effort may be inflated and recovery should be conservative.")
            elif dev >= 10.0:
                score -= 4
                reasons.append(f"Last workout temperature was {c.last_activity_temp_c:.1f} °C; deviation from 15 °C may reduce performance and raise HR.")
            elif dev >= 6.0:
                score -= 1
                reasons.append(f"Last workout temperature was {c.last_activity_temp_c:.1f} °C; small weather adjustment applied.")

        if c.profile_bmi is not None:
            if c.profile_bmi >= 30.0:
                score -= 6
                reasons.append(f"Current BMI estimate is {c.profile_bmi:.1f}; impact-heavy run intensity should be progressed carefully.")
            elif c.profile_bmi >= 25.0:
                score -= 2
                reasons.append(f"Current BMI estimate is {c.profile_bmi:.1f}; keeping most sessions easy helps joint/load tolerance.")
            elif 18.5 <= c.profile_bmi < 25.0:
                reasons.append(f"Current BMI estimate is {c.profile_bmi:.1f}, in the normal range; no BMI load penalty applied.")

        last_power_eff = _power_hr_efficiency(c.last_activity) if c.last_activity else None
        if last_power_eff is not None and _is_ride(c.last_activity.get("sport_type")):
            reasons.append(f"Last ride power/HR efficiency was {last_power_eff:.2f} run-equivalent W per bpm; cycling power is considered in load interpretation.")

        drift = c.last_run_hr_efficiency_drift_pct
        # Drift is noisy, especially on hills/heat/pauses. It should guide intensity, not automatically prescribe rest.
        if drift is None:
            reasons.append("No recent run HR-efficiency drift available; not using drift as a fatigue limiter.")
        elif drift <= -10.0:
            score -= 20
            reasons.append(f"Last run HR-efficiency drift was very negative ({drift:.1f} %); recovery or cycling is safer than a hard run.")
        elif drift <= -7.0:
            score -= 12
            reasons.append(f"Last run HR-efficiency drift was clearly negative ({drift:.1f} %); keep the next run easy/short.")
        elif drift <= -5.0:
            score -= 6
            reasons.append(f"Last run HR-efficiency drift was mildly negative ({drift:.1f} %); use Z1-Z2 only.")
        elif drift <= -3.0:
            score -= 2
            reasons.append(f"Last run HR-efficiency drift was slightly negative ({drift:.1f} %), but not enough to force rest.")
        elif drift <= 3.0:
            score += 5
            reasons.append(f"Last run HR-efficiency drift was stable ({drift:.1f} %), supporting normal aerobic work.")
        else:
            score += 3
            reasons.append(f"Last run HR-efficiency drift was positive ({drift:.1f} %); this may reflect good pacing or route effects.")

        ratio = c.load_ratio_7d_vs_30d_weekly
        if ratio is not None:
            if ratio > 1.65:
                score -= 16
                reasons.append(f"7-day load is very high versus 30-day baseline ({ratio:.2f}x); no hard workout today.")
            elif ratio > 1.35:
                score -= 8
                reasons.append(f"7-day load is elevated versus baseline ({ratio:.2f}x); choose easy aerobic work.")
            elif 0.75 <= ratio <= 1.20:
                score += 5
                reasons.append(f"7-day load is close to baseline ({ratio:.2f}x), which supports consistent training.")
            elif ratio < 0.65:
                score += 2
                reasons.append(f"Recent load is low versus baseline ({ratio:.2f}x); training is appropriate, but build progressively.")
            else:
                reasons.append(f"7-day load is moderately below baseline ({ratio:.2f}x); aerobic work is appropriate.")

        if c.last_hard_hours_since_end is not None:
            if c.last_hard_hours_since_end < 24:
                score -= 18
                reasons.append(f"A genuinely hard workout ended {c.last_hard_hours_since_end:.1f} h ago; avoid another hard session.")
            elif c.last_hard_hours_since_end < 36:
                score -= 8
                reasons.append(f"A hard workout ended {c.last_hard_hours_since_end:.1f} h ago; easy aerobic work only.")
            elif c.last_hard_hours_since_end < 48:
                score -= 3
                reasons.append(f"A hard workout ended {c.last_hard_hours_since_end:.1f} h ago; intensity should still be conservative.")
            elif c.last_hard_hours_since_end >= 72:
                score += 4
                reasons.append(f"No genuinely hard workout in {c.last_hard_hours_since_end:.1f} h; quality work is possible if other signals agree.")

        if c.hard_minutes_7d > 75:
            score -= 12
            reasons.append(f"Hard-zone time in the last 7 days is high ({c.hard_minutes_7d:.0f} min).")
        elif c.hard_minutes_7d > 45:
            score -= 6
            reasons.append(f"Hard-zone time in the last 7 days is moderate-high ({c.hard_minutes_7d:.0f} min).")
        elif c.hard_minutes_7d < 20:
            score += 3
            reasons.append(f"Hard-zone time in the last 7 days is low ({c.hard_minutes_7d:.0f} min).")

        # A floor rule: after >24h since a non-hard workout, do not prescribe pure rest unless there are severe red flags.
        severe_flags = 0
        if drift is not None and drift <= -10.0:
            severe_flags += 1
        if ratio is not None and ratio > 1.65:
            severe_flags += 1
        if c.last_activity_stress_label == "very_hard":
            severe_flags += 1
        if h is not None and h >= 24 and c.last_activity_stress_label not in {"very_hard"} and severe_flags == 0:
            score = max(score, 46.0)
            reasons.append("Floor rule applied: >24 h after a non-extreme workout should permit at least light aerobic training.")

        return max(0.0, min(100.0, score)), reasons

    def _candidate_votes(self, c: TrainingContext, recovery_score: float) -> Dict[str, str]:
        """Three deterministic 'coach' policies. This gives inspectable majority voting."""
        h = c.last_activity_hours_since_end or 999.0
        drift = c.last_run_hr_efficiency_drift_pct
        ratio = c.load_ratio_7d_vs_30d_weekly or 1.0
        hard_recent = c.last_hard_hours_since_end is not None and c.last_hard_hours_since_end < 48

        # Safety coach: protects against too much intensity.
        if h < 12 or recovery_score < 30:
            safety = "rest_or_mobility"
        elif recovery_score < 48 or (drift is not None and drift <= -8) or ratio > 1.55:
            safety = "recovery_aerobic"
        elif hard_recent or ratio > 1.25:
            safety = "easy_aerobic"
        elif recovery_score >= 78:
            safety = "steady_progression"
        else:
            safety = "easy_aerobic"

        # Balanced coach: default training logic.
        if h < 10:
            balanced = "rest_or_mobility"
        elif recovery_score < 42:
            balanced = "recovery_aerobic"
        elif recovery_score < 68:
            balanced = "easy_aerobic"
        elif recovery_score < 80 or hard_recent:
            balanced = "steady_progression"
        else:
            balanced = "quality"

        # Progression coach: assumes the goal is to improve, not just avoid fatigue.
        if h < 8 or recovery_score < 28:
            progression = "rest_or_mobility"
        elif recovery_score < 40:
            progression = "recovery_aerobic"
        elif recovery_score < 62 or ratio > 1.45:
            progression = "easy_aerobic"
        elif hard_recent:
            progression = "steady_progression"
        else:
            progression = "quality" if recovery_score >= 76 and ratio <= 1.25 else "steady_progression"

        return {"safety": safety, "balanced": balanced, "progression": progression}

    def _select_family_from_votes(self, votes: Dict[str, str], c: TrainingContext, recovery_score: float) -> str:
        families = list(votes.values())
        counts = Counter(families)
        if counts:
            most_common = counts.most_common()
            if len(most_common) == 1 or most_common[0][1] > most_common[1][1]:
                family = most_common[0][0]
            else:
                # Tie-break: median aggressiveness, slightly progression-biased but not reckless.
                sorted_families = sorted(families, key=lambda x: AGGRESSIVENESS[x])
                family = sorted_families[len(sorted_families) // 2]
        else:
            family = "easy_aerobic"

        # Guardrails.
        h = c.last_activity_hours_since_end or 999.0
        if h < 8:
            return "rest_or_mobility"
        if h < 16 and AGGRESSIVENESS[family] > AGGRESSIVENESS["recovery_aerobic"]:
            return "recovery_aerobic"
        if c.load_ratio_7d_vs_30d_weekly is not None and c.load_ratio_7d_vs_30d_weekly > 1.65:
            return min_family(family, "easy_aerobic")
        if c.last_run_hr_efficiency_drift_pct is not None and c.last_run_hr_efficiency_drift_pct <= -10.0:
            return min_family(family, "recovery_aerobic")
        if recovery_score < 35:
            return min_family(family, "recovery_aerobic")
        return family

    def _choose_sport_type(
        self,
        c: TrainingContext,
        desired_sport_type: Optional[str],
        workout_family: str,
    ) -> Tuple[str, List[str]]:
        reasons: List[str] = []
        available = c.available_sport_types

        if desired_sport_type and desired_sport_type != "Auto / none":
            reasons.append(f"Requested sport type '{desired_sport_type}' was selected; only intensity/duration are adjusted by the model.")
            return desired_sport_type, reasons

        ride_type = _first_available_type(available, RIDE_TYPES)
        run_type = _first_available_type(available, RUN_TYPES)
        drift = c.last_run_hr_efficiency_drift_pct

        if workout_family in {"rest_or_mobility", "recovery_aerobic"}:
            if ride_type and drift is not None and drift <= -8.0:
                reasons.append("Auto-selected cycling/ergometer because the recent run drift was poor and cycling reduces impact load.")
                return ride_type, reasons
            if ride_type and c.run_km_7d > max(8.0, c.run_km_30d_weekly_equivalent * 1.25):
                reasons.append("Auto-selected cycling/ergometer because recent running distance is already high relative to baseline.")
                return ride_type, reasons
            if run_type:
                reasons.append("Auto-selected running because recovery status allows easy aerobic work.")
                return run_type, reasons

        if workout_family in {"easy_aerobic", "steady_progression"}:
            if run_type:
                reasons.append("Auto-selected running to build aerobic running capacity progressively.")
                return run_type, reasons
            if ride_type:
                reasons.append("Auto-selected cycling/ergometer for aerobic progression.")
                return ride_type, reasons

        if workout_family == "quality":
            if run_type and (drift is None or drift > -5.0) and c.last_run_hours_since_end is not None and c.last_run_hours_since_end >= 36:
                reasons.append("Auto-selected running for a controlled quality session because run drift/time-since-run are acceptable.")
                return run_type, reasons
            if ride_type:
                reasons.append("Auto-selected cycling/ergometer for quality work because it is available and lower-impact.")
                return ride_type, reasons

        most_common = _most_common_sport_type(c)
        if most_common:
            reasons.append(f"Auto-selected the most common recent sport type: {most_common}.")
            return most_common, reasons

        return "Generic", ["No known sport type could be selected; using a generic aerobic workout."]

    def _build_workout(
        self,
        sport_type: str,
        workout_family: str,
        recovery_score: float,
        c: TrainingContext,
    ) -> Dict[str, Any]:
        is_run = _is_run(sport_type)
        is_ride = _is_ride(sport_type)

        if workout_family == "rest_or_mobility":
            return {
                "title": "Rest or mobility only",
                "duration_min": 0,
                "target_intensity": "No endurance load",
                "structure": [
                    "No endurance workout recommended by the safety guardrails.",
                    "Optional: 10-20 min easy walk or mobility if you feel normal.",
                ],
                "stop_rules": ["Skip entirely if fatigue, pain, illness, or unusually high resting HR is present."],
            }

        if workout_family == "recovery_aerobic":
            return _recovery_workout(sport_type, is_run, is_ride, c)
        if workout_family == "easy_aerobic":
            return _easy_workout(sport_type, is_run, is_ride, c, progressive=False)
        if workout_family == "steady_progression":
            return _easy_workout(sport_type, is_run, is_ride, c, progressive=True)
        return _quality_workout(sport_type, is_run, is_ride, c)

    def _vote_reasons(self, votes: Dict[str, str], chosen: str) -> List[str]:
        return [
            f"Ensemble votes: safety={votes.get('safety')}, balanced={votes.get('balanced')}, progression={votes.get('progression')}; selected={chosen}.",
            "The progression vote intentionally prevents the model from preserving the current low fitness level when recovery signals are acceptable.",
        ]

    def _context_reasons(self, c: TrainingContext) -> List[str]:
        reasons: List[str] = []
        if c.last_activity is not None:
            name = c.last_activity.get("name") or "last activity"
            h = c.last_activity_hours_since_end
            reasons.append(f"Most recent activity: {name}, estimated {h:.1f} h since finish, stress={c.last_activity_stress_label}, load={c.last_activity_load:.0f}.")
        if c.last_activity_temp_c is not None:
            reasons.append(f"Most recent activity weather: {c.last_activity_temp_c:.1f} °C; deviation from 15 °C = {abs(c.last_activity_temp_c - 15.0):.1f} °C.")
        if c.last_run is not None:
            name = c.last_run.get("name") or "last run"
            h = c.last_run_hours_since_end
            drift = c.last_run_hr_efficiency_drift_pct
            distance = c.last_run_distance_km
            msg = f"Most recent run: {name}"
            if distance is not None:
                msg += f", {distance:.1f} km"
            if h is not None:
                msg += f", estimated {h:.1f} h since finish"
            if drift is not None:
                msg += f", HR-efficiency drift {drift:.1f} %"
            reasons.append(msg + ".")
        reasons.append(f"Last 7 days: {c.activities_7d} activities, load {c.load_7d:.0f}, run {c.run_km_7d:.1f} km, hard-zone time {c.hard_minutes_7d:.0f} min.")
        if c.recent_easy_gap_pace_min_km:
            reasons.append(f"Recent easy GAP pace estimate: {_pace_text(c.recent_easy_gap_pace_min_km)} min/km.")
        profile_bits = []
        if c.profile_age_years is not None:
            profile_bits.append(f"age {c.profile_age_years:.0f}")
        if c.profile_gender:
            profile_bits.append(str(c.profile_gender))
        if c.profile_weight_kg is not None:
            profile_bits.append(f"{c.profile_weight_kg:.1f} kg")
        if c.profile_bmi is not None:
            profile_bits.append(f"BMI {c.profile_bmi:.1f}")
        profile_suffix = "; " + ", ".join(profile_bits) if profile_bits else ""
        reasons.append(f"Configured VO2max estimate: {c.profile_vo2max:.1f} ml/kg/min{profile_suffix}; used as profile context and fallback when recent workout data are insufficient.")
        return reasons

    def _llm_ready_summary(
        self,
        c: TrainingContext,
        recovery_score: float,
        workout_family: str,
        selected_type: str,
        workout: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "instruction": "Explain the recommendation, do not change it unless there is an obvious contradiction. Keep safety caveats concise.",
            "profile": {"vo2max_estimate": c.profile_vo2max, "birthdate": c.profile_birthdate, "gender": c.profile_gender, "age_years": c.profile_age_years, "weight_kg": c.profile_weight_kg, "height_cm": c.profile_height_cm, "bmi": c.profile_bmi, "hr_zones_bpm": {"z1": "<134", "z2": "134-145", "z3": "146-155", "z4": "156-166", "z5": ">=167"}},
            "context": _context_to_dict(c),
            "selected": {"sport_type": selected_type, "workout_family": workout_family, "recovery_score": recovery_score, "workout": workout},
        }

    def _no_data_recommendation(self, target_datetime: datetime, desired_sport_type: Optional[str]) -> Dict[str, Any]:
        sport = desired_sport_type or "Run"
        return {
            "status": "no_data",
            "target_datetime": target_datetime.isoformat(timespec="seconds"),
            "desired_sport_type": desired_sport_type or "Auto / none",
            "selected_sport_type": sport,
            "workout_family": "easy_baseline",
            "recovery_score_0_100": None,
            "recommendation": {
                "title": "Conservative baseline workout",
                "duration_min": 30,
                "target_intensity": "Z1-Z2 only",
                "structure": [
                    f"30 min easy {sport}.",
                    "Keep HR below Z3.",
                    "Stop if effort feels unexpectedly high.",
                ],
                "stop_rules": ["No historical data available; stay conservative."],
            },
            "candidate_votes": {},
            "reasons": ["No previous activities were available before the selected target time."],
            "context": {},
            "llm_ready_summary": {},
            "method": "deterministic_progressive_ensemble_v2",
            "disclaimer": "Algorithmic training suggestion, not medical advice.",
        }


def _recovery_workout(sport_type: str, is_run: bool, is_ride: bool, c: TrainingContext) -> Dict[str, Any]:
    if is_run:
        pace = _run_pace_guidance(c, effort="recovery")
        dist = _suggest_run_distance_km(c, family="recovery")
        return {
            "title": "Recovery run / walk-run",
            "duration_min": 30,
            "target_distance_km": dist,
            "target_intensity": "Z1 to low Z2; ideally HR <140-146 bpm",
            "structure": [
                "5-8 min very easy warm-up walk/jog.",
                f"Then {dist:.1f} km total or about 25-35 min on flat terrain.",
                pace,
                "No strides, hills, threshold sections or intervals.",
            ],
            "stop_rules": ["Stop or switch to walking if knee discomfort appears or HR climbs into Z3 at easy effort."],
        }
    if is_ride:
        return {
            "title": "Recovery ride / ergometer",
            "duration_min": 40,
            "target_intensity": "Z1-Z2; keep HR <146 bpm",
            "structure": ["10 min very easy spin.", "25 min comfortable cadence in Z1-Z2.", "5 min cool-down."],
            "stop_rules": ["Keep resistance low; this should feel restorative, not productive."],
        }
    return {
        "title": "Recovery aerobic session",
        "duration_min": 30,
        "target_intensity": "Z1-Z2 only",
        "structure": ["30 min very easy aerobic work.", "Avoid intervals and hills."],
        "stop_rules": ["Stop if perceived exertion is unexpectedly high."],
    }


def _easy_workout(sport_type: str, is_run: bool, is_ride: bool, c: TrainingContext, progressive: bool) -> Dict[str, Any]:
    if is_run:
        dist = _suggest_run_distance_km(c, family="progressive" if progressive else "easy")
        duration = _duration_from_distance_and_pace(dist, c.recent_easy_gap_pace_min_km or c.recent_easy_pace_min_km or _fallback_easy_pace_from_vo2max(c.profile_vo2max))
        title = "Progressive aerobic run" if progressive else "Easy aerobic run"
        structure = [
            "8-10 min easy warm-up in Z1.",
            f"Run about {dist:.1f} km total, mostly Z2 (134-145 bpm).",
            _run_pace_guidance(c, effort="easy"),
            "Use grade-adjusted effort rather than raw pace on hills.",
        ]
        if progressive:
            structure.append("Last 8-12 min may drift to upper Z2/low Z3 only if breathing stays controlled and legs feel normal.")
        else:
            structure.append("Optional only if legs feel excellent: 4 x 15 s relaxed strides with full recovery.")
        structure.append("5 min easy cool-down.")
        return {
            "title": title,
            "duration_min": duration,
            "target_distance_km": dist,
            "target_intensity": "Mostly Z2; HR 134-145 bpm, short Z1 allowed",
            "structure": structure,
            "stop_rules": ["Downgrade to 25-30 min recovery jog/walk if HR is >10 bpm higher than usual at easy pace, HR drifts strongly upward, or knee discomfort appears."],
        }
    if is_ride:
        duration = 70 if progressive else 60
        return {
            "title": "Progressive aerobic ride / ergometer" if progressive else "Easy aerobic ride / ergometer",
            "duration_min": duration,
            "target_intensity": "Mostly Z2; HR 134-145 bpm",
            "structure": [
                "10 min easy warm-up.",
                f"{duration - 20} min steady Z2. Keep cadence smooth.",
                "Optional: last 10 min upper Z2 only if HR remains controlled." if progressive else "Keep it boring and aerobic.",
                "5-10 min cool-down.",
            ],
            "stop_rules": ["Do not chase speed or watts; HR control is the session goal."],
        }
    return {
        "title": "Progressive aerobic workout" if progressive else "Easy aerobic workout",
        "duration_min": 50 if progressive else 45,
        "target_intensity": "Z2 dominant",
        "structure": ["Comfortable aerobic work.", "Avoid hard intervals."],
        "stop_rules": ["Stop if effort feels like threshold work."],
    }


def _quality_workout(sport_type: str, is_run: bool, is_ride: bool, c: TrainingContext) -> Dict[str, Any]:
    if is_run:
        return {
            "title": "Controlled run intervals",
            "duration_min": 50,
            "target_distance_km": None,
            "target_intensity": "Z4 only during work bouts; never all-out",
            "structure": [
                "12-15 min warm-up in Z1-Z2.",
                "5-6 x 2 min controlled hard effort around Z4 with 2 min very easy jog/walk between reps.",
                "10 min cool-down.",
                "Keep terrain flat to make HR/pace interpretable.",
            ],
            "stop_rules": ["Stop the interval block if HR does not recover between reps, breathing feels uncontrolled, or knee discomfort appears."],
        }
    if is_ride:
        return {
            "title": "Controlled bike intervals",
            "duration_min": 55,
            "target_intensity": "Z4 during intervals, Z1-Z2 between intervals",
            "structure": ["12 min easy warm-up.", "5 x 3 min Z4 with 3 min easy spinning between reps.", "10 min cool-down."],
            "stop_rules": ["Keep cadence smooth; stop if HR remains elevated during easy recoveries."],
        }
    return {
        "title": "Controlled quality workout",
        "duration_min": 45,
        "target_intensity": "Moderate-hard intervals, not maximal",
        "structure": ["Warm up well.", "Main set: 5-6 controlled intervals.", "Cool down thoroughly."],
        "stop_rules": ["Avoid maximal efforts."],
    }


def _suggest_run_distance_km(c: TrainingContext, family: str) -> float:
    """Suggest distance from recent run history and recovery state."""
    last = c.last_run_distance_km or 0.0
    median_dist = c.median_run_distance_30d_km or last or 5.0
    longest = c.longest_run_30d_km or max(median_dist, last, 5.0)
    ratio = c.load_ratio_7d_vs_30d_weekly or 1.0
    h = c.last_run_hours_since_end or 999.0

    if family == "recovery":
        base = min(0.45 * max(last, median_dist), 0.65 * median_dist)
        return _clamp(round(base, 1), 3.0, min(5.5, longest * 0.7))

    if family == "easy":
        # After a recent 10k, roughly 6-8 km easy is reasonable unless there are strong red flags.
        # Elevated 7-day load should mainly reduce intensity, not automatically collapse the run to rest.
        if h < 36 and last >= 8:
            base = 0.75 * last
        else:
            base = 0.90 * median_dist
        if ratio > 1.65:
            base *= 0.85
        elif ratio > 1.35:
            base *= 0.90
        elif ratio < 0.75:
            base *= 1.08
        return _clamp(round(base, 1), 4.0, min(8.5, longest * 0.95))

    # steady progression: mild overload, not a long-run jump.
    base = max(0.90 * median_dist, 0.70 * last if last else 0.0)
    if ratio < 0.90:
        base *= 1.10
    elif ratio > 1.20:
        base *= 0.90
    return _clamp(round(base, 1), 5.0, min(10.0, longest * 1.05))


def _run_pace_guidance(c: TrainingContext, effort: str) -> str:
    pace = c.recent_easy_gap_pace_min_km or c.recent_easy_pace_min_km
    source = "recent easy GAP pace" if c.recent_easy_gap_pace_min_km else "recent easy pace"
    if pace is None:
        pace = _fallback_easy_pace_from_vo2max(c.profile_vo2max)
        source = "VO2max fallback"
    if effort == "recovery":
        lo = pace + 0.25
        hi = pace + 0.85
        return f"Pace guide: roughly {_pace_text(lo)}-{_pace_text(hi)} min/km if this keeps HR below Z2; otherwise slow down/walk. Source: {source}."
    lo = pace - 0.10
    hi = pace + 0.50
    return f"Pace guide: roughly {_pace_text(lo)}-{_pace_text(hi)} min/km on flat terrain, but HR/effort overrides pace. Source: {source}."


def _fallback_easy_pace_from_vo2max(vo2max: float) -> float:
    # ACSM-style flat-running oxygen cost: VO2 = 0.2 * speed_m_min + 3.5.
    # Easy aerobic target around 58-64% of VO2max for a conservative beginner/intermediate estimate.
    target_vo2 = max(16.0, vo2max * 0.60)
    speed_m_min = max(80.0, (target_vo2 - 3.5) / 0.2)
    return 1000.0 / speed_m_min


def _duration_from_distance_and_pace(distance_km: float, pace_min_km: float) -> int:
    return int(round(_clamp(distance_km * pace_min_km, 30.0, 75.0)))


def _median_easy_run_pace(runs: List[Dict[str, Any]], use_gap: bool) -> Optional[float]:
    values: List[float] = []
    key = "avg_gap_pace_min_km" if use_gap else "avg_pace_min_km"
    for a in runs:
        dur = _duration_min(a)
        if dur < 20 or dur > 120:
            continue
        pace = _safe_float(a.get(key))
        if pace is None or pace <= 2.5 or pace > 12.0:
            continue
        z_total = _zone_total_s(a)
        easy_frac = 0.0
        if z_total > 0:
            easy_frac = ((_safe_float(a.get("z1_s")) or 0.0) + (_safe_float(a.get("z2_s")) or 0.0)) / z_total
        avg_hr = _safe_float(a.get("avg_hr"))
        if easy_frac >= 0.55 or (avg_hr is not None and avg_hr < HR_Z2_MAX):
            values.append(pace)
    return median(values) if values else None


def _clean_and_sort_activities(activities: List[Dict[str, Any]], target_datetime: datetime) -> List[Dict[str, Any]]:
    valid = []
    for a in activities:
        dt = _activity_dt(a)
        if dt is None:
            continue
        if dt <= target_datetime:
            valid.append(a)
    valid.sort(key=lambda x: _activity_dt(x) or datetime.min, reverse=True)
    return valid


def _activity_dt(a: Optional[Dict[str, Any]]) -> Optional[datetime]:
    if not a:
        return None
    value = a.get("start_date_local")
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _activity_end_dt(a: Optional[Dict[str, Any]]) -> Optional[datetime]:
    dt = _activity_dt(a)
    if dt is None:
        return None
    seconds = _safe_float(a.get("elapsed_time_s")) or _safe_float(a.get("moving_time_s")) or 0.0
    return dt + timedelta(seconds=max(0.0, seconds))


def _hours_between_activity_start_and_target(a: Optional[Dict[str, Any]], target: datetime) -> Optional[float]:
    dt = _activity_dt(a)
    if dt is None:
        return None
    return max(0.0, (target - dt).total_seconds() / 3600.0)


def _hours_between_activity_end_and_target(a: Optional[Dict[str, Any]], target: datetime) -> Optional[float]:
    dt = _activity_end_dt(a)
    if dt is None:
        return None
    return max(0.0, (target - dt).total_seconds() / 3600.0)


def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        v = float(x)
        if not math.isfinite(v):
            return None
        return v
    except Exception:
        return None


def _duration_min(a: Optional[Dict[str, Any]]) -> float:
    if not a:
        return 0.0
    seconds = _safe_float(a.get("moving_time_s")) or _safe_float(a.get("elapsed_time_s")) or 0.0
    return max(0.0, seconds / 60.0)


def _distance_km(a: Optional[Dict[str, Any]]) -> float:
    if not a:
        return 0.0
    return max(0.0, (_safe_float(a.get("distance_m")) or 0.0) / 1000.0)


def _zone_total_s(a: Dict[str, Any]) -> float:
    return sum((_safe_float(a.get(k)) or 0.0) for k in ["z1_s", "z2_s", "z3_s", "z4_s", "z5_s"])



def _age_years_at(birthdate_iso: Optional[str], when_iso: Optional[str]) -> Optional[float]:
    try:
        if not birthdate_iso or not when_iso:
            return None
        b = datetime.fromisoformat(str(birthdate_iso)[:10]).date()
        w = datetime.fromisoformat(str(when_iso).replace("Z", "+00:00")).date()
        return float(w.year - b.year - ((w.month, w.day) < (b.month, b.day)))
    except Exception:
        return None


def _calc_bmi(weight_kg: Optional[float], height_cm: Optional[float]) -> Optional[float]:
    try:
        w = float(weight_kg)
        h = float(height_cm) / 100.0
        if w <= 0 or h <= 0:
            return None
        return w / (h * h)
    except Exception:
        return None


def _power_based_load(a: Optional[Dict[str, Any]], duration_min: float) -> float:
    if not a or duration_min <= 0:
        return 0.0
    p = _bike_equivalent_power_w(a) if _is_ride(a.get("sport_type")) else _safe_float(a.get("avg_power"))
    if p is None or p <= 0:
        return 0.0
    # Conservative pseudo-zone load from cycling-equivalent watts. For ergometer
    # work, 210 W is treated as comparable to roughly 175 W running power.
    if p < 130:
        mult = 1.0
    elif p < 170:
        mult = 1.8
    elif p < 210:
        mult = 2.6
    elif p < 250:
        mult = 3.6
    elif p < 300:
        mult = 4.8
    else:
        mult = 6.0
    return duration_min * mult


def _bike_equivalent_power_w(a: Optional[Dict[str, Any]]) -> Optional[float]:
    if not a:
        return None
    for key in ("bike_equivalent_power_w", "cycling_power_w", "avg_power", "normalized_power", "bike_inferred_power_w"):
        val = _safe_float(a.get(key))
        if val is not None and val > 0:
            return val
    return None


def _run_equivalent_power_w(a: Optional[Dict[str, Any]]) -> Optional[float]:
    if not a:
        return None
    val = _safe_float(a.get("run_equivalent_power_w"))
    if val is not None and val > 0:
        return val
    bike = _bike_equivalent_power_w(a)
    if bike is not None and _is_ride(a.get("sport_type")):
        return bike * (175.0 / 210.0)
    return _safe_float(a.get("avg_power"))


def _power_hr_efficiency(a: Optional[Dict[str, Any]]) -> Optional[float]:
    if not a:
        return None
    existing = _safe_float(a.get("power_hr_efficiency"))
    if existing is not None:
        return existing
    p = _run_equivalent_power_w(a)
    hr = _safe_float(a.get("avg_hr"))
    if p is None or hr is None or hr <= 0:
        return None
    return p / hr


def _activity_temp_c(a: Optional[Dict[str, Any]]) -> Optional[float]:
    if not a:
        return None
    for key in ("weather_temp_c", "avg_temp_c", "temperature_c"):
        val = _safe_float(a.get(key))
        if val is not None and -50.0 <= val <= 60.0:
            return val
    return None

def _training_load(a: Optional[Dict[str, Any]]) -> float:
    if not a:
        return 0.0
    zone_total = _zone_total_s(a)
    if zone_total > 0:
        return sum(((_safe_float(a.get(key)) or 0.0) / 60.0) * weight for key, weight in ZONE_WEIGHTS.items())

    dur = _duration_min(a)
    avg_hr = _safe_float(a.get("avg_hr"))
    p_load = _power_based_load(a, dur)
    if avg_hr is None:
        return max(dur * 1.5, p_load or 0.0)
    if avg_hr < HR_Z1_MAX:
        mult = 1.0
    elif avg_hr < HR_Z2_MAX:
        mult = 2.0
    elif avg_hr < HR_Z3_MAX:
        mult = 3.0
    elif avg_hr < HR_Z4_MAX:
        mult = 4.5
    else:
        mult = 6.0
    hr_load = dur * mult
    return max(hr_load, p_load or 0.0)


def _hard_minutes(a: Dict[str, Any]) -> float:
    z4 = (_safe_float(a.get("z4_s")) or 0.0) / 60.0
    z5 = (_safe_float(a.get("z5_s")) or 0.0) / 60.0
    if z4 + z5 > 0:
        return z4 + z5
    avg_hr = _safe_float(a.get("avg_hr"))
    if avg_hr is not None and avg_hr >= HR_Z3_MAX:
        return _duration_min(a) * 0.5
    return 0.0


def _hard_fraction(a: Optional[Dict[str, Any]]) -> float:
    if not a:
        return 0.0
    zone_total = _zone_total_s(a)
    if zone_total > 0:
        return ((_safe_float(a.get("z4_s")) or 0.0) + (_safe_float(a.get("z5_s")) or 0.0)) / zone_total
    dur = _duration_min(a)
    if dur <= 0:
        return 0.0
    return _hard_minutes(a) / dur


def _is_hard_activity(a: Dict[str, Any]) -> bool:
    hm = _hard_minutes(a)
    frac = _hard_fraction(a)
    dur = _duration_min(a)
    load = _training_load(a)
    # Short HR spikes / single max-HR events are no longer enough.
    return hm >= 14.0 or (hm >= 8.0 and frac >= 0.18) or (dur >= 55.0 and load >= 180.0 and frac >= 0.12)


def _activity_stress_label(a: Optional[Dict[str, Any]]) -> str:
    if not a:
        return "unknown"
    hm = _hard_minutes(a)
    frac = _hard_fraction(a)
    load = _training_load(a)
    dur = _duration_min(a)
    if hm >= 30 or frac >= 0.35 or load >= 260:
        return "very_hard"
    if _is_hard_activity(a):
        return "hard"
    if dur >= 75 or load >= 120 or frac >= 0.08:
        return "moderate"
    return "easy"


def _norm_type(sport_type: Any) -> str:
    return str(sport_type or "").strip().replace(" ", "").replace("-", "").lower()


def _is_run(sport_type: Any) -> bool:
    return _norm_type(sport_type) in {t.replace("_", "") for t in RUN_TYPES}


def _is_ride(sport_type: Any) -> bool:
    return _norm_type(sport_type) in {t.replace("_", "") for t in RIDE_TYPES}


def _first_available_type(available: Iterable[str], normalized_set: set[str]) -> Optional[str]:
    norm_targets = {t.replace("_", "") for t in normalized_set}
    for original in available:
        if _norm_type(original) in norm_targets:
            return original
    return None


def _most_common_sport_type(c: TrainingContext) -> Optional[str]:
    if not c.sport_type_counts:
        return None
    return sorted(c.sport_type_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def min_family(a: str, b: str) -> str:
    return a if AGGRESSIVENESS[a] <= AGGRESSIVENESS[b] else b


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _pace_text(value: float) -> str:
    if value is None or not math.isfinite(value):
        return ""
    minutes = int(value)
    seconds = int(round((value - minutes) * 60))
    if seconds == 60:
        minutes += 1
        seconds = 0
    return f"{minutes}:{seconds:02d}"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _context_to_dict(c: TrainingContext) -> Dict[str, Any]:
    return {
        "considered_count": c.considered_count,
        "profile_vo2max": c.profile_vo2max,
        "profile_birthdate": c.profile_birthdate,
        "profile_gender": c.profile_gender,
        "profile_age_years": c.profile_age_years,
        "profile_weight_kg": c.profile_weight_kg,
        "profile_height_cm": c.profile_height_cm,
        "profile_bmi": c.profile_bmi,
        "last_activity_name": c.last_activity.get("name") if c.last_activity else None,
        "last_activity_type": c.last_activity.get("sport_type") if c.last_activity else None,
        "last_activity_hours_since_start": c.last_activity_hours_since_start,
        "last_activity_hours_since_end": c.last_activity_hours_since_end,
        "last_activity_load": c.last_activity_load,
        "last_activity_temp_c": c.last_activity_temp_c,
        "last_activity_temp_deviation_from_15_c": c.last_activity_temp_deviation_from_15_c,
        "last_activity_hard_fraction": c.last_activity_hard_fraction,
        "last_activity_stress_label": c.last_activity_stress_label,
        "last_run_name": c.last_run.get("name") if c.last_run else None,
        "last_run_hours_since_end": c.last_run_hours_since_end,
        "last_run_distance_km": c.last_run_distance_km,
        "last_run_duration_min": c.last_run_duration_min,
        "last_run_hr_efficiency_drift_pct": c.last_run_hr_efficiency_drift_pct,
        "last_run_hard_fraction": c.last_run_hard_fraction,
        "last_run_load": c.last_run_load,
        "load_7d": c.load_7d,
        "load_14d": c.load_14d,
        "load_30d": c.load_30d,
        "load_30d_weekly_equivalent": c.load_30d_weekly_equivalent,
        "load_ratio_7d_vs_30d_weekly": c.load_ratio_7d_vs_30d_weekly,
        "hard_minutes_7d": c.hard_minutes_7d,
        "hard_minutes_30d": c.hard_minutes_30d,
        "last_hard_hours_since_end": c.last_hard_hours_since_end,
        "activities_7d": c.activities_7d,
        "run_km_7d": c.run_km_7d,
        "run_km_30d": c.run_km_30d,
        "run_km_30d_weekly_equivalent": c.run_km_30d_weekly_equivalent,
        "run_minutes_7d": c.run_minutes_7d,
        "ride_minutes_7d": c.ride_minutes_7d,
        "median_run_distance_30d_km": c.median_run_distance_30d_km,
        "median_run_duration_30d_min": c.median_run_duration_30d_min,
        "longest_run_30d_km": c.longest_run_30d_km,
        "recent_easy_gap_pace_min_km": c.recent_easy_gap_pace_min_km,
        "recent_easy_pace_min_km": c.recent_easy_pace_min_km,
        "available_sport_types": c.available_sport_types,
        "sport_type_counts": c.sport_type_counts,
    }
