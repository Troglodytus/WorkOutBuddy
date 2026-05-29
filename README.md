# WorkOutBuddy Web

A web version of the WorkOutBuddy Strava/TCX training analysis app.

This project keeps the same backend logic as the PySide desktop prototype:

- Strava OAuth + small incremental sync
- local TCX import
- SQLite database
- HR-zone inference from local thresholds
- activity metrics
- OpenStreetMap route display
- VO2max profile setting
- race-goal settings and projected times
- single-workout recommendation
- editable 14-day workout calendar planner

The UI is implemented with NiceGUI, so it can be opened from an iPhone browser.

## 1. Install

```powershell
cd C:\Users\Daniel\Documents\Coding\WorkOutBuddyWeb
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 2. Configure

```powershell
copy .env.example .env
notepad .env
```

Set:

```env
STRAVA_CLIENT_ID=your_client_id
STRAVA_CLIENT_SECRET=your_client_secret
WORKOUTBUDDY_DB=data/workoutbuddy.sqlite
WORKOUTBUDDY_WEB_HOST=0.0.0.0
WORKOUTBUDDY_WEB_PORT=8080
WORKOUTBUDDY_PUBLIC_BASE_URL=http://localhost:8080
```

For Tailscale/iPhone access, set `WORKOUTBUDDY_PUBLIC_BASE_URL` to the exact URL you use from the iPhone, for example:

```env
WORKOUTBUDDY_PUBLIC_BASE_URL=http://your-pc.your-tailnet.ts.net:8080
```

Then in the Strava API app, set the callback domain to the domain part only, for example:

```text
localhost
```

or:

```text
your-pc.your-tailnet.ts.net
```

The app callback URL is:

```text
<WORKOUTBUDDY_PUBLIC_BASE_URL>/strava/callback
```

## 3. Run

```powershell
.\.venv\Scripts\python.exe main.py
```

Open on the same PC:

```text
http://localhost:8080
```

Open from iPhone over Tailscale:

```text
http://your-pc.your-tailnet.ts.net:8080
```

## 4. Use

- Settings / Auth → Connect Strava
- Activities → Sync Strava
- Activities → Upload TCX file(s)
- Planner → enter Apple Watch VO2max and goals
- Planner → Plan Workout Calendar
- Click a calendar workout to edit it; saving a manual override recalculates the rest of the plan.

## Notes

- Strava access tokens expire after six hours. This is normal. The app stores a refresh token in `.secrets/token.json` and refreshes access automatically when needed.
- Keep `.env`, `.secrets/`, and `data/` private.
- For first bulk import, TCX export/import is still better than hammering the Strava API.
