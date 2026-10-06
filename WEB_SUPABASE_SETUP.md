# WorkOutBuddy Web + Supabase setup

This branch is a browser-first replacement for the local NiceGUI application.

## Architecture

- GitHub Pages publishes only `docs/`.
- Supabase Auth protects the app data.
- Supabase Postgres stores workouts, profile data, plans and analysis snapshots.
- Supabase Storage keeps original workout files in a private `workout-files` bucket.
- The browser parses TCX uploads and stores normalized metrics + streams.
- The optional `coach` Edge Function calls OpenAI without exposing the OpenAI key to the browser.
- The old Python application remains on `main`.

## Important security rule

Publish **only `docs/`** with GitHub Pages. Do not publish the repository root.

The repository history contains legacy personal workout data and previously contained a Strava token. Do not make the existing repository public merely to get GitHub Pages. If your GitHub plan cannot serve Pages from a private repository, create a clean public Pages-only repository later containing only the files from `docs/`.

The legacy tracked Strava token has been removed from this web branch, but removing a file does not erase it from Git history. Rotate/revoke obsolete Strava credentials before ever making the repository public.

---

## 1. Create the Supabase project

1. Sign in to Supabase and create a new project.
2. Project name: `WorkOutBuddy`.
3. Choose a strong database password and store it in your password manager.
4. Pick a European region close to Austria.
5. Wait until the project is ready.

You do not need to create tables manually one by one.

## 2. Create the database schema and private file bucket

1. In the Supabase Dashboard open **SQL Editor**.
2. Open this repository file:
   `supabase/migrations/001_workoutbuddy_web.sql`
3. Copy the complete file into a new SQL query.
4. Click **Run**.
5. Verify that these tables now exist:
   - `profiles`
   - `activities`
   - `activity_streams`
   - `training_plans`
   - `analysis_snapshots`
6. Open **Storage** and verify that the private bucket `workout-files` exists.

The SQL also enables Row Level Security. Every row is restricted to the currently authenticated user's UUID. Storage policies likewise restrict files to the user's own top-level folder.

## 3. Create the one WorkOutBuddy login user

The website deliberately shows only a password field. The email is fixed in `docs/config.js`.

1. Open **Authentication -> Users**.
2. Add a permanent email/password user. If your Dashboard offers **Create user**, create it directly. If it offers only **Send invitation**, invite an email address you control and finish setting the password from the invitation.
3. Copy the user's UUID; it will be needed only for the one-time SQLite migration.
4. Open the Auth general configuration and turn **Allow new users to sign up** OFF after your user exists.
5. Keep Email/Password authentication enabled.

Use an email address you are comfortable storing in the public browser configuration. The email is only the fixed login identifier; the password remains secret.

## 4. Connect the website to Supabase

1. In Supabase open the project's **Connect** dialog or **Settings -> API Keys**.
2. Copy:
   - Project URL, e.g. `https://abcdef.supabase.co`
   - **Publishable** key beginning with `sb_publishable_`
3. In GitHub switch to branch `web-supabase-v2`.
4. Edit `docs/config.js`.
5. Replace:
   - `SUPABASE_URL`
   - `SUPABASE_PUBLISHABLE_KEY`
   - `LOGIN_EMAIL`
6. Commit the change to `web-supabase-v2`.

The publishable key is designed for browser code. Never put a Supabase **secret** key, database password or OpenAI API key in `docs/config.js`.

## 5. Optional: enable the AI Coach

The deterministic Analysis tab works without OpenAI. The AI button requires the Supabase Edge Function.

### 5A. Set the secret

In Supabase open **Edge Function Secrets** and add:

- `OPENAI_API_KEY` = your OpenAI API key
- optionally `OPENAI_MODEL` = `gpt-6-luna`

### 5B. Deploy the function from the Dashboard

1. Open **Edge Functions**.
2. Choose **Deploy a new function** / **Via Editor**.
3. Function name: `coach`.
4. Use the contents of:
   `supabase/functions/coach/index.ts`
5. Deploy it.
6. Keep JWT verification enabled; this function is intentionally user-authenticated.

### 5C. Or deploy with the Supabase CLI

From the repository root:

```bash
supabase login
supabase link --project-ref YOUR_PROJECT_REF
supabase functions deploy coach
```

The OpenAI key stays in Supabase Secrets and is never sent to GitHub Pages.

## 6. Migrate the existing SQLite workouts

Do this on the computer that has the existing repository/database.

Switch to the web branch:

```powershell
git fetch
git switch web-supabase-v2
```

Set temporary environment variables in PowerShell:

