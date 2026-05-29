# Workout Buddy v8: database augmentation crash fix

The current crash is in `database.py`:

    AttributeError: 'WorkoutDatabase' object has no attribute '_augment_profile_power_metrics'

The v7 database code called this method from:

    read_activities_dataframe()
    read_activity_row()

but the method itself was missing. The app therefore crashed while the statistics panel asked for available metrics.

## Fix

`database.py` now includes `WorkoutDatabase._augment_profile_power_metrics(...)`.

It adds/fills DataFrame-level derived fields:

- body_weight_kg_effective
- bmi
- bmi_category
- age_years_at_activity
- gender
- bike_inferred_power_w
- cycling_power_w
- run_equivalent_power_w
- bike_equivalent_power_w
- effective_power_w
- power_hr_efficiency
- power_hr_efficiency_drift_pct

It also adds `effective_power_w` to the metrics migration/preservation list.

## Replace

Replace the files from this ZIP into `src/workoutbuddy/`.

The crash-specific file is:

- `database.py`

The ZIP also contains the other v7 changed files for consistency.
