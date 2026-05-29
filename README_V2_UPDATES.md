# WorkOutBuddy Web App v2 update

This update focuses on TCX-first data quality, duplicate handling, manual Apple Watch VO2max tracking, and a more explicit progression model.

## Key changes

- TCX import now de-duplicates by start time, duration, sport type and distance tolerance, not by activity name or file name.
- Existing Strava/API rows can be updated by matching TCX imports instead of creating duplicate rows.
- Activity table has an editable `Apple VO2` column. Edit a value directly in the table and it is saved to SQLite.
- New metrics include:
  - grade-adjusted HR efficiency drift
  - training-load score
  - easy-zone fraction
  - hard-zone fraction
  - Apple VO2max per workout
- Planner uses at least the latest 20 activities; in practice it loads up to 240 rows for planning, with the latest 30-60 weighted most heavily.
- Planner uses recent Apple VO2max values if entered; otherwise it falls back to the profile VO2max field.
- Planner has a five-level progression speed dropdown:
  - 1 Easy / conservative
  - 2 Moderately easy
  - 3 Balanced
  - 4 Ambitious
  - 5 Hard / fastest safe progress

## Replace files

Copy these files into your existing webapp project:

```text
main.py
src/workoutbuddy/database.py
src/workoutbuddy/metrics.py
src/workoutbuddy/tcx_importer.py
src/workoutbuddy/planner.py
src/workoutbuddy/web_app.py
```

Keep your existing:

```text
.env
.secrets/
data/
.venv/
```

Then run:

```powershell
cd C:\Users\Daniel\Documents\Coding\workoutbuddy_webapp
.\.venv\Scripts\python.exe main.py
```

## Strava vs TCX

The Strava stream API can provide streams such as time, distance, latlng, altitude, heartrate, watts and grade_smooth when available and authorized. The app requests those streams. However, TCX import remains the preferred high-quality source for historic full-detail analysis, because it avoids rate limits and gives you the raw exported device data.
