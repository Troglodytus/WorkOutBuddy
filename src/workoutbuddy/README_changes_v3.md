# WorkOutBuddy changed files v3

## Added

### Weather archive temperature logging

New file: `weather.py`

- Uses the first GPS coordinate in the activity stream.
- Uses the activity start time.
- Queries Open-Meteo archive hourly `temperature_2m` and `apparent_temperature`.
- Stores values in the database:
  - `avg_temp_c`
  - `weather_temp_c`
  - `weather_apparent_temp_c`
  - `weather_temp_deviation_from_15_c`
  - `weather_lat`
  - `weather_lon`
  - `weather_source`
  - `weather_fetched_at`
- Adds a `weather_cache` table to avoid repeatedly querying the same coordinate/hour.

Weather lookup is triggered automatically after:

- Strava sync
- TCX import
- full metric recalculation

There is also a GUI button: **Fetch weather archive**.

### Weather-aware training estimation

Changed file: `recommendation.py`

The recommendation engine now uses the most recent activity temperature as a recovery/training-readiness factor. Deviations from 15 °C reduce the recovery score modestly:

- small adjustment above ~6 °C deviation
- moderate adjustment above ~10 °C deviation
- stronger adjustment above ~18 °C deviation

This reflects that heat/cold can raise HR and perceived effort, but does not treat temperature as a direct injury signal.

### Duplicate cleanup

New file: `duplicate_cleanup.py`
Changed file: `database.py`
Changed file: `web_app.py`

The duplicate checker now compares multiple overlap signals:

- sport type / sport family
- start time using timezone-aware variants for Zulu/UTC vs local-clock exports
- moving duration
- distance
- starting GPS coordinate
- sampled route similarity

If there is sufficient overlap and one row is Strava/API while the other is TCX/local, the Strava/API row is removed and the TCX/local row is kept.

There is now a GUI button: **Duplicate Cleanup**.

## Changed files to replace/add

Replace existing:

- `database.py`
- `sync.py`
- `tcx_importer.py`
- `web_app.py`
- `recommendation.py`

Add new:

- `weather.py`
- `duplicate_cleanup.py`

## Notes

The Open-Meteo archive API may not have same-day or very recent activity data immediately available. If no archive data are returned, the activity is skipped and the app continues normally.

The duplicate cleanup is intentionally conservative. If two activities are similar but do not have enough overlap, it leaves both unchanged.