```powershell
$env:SUPABASE_URL="https://YOUR_PROJECT_REF.supabase.co"
$env:SUPABASE_SECRET_KEY="sb_secret_YOUR_SECRET_KEY"
$env:WORKOUTBUDDY_USER_ID="UUID_FROM_AUTH_USERS"
$env:WORKOUTBUDDY_DB="data/workoutbuddy.sqlite"
```

Run:

```powershell
python tools/migrate_sqlite_to_supabase.py
```

When it finishes, remove the secret from the shell:

```powershell
Remove-Item Env:SUPABASE_SECRET_KEY
```

The migration:

- copies every legacy activity;
- maps the most important metrics to canonical column names;
- preserves the complete legacy row in `metrics_json`;
- migrates saved stream JSON when the referenced file still exists;
- uses a deterministic duplicate key so rerunning the migration skips existing workouts.

The `sb_secret_` key bypasses RLS. It is appropriate for this local one-time migration but must never be committed or pasted into browser code.

## 7. Enable GitHub Pages

1. Open the WorkOutBuddy repository on GitHub.
2. Open **Settings -> Pages**.
3. Under **Build and deployment**, choose **Deploy from a branch**.
4. Branch: `web-supabase-v2`.
5. Folder: **/docs**.
6. Click **Save**.

Do **not** select repository root.

If GitHub does not offer Pages for this private repository on your current plan, stop here. Do not make this repository public. A clean Pages-only repository containing only `docs/` is the safe fallback.

## 8. First browser test

Open the GitHub Pages URL.

Expected sequence:

1. WorkOutBuddy password screen appears.
2. Enter the password of the Supabase Auth user.
3. Home loads the most recent workouts.
4. Open Settings (gear) and set:
   - profile VO2max
   - height / weight
   - primary goal
   - desired running days/week
   - strength sessions/week
   - HR zone limits
5. Verify the Home page shows:
   - today's recommended training
   - readiness score
   - quick load / distance / elevation summary
   - next 7-day plan
   - recent workouts
6. Open History and verify plotting.
7. The text above the plot explicitly reports how many selected workouts actually contain the chosen metric, preventing missing values from looking like lost workouts.
8. Open Analysis and verify:
   - time-based 7/28/90-day calculations
   - comparable easy-run trend
   - intensity distribution
   - durability / HR-drift assessment
   - weekly running volume
9. If the Edge Function is deployed, click **Ask AI coach**.

## 9. Upload directly from iPhone / WorkOutDoors

Current reliable workflow:

1. In WorkOutDoors export the workout as **TCX**.
2. Save/share the file to Files on the iPhone.
3. Open the WorkOutBuddy website in Safari.
4. Tap **Upload TCX**.
5. Select the exported file.
6. WorkOutBuddy parses the file in the browser, stores the original privately in Supabase Storage, inserts the metrics/stream, checks duplicates, and immediately refreshes Home/History/Analysis.

Multiple TCX files can be selected at once.

A dedicated iOS Shortcut / share-sheet workflow can be added after the core hosted version is verified.

## 10. What is intentionally different from the old app

The new analysis is based on real time windows rather than "last N workouts":

- acute load: 7 days
- baseline load: 28 days
- analysis window: 90 days
- run trend: comparable easy runs with a trend expressed per week

History plotting also reports metric coverage instead of silently making workouts disappear when a selected metric is null.

Canonical names are now used consistently, e.g.:

- `avg_pace_min_km`
- `gap_hr_efficiency_drift_pct`
- `hr_efficiency_drift_pct`

The complete legacy metrics remain available in `metrics_json` during migration so advanced calculations can be ported incrementally without losing old information.

## Current v0.1 scope

Implemented:

- password login through Supabase Auth
- private per-user RLS
- private workout-file storage
- multi-file TCX upload
- TCX route/HR stream storage
- duplicate detection
- recent workout landing page
- dynamic readiness / daily recommendation
- 7-day training plan
- profile, goals and HR zones
- workout history and filtering
- explicit plot data-coverage reporting
- route map for GPS workouts
- HR stream chart
- time-based deterministic analysis
- weekly run-volume and HR-zone charts
- authenticated optional OpenAI coach
- legacy SQLite migration utility

Next parity items after the hosted version is verified:

- direct FIT upload/parser
- richer legacy metric calculations (GAP, best efforts, normalized power, impact heuristics, weather)
- manual per-workout Apple VO2max editing
- persisted/editable 14-day plan
- more advanced constrained planner
- iOS Shortcut for one-tap WorkOutDoors sharing
