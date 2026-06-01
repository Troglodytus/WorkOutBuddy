# WorkOutBuddy Desktop

WorkOutBuddy can now run in two modes:

- `main.py` keeps the existing browser/Tailscale web app.
- `desktop.py` opens the same NiceGUI interface in its own native desktop window.

The desktop mode does not rebuild the UI with standard Python widgets. It embeds the current NiceGUI app, so the existing tables, maps, Plotly graphs, uploads, planner, statistics, and backend logic stay dynamic.

## Test The Desktop GUI

Install the native window dependency:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Run the desktop app:

```powershell
.\.venv\Scripts\python.exe desktop.py
```

If port `8080` is already in use, test with another local port:

```powershell
$env:WORKOUTBUDDY_DESKTOP_PORT="8090"
$env:WORKOUTBUDDY_DESKTOP_PUBLIC_BASE_URL="http://localhost:8090"
.\.venv\Scripts\python.exe desktop.py
```

The normal browser version still runs with:

```powershell
.\.venv\Scripts\python.exe main.py
```

## Desktop Settings

Desktop mode uses local-only defaults:

```env
WORKOUTBUDDY_DESKTOP_HOST=127.0.0.1
WORKOUTBUDDY_DESKTOP_PORT=8080
WORKOUTBUDDY_DESKTOP_PUBLIC_BASE_URL=http://localhost:8080
```

You usually do not need to add these to `.env`. If port `8080` is already busy, set `WORKOUTBUDDY_DESKTOP_PORT` and `WORKOUTBUDDY_DESKTOP_PUBLIC_BASE_URL` to a matching localhost URL.

For Strava OAuth in desktop mode, keep the Strava callback domain set to:

```text
localhost
```

## Database Safety

No database file is bundled or modified by the desktop launcher. When running from this project folder, it uses the same configured database path as before:

```env
WORKOUTBUDDY_DB=data/workoutbuddy.sqlite
```

When you later package an `.exe`, the app root becomes the folder that contains the executable. To keep using the existing database, set `WORKOUTBUDDY_DB` in the packaged app's `.env` to an absolute path, for example:

```env
WORKOUTBUDDY_DB=C:\Users\Daniel\Documents\Coding\workoutbuddy_webapp\data\workoutbuddy.sqlite
```

## Build An EXE

The reproducible desktop build uses:

- [requirements-desktop-build.lock.txt](requirements-desktop-build.lock.txt) for pinned runtime and build dependencies.
- [WorkOutBuddy.spec](WorkOutBuddy.spec) for PyInstaller bundling rules.
- [scripts/build_exe.ps1](scripts/build_exe.ps1) for the actual build command.

Build the local GUI as a single-file executable:

```powershell
.\scripts\build_exe.ps1
```

The output folder is:

```text
dist\
```

The build script creates:

- `dist\WorkOutBuddy.exe`
- `dist\.env`
- `dist\data\`
- `dist\.secrets\`
- `dist\README_DIST.txt`

This makes `dist` a standalone fresh desktop app home. It uses a fresh database at `dist\data\workoutbuddy.sqlite` and a separate Strava token at `dist\.secrets\token.json`.

This bundles the Python app, NiceGUI runtime/static assets, the Windows x64 pywebview/WebView2 bridge, Plotly, AG Grid, Folium, pandas, and the other required libraries. It does not copy the existing project database or existing Strava token into `dist`.
