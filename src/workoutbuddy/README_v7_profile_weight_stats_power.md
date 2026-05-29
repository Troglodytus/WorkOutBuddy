# Workout Buddy v7: profile, weight/BMI, flexible statistics, trends and power normalization

## New profile fields

Defaults:
- birthday: 1993-06-16
- gender: Male
- height: 178 cm (editable; required for BMI)
- current weight: 75 kg (editable)

The planner/settings card now stores these values.

## Per-workout weight and BMI

Next to the Apple VO2max field for the selected workout there is now a body-weight input.
Saving it stores:
- `body_weight_kg`
- `bmi`
- `bmi_category`
- `age_years_at_activity`
- `gender`

BMI needs height, so the app uses the editable profile height.

## Statistics / graphics

The statistics tab is more pivot-chart-like:
- free X axis
- multiple Y metrics
- plot type: line / scatter / bar
- color/group by any available field
- optional aggregation: mean, median, sum, min, max, count
- optional trend line: linear, exponential, or auto-parametric
- trend legend shows R² and linearity

Default plot is now time on X and distance on Y.

Scatter and bar plots now use grouping/colors the same way line mode does.

## Power handling

Virtual ride / ergometer power is now considered more strongly.

Power normalization uses your assumption:
- 175 W running ≈ 210 W virtual cycling/ergometer
- ride-to-run equivalent factor = 175/210
- run-to-bike equivalent factor = 210/175

New derived metrics:
- `cycling_power_w`
- `run_equivalent_power_w`
- `bike_equivalent_power_w`
- `bike_inferred_power_w`
- `effective_power_w`
- `power_hr_efficiency`
- `power_hr_efficiency_drift_pct`

For outdoor bike rides without measured power, the app estimates power from speed, elevation gain, body weight and an older/non-aero bike assumption.

## Files changed

- `database.py`
- `web_app.py`
- `recommendation.py`
- plus the v5/v6 fixes retained for:
  - `weather.py`
  - `duplicate_cleanup.py`
