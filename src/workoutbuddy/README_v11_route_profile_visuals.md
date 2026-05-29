# Workout Buddy v11: route map + workout profile visuals

This patch adds a WorkoutOutDoors-style route/profile visualization on the Activities landing page.

## New behavior

- The route map is still shown at the top.
- Below the route map there is now a workout profile graph.
- The graph defaults to:
  - x-axis: distance
  - left y-axis: altitude
  - overlay metric: pace
- Dropdowns let you select:
  - pace
  - velocity/speed
  - heart rate
  - HR zone
  - power
  - altitude
  - grade
  - cadence
  - x-axis as distance or time
- The selected metric colors both:
  - the route line on the map
  - the overlay metric line/markers in the profile graph

For pace, the color scale is:

- fastest = green
- slowest = red

For speed/velocity, higher speed is green and lower speed is red. For effort-like metrics such as HR, zone, power, grade, higher values move towards red.

## Activities without GPS

If an activity has no GPS/GPX/latlng data, the map shows a diagnostic message, but the profile chart can still work if the TCX/Strava stream contains time, distance, altitude, HR, power, cadence, etc. This is especially useful for VirtualRide / ergometer workouts.

## Changed file

- `web_app.py`

The ZIP also contains the full v10 set of changed files so replacing all files is safe.
