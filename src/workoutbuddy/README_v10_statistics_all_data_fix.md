# Workout Buddy v10: Statistics all-data / filter visibility fix

The statistics tab previously defaulted to only the last 180 days. That made it look like it was not reading the full database, especially for VirtualRide/ergometer workouts outside that window.

## Changes

- Statistics now defaults to **all dates**.
- Added buttons: **All dates** and **Last 180 d**.
- Date fields can be left empty to mean no date filter.
- Added a visible row-count/status label showing:
  - total DB rows
  - rows with parseable dates
  - rows after sport-type filter
  - rows after date filter
  - plotted/table rows
  - sport-type counts
- More robust mixed timestamp parsing for statistics. Z/offset timestamps and local timestamps no longer cause rows to disappear from statistics because of pandas mixed-timezone parsing.
- Statistics data table now shows up to 1000 filtered rows.

## Most important file

- `web_app.py`

Replace the files in `src/workoutbuddy/`.
