# WorkOutBuddy v16: planner type-change and analyzer usefulness fixes

## A) Crash fix

The planner editor crashed after changing a workout to Hike because `web_app.py`
had `_is_hike(...)` and `_is_strength(...)` calling `_norm_sport(...)`, but the
actual helper was named `_norm_sport_type(...)`.

Fixed by adding a backward-compatible alias and by using `_norm_sport_type(...)`
directly.

## B) Manual workout type change now auto-fills reliably

Changing the sport type in the day planner now always recalculates:

- title
- family
- duration
- distance
- HR zone/intensity
- pace or power field
- notes

This is no longer dependent only on the planner adaptation path. There is now a
local fallback builder in `web_app.py` so a change such as:

```text
Strength -> Hike
```

immediately becomes something like:

```text
Easy aerobic hike / Long aerobic hike / Hilly aerobic hike
```

with duration, distance, Z1-Z2/Z2/Z3 target and notes.

## C) Better Ollama analysis

`analysis_engine.py` now sends a much more explicit task prompt. It asks the LLM
to diagnose likely weaknesses, not just summarize workouts.

It specifically asks about:

- whether fitness is improving/stable/declining
- running-specific trend
- Z1-Z2 vs Z3 vs Z4-Z5 distribution
- HR versus pace/power
- fatigue resistance / HR drift
- easy/Z2 performance versus hard Z4-Z5 work
- whether the next block should prioritize Z2, less intensity, strength, hills/hikes, or recovery
- concrete next-14-day recommendations

It also sends zone fields to Ollama if available:

- `z1_s` ... `z5_s`
- `easy_zone_fraction`
- `hard_zone_fraction`

## Files to replace

Copy these into:

```text
src\workoutbuddy\
```

- `web_app.py`
- `planner.py`
- `analysis_engine.py`
