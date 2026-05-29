# Weather time handling fix v4

This patch fixes a subtle UTC/local-time ambiguity in the weather archive lookup.

## Problem

`start_date_local` can be misleading:

- Strava `start_date_local` is the local clock time of the activity. It may still look like an ISO timestamp with a trailing `Z`, but it should **not** be shifted as UTC for local weather matching.
- TCX/GPX timestamps with `Z` are usually true UTC/Zulu instants and **should** be converted to the local time at the GPS coordinate.
- Local TCX timestamps without timezone should be treated as local clock times.

The previous v3 code mostly worked for local/naive timestamps, but could shift a Strava `start_date_local` by the timezone offset if it ended with `Z`.

## New behavior

`weather.py` now builds an explicit `WeatherTimeRef`:

- `local_clock`: match directly to Open-Meteo's local hourly timestamp.
- `utc_instant`: convert the true UTC instant using Open-Meteo's `utc_offset_seconds`.

The Open-Meteo request uses `timezone=auto`, receives local hourly timestamps, and requests a 3-day window to avoid midnight/DST edge cases.

## Practical result

- Strava local times are not accidentally shifted.
- Zulu TCX times are converted to the coordinate-local hour.
- Naive local TCX times are used as local clock times.
- The stored `weather_source` includes the matched local hour and time-source interpretation for auditing.
