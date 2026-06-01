from __future__ import annotations

import asyncio
import json
import math
import html as html_lib
import re
import time
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import folium
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import polyline
from fastapi import File, UploadFile
from nicegui import app, ui
from starlette.responses import HTMLResponse, RedirectResponse

from .config import AppConfig
from .database import WorkoutDatabase
from .metrics import MetricCalculator, StreamUtils
from .planner import AGGRESSION_LABELS, GOAL_LABELS, RacePredictor, TrainingGoals, WorkoutPlanner
from .recommendation import RecommendationEngine
from .strava_oauth import StravaAuthenticator
from .sync import StravaSyncService
from .tcx_importer import TCXImportService
from .weather import update_activity_weather_from_archive, update_missing_weather_for_recent_activities
from .analysis_engine import analyze_recent_trainings


# ----------------------------- formatting helpers -----------------------------


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        x = float(value)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def _fmt(value: Any, decimals: int = 1, suffix: str = "") -> str:
    x = _safe_float(value, None)
    if x is None:
        return "—"
    return f"{x:.{decimals}f}{suffix}"


def _fmt_pace(value: Any) -> str:
    x = _safe_float(value, None)
    if x is None or x <= 0:
        return "—"
    minutes = int(x)
    seconds = int(round((x - minutes) * 60))
    if seconds >= 60:
        minutes += 1
        seconds -= 60
    return f"{minutes}:{seconds:02d}/km"


def _parse_datetime(date_value: str, time_value: str) -> datetime:
    d = date.fromisoformat(str(date_value).strip())
    t = str(time_value or "18:00").strip()
    parts = t.split(":")
    hour = int(parts[0]) if parts else 18
    minute = int(parts[1]) if len(parts) > 1 else 0
    return datetime(d.year, d.month, d.day, hour, minute)


def _time_minutes(text: str) -> Optional[float]:
    """Accept HH:MM:SS, HH:MM, MM:SS, or minutes."""
    s = str(text or "").strip()
    if not s:
        return None
    if ":" not in s:
        return _safe_float(s, None)
    parts = [p.strip() for p in s.split(":")]
    try:
        nums = [float(p) for p in parts]
    except Exception:
        return None
    if len(nums) == 3:
        return nums[0] * 60 + nums[1] + nums[2] / 60.0
    if len(nums) == 2:
        # Interpret 0:55 as 55 minutes, 1:55 as 1h55.
        if nums[0] <= 4:
            return nums[0] * 60 + nums[1]
        return nums[0] + nums[1] / 60.0
    return None


def _race_time_text(minutes: Any) -> str:
    x = _safe_float(minutes, None)
    if x is None:
        return "—"
    total_seconds = int(round(x * 60))
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _norm_sport_type(sport_type: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(sport_type or "").lower())


def _norm_sport(sport_type: Any) -> str:
    """Backward-compatible alias used by hike/strength helpers."""
    return _norm_sport_type(sport_type)


def _is_run(sport_type: Any) -> bool:
    return _norm_sport_type(sport_type) in {"run", "running", "trailrun", "virtualrun"}


def _is_ride(sport_type: Any) -> bool:
    return _norm_sport_type(sport_type) in {"ride", "cycling", "bike", "virtualride", "gravelride", "mountainbikeride", "ebikeride"}


def _clean_rows(df: pd.DataFrame) -> List[Dict[str, Any]]:
    if df.empty:
        return []
    out: List[Dict[str, Any]] = []
    for row in df.to_dict("records"):
        clean = {}
        for k, v in row.items():
            if pd.isna(v):
                clean[k] = None
            elif isinstance(v, float):
                clean[k] = round(v, 3)
            elif hasattr(v, "isoformat"):
                try:
                    clean[k] = v.isoformat()
                except Exception:
                    clean[k] = str(v)
            else:
                clean[k] = v
        out.append(clean)
    return out



def _parse_stats_datetime_series(series: pd.Series) -> pd.Series:
    """Parse activity times for statistics without silently losing rows.

    Mixed Strava/TCX data can contain naive local timestamps, explicit offsets,
    and true Zulu/UTC timestamps. For plotting/filtering we only need a stable
    naive timestamp. Explicit offsets/Z are converted to naive UTC-equivalent
    timestamps; naive values remain as written. This avoids pandas failing on
    mixed timezone strings and prevents the statistics table from dropping rows
    just because one source stores times differently.
    """
    def parse_one(value: Any):
        if value is None or str(value).strip() == "":
            return pd.NaT
        text = str(value).strip()
        try:
            if text.endswith("Z") or re.search(r"[+-]\d\d:?\d\d$", text):
                dt = pd.to_datetime(text.replace("Z", "+00:00"), errors="coerce", utc=True)
                if pd.isna(dt):
                    return pd.NaT
                return pd.Timestamp(dt).tz_convert(None)
            dt = pd.to_datetime(text, errors="coerce")
            if pd.isna(dt):
                return pd.NaT
            return pd.Timestamp(dt).tz_localize(None) if getattr(dt, "tzinfo", None) is not None else pd.Timestamp(dt)
        except Exception:
            return pd.NaT

    return pd.Series([parse_one(v) for v in series], index=series.index)


def _safe_filename(name: str) -> str:
    name = Path(name).name
    return re.sub(r"[^A-Za-z0-9_. -]", "_", name)


# ----------------------------- web application -----------------------------


def _fit_trend_for_plot(x_values: pd.Series, y_values: pd.Series, mode: str):
    """Return x, yfit, label, best_r2, linearity for trend overlays."""
    try:
        x = pd.Series(x_values).reset_index(drop=True)
        y = pd.to_numeric(pd.Series(y_values).reset_index(drop=True), errors="coerce")
        mask = y.notna()
        x = x[mask]
        y = y[mask].astype(float)
        if len(y) < 3 or y.nunique() <= 1:
            return None
        if pd.api.types.is_datetime64_any_dtype(x):
            x_num = x.astype("int64") / 1e9 / 86400.0
        else:
            x_num = pd.to_numeric(x, errors="coerce")
        x_num = pd.Series(x_num).astype(float)
        mask = x_num.notna() & np.isfinite(x_num) & np.isfinite(y)
        x = x[mask]
        y = y[mask]
        x_num = x_num[mask]
        if len(y) < 3 or x_num.nunique() <= 1:
            return None
        x0 = x_num - x_num.min()
        if x0.max() > 0:
            xs = x0 / x0.max()
        else:
            xs = x0

        def r2_score(obs, pred):
            obs = np.asarray(obs, dtype=float)
            pred = np.asarray(pred, dtype=float)
            ss_res = float(np.sum((obs - pred) ** 2))
            ss_tot = float(np.sum((obs - np.mean(obs)) ** 2))
            return 0.0 if ss_tot <= 0 else max(0.0, min(1.0, 1.0 - ss_res / ss_tot))

        fits = []
        # linear
        coef = np.polyfit(xs, y, 1)
        y_lin = np.polyval(coef, xs)
        r2_lin = r2_score(y, y_lin)
        fits.append(("linear", y_lin, r2_lin))
        # exponential y=a*exp(b*x), only if positive y
        if (y > 0).all():
            coef_exp = np.polyfit(xs, np.log(y), 1)
            y_exp = np.exp(np.polyval(coef_exp, xs))
            fits.append(("exponential", y_exp, r2_score(y, y_exp)))
        # auto-parametric: quadratic polynomial as simple parametric non-linear model
        if len(y) >= 4:
            coef_quad = np.polyfit(xs, y, 2)
            y_quad = np.polyval(coef_quad, xs)
            fits.append(("quadratic", y_quad, r2_score(y, y_quad)))
        if mode == "linear":
            label, yfit, r2 = fits[0]
        elif mode == "exponential":
            exp = [f for f in fits if f[0] == "exponential"]
            if not exp:
                return None
            label, yfit, r2 = exp[0]
        else:
            label, yfit, r2 = sorted(fits, key=lambda f: f[2], reverse=True)[0]
        linearity = r2_lin / r2 if r2 > 1e-9 else 0.0
        order = np.argsort(x_num.to_numpy())
        return x.iloc[order], pd.Series(yfit).iloc[order], label, float(r2), float(max(0.0, min(1.0, linearity)))
    except Exception:
        return None


