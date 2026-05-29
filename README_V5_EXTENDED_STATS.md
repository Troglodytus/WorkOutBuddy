# WorkOutBuddy Web v5 extended statistics

This update adds:

- route map to the right of the workout table when GPS/latlng data exist
- TCX-first duplicate handling remains active; TCX/custom imports are preferred over Strava API duplicates
- expanded activity metrics:
  - raw and grade-adjusted HR efficiency drift
  - per-km HR efficiency and per-km grade-adjusted drift
  - HR zone durations, easy/hard fractions, TRIMP-like load
  - Apple Watch VO2max manual column, preserved during recalculation
  - estimated VO2 demand from speed + grade
  - run cadence normalized to step frequency, estimated total steps
  - heuristic average/max impact in bodyweight multiples and impact load index
  - elevation gain/loss, vertical speed, grade min/avg/max
  - normalized power/xPower-style estimate and variability index
  - best 1/5/10/20 minute pace and best 1/5/20 minute power
  - stream/data quality flags
- a button to recalculate metrics for all existing activities from their saved raw/stream JSON
- a new Statistics page with customizable Plotly graphs
- planner model v5 with additional factors: acute/chronic load ratio, per-km HR drift, Apple VO2max trend, easy/hard zone fractions, impact load, and manual progression speed

Important: impact values are heuristic estimates from speed/cadence/grade, not force-plate measurements.
