# WorkOutBuddy web parity audit

This checklist compares the original Python/NiceGUI application on `main` with the GitHub Pages + Supabase web version on `web-supabase-v2`.

## Restored / web-native

- Supabase Auth + RLS-backed private workout data
- TCX browser upload and private raw-file storage
- Duplicate detection during TCX import with explicit **Overwrite existing** / **Skip**
- Workout table with editable Apple VO2max and body weight
- Route map and synchronized stream profile
- Route/profile coloring by pace, speed, HR, HR zone, power, altitude, grade and cadence
- Linked map/graph cursor with live metric readout
- Flexible workout statistics: configurable X/Y, line/scatter/bar, sport grouping
- Deterministic training-state analysis (fitness trend, aerobic base, durability, load, run volume, evidence/limiters)
- Optional authenticated cloud AI coach through Supabase Edge Function
- Adaptive 14-day planner
- Manual per-day planner overrides with downstream replanning
- Training progression/aggressiveness setting
- Profile, HR zones, primary goal and training-frequency preferences
- Home recommendation and recent-workout viewer
- Gradient-adjusted pace and heuristic grade-normalized VO2max estimate for running

## Partially restored / should be expanded

- Statistics: original app supported multiple Y metrics, aggregation (mean/median/sum/min/max/count), trend overlays (linear/exponential/auto-parametric), and a statistics data table.
- Planner goals: original app supported simultaneously active 10k / half-marathon / marathon goals with target times and race-time projections. Web currently has one primary goal/date.
- Recommendation: original app allowed choosing a future date/time and desired sport type; web currently focuses on today's recommendation.
- Activity details: original app showed a much wider physiology/biomechanics readout and per-km splits. Many migrated values remain available in `metrics_json`, but the web detail UI does not expose all of them yet.
- Metric calculation: new TCX imports calculate core metrics, GAP and VO2 estimate, but the original Python MetricCalculator computed more advanced mechanics and best-effort metrics.
- Duplicate handling: future imports are protected. A separate audit/cleanup UI for duplicates already present in the database is still useful.

## Original functionality not yet ported

- Export XLSX
- One-click full metric recalculation for every stored activity
- Weather archive lookup and temperature-normalized context
- Per-km split table
- Best 1/5/10/20-minute pace and power efforts
- Run step frequency / total-step estimates
- Impact estimates and impact-load index
- Normalized-power variability metrics beyond values already migrated/stored
- Cycling inferred power, run-equivalent power and power/HR efficiency
- BMI / age-at-activity derived fields
- Detailed data-quality flags and diagnostics
- Advanced duplicate cleanup UI
- Race predictor / VDOT projections for 10k, HM and marathon
- Goal-specific 14-day periodization equivalent to the complete Python `WorkoutPlanner`
- Weather-aware analysis
- XLSX statistics/data export

## Intentionally retired

- Strava OAuth/sync: removed as a core dependency because the web rebuild is TCX-first.
- Local Ollama/Qwen AI backend: replaced by optional cloud API use; no large local model is required.
- Server-side TCX folder import: not appropriate for a static browser-hosted application; multi-file browser upload replaces it.
- Tailscale/local NiceGUI server requirement: no longer needed.

## Recommended UI organization

- **Home**: today's recommendation, quick status, next 7 days, recent/latest workout.
- **Workouts**: table, viewer and flexible statistics.
- **Planner**: full 14-day calendar, goals, race projections and manual overrides.
- **Analysis**: deterministic analysis first, optional cloud AI interpretation second.
- **Advanced / Tools**: exports, metric recalculation, weather enrichment, duplicate audit, detailed physiology, data-quality diagnostics.