class WorkOutBuddyWeb:
    def __init__(self, config: AppConfig):
        self.config = config
        self.db = WorkoutDatabase(config.db_path)
        self.sync_service = StravaSyncService(config, self.db)
        self.tcx_service = TCXImportService(config, self.db)
        self.auth = StravaAuthenticator(config)

        self.current_plan: Optional[Dict[str, Any]] = None
        self.current_plan_start_dt: Optional[datetime] = None
        self.selected_plan_date: Optional[str] = None
        self.planned_by_date: Dict[str, Dict[str, Any]] = {}

        self.status_label = None
        self.token_label = None
        self.activity_grid = None
        self.landing_summary_markdown = None
        self.detail_markdown = None
        self.map_frame = None
        self.map_link = None
        self.map_status = None
        self.route_metric_select = None
        self.route_x_select = None
        self.route_profile_frame = None
        self.route_profile_status = None
        self.selected_activity_id: Optional[str] = None
        self.selected_apple_vo2_input = None
        self.selected_body_weight_input = None
        self.sport_select = None
        self.recommendation_markdown = None
        self.projected_labels: Dict[str, Any] = {}
        self.calendar_container = None
        self.plan_summary_label = None
        self.goal_widgets: Dict[str, Dict[str, Any]] = {}

        # Editor widgets
        self.edit_dialog = None
        self.edit_title = None
        self.edit_no_workout = None
        self.edit_sport_type = None
        self.edit_family = None
        self.edit_date = None
        self.edit_time = None
        self.edit_duration = None
        self.edit_distance = None
        self.edit_zone = None
        self.edit_pace = None
        self.edit_wattage = None
        self.edit_notes = None

        # Compact recommendation controls
        self.target_date = None
        self.target_time = None
        self.desired_type = None
        self.vo2_input = None
        self.profile_birthdate_input = None
        self.profile_gender_select = None
        self.profile_height_input = None
        self.profile_weight_input = None
        self.aggression_select = None
        self.server_folder_input = None

        # Statistics widgets
        self.stats_type_select = None
        self.stats_start_date = None
        self.stats_end_date = None
        self.stats_x_axis = None
        self.stats_y_axes = None
        self.stats_plot_kind = None
        self.stats_color_by = None
        self.stats_aggregate = None
        self.stats_trend = None
        self.stats_plot = None
        self.stats_table = None
        self.stats_count_label = None
        self.analysis_dialog = None
        self.analysis_markdown = None
        self.analysis_use_ollama = None
        self.analysis_backend_select = None
        self.analysis_model_input = None

    # ----------------------------- UI construction -----------------------------

    def build(self) -> None:
        ui.add_head_html(
            """
            <style>
              .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
              .calendar-card { min-height: 170px; cursor: pointer; white-space: pre-wrap; }
              .calendar-card:hover { outline: 2px solid #777; }
            </style>
            """
        )

        with ui.header().classes("items-center justify-between"):
            title = "WorkOutBuddy" if self.config.app_mode == "desktop" else "WorkOutBuddy Web"
            ui.label(title).classes("text-xl font-semibold")
            with ui.row().classes("items-center gap-3"):
                self.status_label = ui.label("Ready").classes("text-sm")
                ui.button("Exit", on_click=self.exit_app).props("color=negative outline dense").classes("px-3")

        with ui.tabs().classes("w-full") as tabs:
            activity_tab = ui.tab("Activities")
            planner_tab = ui.tab("Planner")
            stats_tab = ui.tab("Statistics")
            auth_tab = ui.tab("Settings / Auth")

        with ui.tab_panels(tabs, value=activity_tab).classes("w-full"):
            with ui.tab_panel(activity_tab):
                self._build_activities_panel()
            with ui.tab_panel(planner_tab):
                self._build_planner_panel()
            with ui.tab_panel(stats_tab):
                self._build_statistics_panel()
            with ui.tab_panel(auth_tab):
                self._build_settings_panel()

        self._build_edit_dialog()
        self._build_analysis_dialog()
        self.refresh_all()

    def _build_activities_panel(self) -> None:
        with ui.row().classes("w-full items-center gap-2"):
            ui.button("Sync Strava", on_click=self.sync_strava).props("color=primary")
            ui.button("Reload", on_click=self.reload_activity_table)
            ui.button("Export XLSX", on_click=self.export_xlsx)
            ui.button("Recalculate metrics", on_click=self.recalculate_all_metrics).props("color=secondary")
            ui.button("Fetch weather archive", on_click=self.fetch_weather_archive).props("color=secondary")
            ui.button("Duplicate Cleanup", on_click=self.cleanup_duplicates).props("color=warning")
            ui.button("Analyze", on_click=self.analyze_recent_trainings).props("color=accent")

        # Plain FastAPI multipart upload form. This is intentionally not NiceGUI's
        # ui.upload event handler because UploadEventArguments changed across
        # NiceGUI versions and was unreliable on some installations. The endpoint
        # /api/tcx_upload below receives the real uploaded file bytes directly.
        ui.html(
            """
            <form action="/api/tcx_upload" method="post" enctype="multipart/form-data"
                  style="display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap; margin:0.5rem 0;">
              <label style="font-weight:600;">Upload TCX file(s):</label>
              <input type="file" name="files" accept=".tcx,application/vnd.garmin.tcx+xml,application/xml,text/xml" multiple required>
              <button type="submit"
                      style="padding:0.45rem 0.8rem; border:1px solid #888; border-radius:6px; cursor:pointer;">
                Import TCX
              </button>
            </form>
            """
        ).classes("w-full")

        with ui.expansion("Optional: import a TCX folder on the server/PC", icon="folder").classes("w-full"):
            with ui.row().classes("w-full items-center"):
                self.server_folder_input = ui.input("Server-side folder path", placeholder=r"C:\Users\Daniel\Downloads\StravaExport").classes("w-2/3")
                ui.button("Import folder", on_click=self.import_server_folder)

        ui.separator()
        with ui.row().classes("w-full gap-4 items-start").style("display:flex; flex-wrap:wrap;"):
            with ui.column().classes("min-w-[760px]").style("flex: 1 1 58%;"):
                ui.label("Activities").classes("text-lg font-semibold")
                self.activity_grid = ui.aggrid(
                    {
                        "columnDefs": self._activity_column_defs(),
                        "rowData": [],
                        "pagination": True,
                        "paginationPageSize": 25,
                        "rowSelection": "single",
                        "defaultColDef": {"resizable": True, "sortable": True, "filter": True, "minWidth": 95},
                        "singleClickEdit": True,
                        "stopEditingWhenCellsLoseFocus": True,
                        "suppressColumnVirtualisation": True,
                        "domLayout": "normal",
                    }
                ).classes("w-full h-[560px]")
                self.activity_grid.on("cellClicked", self.on_activity_grid_click)
                self.activity_grid.on("cellValueChanged", self.on_activity_grid_cell_changed)

                with ui.card().classes("w-full mt-2"):
                    ui.label("Quick training summary").classes("font-semibold")
                    self.landing_summary_markdown = ui.markdown("Loading summary…").classes("w-full mono text-sm")

            with ui.column().classes("min-w-[420px]").style("flex: 1 1 38%;"):
                ui.label("Route map").classes("text-lg font-semibold")
                # Use a real iframe element, not ui.html(innerHTML). Some NiceGUI/Quasar
                # versions do not reliably render or update iframes injected through
                # innerHTML. A real element also makes route-map failures visible.
                self.map_frame = ui.element("iframe").classes("w-full").style(
                    "height:520px; border:1px solid #ddd; border-radius:6px; background:#fafafa;"
                )
                self._set_map_frame_none()
                with ui.row().classes("w-full items-center gap-2"):
                    self.map_link = ui.link("Open route map in new tab", "#", new_tab=True).classes("text-sm")
                    self.map_status = ui.label("No route selected").classes("text-xs text-gray-500")

                with ui.card().classes("w-full"):
                    ui.label("Workout route/profile coloring").classes("font-semibold")
                    with ui.row().classes("w-full items-end gap-2"):
                        self.route_metric_select = ui.select(
                            options=self._route_metric_options(),
                            value="pace_min_km",
                            label="Metric overlay / route color",
                            on_change=self.refresh_route_visuals,
                        ).classes("w-64")
                        self.route_x_select = ui.select(
                            options={"distance_km": "Distance", "time_min": "Time"},
                            value="distance_km",
                            label="Profile x-axis",
                            on_change=self.refresh_route_visuals,
                        ).classes("w-36")
                    self.route_profile_frame = ui.element("iframe").classes("w-full").style(
                        "height:300px; border:1px solid #ddd; border-radius:6px; background:#fafafa;"
                    )
                    self.route_profile_status = ui.label("No route/profile selected").classes("text-xs text-gray-500")
                    self._set_profile_frame_none()

                with ui.card().classes("w-full"):
                    ui.label("Selected activity manual values").classes("font-semibold")
                    with ui.row().classes("w-full items-end"):
                        self.selected_apple_vo2_input = ui.number("Apple VO₂max", min=20, max=80, step=0.1).classes("w-40")
                        self.selected_body_weight_input = ui.number("Weight kg", min=35, max=160, step=0.1).classes("w-32")
                        ui.button("Save VO₂/weight", on_click=self.save_selected_activity_manual_values).props("color=secondary")
                        ui.button("Clear VO₂", on_click=self.clear_selected_activity_apple_vo2)
                        ui.button("Clear weight", on_click=self.clear_selected_activity_body_weight)

                ui.label("Selected activity").classes("text-lg font-semibold")
                self.detail_markdown = ui.markdown("Select an activity row.").classes("w-full mono text-sm")

    def _activity_column_defs(self) -> List[Dict[str, Any]]:
        """Wide table: raw data, physiology, run mechanics, best efforts, and data quality."""
        num = "agNumberColumnFilter"
        return [
            {"headerName": "Date", "field": "start_date_local", "width": 180},
            {"headerName": "Type", "field": "sport_type", "width": 100},
            {"headerName": "Source", "field": "source", "width": 105},
            {"headerName": "Name", "field": "name", "width": 220},
            {"headerName": "km", "field": "distance_km", "filter": num, "width": 80},
            {"headerName": "min", "field": "moving_time_min", "filter": num, "width": 80},
            {"headerName": "pace", "field": "avg_pace_min_km", "filter": num, "width": 85},
            {"headerName": "GAP", "field": "avg_gap_pace_min_km", "filter": num, "width": 85},
            {"headerName": "avg HR", "field": "avg_hr", "filter": num, "width": 90},
            {"headerName": "max HR", "field": "max_hr", "filter": num, "width": 90},
            {"headerName": "HR drift %", "field": "hr_efficiency_drift_pct", "filter": num, "width": 115},
            {"headerName": "GAP drift %", "field": "gap_hr_efficiency_drift_pct", "filter": num, "width": 115},
            {"headerName": "km GAP drift %", "field": "km_gap_hr_efficiency_drift_pct", "filter": num, "width": 130},
            {"headerName": "Apple VO2", "field": "apple_vo2max", "editable": True, "filter": num, "width": 110},
            {"headerName": "Weight kg", "field": "body_weight_kg", "editable": True, "filter": num, "width": 110},
            {"headerName": "BMI", "field": "bmi", "filter": num, "width": 80},
            {"headerName": "Age", "field": "age_years_at_activity", "filter": num, "width": 80},
            {"headerName": "Weather °C", "field": "weather_temp_c", "filter": num, "width": 115},
            {"headerName": "Weather |°C-15|", "field": "weather_temp_deviation_from_15_c", "filter": num, "width": 135},
            {"headerName": "VO2 demand", "field": "vo2_demand_est_ml_kg_min", "filter": num, "width": 120},
            {"headerName": "Load", "field": "training_load_score", "filter": num, "width": 85},
            {"headerName": "TRIMP", "field": "trimp_score", "filter": num, "width": 85},
            {"headerName": "easy frac", "field": "easy_zone_fraction", "filter": num, "width": 95},
            {"headerName": "hard frac", "field": "hard_zone_fraction", "filter": num, "width": 95},
            {"headerName": "elev +m", "field": "elevation_gain_m", "filter": num, "width": 95},
            {"headerName": "elev -m", "field": "elevation_loss_m", "filter": num, "width": 95},
            {"headerName": "avg grade", "field": "avg_grade_pct", "filter": num, "width": 95},
            {"headerName": "step spm", "field": "run_step_frequency_spm", "filter": num, "width": 105},
            {"headerName": "steps", "field": "estimated_total_steps", "filter": num, "width": 100},
            {"headerName": "impact avg", "field": "avg_impact_bw", "filter": num, "width": 105},
            {"headerName": "impact max", "field": "max_impact_bw", "filter": num, "width": 105},
            {"headerName": "impact load", "field": "impact_load_index", "filter": num, "width": 110},
            {"headerName": "NP/xPower", "field": "normalized_power", "filter": num, "width": 105},
            {"headerName": "Bike W est", "field": "bike_inferred_power_w", "filter": num, "width": 110},
            {"headerName": "Cycling W", "field": "cycling_power_w", "filter": num, "width": 105},
            {"headerName": "Run-eq W", "field": "run_equivalent_power_w", "filter": num, "width": 105},
            {"headerName": "W/HR", "field": "power_hr_efficiency", "filter": num, "width": 90},
            {"headerName": "VI", "field": "variability_index", "filter": num, "width": 80},
            {"headerName": "best 5m pace", "field": "best_5min_pace_min_km", "filter": num, "width": 120},
            {"headerName": "best 20m pace", "field": "best_20min_pace_min_km", "filter": num, "width": 125},
            {"headerName": "best 5m W", "field": "best_5min_power", "filter": num, "width": 105},
            {"headerName": "GPS", "field": "has_gps", "filter": num, "width": 75},
            {"headerName": "points", "field": "data_point_count", "filter": num, "width": 90},
        ]

    def _activity_column_defs_for_rows(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Return AG Grid column defs with widths estimated from visible content.

        AG Grid/NiceGUI auto-size is unreliable across browsers and versions.
        This deterministic approximation makes date/name/source and metric columns
        wide enough based on the longest displayed entry. Horizontal scrolling is
        intentional; cramped columns are worse for this analysis app.
        """
        defs = self._activity_column_defs()
        sample_rows = rows[:500]
        for col in defs:
            field = col.get("field")
            header = str(col.get("headerName") or field or "")
            max_len = len(header)
            if field:
                for row in sample_rows:
                    val = row.get(field, "")
                    if val is None:
                        continue
                    txt = str(val)
                    if len(txt) > max_len:
                        max_len = min(len(txt), 42)
            estimated = int(max(85, min(380, max_len * 8 + 34)))
            if field == "start_date_local":
                estimated = max(estimated, 220)
            elif field == "name":
                estimated = max(estimated, 260)
            elif field == "activity_id":
                estimated = max(estimated, 180)
            col["width"] = max(int(col.get("width", 0) or 0), estimated)
            col["minWidth"] = min(col["width"], 140)
        return defs

    def _build_planner_panel(self) -> None:
        with ui.row().classes("w-full gap-4"):
            with ui.card().classes("w-full lg:w-1/3"):
                ui.label("Profile and goals").classes("text-lg font-semibold")
                profile = self.db.get_profile()
                self.vo2_input = ui.number("Current Apple Watch VO₂max", value=self.db.get_float_setting("profile_vo2max", 41.0), min=20, max=80, step=0.1).classes("w-full")
                with ui.row().classes("w-full items-end"):
                    self.profile_birthdate_input = ui.input("Birthday", value=str(profile.get("birthdate") or "1993-06-16")).classes("w-36")
                    self.profile_gender_select = ui.select(["Male", "Female", "Other"], value=str(profile.get("gender") or "Male"), label="Gender").classes("w-32")
                with ui.row().classes("w-full items-end"):
                    self.profile_height_input = ui.number("Height cm", value=_safe_float(profile.get("height_cm"), 178.0), min=120, max=230, step=0.5).classes("w-32")
                    self.profile_weight_input = ui.number("Current weight kg", value=_safe_float(profile.get("current_weight_kg"), 75.0), min=35, max=160, step=0.1).classes("w-40")
                current_agg = self.db.get_int_setting("training_aggressiveness", 3)
                self.aggression_select = ui.select(
                    options={str(k): v for k, v in AGGRESSION_LABELS.items()},
                    value=str(current_agg if current_agg in AGGRESSION_LABELS else 3),
                    label="Progression speed",
                ).classes("w-full")
                ui.button("Save profile / goals / progression", on_click=self.save_profile_and_goals)

                ui.separator()
                ui.label("Goals").classes("font-semibold")
                goals = TrainingGoals.from_dict(self.db.get_json_setting("training_goals", TrainingGoals.default().to_dict()))
                for key in ["10k", "hm", "m"]:
                    g = goals.goals[key]
                    with ui.row().classes("w-full items-center"):
                        cb = ui.checkbox(GOAL_LABELS[key], value=bool(g.active)).classes("w-1/3")
                        time_input = ui.input("target", value=_race_time_text(g.target_minutes) if g.target_minutes else "").classes("w-1/3")
                        proj = ui.label("projected: —").classes("text-sm w-1/3")
                        self.goal_widgets[key] = {"active": cb, "target": time_input}
                        self.projected_labels[key] = proj

            with ui.card().classes("w-full lg:w-2/3"):
                ui.label("Compact single-workout recommendation").classes("text-lg font-semibold")
                now = datetime.now().replace(second=0, microsecond=0)
                with ui.row().classes("w-full items-end"):
                    self.target_date = ui.input("date", value=now.date().isoformat()).classes("w-36")
                    self.target_time = ui.input("time", value=f"{now.hour:02d}:{now.minute:02d}").classes("w-28")
                    ui.button("Now", on_click=self.set_recommendation_now)
                    self.desired_type = ui.select(["Auto / none"], value="Auto / none", label="wanted type").classes("w-48")
                    ui.button("Give recommendation", on_click=self.give_recommendation).props("color=primary")
                self.recommendation_markdown = ui.markdown("No recommendation generated yet.").classes("w-full mono text-sm")

        ui.separator()
        with ui.row().classes("w-full items-center"):
            ui.button("Plan Workout Calendar", on_click=self.plan_workout_calendar).props("color=primary")
            self.plan_summary_label = ui.label("No 14-day plan generated yet.")
        self.calendar_container = ui.grid(columns=7).classes("w-full gap-2")

    def _build_statistics_panel(self) -> None:
        ui.label("Statistics").classes("text-xl font-semibold")
        ui.label("Custom plots over all calculated metrics. Standard mode: selected metrics over datetime; switch X/Y axes for scatter plots.").classes("text-sm")
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-end gap-3"):
                types = self.db.read_sport_types()
                self.stats_type_select = ui.select(options=types, value=[], multiple=True, label="Filter sport types (empty = all)").classes("w-64")
                now = date.today()
                self.stats_start_date = ui.input("start date (empty = all)", value="").classes("w-44")
                self.stats_end_date = ui.input("end date (empty = all)", value="").classes("w-44")
                metric_options = self._stats_metric_options()
                self.stats_x_axis = ui.select(options=metric_options, value="start_date_local", label="X axis").classes("w-56")
                self.stats_y_axes = ui.select(options=metric_options, value=["distance_km"], multiple=True, label="Y metric(s)").classes("w-96")
                self.stats_plot_kind = ui.select(options=["line", "scatter", "bar"], value="line", label="plot").classes("w-32")
                color_options = ["None"] + metric_options
                self.stats_color_by = ui.select(options=color_options, value="sport_type" if "sport_type" in metric_options else "None", label="color/group").classes("w-44")
                self.stats_aggregate = ui.select(options=["none", "mean", "median", "sum", "min", "max", "count"], value="none", label="aggregate").classes("w-32")
                self.stats_trend = ui.select(options=["none", "linear", "exponential", "auto-parametric"], value="none", label="trend").classes("w-44")
                ui.button("Update graph", on_click=self.refresh_statistics_plot).props("color=primary")
                ui.button("All dates", on_click=self._stats_all_dates)
                ui.button("Last 180 d", on_click=self._stats_last_180_days)
            self.stats_count_label = ui.label("Statistics use all dates by default. Set date filters only when needed.").classes("text-sm text-gray-600")
            self.stats_plot = ui.plotly(go.Figure()).classes("w-full h-[560px]")
        with ui.expansion("Statistics data table", icon="table_chart").classes("w-full"):
            self.stats_table = ui.aggrid({"columnDefs": [], "rowData": [], "pagination": True, "paginationPageSize": 25}).classes("w-full h-[420px]")


    def _stats_all_dates(self) -> None:
        if self.stats_start_date is not None:
            self.stats_start_date.value = ""
            self.stats_start_date.update()
        if self.stats_end_date is not None:
            self.stats_end_date.value = ""
            self.stats_end_date.update()
        self.refresh_statistics_plot()

    def _stats_last_180_days(self) -> None:
        today = date.today()
        if self.stats_start_date is not None:
            self.stats_start_date.value = (today - timedelta(days=180)).isoformat()
            self.stats_start_date.update()
        if self.stats_end_date is not None:
            self.stats_end_date.value = today.isoformat()
            self.stats_end_date.update()
        self.refresh_statistics_plot()

    def _stats_metric_options(self) -> List[str]:
        df = self.db.read_activities_dataframe()
        if df.empty:
            return ["start_date_local"]
        skip = {"activity_id", "raw_json_path", "streams_json_path", "zones_json", "km_splits_json", "best_efforts_json", "flags_json"}
        cols = [c for c in df.columns if c not in skip]
        preferred = [
            "start_date_local", "distance_km", "moving_time_min", "avg_pace_min_km", "avg_gap_pace_min_km",
            "avg_hr", "max_hr", "hr_efficiency_drift_pct", "gap_hr_efficiency_drift_pct", "km_gap_hr_efficiency_drift_pct",
            "apple_vo2max", "body_weight_kg", "bmi", "age_years_at_activity", "vo2_demand_est_ml_kg_min", "training_load_score", "trimp_score",
            "weather_temp_c", "weather_temp_deviation_from_15_c", "cycling_power_w", "run_equivalent_power_w", "bike_inferred_power_w", "power_hr_efficiency",
            "easy_zone_fraction", "hard_zone_fraction", "run_step_frequency_spm", "estimated_total_steps",
            "avg_impact_bw", "max_impact_bw", "impact_load_index", "normalized_power", "variability_index",
            "best_5min_pace_min_km", "best_20min_pace_min_km", "best_5min_power", "elevation_gain_m",
        ]
        ordered = [c for c in preferred if c in cols] + [c for c in cols if c not in preferred]
        return ordered

    def refresh_statistics_plot(self) -> None:
        try:
            df = self.db.read_activities_dataframe()
            if df.empty:
                ui.notify("No activities available for statistics.", type="warning")
                return
            df = df.copy()
            total_count = len(df)
            type_counts_all = df["sport_type"].astype(str).value_counts(dropna=False).to_dict() if "sport_type" in df.columns else {}
            df["start_date_local_dt"] = _parse_stats_datetime_series(df["start_date_local"])
            after_time_parse_count = int(df["start_date_local_dt"].notna().sum())
            if self.stats_type_select is not None and self.stats_type_select.value:
                wanted = set(str(x) for x in self.stats_type_select.value)
                df = df[df["sport_type"].astype(str).isin(wanted)]
            after_type_count = len(df)
            if self.stats_start_date is not None and self.stats_start_date.value:
                start = pd.to_datetime(str(self.stats_start_date.value), errors="coerce")
                if pd.notna(start):
                    start = pd.Timestamp(start).tz_localize(None)
                    df = df[df["start_date_local_dt"] >= start]
            if self.stats_end_date is not None and self.stats_end_date.value:
                end = pd.to_datetime(str(self.stats_end_date.value), errors="coerce")
                if pd.notna(end):
                    end = pd.Timestamp(end).tz_localize(None)
                    df = df[df["start_date_local_dt"] <= end + pd.Timedelta(days=1)]
            after_date_count = len(df)

            x = str(self.stats_x_axis.value or "start_date_local")
            y_vals = self.stats_y_axes.value or []
            if isinstance(y_vals, str):
                y_vals = [y_vals]
            y_vals = [str(y) for y in y_vals if str(y) in df.columns]
            if not y_vals:
                ui.notify("Select at least one Y metric.", type="warning")
                return
            x_plot = "start_date_local_dt" if x == "start_date_local" else x
            if x_plot not in df.columns:
                ui.notify(f"X axis not available: {x}", type="warning")
                return

            kind = str(self.stats_plot_kind.value or "line")
            color_by = str(self.stats_color_by.value or "None") if self.stats_color_by is not None else "None"
            if color_by == "None" or color_by not in df.columns:
                color_by = None
            agg = str(self.stats_aggregate.value or "none") if self.stats_aggregate is not None else "none"
            trend = str(self.stats_trend.value or "none") if self.stats_trend is not None else "none"

            plot_df = df.copy()
            # Coerce selected Y metrics to numeric; silently dropping nonnumeric rows
            # is much better than a hard plot failure for mixed DB columns.
            for y in y_vals:
                plot_df[y] = pd.to_numeric(plot_df[y], errors="coerce")
            plot_df = plot_df.dropna(subset=[x_plot])

            if agg != "none":
                group_cols = [x_plot] + ([color_by] if color_by else [])
                if pd.api.types.is_datetime64_any_dtype(plot_df[x_plot]):
                    plot_df = plot_df.copy()
                    plot_df[x_plot] = plot_df[x_plot].dt.floor("D")
                agg_map = {"mean": "mean", "median": "median", "sum": "sum", "min": "min", "max": "max", "count": "count"}[agg]
                plot_df = plot_df.groupby(group_cols, dropna=False)[y_vals].agg(agg_map).reset_index()

            fig = go.Figure()
            annotations = []
            marker_symbols = ["circle", "square", "diamond", "cross", "x", "triangle-up", "triangle-down"]
            group_values = [None]
            if color_by:
                group_values = list(plot_df[color_by].dropna().astype(str).unique()) or [None]

            for yi, y in enumerate(y_vals):
                for gi, group in enumerate(group_values):
                    sub = plot_df
                    name = y
                    if color_by and group is not None:
                        sub = plot_df[plot_df[color_by].astype(str) == str(group)]
                        name = f"{y} · {group}"
                    sub = sub[[x_plot, y] + ([color_by] if color_by else [])].dropna(subset=[x_plot, y]).copy()
                    if sub.empty:
                        continue
                    sub = sub.sort_values(x_plot)
                    symbol = marker_symbols[(yi + gi) % len(marker_symbols)]
                    if kind == "bar":
                        fig.add_trace(go.Bar(x=sub[x_plot], y=sub[y], name=name))
                    else:
                        mode = "markers" if kind == "scatter" else "lines+markers"
                        fig.add_trace(go.Scatter(x=sub[x_plot], y=sub[y], mode=mode, name=name, marker={"symbol": symbol}))

                    if trend != "none" and len(sub) >= 3:
                        fit = _fit_trend_for_plot(sub[x_plot], sub[y], trend)
                        if fit is not None:
                            tx, ty, label, r2, linearity = fit
                            fig.add_trace(go.Scatter(x=tx, y=ty, mode="lines", name=f"trend {name}: {label} R²={r2:.2f}, lin={linearity:.2f}", line={"dash": "dash"}, hoverinfo="skip"))
                            annotations.append(f"{name}: {label} R²={r2:.3f}, linearity={linearity:.3f}")

            if not fig.data:
                if self.stats_count_label is not None:
                    selected_types = ", ".join(str(x) for x in (self.stats_type_select.value or [])) if self.stats_type_select is not None and self.stats_type_select.value else "all"
                    top_counts = ", ".join(f"{k}: {v}" for k, v in list(type_counts_all.items())[:8])
                    self.stats_count_label.set_text(
                        f"DB rows: {total_count} | parsed dates: {after_time_parse_count} | after type filter ({selected_types}): {after_type_count} | "
                        f"after date filter: {after_date_count} | plottable rows: 0 | sport counts: {top_counts}"
                    )
                ui.notify("No plottable numeric data for selected X/Y/filter combination. Check date/type filters and whether the chosen Y metric is populated.", type="warning")
                return

            title = f"{', '.join(y_vals)} vs {x}" + (f" · grouped by {color_by}" if color_by else "")
            if annotations:
                title += "<br><sup>" + " | ".join(annotations[:5]) + (" ..." if len(annotations) > 5 else "") + "</sup>"
            fig.update_layout(title=title, margin=dict(l=20, r=20, t=80, b=20), legend_title_text="metric / group", barmode="group")
            fig.update_xaxes(title=x)
            fig.update_yaxes(title=", ".join(y_vals))

            if self.stats_count_label is not None:
                selected_types = ", ".join(str(x) for x in (self.stats_type_select.value or [])) if self.stats_type_select is not None and self.stats_type_select.value else "all"
                top_counts = ", ".join(f"{k}: {v}" for k, v in list(type_counts_all.items())[:8])
                self.stats_count_label.set_text(
                    f"DB rows: {total_count} | parsed dates: {after_time_parse_count} | after type filter ({selected_types}): {after_type_count} | "
                    f"after date filter: {after_date_count} | plotted table rows: {len(plot_df)} | sport counts: {top_counts}"
                )
            if self.stats_plot is not None:
                self.stats_plot.figure = fig
                self.stats_plot.update()
            if self.stats_table is not None:
                # In non-aggregated mode, show the full filtered activity rows, not only the subset used by a trace.
                table_source = plot_df
                rows = _clean_rows(table_source.tail(1000).sort_values("start_date_local_dt", ascending=False) if "start_date_local_dt" in table_source.columns else table_source.tail(1000))
                cols = [{"headerName": c, "field": c, "sortable": True, "filter": True, "resizable": True, "width": max(110, min(320, len(str(c)) * 9 + 40))} for c in plot_df.columns if c != "start_date_local_dt"]
                self.stats_table.options["columnDefs"] = cols
                self.stats_table.options["rowData"] = rows
                self.stats_table.update()
        except Exception as e:
            ui.notify(f"Statistics plot failed: {e}", type="negative", multi_line=True)

    def _build_settings_panel(self) -> None:
        with ui.card().classes("w-full"):
            ui.label("Strava authorization").classes("text-lg font-semibold")
            self.token_label = ui.label("")
            ui.label(f"Configured redirect URI: {self.config.redirect_uri}").classes("mono text-sm")
            if self.config.app_mode == "desktop":
                ui.label("Desktop mode uses a local callback. In your Strava API app, the callback domain should be localhost.").classes("text-sm")
            else:
                ui.label("In your Strava API app, the callback domain must match the domain part of that URL. For local use: localhost. For Tailscale use: your MagicDNS .ts.net hostname.").classes("text-sm")
            with ui.row().classes("items-center"):
                ui.button("Connect Strava", on_click=self.open_strava_authorization).props("color=primary")
                ui.button("Refresh token now", on_click=self.refresh_strava_token)
                ui.button("Refresh status", on_click=self.update_token_status)

        if self.config.app_mode != "desktop":
            with ui.card().classes("w-full"):
                ui.label("Access from iPhone via Tailscale").classes("text-lg font-semibold")
                ui.markdown(
                    f"""
                    1. Run this web app on the PC/server.
                    2. Install Tailscale on the PC/server and iPhone.
                    3. Open the PC's Tailscale/MagicDNS URL on the iPhone, for example `http://your-pc.your-tailnet.ts.net:{self.config.web_port}`.
                    4. Set `WORKOUTBUDDY_PUBLIC_BASE_URL` in `.env` to that same URL before Strava OAuth.
                    """
                )


    def _build_analysis_dialog(self) -> None:
        self.analysis_dialog = ui.dialog()
        with self.analysis_dialog, ui.card().classes("w-full max-w-6xl"):
            ui.label("Analyze recent training").classes("text-lg font-semibold")
            ui.label(
                "The deterministic analysis always runs locally. "
                "You can optionally add a local Ollama model."
            ).classes("text-sm text-gray-600")
            with ui.row().classes("w-full items-end"):
                self.analysis_backend_select = ui.select(
                    ["Deterministic only", "Local Ollama"],
                    value="Deterministic only",
                    label="Analysis mode",
                ).classes("w-64")
                self.analysis_model_input = ui.input(
                    "Model",
                    value="qwen3:8b",
                    placeholder="qwen3:8b",
                ).classes("w-56")
                ui.button("Run analysis", on_click=self.analyze_recent_trainings).props("color=primary")
                ui.button("Close", on_click=self.analysis_dialog.close)
            ui.label(
                "Local Ollama uses your installed Ollama service. Default model: qwen3:8b."
            ).classes("text-xs text-gray-500")
            self.analysis_markdown = ui.markdown("Click **Run analysis** to analyze the last 35 trainings.").classes("w-full")

    def _build_edit_dialog(self) -> None:
        self.edit_dialog = ui.dialog()
        with self.edit_dialog, ui.card().classes("w-full max-w-3xl"):
            ui.label("Edit planned workout").classes("text-lg font-semibold")
            self.edit_date = ui.label("").classes("mono")
            self.edit_no_workout = ui.checkbox("No workout / rest day", value=False)
            self.edit_no_workout.on("update:model-value", lambda e: self._adapt_edit_fields())
            self.edit_title = ui.input("title").classes("w-full")
            with ui.row().classes("w-full"):
                self.edit_sport_type = ui.select(["Run", "Ride", "VirtualRide", "Hike", "Walk", "Strength", "None"], label="sport type").classes("w-1/3")
                self.edit_sport_type.on("update:model-value", lambda e: ui.timer(0.05, self.recalculate_edit_recommendation_for_type, once=True))
                self.edit_family = ui.select(
                    ["recovery_aerobic", "easy_aerobic", "steady_progression", "quality", "tempo", "long_run", "cross_training_ride", "endurance_ride", "bike_intervals", "rest_or_mobility", "easy_hike", "long_hike", "hilly_hike", "recovery_hike"],
                    label="family",
                ).classes("w-1/3")
                self.edit_time = ui.input("start time", placeholder="18:00").classes("w-1/3")
            with ui.row().classes("w-full"):
                self.edit_duration = ui.number("duration min", min=0, max=400, step=5).classes("w-1/4")
                self.edit_distance = ui.number("distance km", min=0, max=80, step=0.1).classes("w-1/4")
                self.edit_zone = ui.input("zone / intensity").classes("w-1/4")
                self.edit_pace = ui.input("pace").classes("w-1/4")
            self.edit_wattage = ui.input("wattage / bike power target").classes("w-full")
            self.edit_notes = ui.textarea("notes / interval structure").classes("w-full")
            with ui.row().classes("justify-end w-full"):
                ui.button("Clear manual override", on_click=self.clear_selected_plan_override)
                ui.button("Cancel", on_click=self.edit_dialog.close)
                ui.button("Save and replan", on_click=self.save_selected_plan_workout).props("color=primary")

    # ----------------------------- activities -----------------------------

    def refresh_all(self) -> None:
        self.reload_activity_table()
        self.update_sport_type_options()
        self.update_projected_times()
        self.update_token_status()
        self._refresh_statistics_controls()

    def _refresh_statistics_controls(self) -> None:
        try:
            types = self.db.read_sport_types()
            if self.stats_type_select is not None:
                self.stats_type_select.options = types
                self.stats_type_select.update()
            metric_options = self._stats_metric_options() if hasattr(self, "_stats_metric_options") else ["start_date_local"]
            if self.stats_x_axis is not None:
                cur = self.stats_x_axis.value if self.stats_x_axis.value in metric_options else "start_date_local"
                self.stats_x_axis.options = metric_options
                self.stats_x_axis.value = cur
                self.stats_x_axis.update()
            if self.stats_y_axes is not None:
                current = self.stats_y_axes.value or []
                if isinstance(current, str):
                    current = [current]
                current = [v for v in current if v in metric_options]
                if not current:
                    current = [v for v in ["distance_km"] if v in metric_options] or [v for v in ["avg_hr", "apple_vo2max"] if v in metric_options]
                self.stats_y_axes.options = metric_options
                self.stats_y_axes.value = current
                self.stats_y_axes.update()
            if self.stats_color_by is not None:
                color_options = ["None"] + metric_options
                cur = self.stats_color_by.value if self.stats_color_by.value in color_options else ("sport_type" if "sport_type" in metric_options else "None")
                self.stats_color_by.options = color_options
                self.stats_color_by.value = cur
                self.stats_color_by.update()
        except Exception:
            pass

    def reload_activity_table(self) -> None:
        try:
            df = self.db.read_activities_dataframe()
            rows = _clean_rows(df)
            if self.activity_grid is not None:
                self.activity_grid.options["columnDefs"] = self._activity_column_defs_for_rows(rows)
                self.activity_grid.options["rowData"] = rows
                self.activity_grid.update()
            self.update_sport_type_options()
            self.update_landing_summary(df)
            self.set_status(f"Loaded {len(rows)} activities")
        except Exception as e:
            self.set_status(f"Failed to load activities: {e}")


    def update_landing_summary(self, df: Optional[pd.DataFrame] = None) -> None:
        """Update the small summary below the activities table.

        Uses the full activities dataframe, not the currently selected/filtered
        statistics dataframe.
        """
        if self.landing_summary_markdown is None:
            return
        try:
            if df is None:
                df = self.db.read_activities_dataframe()
            self.landing_summary_markdown.set_content(self._landing_summary_markdown(df))
        except Exception as e:
            self.landing_summary_markdown.set_content(f"Could not build summary: {e}")

    def _landing_summary_markdown(self, df: pd.DataFrame) -> str:
        if df is None or df.empty:
            return (
                "- Workouts last 14 days: **0**\n"
                "- Workouts last 7 days: **0**\n"
                "- Total km this year: **0.0 km**\n"
                "- Total km last year: **0.0 km**\n"
                "- Run km this year: **0.0 km**\n"
                "- Run km last year: **0.0 km**\n"
                "- Elevation gain this year: **0 m**"
            )

        data = df.copy()
        if "start_date_local" not in data.columns:
            data["start_date_local"] = None

        data["_dt"] = pd.to_datetime(data["start_date_local"], errors="coerce", utc=True)
        data = data.dropna(subset=["_dt"]).copy()
        if data.empty:
            return "No activities with parseable dates."

        now = pd.Timestamp.now(tz="UTC")
        this_year = int(now.year)
        last_year = this_year - 1

        dist = pd.to_numeric(data.get("distance_km", 0.0), errors="coerce").fillna(0.0)

        # Elevation gain is already a first-class metric in the app/table as
        # elevation_gain_m. Some older imports may have alternative column names,
        # so fall back conservatively.
        elev_col = None
        for candidate in ["elevation_gain_m", "total_elevation_gain", "elev_gain_m"]:
            if candidate in data.columns:
                elev_col = candidate
                break
        elev = pd.to_numeric(data.get(elev_col, 0.0), errors="coerce").fillna(0.0) if elev_col else pd.Series(0.0, index=data.index)

        years = data["_dt"].dt.year
        sport_norm = data.get("sport_type", pd.Series("", index=data.index)).astype(str).map(_norm_sport_type)
        is_run = sport_norm.isin({"run", "running", "trailrun", "virtualrun"})

        last_14 = int((data["_dt"] >= now - pd.Timedelta(days=14)).sum())
        last_7 = int((data["_dt"] >= now - pd.Timedelta(days=7)).sum())

        km_this_year = float(dist[years == this_year].sum())
        km_last_year = float(dist[years == last_year].sum())
        run_km_this_year = float(dist[(years == this_year) & is_run].sum())
        run_km_last_year = float(dist[(years == last_year) & is_run].sum())
        elev_this_year = float(elev[years == this_year].sum())

        # Also show the source column used for elevation so it is clear that this
        # is not ignored silently.
        elev_note = elev_col or "none"

        return (
            f"- Workouts last 14 days: **{last_14}**\n"
            f"- Workouts last 7 days: **{last_7}**\n"
            f"- Total km this year: **{km_this_year:.1f} km**\n"
            f"- Total km last year: **{km_last_year:.1f} km**\n"
            f"- Run km this year: **{run_km_this_year:.1f} km**\n"
            f"- Run km last year: **{run_km_last_year:.1f} km**\n"
            f"- Elevation gain this year: **{elev_this_year:.0f} m**\n"
            f"- Elevation metric: `{elev_note}`"
        )


    def on_activity_grid_click(self, e: Any) -> None:
        try:
            row = e.args.get("data") if isinstance(e.args, dict) else None
            if not row:
                return
            activity_id = str(row.get("activity_id"))
            full = self.db.read_activity_row(activity_id)
            if not full:
                return
            self.show_activity(full)
        except Exception as ex:
            self.set_status(f"Could not open activity: {ex}")


    def on_activity_grid_cell_changed(self, e: Any) -> None:
        """Persist editable grid cells.

        NiceGUI/AG Grid event payloads differ by version. Some send the edited
        value as args['newValue'], others only update args['data'][field]. The
        previous handler only read the row dictionary, which made Apple VO2max
        edits appear to work visually but not always persist.
        """
        try:
            args = e.args if isinstance(e.args, dict) else {}
            col = (
                args.get("colDef", {}).get("field")
                or args.get("column", {}).get("colId")
                or args.get("column", {}).get("colDef", {}).get("field")
            )
            if col not in {"apple_vo2max", "body_weight_kg"}:
                return
            row = args.get("data") or {}
            activity_id = str(row.get("activity_id") or "")
            if not activity_id:
                return
            raw_value = args.get("newValue", row.get("apple_vo2max"))
            val = _safe_float(raw_value, None)
            if col == "apple_vo2max":
                if val is not None and not (20 <= val <= 80):
                    ui.notify("Apple VO₂max should be between 20 and 80; value ignored.", type="warning")
                    self.reload_activity_table()
                    return
                self.db.set_activity_apple_vo2max(activity_id, val)
                if self.selected_activity_id == activity_id and self.selected_apple_vo2_input is not None:
                    self.selected_apple_vo2_input.value = val
                    self.selected_apple_vo2_input.update()
                self.update_projected_times()
                ui.notify(f"Saved Apple VO₂max for activity: {val if val is not None else 'empty'}")
            elif col == "body_weight_kg":
                if val is not None and not (35 <= val <= 160):
                    ui.notify("Weight should be between 35 and 160 kg; value ignored.", type="warning")
                    self.reload_activity_table()
                    return
                self.db.set_activity_body_weight(activity_id, val)
                if self.selected_activity_id == activity_id and self.selected_body_weight_input is not None:
                    self.selected_body_weight_input.value = val
                    self.selected_body_weight_input.update()
                ui.notify(f"Saved body weight for activity: {val if val is not None else 'empty'} kg")
        except Exception as ex:
            ui.notify(f"Could not save manual value: {ex}", type="negative", multi_line=True)

    def show_activity(self, row: Dict[str, Any]) -> None:
        self.selected_activity_id = str(row.get("activity_id") or "")
        if self.selected_apple_vo2_input is not None:
            self.selected_apple_vo2_input.value = _safe_float(row.get("apple_vo2max"), None)
            self.selected_apple_vo2_input.update()
        if self.selected_body_weight_input is not None:
            self.selected_body_weight_input.value = _safe_float(row.get("body_weight_kg"), None)
            self.selected_body_weight_input.update()
        if self.detail_markdown is not None:
            self.detail_markdown.set_content(self.format_activity_detail(row))
        if self.map_frame is not None:
            if self.selected_activity_id:
                self._set_map_frame_activity(self.selected_activity_id)
            else:
                self._set_map_frame_none()


    def _set_link_target_safe(self, link, target: str) -> None:
        """Set a NiceGUI link target across NiceGUI versions.

        Some NiceGUI versions have no Link.set_target() method. Updating the
        private props is acceptable here because ui.link ultimately renders an
        anchor/router link and the rest of this app already updates iframe props
        in the same way.
        """
        if link is None:
            return
        if hasattr(link, "set_target"):
            link.set_target(target)
            return

        # Different NiceGUI/Quasar versions use either href or to internally.
        try:
            link._props["href"] = target
            link._props["to"] = target
            link._props["target"] = "_blank"
            link.update()
        except Exception:
            try:
                setattr(link, "target", target)
                link.update()
            except Exception:
                pass

    def _set_map_frame_none(self) -> None:
        """Show an empty diagnostic document in the route-map iframe."""
        if self.map_frame is not None:
            doc = self._route_message_document("No route selected", "Select an activity row.")
            # Use srcdoc for the empty state only; real maps use src=/api/...
            self.map_frame._props["srcdoc"] = doc
            self.map_frame._props.pop("src", None)
            self.map_frame.update()
        if self.map_link is not None:
            self._set_link_target_safe(self.map_link, "#")
        if self.map_status is not None:
            self.map_status.set_text("No route selected")
        self._set_profile_frame_none()

    def _set_map_frame_activity(self, activity_id: str) -> None:
        """Load the selected activity map/profile in iframes and expose a debug link."""
        metric = urllib.parse.quote(str(self._current_route_metric()))
        url = f"/api/activity_map/{activity_id}?metric={metric}&t={int(time.time())}"
        if self.map_frame is not None:
            self.map_frame._props.pop("srcdoc", None)
            self.map_frame._props["src"] = url
            self.map_frame._props["loading"] = "eager"
            self.map_frame._props["referrerpolicy"] = "no-referrer"
            self.map_frame.update()
        if self.map_link is not None:
            self._set_link_target_safe(self.map_link, url)
        if self.map_status is not None:
            row = self.db.read_activity_row(activity_id) or {}
            pts = self._load_points(row) if row else []
            self.map_status.set_text(f"Route endpoint: {url} · GPS points detected: {len(pts)}")
        self._set_profile_frame_activity(activity_id)

    def _set_profile_frame_none(self) -> None:
        if self.route_profile_frame is not None:
            doc = self._route_message_document("No profile selected", "Select an activity row.")
            self.route_profile_frame._props["srcdoc"] = doc
            self.route_profile_frame._props.pop("src", None)
            self.route_profile_frame.update()
        if self.route_profile_status is not None:
            self.route_profile_status.set_text("No route/profile selected")

    def _set_profile_frame_activity(self, activity_id: str) -> None:
        metric = urllib.parse.quote(str(self._current_route_metric()))
        x_axis = urllib.parse.quote(str(self._current_route_x_axis()))
        url = f"/api/activity_profile/{activity_id}?metric={metric}&x_axis={x_axis}&t={int(time.time())}"
        if self.route_profile_frame is not None:
            self.route_profile_frame._props.pop("srcdoc", None)
            self.route_profile_frame._props["src"] = url
            self.route_profile_frame._props["loading"] = "eager"
            self.route_profile_frame._props["referrerpolicy"] = "no-referrer"
            self.route_profile_frame.update()
        if self.route_profile_status is not None:
            self.route_profile_status.set_text(f"Profile endpoint: {url}")

    def refresh_route_visuals(self, *args) -> None:
        if self.selected_activity_id:
            self._set_map_frame_activity(self.selected_activity_id)
        else:
            self._set_map_frame_none()
            self._set_profile_frame_none()

    def save_selected_activity_manual_values(self) -> None:
        if not self.selected_activity_id:
            ui.notify("Select an activity first.", type="warning")
            return
        vo2 = _safe_float(self.selected_apple_vo2_input.value if self.selected_apple_vo2_input is not None else None, None)
        weight = _safe_float(self.selected_body_weight_input.value if self.selected_body_weight_input is not None else None, None)
        if vo2 is not None and not (20 <= vo2 <= 80):
            ui.notify("Apple VO₂max should be between 20 and 80.", type="warning")
            return
        if weight is not None and not (35 <= weight <= 160):
            ui.notify("Weight should be between 35 and 160 kg.", type="warning")
            return
        self.db.set_activity_apple_vo2max(self.selected_activity_id, vo2)
        self.db.set_activity_body_weight(self.selected_activity_id, weight)
        self.reload_activity_table()
        full = self.db.read_activity_row(self.selected_activity_id)
        if full:
            self.show_activity(full)
        self.update_projected_times()
        ui.notify(f"Saved manual values: VO₂max={vo2 if vo2 is not None else 'empty'}, weight={weight if weight is not None else 'empty'} kg")

    def save_selected_activity_apple_vo2(self) -> None:
        self.save_selected_activity_manual_values()

    def clear_selected_activity_apple_vo2(self) -> None:
        if self.selected_apple_vo2_input is not None:
            self.selected_apple_vo2_input.value = None
            self.selected_apple_vo2_input.update()
        self.save_selected_activity_manual_values()

    def clear_selected_activity_body_weight(self) -> None:
        if self.selected_body_weight_input is not None:
            self.selected_body_weight_input.value = None
            self.selected_body_weight_input.update()
        self.save_selected_activity_manual_values()

    def format_activity_detail(self, row: Dict[str, Any]) -> str:
        lines = [
            f"### {row.get('name', '')}",
            "",
            f"- **Activity ID:** `{row.get('activity_id', '')}`",
            f"- **Type:** {row.get('sport_type', '')}",
            f"- **Source:** {row.get('source', '')}",
            f"- **Date local:** {row.get('start_date_local', '')}",
            f"- **Distance:** {_fmt(_safe_float(row.get('distance_m'), 0) / 1000.0, 2, ' km')}",
            f"- **Moving time:** {_fmt(_safe_float(row.get('moving_time_s'), 0) / 60.0, 1, ' min')}",
            f"- **Elevation gain:** {_fmt(row.get('elevation_gain_m'), 0, ' m')}",
            f"- **Average pace:** {_fmt_pace(row.get('avg_pace_min_km'))}",
            f"- **Grade-adjusted pace:** {_fmt_pace(row.get('avg_gap_pace_min_km'))}",
            f"- **Average HR:** {_fmt(row.get('avg_hr'), 0, ' bpm')}",
            f"- **Max HR:** {_fmt(row.get('max_hr'), 0, ' bpm')}",
            f"- **Average power:** {_fmt(row.get('avg_power'), 0, ' W')}",
            f"- **Raw HR efficiency drift:** {_fmt(row.get('hr_efficiency_drift_pct'), 1, ' %')}",
            f"- **Grade-adjusted HR efficiency drift:** {_fmt(row.get('gap_hr_efficiency_drift_pct'), 1, ' %')}",
            f"- **Estimated VO₂ demand:** {_fmt(row.get('vo2_demand_est_ml_kg_min'), 1, ' ml/kg/min')}",
            f"- **Manual Apple VO₂max for this workout:** {_fmt(row.get('apple_vo2max'), 1, ' ml/kg/min')}",
            f"- **Manual body weight / BMI:** {_fmt(row.get('body_weight_kg'), 1, ' kg')} / {_fmt(row.get('bmi'), 1)} ({row.get('bmi_category') or '—'}), age {_fmt(row.get('age_years_at_activity'), 0)}",
            f"- **Power normalization:** cycling {_fmt(row.get('cycling_power_w'), 0, ' W')}, run-equivalent {_fmt(row.get('run_equivalent_power_w'), 0, ' W')}, W/HR {_fmt(row.get('power_hr_efficiency'), 2)}",
            f"- **Weather archive temperature:** {_fmt(row.get('weather_temp_c') if row.get('weather_temp_c') is not None else row.get('avg_temp_c'), 1, ' °C')} (source: {row.get('weather_source') or 'stream/manual/unknown'})",
            f"- **Weather deviation from 15 °C:** {_fmt(row.get('weather_temp_deviation_from_15_c'), 1, ' °C')}",
            f"- **Training load score / TRIMP:** {_fmt(row.get('training_load_score'), 1)} / {_fmt(row.get('trimp_score'), 1)}",
            f"- **Easy / hard fraction:** {_fmt((_safe_float(row.get('easy_zone_fraction'), 0) or 0) * 100, 0, ' %')} / {_fmt((_safe_float(row.get('hard_zone_fraction'), 0) or 0) * 100, 0, ' %')}",
            f"- **Per-km GAP HR drift:** {_fmt(row.get('km_gap_hr_efficiency_drift_pct'), 1, ' %')}",
            f"- **Step frequency / total steps:** {_fmt(row.get('run_step_frequency_spm'), 0, ' spm')} / {_fmt(row.get('estimated_total_steps'), 0)}",
            f"- **Impact estimate avg/max:** {_fmt(row.get('avg_impact_bw'), 2, ' ×BW')} / {_fmt(row.get('max_impact_bw'), 2, ' ×BW')}",
            f"- **Impact load index:** {_fmt(row.get('impact_load_index'), 1)}",
            f"- **Best 5/20 min pace:** {_fmt_pace(row.get('best_5min_pace_min_km'))} / {_fmt_pace(row.get('best_20min_pace_min_km'))}",
            f"- **Normalized power / VI:** {_fmt(row.get('normalized_power'), 0, ' W')} / {_fmt(row.get('variability_index'), 2)}",
            f"- **Data quality:** GPS={row.get('has_gps')}, altitude={row.get('has_altitude')}, HR={row.get('has_hr')}, power={row.get('has_power')}, points={row.get('data_point_count')}",
            "",
            "#### HR zones from local thresholds",
            f"- Z1 <134: {_fmt(_safe_float(row.get('z1_s'), 0) / 60.0, 1, ' min')}",
            f"- Z2 134–145: {_fmt(_safe_float(row.get('z2_s'), 0) / 60.0, 1, ' min')}",
            f"- Z3 146–155: {_fmt(_safe_float(row.get('z3_s'), 0) / 60.0, 1, ' min')}",
            f"- Z4 156–166: {_fmt(_safe_float(row.get('z4_s'), 0) / 60.0, 1, ' min')}",
            f"- Z5 ≥167: {_fmt(_safe_float(row.get('z5_s'), 0) / 60.0, 1, ' min')}",
            "",
            "#### Per-km splits",
        ]
        lines.extend(self._format_km_splits(row))
        return "\n".join(lines)

    def _format_km_splits(self, row: Dict[str, Any]) -> List[str]:
        raw = row.get("km_splits_json")
        if not raw:
            return ["No per-km split data available."]
        try:
            splits = json.loads(str(raw))
        except Exception:
            return ["No readable per-km split data available."]
        if not isinstance(splits, list) or not splits:
            return ["No per-km split data available."]
        lines = ["| km | HR | pace | GAP | grade | eff drift input |", "|---:|---:|---:|---:|---:|---:|"]
        for sp in splits[:25]:
            if not isinstance(sp, dict):
                continue
            lines.append(
                f"| {sp.get('km', '')} | {_fmt(sp.get('avg_hr'), 0)} | {_fmt_pace(sp.get('avg_pace_min_km'))} | "
                f"{_fmt_pace(sp.get('avg_gap_pace_min_km'))} | {_fmt(sp.get('avg_grade_pct'), 1, ' %')} | "
                f"{_fmt(sp.get('eff_gap_speed_per_hr'), 4)} |"
            )
        if len(splits) > 25:
            lines.append(f"... {len(splits) - 25} more km omitted in detail view.")
        return lines


    def _route_metric_options(self) -> Dict[str, str]:
        return {
            "pace_min_km": "Pace (fast green → slow red)",
            "velocity_kmh": "Velocity / speed",
            "heart_rate": "Heart rate",
            "zone": "HR zone",
            "power_w": "Power (W)",
            "altitude_m": "Altitude",
            "grade_pct": "Grade %",
            "cadence": "Cadence",
        }

    def _current_route_metric(self) -> str:
        value = getattr(self.route_metric_select, "value", None) if self.route_metric_select is not None else None
        if value not in self._route_metric_options():
            return "pace_min_km"
        return str(value)

    def _current_route_x_axis(self) -> str:
        value = getattr(self.route_x_select, "value", None) if self.route_x_select is not None else None
        return str(value) if value in {"distance_km", "time_min"} else "distance_km"

    def _load_streams(self, row: Dict[str, Any]) -> Dict[str, Any]:
        streams_path = self._resolve_existing_path(row.get("streams_json_path"))
        if streams_path is None:
            return {}
        try:
            obj = json.loads(streams_path.read_text(encoding="utf-8"))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def _stream_values(self, streams: Dict[str, Any], *keys: str) -> List[Any]:
        for key in keys:
            obj = streams.get(key)
            if isinstance(obj, dict) and "data" in obj:
                obj = obj.get("data")
            if isinstance(obj, list):
                return obj
        return []

    def _resample_numeric(self, values: List[Any], n: int) -> List[Optional[float]]:
        vals: List[Optional[float]] = [_safe_float(v, None) for v in values]
        if n <= 0:
            return []
        if not vals:
            return [None] * n
        if len(vals) == n:
            return vals
        x_old = np.linspace(0.0, 1.0, len(vals))
        x_new = np.linspace(0.0, 1.0, n)
        y = np.array([np.nan if v is None else float(v) for v in vals], dtype=float)
        valid = np.isfinite(y)
        if valid.sum() < 2:
            fill = float(y[valid][0]) if valid.sum() == 1 else np.nan
            return [None if not np.isfinite(fill) else fill] * n
        out = np.interp(x_new, x_old[valid], y[valid])
        return [None if not np.isfinite(v) else float(v) for v in out]

    def _resample_latlng(self, points: List[Any], n: int) -> Tuple[List[Optional[float]], List[Optional[float]]]:
        pts: List[Tuple[float, float]] = []
        for p in points:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                lat = _safe_float(p[0], None)
                lon = _safe_float(p[1], None)
                if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
                    pts.append((float(lat), float(lon)))
        if not pts or n <= 0:
            return [None] * n, [None] * n
        if len(pts) == n:
            return [p[0] for p in pts], [p[1] for p in pts]
        x_old = np.linspace(0.0, 1.0, len(pts))
        x_new = np.linspace(0.0, 1.0, n)
        lat = np.interp(x_new, x_old, [p[0] for p in pts])
        lon = np.interp(x_new, x_old, [p[1] for p in pts])
        return [float(v) for v in lat], [float(v) for v in lon]

    def _activity_stream_dataframe(self, row: Dict[str, Any]) -> pd.DataFrame:
        streams = self._load_streams(row)
        if not streams:
            return pd.DataFrame()

        time_values = self._stream_values(streams, "time")
        dist_values = self._stream_values(streams, "distance")
        alt_values = self._stream_values(streams, "altitude", "altitude_m")
        vel_values = self._stream_values(streams, "velocity_smooth", "speed", "speed_mps")
        hr_values = self._stream_values(streams, "heartrate", "heart_rate", "hr")
        power_values = self._stream_values(streams, "watts", "power", "power_w")
        grade_values = self._stream_values(streams, "grade_smooth", "grade", "grade_pct")
        cadence_values = self._stream_values(streams, "cadence")
        latlng_values = self._stream_values(streams, "latlng")

        lengths = [len(v) for v in [time_values, dist_values, alt_values, vel_values, hr_values, power_values, grade_values, cadence_values] if v]
        if not lengths and latlng_values:
            lengths = [len(latlng_values)]
        n = max(lengths) if lengths else 0
        if n <= 0:
            return pd.DataFrame()

        time_s = self._resample_numeric(time_values, n)
        if not time_values:
            time_s = [float(i) for i in range(n)]
        dist_m = self._resample_numeric(dist_values, n)
        alt_m = self._resample_numeric(alt_values, n)
        vel_mps = self._resample_numeric(vel_values, n)
        hr = self._resample_numeric(hr_values, n)
        power = self._resample_numeric(power_values, n)
        grade = self._resample_numeric(grade_values, n)
        cadence = self._resample_numeric(cadence_values, n)
        lat, lon = self._resample_latlng(latlng_values, n)

        # Derive distance/speed if one of them is missing.
        if all(v is None for v in dist_m):
            total_km = _safe_float(row.get("distance_km"), None)
            if total_km is not None and total_km > 0:
                dist_m = [float(total_km * 1000.0 * i / max(n - 1, 1)) for i in range(n)]
        if all(v is None for v in vel_mps) and not all(v is None for v in dist_m):
            vel_mps = [None]
            for i in range(1, n):
                d0, d1 = dist_m[i - 1], dist_m[i]
                t0, t1 = time_s[i - 1], time_s[i]
                if d0 is None or d1 is None or t0 is None or t1 is None or t1 <= t0:
                    vel_mps.append(None)
                else:
                    vel_mps.append(float(max(0.0, (d1 - d0) / (t1 - t0))))

        velocity_kmh = [None if v is None else float(v) * 3.6 for v in vel_mps]
        pace_min_km = [None if v is None or v <= 0.1 else float(1000.0 / v / 60.0) for v in vel_mps]
        zone = []
        for h in hr:
            if h is None:
                zone.append(None)
            elif h < 134:
                zone.append(1.0)
            elif h < 146:
                zone.append(2.0)
            elif h < 156:
                zone.append(3.0)
            elif h < 167:
                zone.append(4.0)
            else:
                zone.append(5.0)

        df = pd.DataFrame({
            "time_s": time_s,
            "time_min": [None if v is None else float(v) / 60.0 for v in time_s],
            "distance_m": dist_m,
            "distance_km": [None if v is None else float(v) / 1000.0 for v in dist_m],
            "altitude_m": alt_m,
            "velocity_kmh": velocity_kmh,
            "pace_min_km": pace_min_km,
            "heart_rate": hr,
            "zone": zone,
            "power_w": power,
            "grade_pct": grade,
            "cadence": cadence,
            "lat": lat,
            "lon": lon,
        })
        # Smooth extreme spikes for visualization only.
        if "pace_min_km" in df:
            df.loc[(df["pace_min_km"] < 2.0) | (df["pace_min_km"] > 20.0), "pace_min_km"] = np.nan
        return df

    def _metric_label(self, metric_key: str) -> str:
        return self._route_metric_options().get(metric_key, metric_key)

    def _metric_unit(self, metric_key: str) -> str:
        return {
            "pace_min_km": "min/km",
            "velocity_kmh": "km/h",
            "heart_rate": "bpm",
            "zone": "zone",
            "power_w": "W",
            "altitude_m": "m",
            "grade_pct": "%",
            "cadence": "rpm/spm",
        }.get(metric_key, "")

    def _metric_higher_is_better_for_color(self, metric_key: str) -> bool:
        # Fast speed should be green; slow pace/high HR/high power/high grade effort should move towards red.
        return metric_key in {"velocity_kmh"}

    def _color_for_value(self, value: Any, vmin: float, vmax: float, metric_key: str) -> str:
        x = _safe_float(value, None)
        if x is None or not math.isfinite(vmin) or not math.isfinite(vmax) or vmax <= vmin:
            return "#3b82f6"  # blue fallback
        p = max(0.0, min(1.0, (float(x) - vmin) / (vmax - vmin)))
        if self._metric_higher_is_better_for_color(metric_key):
            p = 1.0 - p
        # green -> yellow -> red
        if p <= 0.5:
            q = p / 0.5
            r = int(34 + q * (250 - 34))
            g = int(197 + q * (204 - 197))
            b = int(94 + q * (21 - 94))
        else:
            q = (p - 0.5) / 0.5
            r = int(250 + q * (220 - 250))
            g = int(204 + q * (38 - 204))
            b = int(21 + q * (38 - 21))
        return f"#{r:02x}{g:02x}{b:02x}"

    def _metric_color_range(self, values: Iterable[Any]) -> Tuple[float, float]:
        vals = pd.to_numeric(pd.Series(list(values)), errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
        if len(vals) == 0:
            return (0.0, 1.0)
        # Percentiles avoid one GPS/pace spike making all colors look the same.
        vmin = float(vals.quantile(0.05))
        vmax = float(vals.quantile(0.95))
        if not math.isfinite(vmin) or not math.isfinite(vmax) or vmax <= vmin:
            vmin = float(vals.min())
            vmax = float(vals.max())
        if vmax <= vmin:
            vmax = vmin + 1.0
        return vmin, vmax

    def _downsample_route_df(self, df: pd.DataFrame, max_points: int = 900) -> pd.DataFrame:
        if len(df) <= max_points:
            return df.reset_index(drop=True)
        idx = np.linspace(0, len(df) - 1, max_points).round().astype(int)
        return df.iloc[idx].reset_index(drop=True)

    def make_activity_map_if_available(self, row: Dict[str, Any]) -> str:
        """Legacy helper retained for compatibility; the UI now uses /api/activity_map."""
        activity_id = str(row.get("activity_id") or "")
        if not activity_id:
            return '<div style="height:460px; display:flex; align-items:center; justify-content:center; border:1px solid #ddd; background:#fafafa;">No route selected</div>'
        return (
            f'<iframe src="/api/activity_map/{html_lib.escape(activity_id, quote=True)}?t={int(time.time())}" '
            'style="width:100%; height:460px; border:1px solid #ddd; border-radius:6px;" '
            'loading="lazy"></iframe>'
        )

    def make_activity_map_document(self, activity_id: str, metric_key: Optional[str] = None) -> str:
        """Return a complete HTML document for the selected activity route map.

        Route color is driven by the selected stream metric. If no GPS points are
        available, this endpoint returns a diagnostic page; the profile endpoint
        can still visualize non-GPS streams such as ergometer power/HR/time.
        """
        row = self.db.read_activity_row(activity_id)
        if not row:
            return self._route_message_document("Activity not found", f"activity_id={activity_id}")
        points = self._load_points(row)
        if not points:
            return self._route_message_document(
                "No GPS route available for this activity",
                self._route_diagnostic(row) + "\n\nIf the activity has time/distance/HR/power streams, the profile chart below the map can still be used.",
            )

        metric_key = metric_key or "pace_min_km"
        if metric_key not in self._route_metric_options():
            metric_key = "pace_min_km"

        stream_df = self._activity_stream_dataframe(row)
        values: List[Optional[float]] = []
        if not stream_df.empty and metric_key in stream_df.columns:
            values = self._resample_numeric(stream_df[metric_key].tolist(), len(points))
        if not values or all(v is None for v in values):
            values = [None] * len(points)

        vmin, vmax = self._metric_color_range(values)
        route_df = pd.DataFrame({"lat": [p[0] for p in points], "lon": [p[1] for p in points], "metric": values})
        route_df = self._downsample_route_df(route_df, max_points=900)
        pts = [(float(r.lat), float(r.lon)) for r in route_df.itertuples()]
        vals = route_df["metric"].tolist()

        lat0, lon0 = pts[len(pts) // 2]
        m = folium.Map(location=[lat0, lon0], zoom_start=13, tiles="OpenStreetMap", control_scale=True)

        if len(pts) >= 2:
            for i in range(1, len(pts)):
                color = self._color_for_value(vals[i], vmin, vmax, metric_key)
                folium.PolyLine([pts[i - 1], pts[i]], weight=5, opacity=0.90, color=color).add_to(m)
        else:
            folium.PolyLine(pts, weight=5, opacity=0.90, color="blue").add_to(m)

        folium.Marker(pts[0], tooltip="Start").add_to(m)
        folium.Marker(pts[-1], tooltip="Finish").add_to(m)
        title = html_lib.escape(str(row.get("name") or activity_id))
        metric_label = html_lib.escape(self._metric_label(metric_key))
        unit = html_lib.escape(self._metric_unit(metric_key))
        body = m.get_root().render()
        legend = f"""
        <div style='position:fixed;left:8px;bottom:8px;z-index:9999;background:white;padding:6px 8px;border:1px solid #ccc;border-radius:4px;font:12px sans-serif;max-width:340px;'>
          <b>{title}</b><br>
          Route colored by: {metric_label}<br>
          <div style='height:10px;width:160px;background:linear-gradient(90deg,#22c55e,#facc15,#dc2626);border:1px solid #aaa;margin-top:4px;'></div>
          <span style='font-size:11px;'>green = better/lower effort, red = slower/harder &nbsp; · &nbsp; range {vmin:.2f}–{vmax:.2f} {unit}</span><br>
          <span style='font-size:11px;'>{len(points)} GPS points · rendered {len(pts)} points</span>
        </div>
        """
        body = body.replace("</body>", legend + "</body>")
        return body

    def make_activity_profile_document(self, activity_id: str, metric_key: Optional[str] = None, x_axis: str = "distance_km") -> str:
        row = self.db.read_activity_row(activity_id)
        if not row:
            return self._route_message_document("Activity not found", f"activity_id={activity_id}")
        metric_key = metric_key or "pace_min_km"
        if metric_key not in self._route_metric_options():
            metric_key = "pace_min_km"
        x_axis = x_axis if x_axis in {"distance_km", "time_min"} else "distance_km"
        df = self._activity_stream_dataframe(row)
        if df.empty:
            return self._route_message_document("No stream/profile data available", self._route_diagnostic(row))
        if x_axis == "distance_km" and ("distance_km" not in df.columns or df["distance_km"].dropna().empty):
            x_axis = "time_min"
        if metric_key not in df.columns or df[metric_key].dropna().empty:
            # Fallback to altitude if selected stream is absent.
            metric_key = "altitude_m" if "altitude_m" in df.columns and not df["altitude_m"].dropna().empty else metric_key
        if x_axis not in df.columns or df[x_axis].dropna().empty:
            return self._route_message_document("No usable x-axis data", "This activity has no distance/time stream.")

        # Keep graph responsive for long activities.
        df_plot = df.copy()
        if len(df_plot) > 1800:
            idx = np.linspace(0, len(df_plot) - 1, 1800).round().astype(int)
            df_plot = df_plot.iloc[idx].reset_index(drop=True)

        x_label = "Distance (km)" if x_axis == "distance_km" else "Time (min)"
        metric_label = self._metric_label(metric_key)
        metric_unit = self._metric_unit(metric_key)
        vmin, vmax = self._metric_color_range(df_plot.get(metric_key, pd.Series(dtype=float)).tolist())
        colors = [self._color_for_value(v, vmin, vmax, metric_key) for v in df_plot.get(metric_key, pd.Series([None] * len(df_plot))).tolist()]

        fig = go.Figure()
        if "altitude_m" in df_plot.columns and not df_plot["altitude_m"].dropna().empty:
            fig.add_trace(go.Scatter(
                x=df_plot[x_axis], y=df_plot["altitude_m"], mode="lines", name="Altitude",
                line=dict(color="rgba(80,80,80,0.55)", width=2), yaxis="y1",
                hovertemplate=f"{x_label}: %{{x:.2f}}<br>Altitude: %{{y:.1f}} m<extra></extra>",
            ))
        if metric_key in df_plot.columns and not df_plot[metric_key].dropna().empty:
            fig.add_trace(go.Scatter(
                x=df_plot[x_axis], y=df_plot[metric_key], mode="lines+markers", name=metric_label,
                line=dict(color="rgba(30,90,200,0.35)", width=1),
                marker=dict(size=4, color=colors), yaxis="y2",
                customdata=np.stack([
                    df_plot.get("time_min", pd.Series([np.nan] * len(df_plot))).to_numpy(),
                    df_plot.get("distance_km", pd.Series([np.nan] * len(df_plot))).to_numpy(),
                    df_plot.get("altitude_m", pd.Series([np.nan] * len(df_plot))).to_numpy(),
                ], axis=-1),
                hovertemplate=(
                    "Time: %{customdata[0]:.1f} min<br>Distance: %{customdata[1]:.2f} km<br>"
                    "Altitude: %{customdata[2]:.1f} m<br>" + html_lib.escape(metric_label) + ": %{y:.2f} " + html_lib.escape(metric_unit) + "<extra></extra>"
                ),
            ))
        fig.update_layout(
            template="plotly_white",
            margin=dict(l=45, r=55, t=35, b=35),
            height=285,
            title=f"Workout profile · {html_lib.escape(str(row.get('name') or activity_id))}",
            xaxis=dict(title=x_label),
            yaxis=dict(title="Altitude (m)", side="left", showgrid=True),
            yaxis2=dict(title=f"{metric_label} ({metric_unit})", overlaying="y", side="right", showgrid=False),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        )
        return fig.to_html(include_plotlyjs=True, full_html=True, config={"responsive": True, "displaylogo": False})

    def _route_message_document(self, title: str, message: str) -> str:
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<style>body{margin:0;font-family:system-ui,Segoe UI,Arial,sans-serif;background:#fafafa;}"
            ".box{height:100vh;display:flex;align-items:center;justify-content:center;text-align:center;padding:1rem;box-sizing:border-box;}"
            "pre{white-space:pre-wrap;text-align:left;background:#fff;border:1px solid #ddd;border-radius:6px;padding:.75rem;max-width:95%;}</style>"
            "</head><body><div class='box'><div>"
            f"<h3>{html_lib.escape(title)}</h3><pre>{html_lib.escape(message)}</pre>"
            "</div></div></body></html>"
        )

    def _resolve_existing_path(self, value: Any) -> Optional[Path]:
        if not value:
            return None
        p = Path(str(value))
        candidates = [p]
        if not p.is_absolute():
            candidates.extend([self.config.root_dir / p, self.config.db_path.parent / p])
        # If a DB row points at an old project root but keeps the standard filename,
        # also try the current raw/streams folders.
        candidates.extend([self.config.streams_dir / p.name, self.config.raw_dir / p.name])
        for c in candidates:
            try:
                if c.exists():
                    return c
            except Exception:
                continue
        return None

    def _load_points(self, row: Dict[str, Any]) -> List[Tuple[float, float]]:
        streams_path = self._resolve_existing_path(row.get("streams_json_path"))
        if streams_path is not None:
            try:
                streams = json.loads(streams_path.read_text(encoding="utf-8"))
                points = StreamUtils.latlng_points(streams)
                if points:
                    return points
                # Be permissive: some import versions may store GPS as separate
                # latitude/longitude arrays or directly as a list rather than
                # Strava-style {"latlng": {"data": [[lat, lon], ...]}}.
                points = self._extract_points_permissive(streams)
                if points:
                    return points
            except Exception:
                pass
        raw_path = self._resolve_existing_path(row.get("raw_json_path"))
        if raw_path is not None:
            try:
                raw = json.loads(raw_path.read_text(encoding="utf-8"))
                detail = raw.get("detail", {}) if isinstance(raw, dict) else {}
                summary_polyline = detail.get("map", {}).get("summary_polyline") if isinstance(detail, dict) else None
                if summary_polyline:
                    return [(float(lat), float(lon)) for lat, lon in polyline.decode(summary_polyline)]
            except Exception:
                pass
        return []

    def _extract_points_permissive(self, streams: Dict[str, Any]) -> List[Tuple[float, float]]:
        def data_for(key: str) -> Any:
            obj = streams.get(key)
            if isinstance(obj, dict) and "data" in obj:
                return obj.get("data")
            return obj

        # Direct latlng list
        latlng = data_for("latlng")
        if isinstance(latlng, list):
            pts: List[Tuple[float, float]] = []
            for p in latlng:
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    lat = _safe_float(p[0], None)
                    lon = _safe_float(p[1], None)
                    if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
                        pts.append((float(lat), float(lon)))
            if pts:
                return pts

        # Separate latitude/longitude arrays used by some parsers/exporters.
        lat_data = data_for("latitude") or data_for("lat") or data_for("LatitudeDegrees")
        lon_data = data_for("longitude") or data_for("lon") or data_for("LongitudeDegrees")
        if isinstance(lat_data, list) and isinstance(lon_data, list):
            pts = []
            for lat_raw, lon_raw in zip(lat_data, lon_data):
                lat = _safe_float(lat_raw, None)
                lon = _safe_float(lon_raw, None)
                if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
                    pts.append((float(lat), float(lon)))
            if pts:
                return pts
        return []

    def _route_diagnostic(self, row: Dict[str, Any]) -> str:
        streams_path = self._resolve_existing_path(row.get("streams_json_path"))
        raw_path = self._resolve_existing_path(row.get("raw_json_path"))
        parts = []
        parts.append(f"streams file: {'found' if streams_path else 'not found'}")
        if streams_path:
            try:
                streams = json.loads(streams_path.read_text(encoding="utf-8"))
                if isinstance(streams, dict):
                    parts.append("stream keys: " + ", ".join(sorted(streams.keys())[:12]))
                    lat = streams.get("latlng")
                    if isinstance(lat, dict):
                        data = lat.get("data") or []
                        parts.append(f"latlng points: {len(data) if isinstance(data, list) else 'unreadable'}")
                    elif isinstance(lat, list):
                        parts.append(f"latlng direct-list points: {len(lat)}")
                    else:
                        pts = self._extract_points_permissive(streams)
                        parts.append(f"permissive GPS points: {len(pts)}")
            except Exception as ex:
                parts.append(f"stream read error: {ex}")
        parts.append(f"raw file: {'found' if raw_path else 'not found'}")
        return " | ".join(parts)

    def sync_strava(self) -> None:
        try:
            count = self.sync_service.sync_recent(max_pages=1, per_page=30, skip_known=True, progress=self.set_status)
            self.reload_activity_table()
            self.update_projected_times()
            ui.notify(f"Strava sync complete: {count} activities imported/updated")
        except Exception as e:
            ui.notify(f"Strava sync failed: {e}", type="negative", multi_line=True)
            self.set_status(f"Strava sync failed: {e}")

    def handle_tcx_upload(self, e: Any) -> None:
        try:
            files = self._extract_uploaded_files(e)
            if not files:
                raise ValueError("No readable uploaded TCX content found in NiceGUI upload event.")

            paths: List[Path] = []
            for filename, data in files:
                safe_name = _safe_filename(filename or f"uploaded_{int(time.time())}.tcx")
                if not safe_name.lower().endswith(".tcx"):
                    safe_name += ".tcx"
                path = self.config.upload_dir / safe_name

                # Avoid accidental overwriting if two uploaded files have the same name.
                if path.exists():
                    stem = path.stem
                    suffix = path.suffix
                    path = self.config.upload_dir / f"{stem}_{int(time.time() * 1000)}{suffix}"

                path.write_bytes(data)
                paths.append(path)

            count = self.tcx_service.import_files(paths, progress=self.set_status)
            self.reload_activity_table()
            self.update_projected_times()
            ui.notify(f"Imported/updated {count} TCX activit{'y' if count == 1 else 'ies'}")
        except Exception as ex:
            ui.notify(f"TCX upload/import failed: {ex}", type="negative", multi_line=True)

    def _extract_uploaded_files(self, e: Any) -> List[Tuple[str, bytes]]:
        """Return [(filename, bytes), ...] for several NiceGUI upload event variants.

        NiceGUI changed the exact UploadEventArguments attributes across versions.
        Some versions expose e.name + e.content, while others expose a file-like
        object with .name/.filename. This helper avoids depending on one specific
        attribute layout.
        """
        candidates: List[Any] = []
        if hasattr(e, "files") and getattr(e, "files"):
            try:
                candidates.extend(list(getattr(e, "files")))
            except Exception:
                pass
        if hasattr(e, "file") and getattr(e, "file") is not None:
            candidates.append(getattr(e, "file"))
        if hasattr(e, "content") and getattr(e, "content") is not None:
            candidates.append(e)

        out: List[Tuple[str, bytes]] = []
        for item in candidates:
            filename = self._upload_filename(item) or self._upload_filename(e) or "uploaded.tcx"
            data = self._upload_bytes(item)
            if data:
                out.append((filename, data))
        return out

    @staticmethod
    def _upload_filename(obj: Any) -> Optional[str]:
        for attr in ("name", "filename", "file_name", "upload_name"):
            value = getattr(obj, attr, None)
            if value:
                return str(value)
        content = getattr(obj, "content", None)
        for attr in ("name", "filename", "file_name"):
            value = getattr(content, attr, None)
            if value:
                return str(value)
        file_obj = getattr(obj, "file", None)
        for attr in ("name", "filename", "file_name"):
            value = getattr(file_obj, attr, None)
            if value:
                return str(value)
        return None

    @staticmethod
    def _upload_bytes(obj: Any) -> bytes:
        # Direct bytes-like object
        if isinstance(obj, bytes):
            return obj
        if isinstance(obj, bytearray):
            return bytes(obj)

        content = getattr(obj, "content", None)
        if isinstance(content, bytes):
            return content
        if isinstance(content, bytearray):
            return bytes(content)
        if isinstance(content, str):
            return content.encode("utf-8")

        # NiceGUI commonly provides e.content as a file-like object.
        for candidate in (content, getattr(obj, "file", None), obj):
            if candidate is None or isinstance(candidate, (str, bytes, bytearray)):
                continue
            read = getattr(candidate, "read", None)
            if callable(read):
                try:
                    seek = getattr(candidate, "seek", None)
                    if callable(seek):
                        seek(0)
                    data = read()
                    if isinstance(data, str):
                        return data.encode("utf-8")
                    if isinstance(data, bytearray):
                        return bytes(data)
                    if isinstance(data, bytes):
                        return data
                except Exception:
                    continue
        return b""

    def import_server_folder(self) -> None:
        folder = Path(str(self.server_folder_input.value or "")).expanduser()
        if not folder.exists():
            ui.notify(f"Folder does not exist: {folder}", type="warning")
            return
        try:
            count = self.tcx_service.import_folder(folder, recursive=True, progress=self.set_status)
            self.reload_activity_table()
            self.update_projected_times()
            ui.notify(f"Imported/updated {count} TCX activities")
        except Exception as e:
            ui.notify(f"Folder import failed: {e}", type="negative", multi_line=True)

    async def recalculate_all_metrics(self) -> None:
        """Recompute metrics without blocking the NiceGUI/uvicorn event loop.

        On Windows, long synchronous callbacks can make the browser/websocket
        disconnect and uvicorn may print WinError 10054. Running the heavy
        recalculation in a worker thread keeps the UI server responsive.
        """
        ui.notify("Metric recalculation started in the background. This can take a while.")
        self.set_status("Metric recalculation running...")
        try:
            count, skipped, messages = await asyncio.to_thread(self._recalculate_all_metrics_worker)
            for msg in messages[-5:]:
                print(msg)
            self.reload_activity_table()
            self.update_projected_times()
            self.set_status(f"Metric recalculation complete: {count} recalculated, {skipped} skipped")
            ui.notify(f"Recalculated {count} activities; skipped {skipped}. Manual Apple VO₂max/weight values were preserved.")
        except (ConnectionResetError, BrokenPipeError) as e:
            # Browser/client disconnected while the background job finished.
            # Data operations are already complete or safely interrupted.
            print(f"Client connection closed during metric recalculation: {e}")
        except Exception as e:
            ui.notify(f"Metric recalculation failed: {e}", type="negative", multi_line=True)

    def _recalculate_all_metrics_worker(self) -> Tuple[int, int, List[str]]:
        """Worker-thread implementation for metric recalculation.

        This intentionally avoids direct NiceGUI element updates from the worker
        thread. It returns a small message list for logging after completion.
        """
        calc = MetricCalculator()
        records = self.db.read_all_activity_records_ordered()
        previous_start: Optional[datetime] = None
        count = 0
        skipped = 0
        messages: List[str] = []
        for rec in records:
            raw_path = Path(str(rec.get("raw_json_path") or ""))
            streams_path = Path(str(rec.get("streams_json_path") or ""))
            if not raw_path.exists() or not streams_path.exists():
                skipped += 1
                continue
            try:
                raw = json.loads(raw_path.read_text(encoding="utf-8"))
                streams = json.loads(streams_path.read_text(encoding="utf-8"))
                summary = raw.get("summary") if isinstance(raw, dict) else None
                detail = raw.get("detail") if isinstance(raw, dict) else None
                if not isinstance(summary, dict):
                    summary = detail if isinstance(detail, dict) else {}
                if not isinstance(detail, dict):
                    detail = summary
                metrics = calc.calculate(summary, detail, streams if isinstance(streams, dict) else {}, zones=None, previous_start=previous_start)
                # Preserve canonical activity id/path/source from DB if raw summary is old/weird.
                metrics.activity_id = str(rec.get("activity_id") or metrics.activity_id)
                source = str(rec.get("source") or "strava_api")
                self.db.upsert_activity(metrics, raw_path, streams_path, source=source)
                try:
                    update_activity_weather_from_archive(self.db, metrics.activity_id)
                except Exception as weather_error:
                    messages.append(f"Weather archive skipped for {metrics.activity_id}: {weather_error}")
                start_str = metrics.start_date_local
                try:
                    previous_start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                except Exception:
                    pass
                count += 1
            except Exception as ex:
                skipped += 1
                messages.append(f"Skipped recalculation for {rec.get('activity_id')}: {ex}")
        return count, skipped, messages


    def fetch_weather_archive(self) -> None:
        try:
            result = update_missing_weather_for_recent_activities(self.db, limit=500, progress=self.set_status)
            self.reload_activity_table()
            ui.notify(
                f"Weather archive lookup complete: {result.get('updated', 0)} updated, "
                f"{result.get('skipped', 0)} skipped, {result.get('failed', 0)} failed."
            )
        except Exception as e:
            ui.notify(f"Weather archive lookup failed: {e}", type="negative", multi_line=True)

    def cleanup_duplicates(self) -> None:
        try:
            result = self.db.cleanup_duplicates()
            self.reload_activity_table()
            removed = result.get("duplicates_removed", 0)
            ui.notify(f"Duplicate cleanup complete: removed {removed} duplicate activities.")
            if removed:
                self.set_status("Duplicate cleanup: " + "; ".join(
                    f"deleted {r.get('deleted_activity_id')} kept {r.get('kept_activity_id')}"
                    for r in result.get("removed", [])[:5]
                ))
            else:
                self.set_status("Duplicate cleanup found no sufficient overlaps.")
        except Exception as e:
            ui.notify(f"Duplicate cleanup failed: {e}", type="negative", multi_line=True)

    def export_xlsx(self) -> None:
        try:
            out = self.config.exports_dir / f"activities_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
            self.db.export_xlsx(out)
            ui.download(str(out))
            self.set_status(f"Exported {out}")
        except Exception as e:
            ui.notify(f"Export failed: {e}", type="negative")

    # ----------------------------- profile, goals, recommendations -----------------------------

    def update_sport_type_options(self) -> None:
        types = self.db.read_sport_types()
        options = ["Auto / none"] + types
        if self.desired_type is not None:
            current = self.desired_type.value if self.desired_type.value in options else "Auto / none"
            self.desired_type.options = options
            self.desired_type.value = current
            self.desired_type.update()

    def save_profile_and_goals(self) -> None:
        try:
            vo2 = _safe_float(self.vo2_input.value, 41.0) or 41.0
            self.db.set_setting("profile_vo2max", vo2)
            if self.profile_birthdate_input is not None:
                self.db.set_setting("profile_birthdate", str(self.profile_birthdate_input.value or "1993-06-16"))
            if self.profile_gender_select is not None:
                self.db.set_setting("profile_gender", str(self.profile_gender_select.value or "Male"))
            if self.profile_height_input is not None:
                self.db.set_setting("profile_height_cm", _safe_float(self.profile_height_input.value, 178.0) or 178.0)
            if self.profile_weight_input is not None:
                self.db.set_setting("profile_current_weight_kg", _safe_float(self.profile_weight_input.value, 75.0) or 75.0)
            if self.aggression_select is not None:
                self.db.set_setting("training_aggressiveness", int(str(self.aggression_select.value or "3")))
            self.db.set_json_setting("training_goals", self.read_goals_from_ui().to_dict())
            self.update_projected_times()
            ui.notify("Saved profile, goals and progression speed")
        except Exception as e:
            ui.notify(f"Could not save profile/goals: {e}", type="negative")

    def read_goals_from_ui(self) -> TrainingGoals:
        goals = TrainingGoals.default()
        for key in ["10k", "hm", "m"]:
            widgets = self.goal_widgets.get(key)
            if not widgets:
                continue
            goals.goals[key].active = bool(widgets["active"].value)
            goals.goals[key].target_minutes = _time_minutes(str(widgets["target"].value or ""))
        return goals

    def update_projected_times(self) -> None:
        try:
            vo2 = _safe_float(self.vo2_input.value if self.vo2_input is not None else None, self.db.get_float_setting("profile_vo2max", 41.0)) or 41.0
            recent = self.db.read_recent_activities(limit=60)
            projected = RacePredictor().project(recent, vo2)
            for key, label in self.projected_labels.items():
                p = projected.get(key, {})
                label.set_text(f"projected: {p.get('text', '—')} ({p.get('source', 'none')})")
        except Exception:
            pass

    def set_recommendation_now(self) -> None:
        now = datetime.now().replace(second=0, microsecond=0)
        self.target_date.value = now.date().isoformat()
        self.target_time.value = f"{now.hour:02d}:{now.minute:02d}"
        self.target_date.update()
        self.target_time.update()

    def give_recommendation(self) -> None:
        try:
            self.save_profile_and_goals()
            target_dt = _parse_datetime(self.target_date.value, self.target_time.value)
            desired = None if self.desired_type.value == "Auto / none" else self.desired_type.value
            profile = self.db.get_profile()
            vo2 = self.db.get_float_setting("profile_vo2max", 41.0)
            engine = RecommendationEngine(
                profile_vo2max=vo2,
                birthdate=str(profile.get("birthdate") or "1993-06-16"),
                gender=str(profile.get("gender") or "Male"),
                weight_kg=_safe_float(profile.get("current_weight_kg"), None),
                height_cm=_safe_float(profile.get("height_cm"), None),
            )
            recent = self.db.read_recent_activities(limit=120, before_iso=target_dt.isoformat(timespec="seconds"))
            rec = engine.recommend(target_dt, recent, desired)
            self.recommendation_markdown.set_content(self.format_recommendation(rec))
            self.set_status("Recommendation generated")
        except Exception as e:
            ui.notify(f"Recommendation failed: {e}", type="negative", multi_line=True)

    def format_recommendation(self, rec: Dict[str, Any]) -> str:
        r = rec.get("recommendation", {}) or {}
        structure = r.get("structure", []) or []
        reasons = rec.get("reasons", []) or []
        lines = [
            f"### {r.get('title', 'Workout recommendation')}",
            f"- **Target:** {rec.get('target_datetime', '')}",
            f"- **Selected type:** {rec.get('selected_sport_type', '')}",
            f"- **Family:** {rec.get('workout_family', '')}",
            f"- **Recovery score:** {_fmt(rec.get('recovery_score_0_100'), 1)} / 100",
            f"- **Duration:** {r.get('duration_min', '')} min",
            f"- **Intensity:** {r.get('target_intensity', '')}",
            "",
            "#### Structure",
        ]
        lines.extend([f"{i}. {s}" for i, s in enumerate(structure, 1)])
        lines.append("\n#### Why")
        lines.extend([f"- {x}" for x in reasons])
        return "\n".join(lines)

    # ----------------------------- calendar planner -----------------------------

    def plan_workout_calendar(self) -> None:
        self.save_profile_and_goals()
        self.current_plan_start_dt = datetime.now().replace(second=0, microsecond=0)
        self._generate_current_plan()

    def _generate_current_plan(self) -> None:
        start_dt = self.current_plan_start_dt or datetime.now().replace(second=0, microsecond=0)
        self.current_plan_start_dt = start_dt
        try:
            vo2 = self.db.get_float_setting("profile_vo2max", 41.0)
            aggressiveness = self.db.get_int_setting("training_aggressiveness", 3)
            planner = WorkoutPlanner(profile_vo2max=vo2, aggressiveness=aggressiveness)
            recent = self.db.read_recent_activities(limit=240, before_iso=(start_dt.replace(hour=23, minute=59, second=59)).isoformat(timespec="seconds"))
            sport_types = self.db.read_sport_types()
            overrides = self.db.read_plan_overrides(start_dt.date().isoformat(), days=14)
            self.current_plan = planner.plan_14_days(start_dt, recent, self.read_goals_from_ui(), sport_types, overrides=overrides)
            self.render_calendar()
            self.render_plan_summary()
            self.update_projected_times()
            self.set_status("14-day workout calendar generated")
        except Exception as e:
            ui.notify(f"Planning failed: {e}", type="negative", multi_line=True)

    def render_plan_summary(self) -> None:
        if not self.current_plan or self.plan_summary_label is None:
            return
        planned = self.current_plan.get("planned_workouts", [])
        baseline = self.current_plan.get("baseline", {})
        run_km = sum(float(w.get("distance_km") or 0.0) for w in planned if _is_run(w.get("sport_type")))
        ride_min = sum(float(w.get("duration_min") or 0.0) for w in planned if _is_ride(w.get("sport_type")))
        total_min = sum(float(w.get("duration_min") or 0.0) for w in planned)
        active_goals = [GOAL_LABELS[k] for k in self.read_goals_from_ui().active_keys()]
        self.plan_summary_label.set_text(
            f"Active goals: {', '.join(active_goals) if active_goals else 'none'} | "
            f"Baseline run volume: {baseline.get('run_km_30d_weekly', 0)} km/week | "
            f"Target: {self.current_plan.get('target_weekly_run_km', 0)} km/week | "
            f"Progression: {self.current_plan.get('aggressiveness_label', 'Balanced')} | "
            f"Effective VO₂max: {_fmt(self.current_plan.get('effective_vo2max'), 1)} | "
            f"Next 14 days: {run_km:.1f} run km, {ride_min:.0f} ride min, {total_min:.0f} total min"
        )

    def render_calendar(self) -> None:
        if self.calendar_container is None:
            return
        self.calendar_container.clear()
        self.planned_by_date = {}
        if not self.current_plan:
            return
        workouts = self.current_plan.get("planned_workouts", [])
        for w in workouts:
            key = str(w.get("date"))
            self.planned_by_date[key] = w
            with self.calendar_container:
                with ui.card().classes("calendar-card p-2").on("click", lambda e, k=key: self.open_plan_editor(k)):
                    ui.label(self._calendar_title(w)).classes("font-semibold")
                    ui.label(self._calendar_body(w)).classes("text-sm mono")

    def _calendar_title(self, w: Dict[str, Any]) -> str:
        locked = " [manual]" if w.get("locked") else ""
        return f"{w.get('weekday', '')} {w.get('date', '')} {w.get('start_time', '')}{locked}"

    def _calendar_body(self, w: Dict[str, Any]) -> str:
        if w.get("no_workout"):
            return "No workout\nRest / mobility"
        dist = float(w.get("distance_km") or 0.0)
        dist_text = f"{dist:.1f} km / " if dist > 0 else ""
        pace_or_power = str(w.get("pace") or w.get("wattage") or "")
        notes = str(w.get("notes") or "")
        short = ""
        if w.get("family") in {"quality", "tempo", "bike_intervals"} and notes:
            short = "\n" + notes.split(". ")[0][:80]
        return f"{w.get('sport_type', '')}\n{w.get('title', '')}\n{dist_text}{w.get('duration_min', 0)} min\n{w.get('zone', '')}\n{pace_or_power}{short}"

    def open_plan_editor(self, plan_date: str) -> None:
        w = self.planned_by_date.get(plan_date)
        if not w:
            return
        self.selected_plan_date = plan_date
        self.edit_date.set_text(plan_date)
        self.edit_no_workout.value = bool(w.get("no_workout"))
        self.edit_title.value = str(w.get("title") or "")
        sport_options = sorted(set(self.db.read_sport_types() + ["Run", "Ride", "VirtualRide", "Walk", "Hike"]))
        self.edit_sport_type.options = sport_options
        self.edit_sport_type.value = str(w.get("sport_type") or "Run")
        self.edit_family.value = str(w.get("family") or "easy_aerobic")
        self.edit_time.value = str(w.get("start_time") or "18:00")
        self.edit_duration.value = float(w.get("duration_min") or 0.0)
        self.edit_distance.value = float(w.get("distance_km") or 0.0)
        self.edit_zone.value = str(w.get("zone") or "")
        self.edit_pace.value = str(w.get("pace") or "")
        self.edit_wattage.value = str(w.get("wattage") or "")
        self.edit_notes.value = str(w.get("notes") or "")
        self._adapt_edit_fields()
        self.edit_dialog.open()

    def _adapt_edit_fields(self) -> None:
        no = bool(self.edit_no_workout.value)
        sport = self.edit_sport_type.value
        ride = _is_ride(sport)
        run = _is_run(sport)
        hike = _is_hike(sport)
        strength = _is_strength(sport)
        # NiceGUI dynamic disabling/labels. Values are still validated at save.
        self.edit_distance.set_visibility(not no)
        self.edit_zone.set_visibility(not no)
        self.edit_pace.set_visibility((not no) and (run or hike))
        self.edit_wattage.set_visibility((not no) and ride)
        if no:
            self.edit_family.value = "rest_or_mobility"
            self.edit_title.value = self.edit_title.value or "Rest / mobility"
        elif strength:
            self.edit_pace.value = ""
            self.edit_wattage.value = "bodyweight/light resistance"
            if self.edit_family.value not in {"strength_prehab", "strength_general"}:
                self.edit_family.value = "strength_prehab"
        elif hike:
            self.edit_wattage.value = ""
            if not self.edit_pace.value:
                self.edit_pace.value = "comfortable hiking pace"
            if self.edit_family.value in {"quality", "tempo", "long_run", "endurance_ride"}:
                self.edit_family.value = "long_hike"
            elif self.edit_family.value in {"strength_prehab", "strength_general", "rest_or_mobility"}:
                self.edit_family.value = "easy_hike"
        elif ride:
            if not self.edit_wattage.value:
                self.edit_wattage.value = "Z2 / comfortable endurance power"
            self.edit_pace.value = ""
            if self.edit_family.value in {"quality", "tempo", "long_run"}:
                self.edit_family.value = "endurance_ride"
        elif run:
            self.edit_wattage.value = ""
            if not self.edit_pace.value:
                self.edit_pace.value = "easy / goal-specific"
        for w in [self.edit_family, self.edit_title, self.edit_pace, self.edit_wattage]:
            w.update()



    def _fallback_planned_workout_for_type(self, old: Dict[str, Any], sport: str, day_dt: datetime) -> Dict[str, Any]:
        """Local fallback for type-change auto-fill.

        This guarantees that changing Strength -> Hike, Run -> Ride, etc. always
        produces usable recommendations even if the planner adaptation path fails.
        """
        family_old = str(old.get("family") or "easy_aerobic")
        sport_norm = _norm_sport_type(sport)
        old_dur = _safe_float(old.get("duration_min"), None)
        old_dist = _safe_float(old.get("distance_km"), None)

        def base(sport_type: str, family: str, title: str, dur: int, dist: float, zone: str, pace: str, wattage: str, notes: str) -> Dict[str, Any]:
            return {
                "date": day_dt.date().isoformat(),
                "weekday": day_dt.strftime("%a"),
                "start_time": day_dt.strftime("%H:%M"),
                "sport_type": sport_type,
                "family": family,
                "title": title,
                "duration_min": int(max(0, round(float(dur or 0)))),
                "distance_km": round(float(dist or 0.0), 1),
                "zone": zone,
                "pace": pace,
                "wattage": wattage,
                "notes": notes,
                "no_workout": False,
                "locked": True,
                "source": "manual_type_change_fallback_auto_fill",
            }

        # Preserve training intent roughly by using the old family.
        quality_like = family_old in {"quality", "tempo", "bike_intervals", "hilly_hike", "steady_progression"}
        long_like = family_old in {"long_run", "endurance_ride", "long_hike"}
        recovery_like = family_old in {"recovery_aerobic", "recovery_hike", "rest_or_mobility"}

        if sport_norm in {"none", "rest", "off"}:
            return {
                "date": day_dt.date().isoformat(),
                "weekday": day_dt.strftime("%a"),
                "start_time": day_dt.strftime("%H:%M"),
                "sport_type": "None",
                "family": "rest_or_mobility",
                "title": "Rest / mobility",
                "duration_min": 0,
                "distance_km": 0.0,
                "zone": "Rest",
                "pace": "",
                "wattage": "",
                "notes": "No endurance workout. Optional 15-20 min walk or gentle mobility.",
                "no_workout": True,
                "locked": True,
                "source": "manual_type_change_fallback_auto_fill",
            }

        if _is_hike(sport):
            if long_like:
                dur = int(max(90, min(180, old_dur * 1.25 if old_dur else 110)))
                dist = max(6.0, min(16.0, dur / 11.0))
                return base(sport, "long_hike", "Long aerobic hike", dur, dist, "Mostly Z1-Z2; short Z3 uphill allowed", "terrain-dependent", "", "Low-impact endurance replacement. Keep climbs controlled; avoid turning every hill into threshold.")
            if quality_like:
                dur = int(max(60, min(120, old_dur * 1.10 if old_dur else 80)))
                dist = max(4.5, min(11.0, dur / 11.5))
                return base(sport, "hilly_hike", "Hilly aerobic hike", dur, dist, "Z2 with controlled Z3 uphill", "brisk uphill / easy downhill", "", "15 min easy, then 3-5 sustained uphill segments at strong but controlled effort. No running required.")
            if recovery_like:
                dur = int(max(35, min(75, old_dur * 1.20 if old_dur else 50)))
                dist = max(2.5, min(6.0, dur / 12.0))
                return base(sport, "recovery_hike", "Recovery hike", dur, dist, "Z1-low Z2", "comfortable walking pace", "", "Easy low-impact recovery. Keep it genuinely easy.")
            dur = int(max(55, min(100, old_dur * 1.15 if old_dur else 70)))
            dist = max(4.0, min(9.0, dur / 12.0))
            return base(sport, "easy_hike", "Easy aerobic hike", dur, dist, "Mostly Z2", "comfortable/brisk walk", "", "Aerobic hiking volume to support general fitness without the same running impact.")

        if _is_strength(sport):
            if quality_like or long_like:
                dur = int(max(35, min(60, old_dur * 0.65 if old_dur else 45)))
                return base(sport, "strength_general", "Running-specific strength", dur, 0.0, "Moderate; not HR-driven", "", "controlled strength work", "Squat/lunge pattern, hinge, calf raises, step-ups/split squats, and core. Keep 2-3 reps in reserve.")
            dur = int(max(25, min(45, old_dur * 0.65 if old_dur else 30)))
            return base(sport, "strength_prehab", "Core / prehab strength", dur, 0.0, "Easy-moderate; not HR-driven", "", "bodyweight/light resistance", "Calves, glute medius, hamstrings, side plank, dead bug, and painless split-squat pattern. Should not create heavy soreness.")

        if _is_ride(sport):
            if long_like:
                dur = int(max(75, min(150, old_dur * 1.25 if old_dur else 90)))
                dist = max(18.0, min(55.0, dur * 0.38))
                return base(sport, "endurance_ride", "Long aerobic virtual ride", dur, dist, "Mostly Z2", "", "comfortable endurance power", "Low-impact endurance replacement. Smooth cadence; do not grind.")
            if quality_like:
                dur = int(max(45, min(90, old_dur * 1.05 if old_dur else 60)))
                dist = max(12.0, min(38.0, dur * 0.38))
                return base(sport, "bike_intervals", "Quality virtual ride", dur, dist, "Z3 blocks, short Z4 only if planned", "", "controlled tempo / sweet spot", "Warm-up, then controlled tempo intervals. Lower impact than running but still a hard session.")
            if recovery_like:
                dur = int(max(30, min(65, old_dur * 1.10 if old_dur else 40)))
                dist = max(8.0, min(25.0, dur * 0.35))
                return base(sport, "recovery_aerobic", "Recovery spin", dur, dist, "Z1-Z2", "", "very easy spin", "Low-force recovery spin. High comfort, no grinding.")
            dur = int(max(40, min(85, old_dur * 1.15 if old_dur else 55)))
            dist = max(10.0, min(32.0, dur * 0.37))
            return base(sport, "cross_training_ride", "Easy virtual ride", dur, dist, "Mostly Z2", "", "endurance power, steady cadence", "Aerobic support without running impact.")

        if _is_run(sport):
            if long_like:
                dist = max(7.0, min(16.0, old_dist if old_dist and old_dist > 0 else 9.0))
                dur = int(round(dist * 7.5))
                return base(sport, "long_run", "Long easy run", dur, dist, "Z2; avoid Z4/Z5", "easy conversational", "", "Main stamina session. Distance matters more than speed.")
            if quality_like:
                dist = max(5.0, min(10.0, old_dist if old_dist and old_dist > 0 else 6.5))
                dur = int(round(dist * 7.0))
                return base(sport, "quality", "Quality run", dur, dist, "Z3-Z4 during work blocks", "controlled hard, not all-out", "", "Warm-up, controlled intervals/tempo, cool-down. Stop if HR is unusually high.")
            if recovery_like:
                dist = max(3.0, min(6.0, old_dist if old_dist and old_dist > 0 else 4.0))
                dur = int(round(dist * 8.0))
                return base(sport, "recovery_aerobic", "Recovery run", dur, dist, "Z1-low Z2", "very easy", "", "Easy enough that HR remains controlled. Walk breaks allowed.")
            dist = max(4.0, min(9.0, old_dist if old_dist and old_dist > 0 else 5.5))
            dur = int(round(dist * 7.7))
            return base(sport, "easy_aerobic", "Easy aerobic run", dur, dist, "Mostly Z2", "easy conversational", "", "Build aerobic consistency. HR/effort overrides pace.")

        dur = int(old_dur or 45)
        return base(sport, "easy_aerobic", "Easy aerobic workout", dur, 0.0, "Z1-Z2", "", "", "Generic aerobic work. Keep it conversational.")


    def recalculate_edit_recommendation_for_type(self) -> None:
        """Auto-fill recommendation fields when the planned sport type changes.

        This is intentionally *not* a dumb manual override. It rebuilds title,
        duration, distance, zone, pace/power and notes for the selected type.
        """
        try:
            if not self.selected_plan_date:
                self._adapt_edit_fields()
                return

            old = dict(self.planned_by_date.get(self.selected_plan_date) or {})
            sport = str(self.edit_sport_type.value or old.get("sport_type") or "Run")
            no = bool(self.edit_no_workout.value) or _norm_sport_type(sport) in {"none", "rest", "off"}

            time_value = str(self.edit_time.value or old.get("start_time") or "18:00")
            try:
                day_dt = _parse_datetime(self.selected_plan_date, time_value)
            except Exception:
                day_dt = datetime.fromisoformat(f"{self.selected_plan_date}T18:00:00")

            if no:
                adapted = self._fallback_planned_workout_for_type(old, "None", day_dt)
            else:
                adapted = None
                try:
                    vo2 = self.db.get_float_setting("profile_vo2max", 41.0)
                    aggressiveness = self.db.get_int_setting("training_aggressiveness", 3)
                    planner = WorkoutPlanner(profile_vo2max=vo2, aggressiveness=aggressiveness)
                    recent = self.db.read_recent_activities(
                        limit=240,
                        before_iso=(day_dt.replace(hour=23, minute=59, second=59)).isoformat(timespec="seconds"),
                    )
                    sport_types = self.db.read_sport_types()
                    adapted = planner.adapt_workout_to_sport_type(
                        old,
                        sport,
                        day_dt,
                        recent,
                        self.read_goals_from_ui(),
                        sport_types,
                    )
                except Exception:
                    adapted = None

                # Fallback is deliberately used if planner returns incomplete values.
                if (
                    not adapted
                    or not str(adapted.get("title") or "").strip()
                    or (not adapted.get("no_workout") and int(float(adapted.get("duration_min") or 0)) <= 0)
                    or (_is_hike(sport) and float(adapted.get("distance_km") or 0.0) <= 0.0)
                ):
                    adapted = self._fallback_planned_workout_for_type(old, sport, day_dt)

            self.edit_title.value = str(adapted.get("title") or "Planned workout")
            self.edit_family.value = str(adapted.get("family") or "easy_aerobic")
            self.edit_duration.value = float(adapted.get("duration_min") or 0.0)
            self.edit_distance.value = float(adapted.get("distance_km") or 0.0)
            self.edit_zone.value = str(adapted.get("zone") or "")
            self.edit_pace.value = str(adapted.get("pace") or "")
            self.edit_wattage.value = str(adapted.get("wattage") or "")
            self.edit_notes.value = str(adapted.get("notes") or "")
            self.edit_no_workout.value = bool(adapted.get("no_workout"))

            for widget in [
                self.edit_title, self.edit_family, self.edit_duration, self.edit_distance,
                self.edit_zone, self.edit_pace, self.edit_wattage, self.edit_notes,
                self.edit_no_workout,
            ]:
                try:
                    widget.update()
                except Exception:
                    pass

            self._adapt_edit_fields()
            ui.notify(f"Auto-filled {sport} recommendation", type="positive")
        except Exception as e:
            self._adapt_edit_fields()
            ui.notify(f"Could not auto-recalculate workout type: {e}", type="warning", multi_line=True)


    def save_selected_plan_workout(self) -> None:
        if not self.selected_plan_date:
            return
        try:
            no = bool(self.edit_no_workout.value)
            sport = str(self.edit_sport_type.value or "Run")
            family = str(self.edit_family.value or ("rest_or_mobility" if no else "easy_aerobic"))
            if no:
                sport = "None"
                family = "rest_or_mobility"
            override = {
                "date": self.selected_plan_date,
                "weekday": date.fromisoformat(self.selected_plan_date).strftime("%a"),
                "start_time": str(self.edit_time.value or "18:00"),
                "title": str(self.edit_title.value or ("No workout" if no else "Manual workout")),
                "sport_type": sport,
                "family": family,
                "duration_min": int(float(self.edit_duration.value or 0.0)) if not no else 0,
                "distance_km": float(self.edit_distance.value or 0.0) if (not no and (_is_run(sport) or _is_hike(sport) or _is_ride(sport))) else 0.0,
                "zone": str(self.edit_zone.value or "") if not no else "Rest",
                "pace": str(self.edit_pace.value or "") if (not no and (_is_run(sport) or _is_hike(sport))) else "",
                "wattage": str(self.edit_wattage.value or "") if (not no and (_is_ride(sport) or _is_strength(sport))) else "",
                "notes": str(self.edit_notes.value or ""),
                "no_workout": no,
                "locked": True,
                "source": "manual_override",
            }
            self.db.upsert_plan_override(self.selected_plan_date, override)
            self.edit_dialog.close()
            self._generate_current_plan()
            ui.notify("Manual override saved; following plan recalculated")
        except Exception as e:
            ui.notify(f"Could not save planned workout: {e}", type="negative", multi_line=True)

    def clear_selected_plan_override(self) -> None:
        if not self.selected_plan_date:
            return
        self.db.delete_plan_override(self.selected_plan_date)
        self.edit_dialog.close()
        self._generate_current_plan()
        ui.notify("Manual override cleared")


    async def analyze_recent_trainings(self) -> None:
        try:
            if self.analysis_dialog is not None:
                self.analysis_dialog.open()
            if self.analysis_markdown is not None:
                self.analysis_markdown.set_content("Analyzing last 35 trainings…")
            self.set_status("Analyzing recent trainings…")

            backend_label = (
                str(self.analysis_backend_select.value)
                if self.analysis_backend_select is not None and self.analysis_backend_select.value
                else "Deterministic only"
            )
            if backend_label == "Local Ollama":
                llm_backend = "ollama"
                default_model = "qwen3:8b"
            else:
                llm_backend = "deterministic"
                default_model = "qwen3:8b"

            model = str(self.analysis_model_input.value or default_model) if self.analysis_model_input is not None else default_model

            result = await asyncio.to_thread(
                analyze_recent_trainings,
                self.db,
                35,
                False,
                model,
                180,
                llm_backend,
            )
            if self.analysis_markdown is not None:
                self.analysis_markdown.set_content(result.get("summary_markdown") or "No analysis generated.")
            self.set_status("Analysis complete")
        except Exception as e:
            if self.analysis_markdown is not None:
                self.analysis_markdown.set_content(f"Analysis failed: {e}")
            ui.notify(f"Analysis failed: {e}", type="negative", multi_line=True)

    # ----------------------------- Strava auth/settings -----------------------------

    def open_strava_authorization(self) -> None:
        try:
            ui.navigate.to(self.auth.authorization_url(force=True), new_tab=True)
        except Exception as e:
            ui.notify(f"Could not open Strava authorization: {e}", type="negative")

    def refresh_strava_token(self) -> None:
        try:
            token = self.auth.get_valid_token()
            self.auth.refresh_token(token.refresh_token)
            self.update_token_status()
            ui.notify("Strava token refreshed")
        except Exception as e:
            ui.notify(f"Could not refresh token: {e}", type="negative", multi_line=True)

    def update_token_status(self) -> None:
        if self.token_label is None:
            return
        try:
            token = self.auth.store.load()
            if token is None:
                self.token_label.set_text("No Strava token stored. Click Connect Strava.")
            else:
                h = token.expires_in_s / 3600.0
                self.token_label.set_text(f"Strava token stored. Access token expires in {h:.1f} h. Refresh token is stored locally and is used automatically.")
        except Exception as e:
            self.token_label.set_text(f"Token status error: {e}")

    # ----------------------------- small helpers -----------------------------

    def set_status(self, message: str) -> None:
        if self.status_label is not None:
            self.status_label.set_text(str(message))
        print(message)

    def exit_app(self) -> None:
        self.set_status("Closing WorkOutBuddy...")
        ui.timer(0.2, app.shutdown, once=True)


def create_app_instance(config: Optional[AppConfig] = None) -> WorkOutBuddyWeb:
    config = config or AppConfig.load()
    app.add_static_files("/maps", str(config.maps_dir))
    app.add_static_files("/exports", str(config.exports_dir))
    web = WorkOutBuddyWeb(config)


    @app.get("/api/activity_map/{activity_id}")
    def activity_map(activity_id: str, metric: str = "pace_min_km"):
        try:
            return HTMLResponse(web.make_activity_map_document(activity_id, metric_key=metric), headers={"Cache-Control": "no-store"})
        except Exception as e:
            return HTMLResponse(web._route_message_document("Route map error", str(e)), status_code=500, headers={"Cache-Control": "no-store"})

    @app.get("/api/activity_profile/{activity_id}")
    def activity_profile(activity_id: str, metric: str = "pace_min_km", x_axis: str = "distance_km"):
        try:
            return HTMLResponse(web.make_activity_profile_document(activity_id, metric_key=metric, x_axis=x_axis), headers={"Cache-Control": "no-store"})
        except Exception as e:
            return HTMLResponse(web._route_message_document("Workout profile error", str(e)), status_code=500, headers={"Cache-Control": "no-store"})

    @app.post("/api/tcx_upload")
    async def tcx_upload(files: List[UploadFile] = File(...)):
        """Robust TCX upload endpoint independent of NiceGUI upload events.

        The browser posts multipart/form-data directly to FastAPI. This avoids
        version-specific NiceGUI UploadEventArguments attributes such as e.name
        or e.content. Duplicate handling and TCX-vs-Strava preference are handled
        by TCXImportService/database logic downstream.
        """
        saved_paths: List[Path] = []
        errors: List[str] = []

        for upload in files:
            original_name = upload.filename or f"uploaded_{int(time.time())}.tcx"
            try:
                data = await upload.read()
            except Exception as ex:
                errors.append(f"{original_name}: could not read upload ({ex})")
                continue

            if not data:
                errors.append(f"{original_name}: empty file")
                continue

            # Accept TCX by suffix or XML content sniffing, because browsers often
            # send generic MIME types for .tcx files.
            looks_like_tcx = (
                original_name.lower().endswith(".tcx")
                or b"TrainingCenterDatabase" in data[:4096]
                or b"<Activity" in data[:4096]
            )
            if not looks_like_tcx:
                errors.append(f"{original_name}: does not look like TCX")
                continue

            safe_name = _safe_filename(original_name)
            if not safe_name.lower().endswith(".tcx"):
                safe_name += ".tcx"

            path = web.config.upload_dir / safe_name
            if path.exists():
                path = web.config.upload_dir / f"{path.stem}_{int(time.time() * 1000)}{path.suffix}"

            try:
                path.write_bytes(data)
                saved_paths.append(path)
            except Exception as ex:
                errors.append(f"{original_name}: could not save file ({ex})")

        imported = 0
        if saved_paths:
            try:
                imported = web.tcx_service.import_files(saved_paths, progress=web.set_status)
            except Exception as ex:
                errors.append(f"TCX parser/import failed: {ex}")

        details = "".join(f"<li>{e}</li>" for e in errors) or "<li>No errors.</li>"
        return HTMLResponse(
            "<html><body>"
            f"<h2>TCX upload complete</h2>"
            f"<p>Saved files: {len(saved_paths)}<br>Imported/updated activities: {imported}</p>"
            f"<h3>Messages</h3><ul>{details}</ul>"
            "<p><a href='/'>Return to WorkOutBuddy</a></p>"
            "</body></html>"
        )

    @app.get("/strava/callback")
    def strava_callback(code: Optional[str] = None, scope: Optional[str] = "", error: Optional[str] = None):
        if error:
            return HTMLResponse(f"<h2>Strava authorization failed</h2><p>{error}</p>", status_code=400)
        if not code:
            return HTMLResponse("<h2>Missing Strava code</h2>", status_code=400)
        try:
            web.auth.exchange_code(code, accepted_scope=scope or "")
            return HTMLResponse(
                "<html><body><h2>Strava authorization complete.</h2>"
                "<p>You can close this tab and return to WorkOutBuddy Web.</p>"
                "<p><a href='/'>Return to app</a></p></body></html>"
            )
        except Exception as e:
            return HTMLResponse(f"<h2>Token exchange failed</h2><pre>{e}</pre>", status_code=500)

    @ui.page("/")
    def index() -> None:
        web.build()

    return web


def run_app(config: AppConfig, *, native: bool, title: str, window_size: Optional[Tuple[int, int]] = None) -> None:
    create_app_instance(config)
    ui.run(
        host=config.web_host,
        port=config.web_port,
        title=title,
        reload=False,
        show=False,
        native=native,
        window_size=window_size,
        uvicorn_logging_level="warning",
    )


def main() -> None:
    config = AppConfig.load("web")
    run_app(config, native=False, title="WorkOutBuddy Web")


def desktop_main() -> None:
    config = AppConfig.load("desktop")
    run_app(config, native=True, title="WorkOutBuddy", window_size=(1500, 950))


def _is_hike(sport_type: Any) -> bool:
    return _norm_sport_type(sport_type) in {"hike", "hiking", "walk", "walking", "trek", "trekking"}

def _is_strength(sport_type: Any) -> bool:
    return _norm_sport_type(sport_type) in {"strength", "strengthtraining", "strength_training", "bodyweightstrength", "bodyweight_strength", "gym", "weights", "workout"}
