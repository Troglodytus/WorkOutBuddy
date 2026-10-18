(() => {
"use strict";

const cfg = window.WORKOUTBUDDY_CONFIG || {};
const configured = cfg.SUPABASE_URL && cfg.SUPABASE_PUBLISHABLE_KEY &&
  !cfg.SUPABASE_URL.includes("YOUR_PROJECT_REF") &&
  !cfg.SUPABASE_PUBLISHABLE_KEY.includes("YOUR_SUPABASE");

let client = null;
let session = null;
let activities = [];
let profile = null;
let selectedActivityId = null;
let selectedActivityIds = [];
let workoutComparisonRequest = 0;
let routeMaps = {history:null, home:null};
let viewerStates = {history:null, home:null};
let currentPlan = [];
let duplicateResolver = null;
let aiHistory = [];
let metricsEnrichmentRunning = false;
let mlModelReport = null;
let mlModelSignature = null;
let mlAnalysisBusy = false;
const streamCache = new Map();

const el = id => document.getElementById(id);
const qs = sel => document.querySelector(sel);
const qsa = sel => Array.from(document.querySelectorAll(sel));

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}
function n(value, fallback = null) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return fallback;
  const x = Number(value);
  return Number.isFinite(x) ? x : fallback;
}
function clamp(x, lo, hi) { return Math.max(lo, Math.min(hi, x)); }
function sum(xs) { return xs.reduce((a,b) => a + (Number.isFinite(b) ? b : 0), 0); }
function mean(xs) { const a = xs.filter(Number.isFinite); return a.length ? sum(a) / a.length : null; }
function median(xs) {
  const a = xs.filter(Number.isFinite).slice().sort((x,y) => x-y);
  if (!a.length) return null;
  const m = Math.floor(a.length / 2);
  return a.length % 2 ? a[m] : (a[m-1] + a[m]) / 2;
}
function fmt(x, digits = 1, suffix = "") {
  const v = n(x);
  return v == null ? "—" : v.toFixed(digits) + suffix;
}
function fmtPace(x) {
  const v = n(x);
  if (v == null || v <= 0) return "—";
  let m = Math.floor(v), s = Math.round((v-m)*60);
  if (s >= 60) { m += 1; s -= 60; }
  return m + ":" + String(s).padStart(2,"0") + "/km";
}
function localDate(value) {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? String(value).slice(0,10) :
    new Intl.DateTimeFormat(undefined,{day:"2-digit",month:"short",year:"numeric"}).format(d);
}
function shortDay(value) {
  const d = new Date(value);
  return new Intl.DateTimeFormat(undefined,{weekday:"short",day:"2-digit",month:"short"}).format(d);
}
function daysBetween(a,b) { return (new Date(a).getTime() - new Date(b).getTime()) / 86400000; }
function nowMs() { return Date.now(); }
function daysAgo(days) { return new Date(nowMs() - days * 86400000); }
function normalizeSport(s) {
  const x = String(s || "").toLowerCase().replace(/[^a-z0-9]/g,"");
  if (x.includes("run")) return "run";
  if (x.includes("ride") || x.includes("bike") || x.includes("cycling") || x.includes("ergo")) return "bike";
  if (x.includes("strength") || x.includes("weight")) return "strength";
  if (x.includes("hike") || x.includes("walk")) return "hike";
  return "other";
}
function derivedLoad(a) {
  const direct = n(a.training_load_score) ?? n(a.trimp_score);
  if (direct != null) return direct;
  const mins = (n(a.moving_time_s) ?? n(a.duration_s) ?? 0) / 60;
  const hard = n(a.hard_zone_fraction,0);
  const easy = n(a.easy_zone_fraction,0);
  const intensity = 1 + easy * .25 + hard * 1.4;
  return Math.round(mins * intensity * 10) / 10;
}
function distanceKm(a) { return (n(a.distance_m,0) / 1000); }
function durationMin(a) { return (n(a.moving_time_s) ?? n(a.duration_s,0)) / 60; }
function toast(message, ms = 3000) {
  const t = el("toast"); t.textContent = message; t.classList.remove("hidden");
  window.setTimeout(() => t.classList.add("hidden"), ms);
}
function setSync(message) { el("syncStatus").textContent = message; }
function safeJson(value, fallback) {
  if (value == null) return fallback;
  if (typeof value === "object") return value;
  try { return JSON.parse(value); } catch (_) { return fallback; }
}
function userId() { return session && session.user ? session.user.id : null; }

function defaultProfile() {
  return {
    user_id: userId(),
    display_name: "",
    birthdate: null,
    height_cm: 178,
    current_weight_kg: 75,
    profile_vo2max: 45,
    hr_zones: {z1_max:130,z2_max:150,z3_max:165,z4_max:178,z5_max:220},
    goals: {primary_goal:"general",goal_date:null,target_minutes:null},
    training_preferences: {weekly_run_days:3,strength_sessions:2,long_run_day:6,training_aggressiveness:3,manual_plan_overrides:{}}
  };
}

async function boot() {
  bindUi();
  if (!configured) {
    el("setupHint").classList.remove("hidden");
    el("loginStatus").textContent = "Configure docs/config.js first.";
    return;
  }
  client = window.supabase.createClient(cfg.SUPABASE_URL, cfg.SUPABASE_PUBLISHABLE_KEY, {
    auth: {persistSession:true, autoRefreshToken:true, detectSessionInUrl:true}
  });
  const res = await client.auth.getSession();
  session = res.data.session;
  client.auth.onAuthStateChange((_event, newSession) => {
    session = newSession;
    if (session) enterApp(); else leaveApp();
  });
  if (session) await enterApp(); else leaveApp();
}

function bindUi() {
  el("loginForm").addEventListener("submit", onLogin);
  el("logoutButton").addEventListener("click", async () => { if (client) await client.auth.signOut(); });
  qsa("[data-view]").forEach(b => b.addEventListener("click", () => openView(b.dataset.view)));
  qsa("[data-view-link]").forEach(b => b.addEventListener("click", () => openView(b.dataset.viewLink)));
  el("uploadTopButton").addEventListener("click", openUpload);
  el("uploadHomeButton").addEventListener("click", openUpload);
  el("importFilesButton").addEventListener("click", importSelectedFiles);
  el("historySportFilter").addEventListener("change", renderHistory);
  el("historyPeriodFilter").addEventListener("change", renderHistory);
  ["historyXMetric","historyYMetric","historyPlotType","historyColorBy"].forEach(id => el(id).addEventListener("change", renderHistory));
  el("fullStatisticsToggle").addEventListener("change", renderHistory);
  el("refreshWeatherButton").addEventListener("click",()=>refreshAllWeather(false));
  el("clearWorkoutComparison").addEventListener("click",()=>{
    selectedActivityIds=[];selectedActivityId=null;workoutComparisonRequest++;
    renderWorkoutTable(filteredActivities());
    renderComparisonSelection();
    el("workoutDetail").textContent="Select a workout from the table.";
    el("historyWorkoutMetrics").innerHTML="";
    el("routeMap").classList.add("hidden");
    el("streamPlot").classList.add("hidden");
    el("historyRouteLegend").classList.add("hidden");
    el("historyCursorReadout").classList.add("hidden");
    if(routeMaps.history){routeMaps.history.remove();routeMaps.history=null;}
    Plotly.purge("streamPlot");
  });
  initWorkoutViewerControls();
  el("regeneratePlanButton").addEventListener("click", recalculateAndPersistPlan);
  el("plannerRegenerateButton").addEventListener("click", recalculateAndPersistPlan);
  el("plannerAiButton").addEventListener("click", askAiPlanner);
  el("raceTrendDistance").addEventListener("change",renderRaceTrend);
  el("planEditForm").addEventListener("submit", savePlanOverride);
  el("planEditCloseButton").addEventListener("click",()=>el("planEditDialog").close());
  el("planEditCancelButton").addEventListener("click",()=>el("planEditDialog").close());
  el("planEditClearButton").addEventListener("click",clearPlanOverride);
  el("planEditSport").addEventListener("change",adaptPlanEditorForSport);
  el("planEditRest").addEventListener("change",adaptPlanEditorForSport);
  el("duplicateSkipButton").addEventListener("click",()=>resolveDuplicateDecision("skip"));
  el("duplicateOverwriteButton").addEventListener("click",()=>resolveDuplicateDecision("overwrite"));
  el("duplicateDialog").addEventListener("cancel",e=>{e.preventDefault();resolveDuplicateDecision("skip");});
  el("aiCoachButton").addEventListener("click", askAiCoach);
  el("mlRecalculateButton").addEventListener("click",async()=>{
    el("mlRecalculateButton").disabled=true;
    try{await computeMlModelReport(true);await applyPersonalFatigueEfficiency();renderHistory();renderAnalysis();}
    catch(err){el("mlAnalysisStatus").textContent="ML recalculation failed: "+(err.message||String(err));}
    finally{el("mlRecalculateButton").disabled=false;}
  });
  el("settingsButton").addEventListener("click", openSettings);
  el("settingsCloseButton").addEventListener("click", () => el("settingsDialog").close());
  el("settingsCancelButton").addEventListener("click", () => el("settingsDialog").close());
  el("settingsForm").addEventListener("submit", saveSettings);
}

async function onLogin(event) {
  event.preventDefault();
  if (!client) return;
  el("loginStatus").textContent = "Signing in…";
  const password = el("passwordInput").value;
  const result = await client.auth.signInWithPassword({email:cfg.LOGIN_EMAIL,password});
  if (result.error) {
    el("loginStatus").textContent = result.error.message;
  } else {
    el("passwordInput").value = "";
    el("loginStatus").textContent = "";
  }
}

function leaveApp() {
  el("loginView").classList.remove("hidden");
  el("appView").classList.add("hidden");
}
async function enterApp() {
  el("loginView").classList.add("hidden");
  el("appView").classList.remove("hidden");
  setSync("Loading…");
  await Promise.all([loadProfile(), loadActivities(), loadAiHistory(), loadLatestTrainingPlan()]);
  renderAll();
  setSync("Synced");
  enrichMetricsInBackground();
}

function openView(name) {
  qsa(".tab").forEach(b => b.classList.toggle("active", b.dataset.view === name));
  qsa(".view").forEach(v => v.classList.remove("active"));
  const target = el(name + "View");
  if (target) target.classList.add("active");
  if (name === "history") renderHistory();
  if (name === "planner") renderPlanner();
  if (name === "analysis") {renderAnalysis();renderMlAnalysis();}
}

async function loadProfile() {
  if (!userId()) return;
  const res = await client.from("profiles").select("*").eq("user_id",userId()).maybeSingle();
  if (res.error) { console.error(res.error); profile = defaultProfile(); return; }
  if (res.data) {
    profile = res.data;
    profile.hr_zones = safeJson(profile.hr_zones,{});
    profile.goals = safeJson(profile.goals,{});
    profile.training_preferences = safeJson(profile.training_preferences,{});
  } else {
    profile = defaultProfile();
    const ins = await client.from("profiles").upsert(profile).select().single();
    if (!ins.error) profile = ins.data;
  }
}


const INHERITED_VITAL_FIELDS=["apple_vo2max","body_weight_kg"];
function inheritedVitalIsValid(field,value){
  const v=n(value);
  return v!=null&&(field==="apple_vo2max"?v>=15&&v<=90:v>=30&&v<=250);
}
async function inheritLatestWorkoutVitals(){
  if(!client||!userId()||!activities.length)return 0;
  // Ascending chronological order: never borrow from a future workout.
  const ordered=activities.slice().sort((a,b)=>new Date(a.start_time)-new Date(b.start_time));
  const latest={},changes=[];
  for(const activity of ordered){
    const mj=safeJson(activity.metrics_json,{});
    const provenance={...(safeJson(mj.inherited_workout_values,{})||{})};
    const update={};let modified=false;
    for(const field of INHERITED_VITAL_FIELDS){
      const existing=n(activity[field]),auto=provenance[field];
      if(inheritedVitalIsValid(field,existing)&&!auto){
        latest[field]={value:existing,source_id:activity.id};
        continue;
      }
      const source=latest[field];
      if(source&&(!inheritedVitalIsValid(field,existing)||!!auto)&&
        (existing!==source.value||!auto||auto.source_id!==source.source_id)){
        update[field]=source.value;activity[field]=source.value;
        provenance[field]={source_id:source.source_id};modified=true;
      }else if(!source&&auto){
        update[field]=null;activity[field]=null;delete provenance[field];modified=true;
      }
      // Inherited values are displayed, but NEVER become independent measurement sources.
    }
    if(modified){
      update.metrics_json={...mj,inherited_workout_values:provenance};
      activity.metrics_json=update.metrics_json;
      changes.push({activity,update});
    }
  }
  let successful=0,failed=0;
  for(const {activity,update} of changes){
    const res=await client.from("activities").update(update)
      .eq("id",activity.id).eq("user_id",userId());
    if(res.error){failed++;console.warn("Vital inheritance failed",activity.id,res.error);}
    else successful++;
  }
  if(failed)toast("Could not save inherited measurements for "+failed+" workouts.",5000);
  return successful;
}
async function loadActivities(){
  if(!userId())return;
  const res=await client.from("activities").select("*").eq("user_id",userId())
    .order("start_time",{ascending:false}).limit(2000);
  if(res.error){toast("Sync failed – previously loaded activities were preserved: "+res.error.message,5000);
    setSync("Sync failed · data not loaded");return;}
  activities=res.data||[];
  await inheritLatestWorkoutVitals();
}

function renderAll() {
  renderHome();
  renderHistory();
  renderPlanner();
  renderAnalysis();
}
function renderHome() {
  renderRecommendation();
  renderQuickStats();
  renderPlan();
  renderRecent();
  renderLatestWorkout();
}

function trainingContext() {
  const now = new Date();
  const a7 = activities.filter(a => new Date(a.start_time) >= daysAgo(7));
  const a28 = activities.filter(a => new Date(a.start_time) >= daysAgo(28));
  const runs7 = a7.filter(a => normalizeSport(a.sport_category || a.sport_type) === "run");
  const runs28 = a28.filter(a => normalizeSport(a.sport_category || a.sport_type) === "run");
  const load7 = sum(a7.map(derivedLoad));
  const load28 = sum(a28.map(derivedLoad));
  const weeklyBaseline = load28 / 4;
  const loadRatio = weeklyBaseline > 0 ? load7 / weeklyBaseline : 1;
  const last = activities[0] || null;
  const lastHours = last ? (nowMs() - new Date(last.start_time).getTime()) / 3600000 : 999;
  const hardRows = activities.filter(a => n(a.hard_zone_fraction,0) >= .15 || (n(a.z4_s,0)+n(a.z5_s,0)) >= 900);
  const lastHard = hardRows[0] || null;
  const lastHardHours = lastHard ? (nowMs() - new Date(lastHard.start_time).getTime()) / 3600000 : 999;
  const hardMin7 = sum(a7.map(a => (n(a.z4_s,0)+n(a.z5_s,0))/60));
  const runKm7 = sum(runs7.map(distanceKm));
  const runKm28Weekly = sum(runs28.map(distanceKm)) / 4;
  const easyRunPaces = runs28.filter(a => n(a.easy_zone_fraction,0) >= .6 && n(a.avg_pace_min_km) != null).map(a => n(a.avg_pace_min_km));
  const easyPace = median(easyRunPaces) ?? median(runs28.map(a => n(a.avg_pace_min_km)).filter(Boolean)) ?? 6.5;
  const runDistances = runs28.map(distanceKm).filter(x => x > 1);
  return {a7,a28,runs7,runs28,load7,load28,weeklyBaseline,loadRatio,last,lastHours,lastHard,lastHardHours,hardMin7,runKm7,runKm28Weekly,easyPace,medianRun:median(runDistances) || 5,longestRun:runDistances.length ? Math.max(...runDistances):5};
}

function recommendation() {
  const c = trainingContext();
  if (!activities.length) return {readiness:65,family:"easy",title:"Easy aerobic session",meta:"30–40 min · conversational effort",reasons:["No workout history yet; start conservatively."],distance:null};
  let score = 72;
  const reasons = [];
  if (c.lastHours < 10) { score -= 35; reasons.push("Your last workout ended less than 10 hours ago."); }
  else if (c.lastHours < 24) { score -= 16; reasons.push("Your previous session was within the last 24 hours."); }
  else if (c.lastHours > 48) { score += 7; reasons.push("You have had more than 48 hours since the last session."); }
  if (c.lastHardHours < 24) { score -= 22; reasons.push("A hard session was performed within 24 hours."); }
  else if (c.lastHardHours < 48) { score -= 10; reasons.push("A hard session is still within the 48-hour recovery window."); }
  else if (c.lastHardHours > 72) { score += 5; reasons.push("No hard workout in the last 72 hours."); }
  if (c.loadRatio > 1.55) { score -= 20; reasons.push("7-day load is much higher than the 28-day baseline ("+c.loadRatio.toFixed(2)+"×)."); }
  else if (c.loadRatio > 1.25) { score -= 9; reasons.push("Acute load is elevated ("+c.loadRatio.toFixed(2)+"× baseline)."); }
  else if (c.loadRatio >= .75 && c.loadRatio <= 1.15) { score += 5; reasons.push("7-day load is close to your recent baseline."); }
  if (c.hardMin7 > 60) { score -= 12; reasons.push("Hard-zone time is already high this week."); }
  else if (c.hardMin7 < 20) { score += 3; reasons.push("Hard-zone exposure has been low this week."); }
  score = clamp(score,5,95);

  let family = "easy";
  if (score < 32) family = "rest";
  else if (score < 48) family = "recovery";
  else if (score >= 78 && c.lastHardHours >= 60 && c.loadRatio <= 1.25 && c.hardMin7 < 45) family = "quality";

  const easyPace = c.easyPace;
  if (family === "rest") return {readiness:score,family,title:"Rest / mobility",meta:"No structured endurance training",reasons,distance:0};
  if (family === "recovery") {
    const d = clamp(c.medianRun*.65,3,5.5);
    return {readiness:score,family,title:"Recovery run or easy bike",meta:d.toFixed(1)+" km run · Z1–low Z2 · "+fmtPace(easyPace+.5),reasons,distance:d};
  }
  if (family === "quality") {
    return {readiness:score,family,title:"Controlled quality run",meta:"10–15 min easy + 5 × 3 min Z4 / 2 min easy + cool-down",reasons,distance:null};
  }
  const d = clamp(c.medianRun*.95,4.5,Math.max(5.5,c.longestRun*.95));
  return {readiness:score,family,title:"Easy aerobic run",meta:d.toFixed(1)+" km · mostly Z2 · "+fmtPace(easyPace+.15),reasons,distance:d};
}

function renderRecommendation() {
  const r = recommendation();
  el("readinessBadge").textContent = Math.round(r.readiness) + " / 100";
  const reasonHtml = r.reasons.slice(0,4).map(x => "<li>"+escapeHtml(x)+"</li>").join("");
  el("recommendationContent").classList.remove("skeleton");
  el("recommendationContent").innerHTML =
    '<div class="recommendation-title">'+escapeHtml(r.title)+'</div>'+
    '<div class="recommendation-meta">'+escapeHtml(r.meta)+'</div>'+
    '<strong>Why this workout?</strong><ul class="reason-list">'+reasonHtml+"</ul>";
}

function renderQuickStats() {
  const c = trainingContext();
  const year = new Date().getFullYear();
  const ytd = activities.filter(a => new Date(a.start_time).getFullYear() === year);
  const elev = sum(ytd.map(a => n(a.elevation_gain_m,0)));
  const values = [
    [activities.filter(a => new Date(a.start_time)>=daysAgo(7)).length,"workouts · 7 d"],
    [c.runKm7.toFixed(1)+" km","running · 7 d"],
    [Math.round(c.load7),"load · 7 d"],
    [Math.round(elev)+" m","elevation · "+year]
  ];
  el("quickStats").innerHTML = values.map(v => '<div class="metric"><div class="metric-value">'+escapeHtml(v[0])+'</div><div class="metric-label">'+escapeHtml(v[1])+"</div></div>").join("");
}


function isoPlanDate(d){ return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,10); }
function planLoad(w){
  if(!w||w.no_workout)return 0;
  const dur=n(w.duration_min,0),family=String(w.family||"");
  const factor=family.includes("quality")||family.includes("interval")?3.4:family==="tempo"||family==="steady_progression"?2.8:
    family.includes("long")?2.2:family.includes("strength")?1.8:family.includes("recovery")?1.3:2.0;
  return dur*factor;
}
function planIsHard(w){
  const family=String(w&&w.family||"");
  return family==="quality"||family==="tempo"||family==="bike_intervals"||String(w&&w.zone||"").toLowerCase().includes("z4");
}
function planSportCategory(w){return normalizeSport(w&&w.sport_type);}
function planMeta(w){
  if(w.no_workout)return "Rest / mobility";
  const parts=[];
  if(n(w.distance_km)>0)parts.push(n(w.distance_km).toFixed(1)+" km");
  if(n(w.duration_min)>0)parts.push(Math.round(n(w.duration_min))+" min");
  if(w.zone)parts.push(w.zone);
  if(w.pace)parts.push(w.pace);
  if(w.wattage)parts.push(w.wattage);
  return parts.join(" · ");
}
function makePlannedWorkout(dateObj,opts={}){
  const key=isoPlanDate(dateObj);
  return {
    date:key,dateObj:new Date(dateObj),weekday:new Intl.DateTimeFormat(undefined,{weekday:"short"}).format(dateObj),
    start_time:opts.start_time||"18:00",title:opts.title||"Rest / mobility",sport_type:opts.sport_type||"None",
    family:opts.family||"rest_or_mobility",duration_min:n(opts.duration_min,0),distance_km:n(opts.distance_km,0),
    zone:opts.zone||"",pace:opts.pace||"",wattage:opts.wattage||"",notes:opts.notes||"",
    no_workout:opts.no_workout!==undefined?!!opts.no_workout:(opts.sport_type==="None"),locked:!!opts.locked,source:opts.source||"planner"
  };
}
function defaultWorkoutForSport(sport,dateObj,existing={}){
  const c=trainingContext(),cat=normalizeSport(sport),base={...existing,sport_type:sport,no_workout:false};
  if(cat==="run")return makePlannedWorkout(dateObj,{...base,title:"Easy aerobic run",family:"easy_aerobic",duration_min:Math.round(clamp(c.medianRun*.9,4.5,8)*c.easyPace),distance_km:clamp(c.medianRun*.9,4.5,8),zone:"Mostly Z2",pace:fmtPace(c.easyPace+.15),wattage:""});
  if(cat==="bike")return makePlannedWorkout(dateObj,{...base,title:"Endurance ride",family:"endurance_ride",duration_min:50,distance_km:0,zone:"Z2",pace:"",wattage:"Comfortable endurance power"});
  if(cat==="strength")return makePlannedWorkout(dateObj,{...base,title:"Strength / prehab",family:"strength_prehab",duration_min:35,distance_km:0,zone:"Controlled",pace:"",wattage:"Bodyweight / light resistance"});
  if(cat==="hike")return makePlannedWorkout(dateObj,{...base,title:"Easy hike",family:"easy_hike",duration_min:75,distance_km:6,zone:"Easy aerobic",pace:"Comfortable hiking pace",wattage:""});
  return makePlannedWorkout(dateObj,{...base,title:"Rest / mobility",sport_type:"None",family:"rest_or_mobility",duration_min:0,distance_km:0,zone:"Rest",no_workout:true});
}
function normalizePlanOverride(raw,dateObj){
  const w=makePlannedWorkout(dateObj,{...raw,locked:true,source:"manual_override"});
  if(w.no_workout||normalizeSport(w.sport_type)==="other"&&String(w.sport_type).toLowerCase()==="none"){
    w.no_workout=true;w.sport_type="None";w.family="rest_or_mobility";w.duration_min=0;w.distance_km=0;w.zone=w.zone||"Rest";
  }
  return w;
}

function tuneDeterministicPlanIntensity(plan,c,aggr,runDays){
  if(!plan.length||n(c.runKm28Weekly,0)<=0)return plan;
  const elevatedLoad=c.loadRatio>1.55||c.hardMin7>65;
  const severeFatigue=c.loadRatio>1.9||c.hardMin7>80;
  const progression=[0,.025,.05,.08,.11][aggr-1];
  // Combine the four-week baseline with a recent higher-volume week cautiously.
  // This avoids treating a sporadic lower-volume month as an absolute fitness ceiling.
  const baseline28=n(c.runKm28Weekly,0),recent7=n(c.runKm7,0);
  const sustainableReference=baseline28+Math.min(Math.max(0,recent7-baseline28)*.35,baseline28*.35);
  for(let week=0;week<2;week++){
    const subset=plan.slice(week*7,(week+1)*7);
    const runSet=subset.filter(w=>!w.no_workout&&planSportCategory(w)==="run");
    const unlocked=runSet.filter(w=>!w.locked);
    if(!unlocked.length)continue;
    // A high recent acute load warrants a lighter first week, not a permanent
    // 14-day ban on quality work. Existing manually locked sessions remain fixed.
    if(week===0&&elevatedLoad){
      for(const w of unlocked){
        if(!planIsHard(w))continue;
        w.family="easy_aerobic";w.title="Easy aerobic run";
        w.zone="Mostly Z2";w.pace=fmtPace(c.easyPace+.2);
        w.notes="Reduced intensity in the first week because recent load is above baseline.";
      }
    }
    const quality=runSet.filter(planIsHard);
    const longs=runSet.filter(w=>w.family==="long_run");
    // Keep the manual dates exactly as entered. Use existing automatic run days
    // to create variety rather than scheduling unrequested extra hard days.
    if(runDays>=2&&longs.length===0){
      const eligible=unlocked.filter(w=>!planIsHard(w));
      const w=eligible.slice().sort((a,b)=>n(b.distance_km,0)-n(a.distance_km,0))[0];
      if(w){w.family="long_run";w.title="Long easy run";w.zone="Z2 (conversational)";w.notes="Controlled aerobic endurance; do not turn this into a tempo run.";}
    }
    const allowQuality=week===1
      ?(!severeFatigue)
      :(!elevatedLoad&&c.lastHardHours>=36);
    if(aggr>=4&&runDays>=3&&n(c.runKm28Weekly,0)>=8&&allowQuality&&quality.length===0){
      const longDay=runSet.find(w=>w.family==="long_run");
      const candidate=unlocked.filter(w=>w.family!=="long_run").sort((a,b)=>
        Math.abs(new Date(b.date)-new Date(longDay?longDay.date:subset[0].date))-
        Math.abs(new Date(a.date)-new Date(longDay?longDay.date:subset[0].date)))[0];
      if(candidate){
        candidate.family="quality";candidate.title="Controlled quality run";candidate.zone="Z3–Z4 work with easy recoveries";
        candidate.notes="Warm up 10–12 min; 3 × 3 min Z3–low Z4 / 2 min easy; cool down. Only progress if recovering well.";
      }
    }
    const maxTotal=Math.max(7,sustainableReference*(week===0&&elevatedLoad?1:1+progression)*
      (week===1?1.055:1));
    const lockedKm=sum(runSet.filter(w=>w.locked).map(w=>n(w.distance_km,0)));
    const kmBudget=Math.max(0,maxTotal-lockedKm);
    const weights=unlocked.map(w=>w.family==="long_run"?1.75:planIsHard(w)?1.05:1);
    const weightSum=sum(weights);
    if(weightSum<=0)continue;
    unlocked.forEach((w,i)=>{
      // If a locked workout already consumes the weekly budget, automatic runs
      // become short recoveries rather than multiplying total load.
      const targetKm=kmBudget*weights[i]/weightSum;
      const dist=clamp(targetKm,0,w.family==="long_run"?c.longestRun+1.5:10);
      w.distance_km=Math.round(dist*10)/10;
      if(w.family==="long_run"){
        w.title="Long easy run";w.pace=fmtPace(c.easyPace+.2);
        w.duration_min=Math.round(dist*(c.easyPace+.2));
      }else if(planIsHard(w)){
        w.title="Controlled intervals / tempo";
        const reps=dist>=5.5&&aggr>=5?4:dist>=4.2?3:2;
        const minutesPerRep=aggr>=5?3:2.5;
        w.pace=reps+" × "+minutesPerRep+" min controlled Z3–Z4";
        w.notes="Easy warm-up 8–12 min; "+reps+" × "+minutesPerRep+
          " min controlled Z3–Z4 with 2 min easy recoveries; easy cool-down. "+
          "Keep total duration and distance within plan; skip intensity if fatigue develops.";
        w.duration_min=Math.round(Math.max(24,dist*c.easyPace));
      }else{
        w.title=w.family==="recovery_aerobic"?"Recovery run":"Easy aerobic run";
        w.zone=w.family==="recovery_aerobic"?"Z1–low Z2":"Mostly Z2";
        w.pace=fmtPace(c.easyPace+.15);
        w.duration_min=Math.round(dist*(c.easyPace+.15));
      }
    });
  }
  return plan;
}
function buildFourteenDayPlan(){
  const c=trainingContext(),pref=(profile&&profile.training_preferences)||{},overrides=safeJson(pref.manual_plan_overrides,{})||{};
  const runDays=clamp(parseInt(pref.weekly_run_days||3,10),1,7),strengthTarget=clamp(parseInt(pref.strength_sessions||2,10),0,5);
  const longDay=parseInt(pref.long_run_day==null?6:pref.long_run_day,10),aggr=clamp(parseInt(pref.training_aggressiveness||3,10),1,5);
  const todayRec=recommendation(),start=new Date();start.setHours(12,0,0,0);
  const out=[],plannedContext=[];
  const weekCounts=[{run:0,strength:0,quality:0},{run:0,strength:0,quality:0}];

  for(let i=0;i<14;i++){
    const d=new Date(start.getTime()+i*86400000),key=isoPlanDate(d),week=Math.floor(i/7),counts=weekCounts[week];
    let w;
    if(overrides[key]){
      w=normalizePlanOverride(overrides[key],d);
    }else{
      const prior24=plannedContext.filter(x=>(d-x.dateObj)/3600000<=30);
      const prior72=plannedContext.filter(x=>(d-x.dateObj)/3600000<=78);
      const load72=sum(prior72.map(planLoad));
      const hardRecently=prior72.some(planIsHard);
      const runKm72=sum(prior72.filter(x=>planSportCategory(x)==="run").map(x=>n(x.distance_km,0)));
      const remaining=7-(i%7),runNeed=Math.max(0,runDays-counts.run),strengthNeed=Math.max(0,strengthTarget-counts.strength);

      if(i===0){
        if(todayRec.family==="rest")w=makePlannedWorkout(d,{title:"Rest / mobility",sport_type:"None",family:"rest_or_mobility",zone:"Rest",no_workout:true,start_time:new Date().toTimeString().slice(0,5)});
        else if(todayRec.family==="recovery")w=makePlannedWorkout(d,{title:"Recovery run",sport_type:"Run",family:"recovery_aerobic",duration_min:35,distance_km:n(todayRec.distance,4),zone:"Z1–low Z2",pace:fmtPace(c.easyPace+.5),start_time:new Date().toTimeString().slice(0,5)});
        else if(todayRec.family==="quality")w=makePlannedWorkout(d,{title:"Controlled quality run",sport_type:"Run",family:"quality",duration_min:50,distance_km:clamp(c.medianRun,5,9),zone:"Z4 work / easy recoveries",pace:"5 × 3 min controlled",notes:"10–15 min easy + 5 × 3 min Z4 / 2 min easy + cool-down",start_time:new Date().toTimeString().slice(0,5)});
        else w=makePlannedWorkout(d,{title:"Easy aerobic run",sport_type:"Run",family:"easy_aerobic",duration_min:Math.round(n(todayRec.distance,c.medianRun)*c.easyPace),distance_km:n(todayRec.distance,c.medianRun),zone:"Mostly Z2",pace:fmtPace(c.easyPace+.15),start_time:new Date().toTimeString().slice(0,5)});
      }else if(prior24.some(x=>planIsHard(x))||load72>Math.max(340,c.weeklyBaseline*2.2)){
        if(runNeed>=remaining)w=makePlannedWorkout(d,{title:"Recovery run",sport_type:"Run",family:"recovery_aerobic",duration_min:30,distance_km:clamp(c.medianRun*.6,3,5),zone:"Z1–low Z2",pace:fmtPace(c.easyPace+.5)});
        else w=makePlannedWorkout(d,{title:"Rest / mobility",sport_type:"None",family:"rest_or_mobility",zone:"Recovery after recent load",no_workout:true});
      }else if(d.getDay()===longDay&&counts.run<runDays&&runKm72<c.longestRun*1.4){
        const dist=clamp(Math.max(c.longestRun*.95,c.medianRun*(1.1+.05*aggr)),7,c.longestRun+1.5);
        w=makePlannedWorkout(d,{title:"Long easy run",sport_type:"Run",family:"long_run",duration_min:Math.round(dist*(c.easyPace+.2)),distance_km:dist,zone:"Z2",pace:fmtPace(c.easyPace+.2)});
      }else if(strengthNeed>0&&(i%7===1||i%7===4)&&!prior24.length){
        w=makePlannedWorkout(d,{title:"Strength / prehab",sport_type:"Strength",family:"strength_prehab",duration_min:35,zone:"Controlled",wattage:"Bodyweight / light resistance",notes:"Calves, hips, core, knee stability"});
      }else if(counts.quality<1&&counts.run<runDays&&!hardRecently&&i%7>=2&&i%7<=5&&c.lastHardHours+i*24>=54){
        w=makePlannedWorkout(d,{title:"Quality run",sport_type:"Run",family:"quality",duration_min:48,distance_km:clamp(c.medianRun,5,9),zone:"Z4 intervals",pace:"5 × 3 min controlled",notes:"Warm up 10–15 min; 5 × 3 min Z4 with 2 min easy; cool down"});
      }else if(runNeed>0&&(i%2===0||runNeed>=remaining)){
        const dist=clamp(c.medianRun*(.88+.03*aggr),4.5,8.5);
        w=makePlannedWorkout(d,{title:"Easy aerobic run",sport_type:"Run",family:"easy_aerobic",duration_min:Math.round(dist*(c.easyPace+.15)),distance_km:dist,zone:"Mostly Z2",pace:fmtPace(c.easyPace+.15)});
      }else if(strengthNeed>0){
        w=makePlannedWorkout(d,{title:"Strength / prehab",sport_type:"Strength",family:"strength_prehab",duration_min:35,zone:"Controlled",wattage:"Bodyweight / light resistance"});
      }else{
        w=makePlannedWorkout(d,{title:"Rest / mobility",sport_type:"None",family:"rest_or_mobility",zone:"Recovery",no_workout:true});
      }
    }

    const cat=planSportCategory(w);if(!w.no_workout){if(cat==="run")counts.run++;if(cat==="strength")counts.strength++;if(planIsHard(w))counts.quality++;}
    out.push(w);plannedContext.push(w);
  }
  tuneDeterministicPlanIntensity(out,c,aggr,runDays);
  currentPlan=out;return out;
}

async function loadLatestTrainingPlan(){
  if(!client||!userId())return;
  const today=isoPlanDate(new Date());
  const res=await client.from("training_plans").select("*").eq("user_id",userId()).eq("start_date",today).order("created_at",{ascending:false}).limit(1).maybeSingle();
  if(res.error){console.warn("Could not load saved plan",res.error);return;}
  const saved=safeJson(res.data&&res.data.plan,{});
  if(Array.isArray(saved.planned_workouts)&&saved.planned_workouts.length===14){
    currentPlan=saved.planned_workouts.map(w=>({...w,dateObj:new Date(w.date+"T12:00:00")}));
    if(saved.ai_summary){
      el("plannerAiSummary").classList.remove("hidden");
      el("plannerAiSummary").innerHTML=window.marked?marked.parse(saved.ai_summary):escapeHtml(saved.ai_summary);
    }
  }
}
async function persistPlan(plan,engineVersion,extra={}){
  if(!client||!userId()||!plan.length)return;
  const row={user_id:userId(),start_date:plan[0].date,engine_version:engineVersion,
    plan:{planned_workouts:plan.map(({dateObj,...w})=>w),generated_at:new Date().toISOString(),...extra}};
  const res=await client.from("training_plans").insert(row);
  if(res.error)console.warn("Could not persist plan",res.error);
}
function renderPlan(){
  const plan=currentPlan.length?currentPlan:buildFourteenDayPlan();
  currentPlan=plan;
  el("weekPlan").innerHTML=plan.slice(0,7).map((p,i)=>
    '<div class="day-card '+(i===0?"today ":"")+(p.locked?"manual":"")+'" data-plan-date="'+escapeHtml(p.date)+'">'+
    '<div class="day-date">'+escapeHtml(shortDay(p.dateObj||new Date(p.date+"T12:00:00")))+(p.locked?' · manual':'')+'</div>'+
    '<div class="day-title">'+escapeHtml(p.title)+'</div><div class="day-meta">'+escapeHtml(planMeta(p))+"</div></div>"
  ).join("");
  qsa("#weekPlan [data-plan-date]").forEach(card=>card.addEventListener("click",()=>openPlanEditor(card.dataset.planDate)));
  renderPlanner();
}
async function recalculateAndPersistPlan(){
  currentPlan=[];
  const plan=buildFourteenDayPlan();
  currentPlan=plan;
  await persistPlan(plan,"deterministic_web_v2");
  el("plannerAiSummary").classList.add("hidden");
  el("plannerAiStatus").textContent="Deterministic plan recalculated.";
  renderPlan();
}
function vdotForTime(distanceKm,timeMin){
  const v=distanceKm*1000/timeMin;
  const vo2=-4.60+0.182258*v+0.000104*v*v;
  const pct=0.8+0.1894393*Math.exp(-0.012778*timeMin)+0.2989558*Math.exp(-0.1932605*timeMin);
  return vo2/pct;
}
function vdotTimeFromVo2(vo2,distanceKm){
  const v=n(vo2);if(v==null||v<=20)return null;
  let lo=Math.max(4,distanceKm*2.4),hi=Math.max(45,distanceKm*12);
  for(let i=0;i<80;i++){
    const mid=(lo+hi)/2,vd=vdotForTime(distanceKm,mid);
    if(vd>v)lo=mid;else hi=mid;
  }
  return (lo+hi)/2;
}

const RACE_DEFINITIONS={
  "10k":{label:"10 km",km:10,minimumRunKm:3,baseUncertainty:.055},
  "hm":{label:"Half marathon",km:21.0975,minimumRunKm:5,baseUncertainty:.075},
  "m":{label:"Marathon",km:42.195,minimumRunKm:10,baseUncertainty:.115}
};
function recentVo2Evidence(asOf=new Date(),allowProfileFallback=true){
  const end=new Date(asOf).getTime(),cutoff=end-180*86400000;
  const recent=activities.filter(a=>{
    const t=new Date(a.start_time).getTime();return t<=end&&t>=cutoff;
  }).sort((a,b)=>new Date(b.start_time)-new Date(a.start_time));
  const apple=recent.map(a=>n(a.apple_vo2max)).filter(v=>v!=null&&v>=20&&v<=80).slice(0,8);
  if(apple.length)return {value:median(apple),source:"recent Apple VO₂max",samples:apple.length};
  const own=recent.map(a=>n(a.own_vo2max_estimate)??n(a.estimated_vo2max)).filter(v=>v!=null&&v>=20&&v<=80).slice(0,8);
  if(own.length)return {value:median(own),source:"WorkOutBuddy VO₂ estimate",samples:own.length};
  const p=allowProfileFallback?n(profile&&profile.profile_vo2max):null;
  return p&&p>=20?{value:p,source:"profile VO₂max",samples:0}:{value:null,source:"none",samples:0};
}
function riegelProjection(targetKm,runs){
  const minDistance=targetKm>=42?10:targetKm>=21?5:3;
  const est=[];
  for(const a of runs){
    const d=distanceKm(a),t=durationMin(a),pace=d>0?t/d:null;
    if(d<minDistance||t<10||pace==null||pace<3||pace>12)continue;
    est.push(t*Math.pow(targetKm/d,1.07));
  }
  est.sort((a,b)=>a-b);
  if(!est.length)return null;
  return mean(est.slice(0,Math.min(3,est.length)));
}
function conservativeRacePredictions(asOf=new Date(),historical=false){
  const end=new Date(asOf).getTime(),runs=activities.filter(a=>{
    const t=new Date(a.start_time).getTime();
    return normalizeSport(a.sport_category||a.sport_type)==="run"&&t<=end&&t>=end-120*86400000;
  });
  const c={runKm28Weekly:sum(runs.filter(a=>new Date(a.start_time).getTime()>=end-28*86400000).map(distanceKm))/4};
  const longest=runs.length?Math.max(...runs.map(distanceKm)):0;
  const vo2=recentVo2Evidence(asOf,!historical),goals=(profile&&profile.goals)||{};
  const out={};
  for(const [key,race] of Object.entries(RACE_DEFINITIONS)){
    const riegel=riegelProjection(race.km,runs);
    const vdotBase=vdotTimeFromVo2(vo2.value,race.km);
    const vdotConservative=vdotBase!=null?vdotBase*1.06:null;
    let base=null,source=[];
    if(riegel!=null&&vdotConservative!=null){
      base=Math.max(vdotConservative,Math.min(riegel,vdotConservative*1.15));
      source=["recent runs",vo2.source];
    }else if(riegel!=null){base=riegel;source=["recent runs"];}
    else if(vdotConservative!=null){base=vdotConservative;source=[vo2.source];}

    let penalty=1,readiness=[],confidence="moderate";
    if(race.km===10){
      if(longest<6){penalty*=1.03;readiness.push("limited recent long-run evidence");confidence="low";}
      if(c.runKm28Weekly<10){penalty*=1.02;readiness.push("low recent weekly volume");confidence="low";}
    }else if(race.km<30){
      if(longest<8){penalty*=1.09;readiness.push("longest recent run <8 km");confidence="low";}
      else if(longest<12){penalty*=1.05;readiness.push("longest recent run <12 km");confidence="low";}
      if(c.runKm28Weekly<12){penalty*=1.07;readiness.push("weekly running <12 km");confidence="low";}
      else if(c.runKm28Weekly<20){penalty*=1.04;readiness.push("weekly running <20 km");}
    }else{
      if(longest<15){penalty*=1.15;readiness.push("longest recent run <15 km");confidence="low";}
      else if(longest<22){penalty*=1.10;readiness.push("longest recent run <22 km");confidence="low";}
      else if(longest<28){penalty*=1.05;readiness.push("longest recent run <28 km");}
      if(c.runKm28Weekly<20){penalty*=1.12;readiness.push("weekly running <20 km");confidence="low";}
      else if(c.runKm28Weekly<30){penalty*=1.07;readiness.push("weekly running <30 km");confidence="low";}
      else if(c.runKm28Weekly<40){penalty*=1.03;readiness.push("weekly running <40 km");}
    }
    // Broad, non-statistical uncertainty bands reflect sparse training evidence,
    // not a formal confidence interval or a calibrated prediction distribution.
    const sparsePenalty=runs.length<4?.045:runs.length<8?.025:0;
    const uncertainty=clamp(race.baseUncertainty+sparsePenalty+
      (confidence==="low"?.03:0)+(vo2.samples===0?.02:0),.05,.27);
    const minutes=base!=null?base*penalty:null;
    const target=goals.primary_goal===key?n(goals.target_minutes):null;
    out[key]={label:race.label,minutes,target_minutes:target,
      delta_minutes:minutes!=null&&target!=null?minutes-target:null,
      source:source.join(" + ")||"insufficient data",confidence,readiness,
      base_minutes:base,endurance_penalty:penalty,
      uncertainty_pct:uncertainty,lower_minutes:minutes==null?null:minutes*(1-uncertainty),
      upper_minutes:minutes==null?null:minutes*(1+uncertainty),
      run_count:runs.length,vo2_samples:vo2.samples};
  }
  return out;
}
function renderRacePredictions(){
  const p=conservativeRacePredictions(),host=el("racePredictions");if(!host)return;
  host.innerHTML=Object.values(p).map(r=>{
    let note="Current conservative capability · "+r.source+" · "+r.confidence+" confidence";
    if(r.readiness.length)note+=" · "+r.readiness.join(", ");
    if(r.target_minutes!=null&&r.minutes!=null){
      const delta=r.delta_minutes;
      note+=" · target "+raceTimeText(r.target_minutes)+" · "+(delta<=0?"estimate is "+raceTimeText(Math.abs(delta))+" faster":"estimate is "+raceTimeText(delta)+" slower");
    }
    return '<div class="analysis-card"><div class="eyebrow">'+escapeHtml(r.label)+'</div><div class="value">'+escapeHtml(raceTimeText(r.minutes))+
      '</div><div class="note">'+escapeHtml(note)+'</div></div>';
  }).join("");
}
function historicalRacePredictions(maxWeeks=26){
  const runs=activities.filter(a=>normalizeSport(a.sport_category||a.sport_type)==="run"&&
    Number.isFinite(new Date(a.start_time).getTime())).sort((a,b)=>new Date(a.start_time)-new Date(b.start_time));
  if(runs.length<2)return [];
  const firstRun=new Date(runs[0].start_time).getTime(),result=[];
  const now=new Date();const today=new Date(now);today.setHours(23,59,59,999);
  for(let w=maxWeeks;w>=0;w--){
    const at=new Date(today.getTime()-w*7*86400000);
    if(at.getTime()<firstRun)continue;
    const seen=runs.filter(a=>new Date(a.start_time)<=at);
    if(seen.length<2)continue;
    const estimates=conservativeRacePredictions(at,true);
    result.push({date:isoPlanDate(at),...estimates});
  }
  return result;
}
function renderRaceTrend(){
  const plot=el("raceTrendPlot"),status=el("raceTrendInfo"),picker=el("raceTrendDistance");
  if(!plot||!status||!picker||typeof Plotly==="undefined")return;
  const history=historicalRacePredictions();
  const selected=picker.value||"hm",def=RACE_DEFINITIONS[selected];
  const pts=history.map(point=>({date:point.date,...point[selected]})).filter(p=>n(p.minutes)!=null);
  if(pts.length<2){
    Plotly.purge(plot);
    status.textContent="A time series needs at least two weekly estimates backed by past run data (no back-filled future information).";
    return;
  }
  const y=pts.map(p=>p.minutes),lower=pts.map(p=>p.lower_minutes),upper=pts.map(p=>p.upper_minutes);
  const lo=Math.min(...lower),hi=Math.max(...upper),ticks=[];
  const step=def.km>=42?10:def.km>=20?5:2;
  for(let x=Math.floor(lo/step)*step;x<=hi+step&&ticks.length<35;x+=step)ticks.push(x);
  const target=n((profile&&profile.goals||{}).primary_goal===selected?(profile.goals||{}).target_minutes:null);
  const shapes=target!=null?[{type:"line",xref:"paper",x0:0,x1:1,y0:target,y1:target,
    line:{color:"rgba(225,110,55,.75)",width:1.5,dash:"dash"}}]:[];
  Plotly.react(plot,[{
    x:pts.map(p=>p.date),y,customdata:pts.map(p=>[
      raceTimeText(p.minutes),raceTimeText(p.lower_minutes),raceTimeText(p.upper_minutes),p.run_count
    ]),
    mode:"lines+markers",type:"scatter",name:def.label,connectgaps:false,
    line:{color:"#3278bd",width:2.5},marker:{size:5,color:"#3278bd"},
    error_y:{type:"data",symmetric:false,array:upper.map((v,i)=>v-y[i]),
      arrayminus:y.map((v,i)=>v-lower[i]),visible:true,thickness:1,width:2,color:"rgba(60,110,160,.4)"},
    hovertemplate:"%{x}<br>Estimated: %{customdata[0]}<br>Heuristic range: %{customdata[1]} – %{customdata[2]}"+
      "<br>Recent runs: %{customdata[3]}<extra></extra>"
  }],{
    margin:{l:66,r:18,t:12,b:43},showlegend:false,
    xaxis:{type:"date",title:"Estimate as of date"},
    yaxis:{title:"Estimated finish time",tickvals:ticks,ticktext:ticks.map(raceTimeText),automargin:true},
    shapes,paper_bgcolor:"transparent",plot_bgcolor:"transparent"
  },{responsive:true,displaylogo:false});
  const first=pts[0],last=pts[pts.length-1],gain=first.minutes-last.minutes;
  const trend=Math.abs(gain)<.1?"Overall estimate approximately stable. ":
    "Change since first displayed estimate: "+(gain>0?"faster by ":"slower by ")+raceTimeText(Math.abs(gain))+". ";
  status.textContent=trend+(target!=null?"Dashed horizontal line = configured race target. ":"")+
    "Each weekly estimate uses only workouts available at that time. "+
    "Error bars are heuristic uncertainty ranges, not statistical confidence intervals; long-race capability is particularly uncertain with limited long runs.";
}
function renderPlanner(){
  const plan=currentPlan.length?currentPlan:buildFourteenDayPlan();
  currentPlan=plan;
  const runKm=sum(plan.filter(w=>planSportCategory(w)==="run").map(w=>n(w.distance_km,0)));
  const rideMin=sum(plan.filter(w=>planSportCategory(w)==="bike").map(w=>n(w.duration_min,0)));
  const totalMin=sum(plan.map(w=>n(w.duration_min,0)));
  const manual=plan.filter(w=>w.locked).length;
  el("plannerSummary").textContent="Next 14 days: "+runKm.toFixed(1)+" run km · "+Math.round(rideMin)+" ride min · "+Math.round(totalMin)+" total min · "+manual+" manual override"+(manual===1?"":"s");
  const target=plannerIntensityTargets(),quality=plan.filter(x=>!x.no_workout&&planIsHard(x)).length;
  const longCount=plan.filter(x=>!x.no_workout&&x.family==="long_run").length;
  el("plannerIntensitySummary").textContent="Training level "+target.level+"/5 · "+target.run_days_per_week+
    " planned run days/week · "+quality+" quality workouts · "+longCount+" long runs / 14 days"+
    (target.recent_weekly_run_km>0?" · recent baseline "+target.recent_weekly_run_km.toFixed(1)+" km/week":"")+
    ". Higher intensity means purposeful quality and progression, not an unbounded volume increase.";
  renderRacePredictions();
  renderRaceTrend();
  el("plannerCalendar").innerHTML=plan.map((w,i)=>
    '<div class="plan-card '+(i===0?"today ":"")+(w.locked?"manual":"")+'" data-plan-date="'+escapeHtml(w.date)+'">'+
    '<div class="plan-date">'+escapeHtml(w.weekday+" "+w.date)+(w.locked?'<span class="plan-manual">MANUAL</span>':'')+'</div>'+
    '<div class="plan-title">'+escapeHtml(w.title)+'</div><div class="plan-meta">'+escapeHtml(planMeta(w))+
    (w.notes?"\n"+escapeHtml(w.notes):"")+"</div></div>"
  ).join("");
  qsa("#plannerCalendar [data-plan-date]").forEach(card=>card.addEventListener("click",()=>openPlanEditor(card.dataset.planDate)));
}
function openPlanEditor(dateKey){
  const w=currentPlan.find(x=>x.date===dateKey);if(!w)return;
  el("planEditDate").value=w.date;el("planEditHeading").textContent="Edit "+w.weekday+" "+w.date;
  el("planEditTime").value=w.start_time||"18:00";el("planEditRest").checked=!!w.no_workout;
  el("planEditSport").value=w.sport_type||"Run";el("planEditFamily").value=w.family||"easy_aerobic";el("planEditTitle").value=w.title||"";
  el("planEditDuration").value=n(w.duration_min,0);el("planEditDistance").value=n(w.distance_km,0);el("planEditZone").value=w.zone||"";
  el("planEditPace").value=w.pace||"";el("planEditPower").value=w.wattage||"";el("planEditNotes").value=w.notes||"";
  el("planEditClearButton").disabled=!w.locked;el("planEditStatus").textContent="";
  adaptPlanEditorForSport(false);el("planEditDialog").showModal();
}
function adaptPlanEditorForSport(autoFill=true){
  const rest=el("planEditRest").checked,sport=rest?"None":el("planEditSport").value,cat=normalizeSport(sport);
  ["planEditDuration","planEditDistance","planEditZone","planEditPace","planEditPower"].forEach(id=>el(id).disabled=rest);
  if(rest){el("planEditSport").value="None";el("planEditFamily").value="rest_or_mobility";if(autoFill)el("planEditTitle").value="Rest / mobility";return;}
  if(autoFill){
    const d=new Date(el("planEditDate").value+"T12:00:00"),fresh=defaultWorkoutForSport(sport,d);
    el("planEditFamily").value=fresh.family;el("planEditTitle").value=fresh.title;el("planEditDuration").value=fresh.duration_min;
    el("planEditDistance").value=fresh.distance_km;el("planEditZone").value=fresh.zone;el("planEditPace").value=fresh.pace;el("planEditPower").value=fresh.wattage;
  }
  el("planEditPace").disabled=!(cat==="run"||cat==="hike");el("planEditPower").disabled=!(cat==="bike"||cat==="strength");
}
async function persistTrainingPreferences(next){
  const merged={...((profile&&profile.training_preferences)||{}),...next};
  const res=await client.from("profiles").update({training_preferences:merged}).eq("user_id",userId()).select().single();
  if(res.error)throw res.error;profile=res.data;profile.hr_zones=safeJson(profile.hr_zones,{});profile.goals=safeJson(profile.goals,{});profile.training_preferences=safeJson(profile.training_preferences,{});
}
async function savePlanOverride(event){
  event.preventDefault();
  const dateKey=el("planEditDate").value;if(!dateKey)return;
  const rest=el("planEditRest").checked;
  const raw={date:dateKey,start_time:el("planEditTime").value||"18:00",no_workout:rest,
    sport_type:rest?"None":el("planEditSport").value,family:rest?"rest_or_mobility":el("planEditFamily").value,
    title:el("planEditTitle").value.trim()||(rest?"Rest / mobility":"Manual workout"),
    duration_min:rest?0:n(el("planEditDuration").value,0),distance_km:rest?0:n(el("planEditDistance").value,0),
    zone:rest?"Rest":el("planEditZone").value.trim(),pace:rest?"":el("planEditPace").value.trim(),
    wattage:rest?"":el("planEditPower").value.trim(),notes:el("planEditNotes").value.trim(),locked:true,source:"manual_override"};
  const pref=(profile&&profile.training_preferences)||{},overrides={...(safeJson(pref.manual_plan_overrides,{})||{})};overrides[dateKey]=raw;
  el("planEditStatus").textContent="Saving override and recalculating…";
  try{
    await persistTrainingPreferences({manual_plan_overrides:overrides});el("planEditDialog").close();
    currentPlan=[];const plan=buildFourteenDayPlan();currentPlan=plan;await persistPlan(plan,"manual_override_web_v2");renderPlan();
    toast("Manual override saved; following schedule recalculated.");
  }catch(err){el("planEditStatus").textContent=err.message||String(err);}
}
async function clearPlanOverride(){
  const dateKey=el("planEditDate").value,pref=(profile&&profile.training_preferences)||{},overrides={...(safeJson(pref.manual_plan_overrides,{})||{})};
  if(!overrides[dateKey]){el("planEditDialog").close();return;}delete overrides[dateKey];
  try{
    await persistTrainingPreferences({manual_plan_overrides:overrides});el("planEditDialog").close();
    currentPlan=[];const plan=buildFourteenDayPlan();currentPlan=plan;await persistPlan(plan,"manual_override_cleared_web_v2");renderPlan();
    toast("Manual override cleared; schedule recalculated.");
  }catch(err){el("planEditStatus").textContent=err.message||String(err);}
}

function workoutRow(a, clickable=true) {
  const sport = normalizeSport(a.sport_category || a.sport_type);
  const km = distanceKm(a);
  const mins = durationMin(a);
  const meta = km > .05 ? km.toFixed(1)+" km · "+Math.round(mins)+" min" : Math.round(mins)+" min";
  return '<div class="workout-row '+(clickable?"clickable":"")+'" '+(clickable?'data-activity-id="'+escapeHtml(a.id)+'"':"")+'>'+
    '<div class="workout-date">'+escapeHtml(localDate(a.start_time))+'</div>'+
    '<div><span class="sport-pill">'+escapeHtml(sport)+'</span></div>'+
    '<div><div class="workout-name">'+escapeHtml(a.name || a.sport_type || "Workout")+'</div></div>'+
    '<div class="workout-meta">'+escapeHtml(meta)+"</div></div>";
}
function renderRecent() {
  el("recentWorkouts").innerHTML = activities.length ? activities.slice(0,5).map(a=>workoutRow(a,true)).join("") : '<div class="muted">No workouts yet. Upload your first TCX file.</div>';
  qsa("#recentWorkouts [data-activity-id]").forEach(r => r.addEventListener("click",() => {
    selectedActivityId = r.dataset.activityId; openView("history"); renderHistory(); showWorkout(selectedActivityId);
  }));
}


const STREAM_METRICS = {
  pace_min_km:{label:"Pace",unit:"min/km",gradient:"linear-gradient(90deg,#22c55e,#facc15,#dc2626)"},
  speed_kmh:{label:"Speed",unit:"km/h",gradient:"linear-gradient(90deg,#dc2626,#facc15,#22c55e)"},
  heart_rate:{label:"Heart rate",unit:"bpm",gradient:"linear-gradient(90deg,#f7f7f7,#ffd3d3,#ff7070,#d60000,#670000)"},
  zone:{label:"HR zone",unit:"zone",gradient:"linear-gradient(90deg,#b9dcff 0%,#b9dcff 16.6%,#43b66f 16.6%,#43b66f 33.2%,#f3d43b 33.2%,#f3d43b 49.8%,#f59e0b 49.8%,#f59e0b 66.4%,#e03b35 66.4%,#e03b35 83%,#7c3aed 83%,#7c3aed 100%)"},
  power_w:{label:"Power",unit:"W",gradient:"linear-gradient(90deg,#f7f5fa,#e1d2f3,#b978e8,#6a0dad)"},
  altitude_m:{label:"Altitude",unit:"m",gradient:"linear-gradient(90deg,#343a40,#555d66,#7d858e,#a7adb5)"},
  grade_pct:{label:"Grade",unit:"%",gradient:"linear-gradient(90deg,#3f6f91,#879aa8,#b8b2aa,#8a5a35)"},
  cadence:{label:"Cadence",unit:"spm / rpm",gradient:"linear-gradient(90deg,#f5f2ed,#dfd2bf,#b99b70,#735b3b)"}
};

function initWorkoutViewerControls() {
  const options = Object.entries(STREAM_METRICS).map(([k,v])=>'<option value="'+k+'">'+escapeHtml(v.label)+'</option>').join("");
  ["historyRouteMetric","homeRouteMetric"].forEach(id => {
    el(id).innerHTML=options;
    el(id).value="pace_min_km";
  });
  el("historyRouteMetric").addEventListener("change",()=>selectedActivityId&&showWorkout(selectedActivityId,"history"));
  el("historyRouteXAxis").addEventListener("change",()=>selectedActivityId&&showWorkout(selectedActivityId,"history"));
  el("homeRouteMetric").addEventListener("change",()=>activities[0]&&showWorkout(activities[0].id,"home"));
  el("homeRouteXAxis").addEventListener("change",()=>activities[0]&&showWorkout(activities[0].id,"home"));
}

function filteredActivities() {
  const sport = el("historySportFilter").value;
  const period = el("historyPeriodFilter").value;
  let rows = activities.slice();
  if (sport !== "all") rows = rows.filter(a => normalizeSport(a.sport_category || a.sport_type) === sport);
  if (period !== "all") rows = rows.filter(a => new Date(a.start_time) >= daysAgo(Number(period)));
  return rows;
}

function humanMetricName(key) {
  const known={
    start_time:"Date / time",distance_km:"Distance (km)",duration_min:"Duration (min)",moving_time_min:"Moving time (min)",
    derived_load:"Training load",avg_hr:"Average HR",max_hr:"Maximum HR",avg_pace_min_km:"Pace (min/km)",
    avg_gap_pace_min_km:"Gradient-adjusted pace (min/km)",elevation_gain_m:"Elevation gain (m)",elevation_loss_m:"Elevation loss (m)",
    apple_vo2max:"Apple VO₂max",own_vo2max_estimate:"Gradient-normalized VO₂max estimate",estimated_vo2max:"Estimated VO₂max",
    body_weight_kg:"Body weight (kg)",training_load_score:"Training load score",trimp_score:"TRIMP",
    easy_zone_fraction:"Easy-zone fraction",hard_zone_fraction:"Hard-zone fraction",z1_min:"Z1 (min)",z2_min:"Z2 (min)",z3_min:"Z3 (min)",z4_min:"Z4 (min)",z5_min:"Z5 (min)",
    average_power:"Average power (W)",normalized_power:"Normalized power (W)",cadence_spm:"Cadence (spm/rpm)",
    hr_efficiency_drift_pct:"HR efficiency drift (%)",gap_hr_efficiency_drift_pct:"GAP HR drift (%)",km_gap_hr_efficiency_drift_pct:"km GAP HR drift (%)"
  };
  if(known[key]) return known[key];
  const raw=key.startsWith("metrics.")?key.slice(8):key;
  return raw.replace(/_/g," ").replace(/\b\w/g,c=>c.toUpperCase());
}

function activityMetricValue(a,key) {
  if(key==="start_time") return a.start_time;
  if(key==="distance_km") return distanceKm(a);
  if(key==="duration_min") return n(a.duration_s)!=null?n(a.duration_s)/60:null;
  if(key==="moving_time_min") return (n(a.moving_time_s)??n(a.duration_s))!=null?(n(a.moving_time_s)??n(a.duration_s))/60:null;
  if(key==="derived_load") return derivedLoad(a);
  if(/^z[1-5]_min$/.test(key)) return n(a[key.replace("_min","_s")])!=null?n(a[key.replace("_min","_s")])/60:null;
  if(key.startsWith("metrics.")) return n(safeJson(a.metrics_json,{})[key.slice(8)]);
  return n(a[key]);
}
function activityMetricIsDate(key){ return key==="start_time"; }

function coreActivityMetricKeys(){
  return [
    "start_time","distance_km","duration_min","moving_time_min","avg_pace_min_km","avg_gap_pace_min_km",
    "avg_hr","max_hr","elevation_gain_m","derived_load","training_load_score","trimp_score",
    "apple_vo2max","own_vo2max_estimate","body_weight_kg","average_power","normalized_power","cadence_spm",
    "hr_efficiency_drift_pct","gap_hr_efficiency_drift_pct","easy_zone_fraction","hard_zone_fraction"
  ];
}
function activityMetricKeys() {
  const full=!!el("fullStatisticsToggle").checked;
  const keys=new Set(coreActivityMetricKeys());
  if(!full)return Array.from(keys);
  ["elevation_loss_m","estimated_vo2max","km_gap_hr_efficiency_drift_pct","z1_min","z2_min","z3_min","z4_min","z5_min"].forEach(k=>keys.add(k));
  const skip=new Set(["id","user_id","source_external_id","raw_file_path","file_sha256","dedupe_key","metrics_json","created_at","updated_at","original_start_time","name","sport_type","sport_category","source","local_timezone"]);
  for(const a of activities){
    for(const [k,v] of Object.entries(a)){
      if(skip.has(k)||keys.has(k)) continue;
      if(n(v)!=null)keys.add(k);
    }
    const mj=safeJson(a.metrics_json,{});
    for(const [k,v] of Object.entries(mj)){
      if(n(v)!=null)keys.add("metrics."+k);
    }
  }
  return Array.from(keys);
}

function populateHistoryMetricControls(){
  const keys=activityMetricKeys();
  const xSel=el("historyXMetric"), ySel=el("historyYMetric");
  const oldX=xSel.value||"start_time", oldY=ySel.value||"distance_km";
  xSel.innerHTML=keys.map(k=>'<option value="'+escapeHtml(k)+'">'+escapeHtml(humanMetricName(k))+'</option>').join("");
  const numeric=keys.filter(k=>!activityMetricIsDate(k));
  ySel.innerHTML=numeric.map(k=>'<option value="'+escapeHtml(k)+'">'+escapeHtml(humanMetricName(k))+'</option>').join("");
  xSel.value=keys.includes(oldX)?oldX:"start_time";
  ySel.value=numeric.includes(oldY)?oldY:(numeric.includes("distance_km")?"distance_km":numeric[0]||"");
}

function renderHistory() {
  populateHistoryMetricControls();
  const rows=filteredActivities();
  renderWorkoutTable(rows);
  const xKey=el("historyXMetric").value, yKey=el("historyYMetric").value;
  const kind=el("historyPlotType").value, group=el("historyColorBy").value;
  const plottable=rows.map(a=>({a,x:activityMetricValue(a,xKey),y:activityMetricValue(a,yKey)}))
    .filter(p=>(activityMetricIsDate(xKey)?!!p.x:n(p.x)!=null)&&n(p.y)!=null);
  el("plotCoverage").textContent=rows.length+" workouts in selection · "+plottable.length+" have both selected metrics · "+plottable.length+" plotted";
  const groups=group==="sport"?Array.from(new Set(plottable.map(p=>normalizeSport(p.a.sport_category||p.a.sport_type)))):["all"];
  const traces=[];
  for(const g of groups){
    const sub=plottable.filter(p=>group!=="sport"||normalizeSport(p.a.sport_category||p.a.sport_type)===g)
      .sort((a,b)=>{
        const ax=activityMetricIsDate(xKey)?new Date(a.x).getTime():n(a.x,0);
        const bx=activityMetricIsDate(xKey)?new Date(b.x).getTime():n(b.x,0);
        return ax-bx;
      });
    if(!sub.length) continue;
    const type=kind==="bar"?"bar":"scatter";
    const mode=kind==="scatter"?"markers":"lines+markers";
    traces.push({
      x:sub.map(p=>p.x),y:sub.map(p=>p.y),type,mode:type==="scatter"?mode:undefined,name:g==="all"?humanMetricName(yKey):g,
      text:sub.map(p=>(p.a.name||p.a.sport_type||"Workout")+" · "+localDate(p.a.start_time)),
      hovertemplate:"%{text}<br>"+escapeHtml(humanMetricName(xKey))+": %{x}<br>"+escapeHtml(humanMetricName(yKey))+": %{y:.2f}<extra></extra>"
    });
  }
  const reverseY=yKey.includes("pace_min_km");
  Plotly.react("historyPlot",traces,{
    margin:{l:65,r:20,t:25,b:55},xaxis:{title:humanMetricName(xKey)},yaxis:{title:humanMetricName(yKey),autorange:reverseY?"reversed":true},
    paper_bgcolor:"transparent",plot_bgcolor:"transparent",legend:{orientation:"h"}
  },{responsive:true,displaylogo:false});
}

function fullTableMetricKeys(rows){
  if(!el("fullStatisticsToggle").checked)return [];
  const keys=[
    "trimp_score","easy_zone_fraction","hard_zone_fraction","z1_min","z2_min","z3_min","z4_min","z5_min",
    "hr_efficiency_drift_pct","gap_hr_efficiency_drift_pct","km_gap_hr_efficiency_drift_pct",
    "normalized_power"
  ];
  const seen=new Set(keys);
  for(const a of rows){
    const mj=safeJson(a.metrics_json,{});
    for(const [k,v] of Object.entries(mj)){
      if(n(v)!=null&&!seen.has("metrics."+k)){keys.push("metrics."+k);seen.add("metrics."+k);}
    }
  }
  return keys;
}
function tableMetricText(a,key){
  const v=activityMetricValue(a,key);
  if(v==null)return "—";
  const label=humanMetricName(key).toLowerCase();
  if(label.includes("pace"))return fmtPace(v);
  if(label.includes("fraction"))return fmt(v*100,1,"%");
  if(label.includes("distance")&&key!=="distance_km")return fmt(v,2);
  return fmt(v,2);
}

const COMPARE_COLORS=["#0d9488","#e8792a","#7c3aed","#e11d48","#2563eb","#a16207","#be185d","#059669"];
function comparisonColor(index){return COMPARE_COLORS[(index-1+COMPARE_COLORS.length)%COMPARE_COLORS.length];}
function renderComparisonSelection(){
  const status=el("workoutComparisonStatus"),chips=el("workoutComparisonChips");
  if(!status||!chips)return;
  const active=selectedActivityIds.map(id=>activities.find(a=>a.id===id)).filter(Boolean);
  if(!active.length){
    status.textContent="Tick several workouts to compare them. First selection = primary; click a row for single view.";
    chips.innerHTML="";
    return;
  }
  status.textContent=active.length===1?
    "One workout selected · tick another to overlay route, graph and metric deltas.":
    active.length+" workouts compared · #1 is primary (100% route opacity, primary-only map bounds).";
  chips.innerHTML=active.map((a,i)=>'<button type="button" class="comparison-chip '+(i===0?"comparison-primary":"")+
    '" data-remove-compare="'+escapeHtml(a.id)+'" title="Remove from comparison">'+
    '<span class="comparison-swatch" style="background:'+(i===0?"#111827":comparisonColor(i))+'"></span>'+
    (i===0?"Primary · ":"#"+(i+1)+" · ")+escapeHtml(a.name||localDate(a.start_time))+' ×</button>').join("");
  qsa("#workoutComparisonChips [data-remove-compare]").forEach(button=>button.addEventListener("click",()=>{
    toggleWorkoutSelection(button.dataset.removeCompare,false);
  }));
}
function toggleWorkoutSelection(id,checked=null){
  if(!activities.some(a=>a.id===id))return;
  const has=selectedActivityIds.includes(id),newState=checked==null?!has:!!checked;
  if(newState&&!has)selectedActivityIds.push(id);
  if(!newState&&has)selectedActivityIds=selectedActivityIds.filter(x=>x!==id);
  selectedActivityId=selectedActivityIds[0]||null;
  renderWorkoutTable(filteredActivities());
  if(selectedActivityId)showWorkout(selectedActivityId,"history");
  else{
    ++workoutComparisonRequest;
    el("workoutDetail").textContent="Select a workout from the table.";
    el("historyWorkoutMetrics").innerHTML="";
    el("routeMap").classList.add("hidden");
    el("streamPlot").classList.add("hidden");
    if(routeMaps.history){routeMaps.history.remove();routeMaps.history=null;}
  }
}
function renderWorkoutTable(rows){
  const wrap=el("historyTableWrap");
  renderComparisonSelection();
  if(!rows.length){wrap.innerHTML='<div class="muted" style="padding:14px">No workouts match these filters.</div>';return;}
  const extras=fullTableMetricKeys(rows);
  const head=["Compare","Date","Sport","Name","Distance","Duration","Pace","GAP","Avg HR","Max HR","Elevation","Power","Cadence","Load","VO₂ est.","Apple VO₂max","Weight",...extras.map(humanMetricName)];
  const body=rows.map(a=>{
    const selected=selectedActivityIds.includes(a.id)?" selected":"";
    const extraCells=extras.map(k=>'<td class="advanced-metric">'+escapeHtml(tableMetricText(a,k))+'</td>').join("");
    return '<tr class="workout-table-row'+selected+'" data-activity-id="'+escapeHtml(a.id)+'">'+
      '<td><input type="checkbox" class="compare-checkbox" aria-label="Compare '+escapeHtml(a.name||"workout")+'" data-id="'+escapeHtml(a.id)+'" '+(selectedActivityIds.includes(a.id)?"checked":"")+'></td>'+
      '<td>'+escapeHtml(localDate(a.start_time))+'</td><td>'+escapeHtml(a.sport_type||a.sport_category||"—")+'</td><td>'+escapeHtml(a.name||"Workout")+'</td>'+
      '<td>'+escapeHtml(fmt(distanceKm(a),2," km"))+'</td><td>'+escapeHtml(fmt(durationMin(a),0," min"))+'</td><td>'+escapeHtml(fmtPace(a.avg_pace_min_km))+'</td>'+
      '<td>'+escapeHtml(fmtPace(a.avg_gap_pace_min_km))+'</td><td>'+escapeHtml(fmt(a.avg_hr,0))+'</td><td>'+escapeHtml(fmt(a.max_hr,0))+'</td>'+
      '<td>'+escapeHtml(fmt(a.elevation_gain_m,0," m"))+'</td><td>'+escapeHtml(fmt(a.average_power,0," W"))+'</td><td>'+escapeHtml(fmt(a.cadence_spm,0))+'</td>'+
      '<td>'+escapeHtml(fmt(derivedLoad(a),1))+'</td><td>'+escapeHtml(fmt(a.own_vo2max_estimate??a.estimated_vo2max,1))+'</td>'+
      '<td><input class="inline-number manual-cell" data-field="apple_vo2max" data-id="'+escapeHtml(a.id)+'" type="number" min="15" max="90" step="0.1" value="'+escapeHtml(n(a.apple_vo2max)!=null?n(a.apple_vo2max):"")+'"></td>'+
      '<td><input class="inline-number manual-cell" data-field="body_weight_kg" data-id="'+escapeHtml(a.id)+'" type="number" min="30" max="250" step="0.1" value="'+escapeHtml(n(a.body_weight_kg)!=null?n(a.body_weight_kg):"")+'"></td>'+
      extraCells+'</tr>';
  }).join("");
  wrap.innerHTML='<table class="workout-table"><thead><tr>'+head.map(h=>"<th>"+escapeHtml(h)+"</th>").join("")+"</tr></thead><tbody>"+body+"</tbody></table>";
  qsa("#historyTableWrap .workout-table-row").forEach(r=>r.addEventListener("click",e=>{
    if(e.target.closest("input")) return;
    const id=r.dataset.activityId;
    if(e.ctrlKey||e.metaKey||e.shiftKey){toggleWorkoutSelection(id);}
    else{selectedActivityIds=[id];selectedActivityId=id;showWorkout(id,"history");}
    renderWorkoutTable(filteredActivities());
  }));
  qsa("#historyTableWrap .compare-checkbox").forEach(input=>{
    input.addEventListener("click",e=>e.stopPropagation());
    input.addEventListener("change",()=>toggleWorkoutSelection(input.dataset.id,input.checked));
  });
  qsa("#historyTableWrap .manual-cell").forEach(inp=>{
    inp.addEventListener("click",e=>e.stopPropagation());
    inp.addEventListener("change",()=>saveManualActivityValue(inp.dataset.id,inp.dataset.field,inp.value));
  });
}

async function saveManualActivityValue(id,field,raw){
  const value=raw===""?null:n(raw);
  if(value!=null&&field==="apple_vo2max"&&(value<15||value>90)){toast("VO₂max must be between 15 and 90.");renderHistory();return;}
  if(value!=null&&field==="body_weight_kg"&&(value<30||value>250)){toast("Weight must be between 30 and 250 kg.");renderHistory();return;}
  const a=activities.find(x=>x.id===id),mj=safeJson(a&&a.metrics_json,{});
  const inherited={...(safeJson(mj.inherited_workout_values,{})||{})};
  delete inherited[field];
  const updatedMetrics={...mj,inherited_workout_values:inherited};
  const res=await client.from("activities").update({[field]:value,metrics_json:updatedMetrics})
    .eq("id",id).eq("user_id",userId());
  if(res.error){toast("Could not save value: "+res.error.message,5000);renderHistory();return;}
  if(a){a[field]=value;a.metrics_json=updatedMetrics;}
  await inheritLatestWorkoutVitals();
  toast(field==="apple_vo2max"?"Apple VO₂max saved; newer missing values updated.":"Weight saved; newer missing values updated.");
  renderHistory();renderHome();renderAnalysis();
  if(selectedActivityId===id) showWorkout(id,"history");
}

function percentile(values,p){
  const a=values.filter(Number.isFinite).slice().sort((x,y)=>x-y);if(!a.length)return null;
  const i=(a.length-1)*p,lo=Math.floor(i),hi=Math.ceil(i);return lo===hi?a[lo]:a[lo]+(a[hi]-a[lo])*(i-lo);
}
function hexRgb(hex){const h=hex.replace("#","");return [parseInt(h.slice(0,2),16),parseInt(h.slice(2,4),16),parseInt(h.slice(4,6),16)];}
function mixColor(a,b,t){const A=hexRgb(a),B=hexRgb(b),c=A.map((v,i)=>Math.round(v+(B[i]-v)*clamp(t,0,1)));return "#"+c.map(v=>v.toString(16).padStart(2,"0")).join("");}
function threeColor(a,b,c,t){return t<=.5?mixColor(a,b,t*2):mixColor(b,c,(t-.5)*2);}
function streamMetricStats(rows,key){
  const vals=rows.map(r=>n(r[key])).filter(Number.isFinite);
  let lo=percentile(vals,.05),hi=percentile(vals,.95);
  if(lo==null||hi==null){lo=0;hi=1;} if(hi<=lo)hi=lo+1;
  return {lo,hi};
}
function viewerZoneIndex(hr,z){
  const h=n(hr);if(h==null)return null;
  const z1=n(z&&z.z1_max,130),z2=n(z&&z.z2_max,150),z3=n(z&&z.z3_max,165),z4=n(z&&z.z4_max,178);
  let z5=n(z&&z.z5_max);
  // Older profiles used 220 as a catch-all maximum. For the 6-zone viewer,
  // infer a practical Z5/Z6 boundary from Z4 unless the user set one explicitly.
  if(z5==null||z5<=z4||z5>210)z5=z4+12;
  if(h<=z1)return 1;if(h<=z2)return 2;if(h<=z3)return 3;if(h<=z4)return 4;if(h<=z5)return 5;return 6;
}
function colorForStreamMetric(key,value,stats){
  const v=n(value);if(v==null)return "#94a3b8";
  if(key==="heart_rate"){
    if(v<=95)return "#f7f7f7";if(v<=130)return mixColor("#f7f7f7","#ffd3d3",(v-95)/35);
    if(v<=160)return mixColor("#ffd3d3","#ff7070",(v-130)/30);
    if(v<=180)return mixColor("#ff7070","#d60000",(v-160)/20);
    return mixColor("#d60000","#670000",clamp((v-180)/25,0,1));
  }
  if(key==="zone"){
    const colors=["#94a3b8","#b9dcff","#43b66f","#f3d43b","#f59e0b","#e03b35","#7c3aed"];
    return colors[clamp(Math.round(v),1,6)];
  }
  let t=clamp((v-stats.lo)/(stats.hi-stats.lo),0,1);
  if(key==="pace_min_km") return threeColor("#22c55e","#facc15","#dc2626",t);
  if(key==="speed_kmh") return threeColor("#dc2626","#facc15","#22c55e",t);
  if(key==="power_w") return mixColor("#f7f5fa","#6a0dad",t);
  if(key==="cadence") return mixColor("#f5f2ed","#735b3b",t);
  if(key==="altitude_m") return mixColor("#343a40","#a7adb5",t);
  if(key==="grade_pct"){
    const span=Math.max(Math.abs(stats.lo),Math.abs(stats.hi),1),zv=clamp(v/span,-1,1);
    return zv<0?mixColor("#879aa8","#3f6f91",-zv):mixColor("#b8b2aa","#8a5a35",zv);
  }
  return threeColor("#dc2626","#facc15","#22c55e",t);
}

function rollingMedian(values,i,radius){
  const a=[];
  for(let j=Math.max(0,i-radius);j<=Math.min(values.length-1,i+radius);j++){
    const v=n(values[j]);if(v!=null)a.push(v);
  }
  return a.length?median(a):null;
}
function lowerBoundDistance(rows,target){
  let lo=0,hi=rows.length;
  while(lo<hi){const mid=(lo+hi)>>1,d=n(rows[mid].distance_m,Infinity);if(d<target)lo=mid+1;else hi=mid;}
  return clamp(lo,0,rows.length-1);
}
function lowerBoundTime(rows,target){
  let lo=0,hi=rows.length;
  while(lo<hi){const mid=(lo+hi)>>1,t=n(rows[mid].elapsed_s,Infinity);if(t<target)lo=mid+1;else hi=mid;}
  return clamp(lo,0,rows.length-1);
}
function deriveSpeedFromDistance(rows,halfWindowSec=4){
  const valid=rows.filter(r=>n(r.elapsed_s)!=null&&n(r.distance_m)!=null);
  if(valid.length<3)return false;
  const firstD=n(valid[0].distance_m),lastD=n(valid[valid.length-1].distance_m);
  const span=(firstD!=null&&lastD!=null)?lastD-firstD:0;
  if(!(span>100))return false;

  for(let i=0;i<rows.length;i++){
    const t=n(rows[i].elapsed_s),d=n(rows[i].distance_m);
    if(t==null||d==null){rows[i].distance_speed_mps=null;continue;}
    let j0=lowerBoundTime(rows,t-halfWindowSec);
    let j1=lowerBoundTime(rows,t+halfWindowSec);
    if(j0>=i&&i>0)j0=i-1;
    if(j1<=i&&i<rows.length-1)j1=i+1;
    const t0=n(rows[j0].elapsed_s),t1=n(rows[j1].elapsed_s);
    const d0=n(rows[j0].distance_m),d1=n(rows[j1].distance_m);
    if(t0==null||t1==null||d0==null||d1==null||t1<=t0||d1<d0){
      rows[i].distance_speed_mps=null;continue;
    }
    rows[i].distance_speed_mps=clamp((d1-d0)/(t1-t0),0,12);
  }
  return rows.some(r=>n(r.distance_speed_mps)!=null);
}
function buildStreamRows(stream){
  const pts=Array.isArray(stream&&stream.points)?stream.points:[];
  if(!pts.length)return [];
  const rows=pts.map((p,i)=>({
    i,
    elapsed_s:n(p.elapsed_s,i),
    distance_m:n(p.distance_calc_m)??n(p.distance_m),
    lat:n(p.lat),lon:n(p.lon),altitude_m:n(p.alt),
    heart_rate:n(p.hr),power_w:n(p.watts),cadence:n(p.cadence),
    raw_speed_mps:n(p.raw_speed_mps)??n(p.speed_mps),
    speed_mps:n(p.speed_mps)
  }));

  for(let i=0;i<rows.length;i++){
    if(rows[i].distance_m==null) rows[i].distance_m=i?rows[i-1].distance_m:null;
  }

  const haveDistanceSpeed=deriveSpeedFromDistance(rows,4);
  for(let i=0;i<rows.length;i++){
    // DistanceMeters/time is authoritative whenever it exists. The TCX Speed
    // extension in WorkOutDoors exports can be temporally misaligned despite
    // having a plausible workout-average value.
    rows[i].speed_mps=haveDistanceSpeed&&n(rows[i].distance_speed_mps)!=null
      ? rows[i].distance_speed_mps
      : rows[i].raw_speed_mps;

    // Last-resort one-step derivative for sparse distance streams.
    if(rows[i].speed_mps==null&&i>0&&rows[i].distance_m!=null&&rows[i-1].distance_m!=null){
      const dt=rows[i].elapsed_s-rows[i-1].elapsed_s,dd=rows[i].distance_m-rows[i-1].distance_m;
      if(dt>0&&dt<60&&dd>=0)rows[i].speed_mps=dd/dt;
    }
    rows[i].time_min=rows[i].elapsed_s/60;
    rows[i].distance_km=rows[i].distance_m==null?null:rows[i].distance_m/1000;
    rows[i].speed_kmh=rows[i].speed_mps==null?null:rows[i].speed_mps*3.6;
    rows[i].pace_min_km=rows[i].speed_mps!=null&&rows[i].speed_mps>.55?1000/rows[i].speed_mps/60:null;
    rows[i].zone=viewerZoneIndex(rows[i].heart_rate,(profile&&profile.hr_zones)||defaultProfile().hr_zones);
  }

  const alt=rows.map(r=>r.altitude_m);
  const altSmooth=rows.map((_,i)=>rollingMedian(alt,i,5));
  const hasDistance=rows.some(r=>n(r.distance_m)!=null);
  if(hasDistance){
    for(let i=0;i<rows.length;i++){
      const d=n(rows[i].distance_m);if(d==null){rows[i].grade_pct=null;continue;}
      const j0=lowerBoundDistance(rows,Math.max(0,d-40));
      const j1=lowerBoundDistance(rows,d+40);
      const dd=n(rows[j1].distance_m)!=null&&n(rows[j0].distance_m)!=null?rows[j1].distance_m-rows[j0].distance_m:null;
      const da=altSmooth[j1]!=null&&altSmooth[j0]!=null?altSmooth[j1]-altSmooth[j0]:null;
      rows[i].grade_pct=dd!=null&&dd>=45&&da!=null?clamp(100*da/dd,-18,18):null;
    }
  }else{
    rows.forEach(r=>r.grade_pct=null);
  }
  return rows;
}
function streamRowsDownsample(rows,maxPoints){
  if(rows.length<=maxPoints)return rows;
  const out=[],step=(rows.length-1)/(maxPoints-1);
  for(let i=0;i<maxPoints;i++)out.push(rows[Math.min(rows.length-1,Math.round(i*step))]);
  return out;
}
function estimatedHrMax(activity){
  const observed=activities.map(a=>n(a.max_hr)).filter(x=>x!=null&&x>120&&x<230);
  const z4=n(profile&&profile.hr_zones&&profile.hr_zones.z4_max);
  const candidates=[n(activity&&activity.max_hr),observed.length?Math.max(...observed):null,z4!=null?z4+10:null,185].filter(x=>x!=null);
  return clamp(Math.max(...candidates),165,215);
}
function gradientNormalizedVo2(rows,activity){
  if(normalizeSport(activity.sport_category||activity.sport_type)!=="run")return null;
  const hrMax=estimatedHrMax(activity),hrRest=60;
  const samples=[];
  for(const r of rows){
    const speed=n(r.speed_mps),hr=n(r.heart_rate),grade=n(r.grade_pct);
    if(speed==null||hr==null||speed<1.6||speed>7||hr<hrRest+20||hr>=hrMax||grade==null)continue;
    const v=speed*60,g=clamp(grade/100,-.10,.15);
    const demand=0.2*v+0.9*v*g+3.5;
    const reserve=clamp((hr-hrRest)/(hrMax-hrRest),.35,.95);
    const estimate=3.5+(demand-3.5)/reserve;
    if(estimate>=20&&estimate<=90)samples.push(estimate);
  }
  return samples.length>=20?median(samples):null;
}
function gradientAdjustedPace(rows){
  const vals=[];
  for(const r of rows){
    const speed=n(r.speed_mps),grade=n(r.grade_pct);
    if(speed==null||speed<.7||grade==null)continue;
    const v=speed*60,g=clamp(grade/100,-.10,.15),demand=0.2*v+0.9*v*g+3.5;
    const flat=(demand-3.5)/0.2/60;
    if(flat>.5&&flat<8)vals.push(flat);
  }
  const eq=mean(vals);return eq?1000/eq/60:null;
}

function rowDuration(rows,i){
  if(i<=0||i>=rows.length)return 0;
  return clamp(n(rows[i].elapsed_s,0)-n(rows[i-1].elapsed_s,0),0,30);
}
function weightedRowMean(rows,key,predicate=null){
  let sw=0,sv=0;
  for(let i=1;i<rows.length;i++){
    if(predicate&&!predicate(rows[i],i))continue;
    const v=n(rows[i][key]),dt=rowDuration(rows,i);
    if(v==null||dt<=0)continue;sv+=v*dt;sw+=dt;
  }
  return sw>0?sv/sw:null;
}
function bestMeanOverWindow(rows,key,windowSec){
  const data=rows.map(r=>({t:n(r.elapsed_s),v:n(r[key])})).filter(x=>x.t!=null).sort((a,b)=>a.t-b.t);
  if(data.length<2)return null;
  let j=0,sumV=0,count=0,best=null;
  for(let i=0;i<data.length;i++){
    if(j<i){j=i;sumV=0;count=0;}
    while(j<data.length&&data[j].t-data[i].t<windowSec){
      if(data[j].v!=null&&data[j].v>0){sumV+=data[j].v;count++;}
      j++;
    }
    if(j-i>=2&&count>=2){
      const m=sumV/count;if(best==null||m>best)best=m;
    }
    if(data[i].v!=null&&data[i].v>0){sumV-=data[i].v;count--;}
  }
  return best;
}
function normalizedPowerFromRows(rows){
  const data=rows.map(r=>({t:n(r.elapsed_s),v:n(r.power_w)})).filter(x=>x.t!=null&&x.v!=null&&x.v>=0).sort((a,b)=>a.t-b.t);
  if(data.length<10)return data.length?mean(data.map(x=>x.v)):null;
  let left=0,sumP=0,count=0;const rolling=[];
  for(let right=0;right<data.length;right++){
    sumP+=data[right].v;count++;
    while(left<right&&data[right].t-data[left].t>30){sumP-=data[left].v;count--;left++;}
    if(data[right].t-data[left].t>=20&&count>=5)rolling.push(sumP/count);
  }
  return rolling.length?Math.pow(mean(rolling.map(v=>Math.pow(v,4))),.25):mean(data.map(x=>x.v));
}
function firstLastDrift(values){
  const a=values.filter(Number.isFinite);if(a.length<2)return null;
  const k=Math.max(1,Math.floor(a.length/3)),first=mean(a.slice(0,k)),last=mean(a.slice(-k));
  return first&&first>0?100*(last/first-1):null;
}
function kmEfficiencySummary(rows,isRun){
  const maxD=Math.max(0,...rows.map(r=>n(r.distance_m,0)));
  const kmCount=Math.floor(maxD/1000),rawEff=[],gapEff=[],splits=[];
  if(kmCount<1)return {raw_drift:null,gap_drift:null,splits:[]};
  const zoneWeights=[0,1,2,3,5,8,10];

  for(let km=1;km<=kmCount;km++){
    const lo=(km-1)*1000,hi=km*1000,sub=rows.filter(r=>n(r.distance_m)!=null&&r.distance_m>=lo&&r.distance_m<hi);
    if(sub.length<5)continue;

    let priorLoad=0,priorZ45=0,priorGapDistance=0,priorElevGain=0,priorTime=0;
    for(let i=1;i<rows.length;i++){
      const r=rows[i],prev=rows[i-1],d=n(r.distance_m);
      if(d==null||d>=lo)break;
      const dt=rowDuration(rows,i);if(dt<=0)continue;
      priorTime+=dt;
      const zi=clamp(Math.round(n(r.zone,0)),0,6);
      priorLoad+=(dt/60)*(zoneWeights[zi]||0);
      if(zi>=4)priorZ45+=dt/60;
      const gs=n(r.gap_speed_mps);if(gs!=null&&gs>0)priorGapDistance+=gs*dt/1000;
      const a=n(prev.altitude_m),b=n(r.altitude_m);
      if(a!=null&&b!=null&&b>a&&b-a<50)priorElevGain+=b-a;
    }

    const hr=weightedRowMean(sub,"heart_rate"),speed=weightedRowMean(sub,"speed_mps"),grade=weightedRowMean(sub,"grade_pct");
    const gap=weightedRowMean(sub,"gap_speed_mps");
    const eff=speed!=null&&hr?speed/hr:null,geff=gap!=null&&hr?gap/hr:null;
    if(eff!=null)rawEff.push(eff);if(geff!=null)gapEff.push(geff);
    splits.push({
      km,avg_hr:hr,avg_speed_mps:speed,avg_pace_min_km:speed?1000/speed/60:null,avg_gap_speed_mps:gap,
      avg_gap_pace_min_km:gap?1000/gap/60:null,avg_grade_pct:grade,eff_speed_per_hr:eff,eff_gap_speed_per_hr:geff,
      cumulative_duration_before_min:priorTime/60,cumulative_distance_before_km:lo/1000,
      cumulative_hr_load_before:priorLoad,cumulative_z45_before_min:priorZ45,
      cumulative_gap_distance_before_km:priorGapDistance,cumulative_elevation_gain_before_m:priorElevGain
    });
  }
  return {raw_drift:firstLastDrift(rawEff),gap_drift:isRun?firstLastDrift(gapEff):null,splits};
}
function ageAtActivity(activity){
  if(!profile||!profile.birthdate||!activity.start_time)return null;
  const b=new Date(profile.birthdate+"T00:00:00"),d=new Date(activity.start_time);
  if(Number.isNaN(b.getTime())||Number.isNaN(d.getTime()))return null;
  return (d-b)/(365.2425*86400000);
}
function previousActivityHours(activity){
  const t=new Date(activity.start_time).getTime();if(!Number.isFinite(t))return null;
  const prev=activities.map(a=>new Date(a.start_time).getTime()).filter(x=>Number.isFinite(x)&&x<t).sort((a,b)=>b-a)[0];
  return prev!=null?(t-prev)/3600000:null;
}
function calculateAdvancedMetrics(rows,activity){
  const sport=normalizeSport(activity.sport_category||activity.sport_type),isRun=sport==="run";
  const endT=rows.length?n(rows[rows.length-1].elapsed_s,0):0,half=endT/2;
  const first=r=>n(r.elapsed_s,0)<=half,second=r=>n(r.elapsed_s,0)>half;

  if(isRun){
    for(const r of rows){
      const speed=n(r.speed_mps),grade=n(r.grade_pct);
      if(speed!=null&&speed>0&&grade!=null){
        const g=clamp(grade/100,-.20,.20),v=speed*60;
        r.vo2_demand=0.2*v+0.9*v*g+3.5;
        r.gap_speed_mps=r.vo2_demand>3.5?(r.vo2_demand-3.5)/0.2/60:null;
      }else{r.vo2_demand=null;r.gap_speed_mps=null;}
    }
  }else rows.forEach(r=>{r.vo2_demand=null;r.gap_speed_mps=null;});

  const avgSpeed=weightedRowMean(rows,"speed_mps"),maxSpeed=Math.max(...rows.map(r=>n(r.speed_mps,-Infinity)));
  const avgHr=weightedRowMean(rows,"heart_rate");
  const firstHr=weightedRowMean(rows,"heart_rate",first),secondHr=weightedRowMean(rows,"heart_rate",second);
  const firstSpeed=weightedRowMean(rows,"speed_mps",first),secondSpeed=weightedRowMean(rows,"speed_mps",second);
  const firstEff=firstHr&&firstSpeed!=null?firstSpeed/firstHr:null,secondEff=secondHr&&secondSpeed!=null?secondSpeed/secondHr:null;
  const rawDrift=firstEff&&secondEff!=null?100*(secondEff/firstEff-1):null;

  const avgGap=weightedRowMean(rows,"gap_speed_mps"),firstGap=weightedRowMean(rows,"gap_speed_mps",first),secondGap=weightedRowMean(rows,"gap_speed_mps",second);
  const firstGapEff=firstHr&&firstGap!=null?firstGap/firstHr:null,secondGapEff=secondHr&&secondGap!=null?secondGap/secondHr:null;
  const gapDrift=firstGapEff&&secondGapEff!=null?100*(secondGapEff/firstGapEff-1):null;

  const powers=rows.map(r=>n(r.power_w)).filter(Number.isFinite),avgPower=weightedRowMean(rows,"power_w"),maxPower=powers.length?Math.max(...powers):null;
  const firstPower=weightedRowMean(rows,"power_w",first),secondPower=weightedRowMean(rows,"power_w",second);
  const firstPowerEff=firstHr&&firstPower!=null?firstPower/firstHr:null,secondPowerEff=secondHr&&secondPower!=null?secondPower/secondHr:null;
  const powerDrift=firstPowerEff&&secondPowerEff!=null?100*(secondPowerEff/firstPowerEff-1):null;
  const np=normalizedPowerFromRows(rows),vi=np!=null&&avgPower&&avgPower>0?np/avgPower:null;

  const cads=rows.map(r=>n(r.cadence)).filter(Number.isFinite);let stepVals=cads.slice(),stepSource="measured";
  if(isRun&&stepVals.length&&mean(stepVals)>=50&&mean(stepVals)<=120)stepVals=stepVals.map(v=>v*2);
  if(isRun&&!stepVals.length){
    stepVals=rows.map(r=>n(r.speed_mps)).filter(v=>v!=null&&v>0).map(v=>clamp(150+(v-2.2)*18,130,190));stepSource="speed_estimate";
  }
  const stepMean=isRun?mean(stepVals):null,stepMax=isRun&&stepVals.length?Math.max(...stepVals):null;
  const movingTime=n(activity.moving_time_s)??n(activity.duration_s)??endT;
  const steps=stepMean!=null&&movingTime?stepMean*(movingTime/60):null;

  const impactVals=[];
  if(isRun){
    for(const r of rows){
      const sp=n(r.speed_mps),g=n(r.grade_pct,0);if(sp==null||sp<=0)continue;
      let cad=n(r.cadence);
      if(cad==null)cad=clamp(150+(sp-2.2)*18,130,190);else if(cad>=50&&cad<=120)cad*=2;
      const downhill=g<0?Math.abs(g):0,uphill=g>0?g:0;
      impactVals.push(clamp(1.55+0.20*sp+0.006*(cad-160)+0.035*downhill-0.012*uphill,1.1,4.5));
    }
  }
  const avgImpact=mean(impactVals),maxImpact=impactVals.length?Math.max(...impactVals):null,impactLoad=avgImpact!=null&&steps!=null?avgImpact*steps/1000:null;

  let elevGain=0,elevLoss=0;
  for(let i=1;i<rows.length;i++){
    const a=n(rows[i-1].altitude_m),b=n(rows[i].altitude_m);if(a==null||b==null)continue;
    const d=b-a;if(d>.5&&d<50)elevGain+=d;else if(d<-.5&&d>-50)elevLoss+=-d;
  }
  const verticalSpeed=elevGain>0&&movingTime>0?elevGain/(movingTime/3600):null;
  let movingSec=0,totalSec=0;
  for(let i=1;i<rows.length;i++){const dt=rowDuration(rows,i);if(!dt)continue;totalSec+=dt;if(n(rows[i].speed_mps,0)>.35)movingSec+=dt;}
  const movingRatio=totalSec?movingSec/totalSec:null;

  const km=kmEfficiencySummary(rows,isRun);
  const best1=bestMeanOverWindow(rows,"speed_mps",60),best5=bestMeanOverWindow(rows,"speed_mps",300),
    best10=bestMeanOverWindow(rows,"speed_mps",600),best20=bestMeanOverWindow(rows,"speed_mps",1200);
  const bestP1=bestMeanOverWindow(rows,"power_w",60),bestP5=bestMeanOverWindow(rows,"power_w",300),bestP20=bestMeanOverWindow(rows,"power_w",1200);

  const weight=n(activity.body_weight_kg)??n(profile&&profile.current_weight_kg),height=n(profile&&profile.height_cm);
  const bmi=weight&&height?weight/Math.pow(height/100,2):null;
  const gpsCount=rows.filter(r=>r.lat!=null&&r.lon!=null).length,hrCount=rows.filter(r=>r.heart_rate!=null).length,powerCount=rows.filter(r=>r.power_w!=null).length;
  const vo2Demand=weightedRowMean(rows,"vo2_demand");

  return {
    advanced_metrics_version:"web_v4",
    avg_speed_mps:avgSpeed,max_speed_mps:Number.isFinite(maxSpeed)?maxSpeed:null,max_speed_kmh:Number.isFinite(maxSpeed)?maxSpeed*3.6:null,
    moving_ratio:movingRatio,elevation_loss_m:elevLoss||null,avg_vertical_speed_m_per_h:verticalSpeed,
    first_half_avg_hr:firstHr,second_half_avg_hr:secondHr,first_half_avg_speed_mps:firstSpeed,second_half_avg_speed_mps:secondSpeed,
    first_half_eff_speed_per_hr:firstEff,second_half_eff_speed_per_hr:secondEff,hr_efficiency_drift_pct:rawDrift,
    avg_gap_speed_mps:avgGap,avg_gap_pace_min_km:avgGap?1000/avgGap/60:null,
    first_half_avg_gap_speed_mps:firstGap,second_half_avg_gap_speed_mps:secondGap,
    first_half_eff_gap_speed_per_hr:firstGapEff,second_half_eff_gap_speed_per_hr:secondGapEff,gap_hr_efficiency_drift_pct:gapDrift,
    km_hr_efficiency_drift_pct:km.raw_drift,km_gap_hr_efficiency_drift_pct:km.gap_drift,
    vo2_demand_est_ml_kg_min:vo2Demand,
    best_1min_pace_min_km:best1?1000/best1/60:null,best_5min_pace_min_km:best5?1000/best5/60:null,
    best_10min_pace_min_km:best10?1000/best10/60:null,best_20min_pace_min_km:best20?1000/best20/60:null,
    best_1min_power_w:bestP1,best_5min_power_w:bestP5,best_20min_power_w:bestP20,
    avg_power_w:avgPower,max_power_w:maxPower,normalized_power_w:np,variability_index:vi,
    first_half_power_hr_efficiency:firstPowerEff,second_half_power_hr_efficiency:secondPowerEff,power_hr_efficiency_drift_pct:powerDrift,
    power_hr_efficiency:avgPower!=null&&avgHr?avgPower/avgHr:null,
    run_step_frequency_spm:stepMean,max_step_frequency_spm:stepMax,estimated_total_steps:steps,step_frequency_source:isRun?stepSource:null,
    avg_impact_bw:avgImpact,max_impact_bw:maxImpact,impact_load_index:impactLoad,
    avg_grade_pct:weightedRowMean(rows,"grade_pct"),
    min_grade_pct:(()=>{const v=rows.map(r=>n(r.grade_pct)).filter(Number.isFinite);return v.length?Math.min(...v):null;})(),
    max_grade_pct:(()=>{const v=rows.map(r=>n(r.grade_pct)).filter(Number.isFinite);return v.length?Math.max(...v):null;})(),
    data_point_count:rows.length,gps_point_count:gpsCount,hr_point_count:hrCount,power_point_count:powerCount,
    has_gps:gpsCount>0?1:0,has_altitude:rows.some(r=>r.altitude_m!=null)?1:0,has_hr:hrCount>0?1:0,has_power:powerCount>0?1:0,
    bmi:bmi,age_years_at_activity:ageAtActivity(activity),time_since_previous_h:previousActivityHours(activity),
    km_splits:km.splits,
    impact_force_note:"Heuristic bodyweight-multiple estimate, not measured ground-reaction force."
  };
}
function derivedStreamSummary(rows,activity){
  const adv=calculateAdvancedMetrics(rows,activity);
  const vo2=gradientNormalizedVo2(rows,activity),gap=adv.avg_gap_pace_min_km;
  return {vo2,gap,max_speed_kmh:adv.max_speed_kmh,max_power_w:adv.max_power_w,
    max_cadence:adv.max_step_frequency_spm??(rows.map(r=>n(r.cadence)).filter(Number.isFinite).length?Math.max(...rows.map(r=>n(r.cadence)).filter(Number.isFinite)):null),
    avg_grade_pct:adv.avg_grade_pct,min_grade_pct:Number.isFinite(adv.min_grade_pct)?adv.min_grade_pct:null,
    max_grade_pct:Number.isFinite(adv.max_grade_pct)?adv.max_grade_pct:null,advanced:adv};
}
async function persistAdvancedMetrics(activity,rows){
  const d=derivedStreamSummary(rows,activity),adv=d.advanced,updates={},mj={...safeJson(activity.metrics_json,{})};
  for(const [k,v] of Object.entries(adv))if(k!=="km_splits"&&v!==undefined)mj[k]=v;
  if(Array.isArray(adv.km_splits))mj.km_splits=adv.km_splits;
  mj.vo2_estimate_method="ACSM running oxygen cost + grade + HR-reserve heuristic";
  mj.metrics_quality="stream-derived; corrected distance/time speed; advanced metrics v4";

  if(d.vo2!=null){updates.own_vo2max_estimate=Math.round(d.vo2*10)/10;updates.estimated_vo2max=Math.round(d.vo2*10)/10;}
  if(d.gap!=null)updates.avg_gap_pace_min_km=d.gap;
  if(adv.elevation_loss_m!=null)updates.elevation_loss_m=adv.elevation_loss_m;
  if(adv.hr_efficiency_drift_pct!=null)updates.hr_efficiency_drift_pct=adv.hr_efficiency_drift_pct;
  if(adv.gap_hr_efficiency_drift_pct!=null)updates.gap_hr_efficiency_drift_pct=adv.gap_hr_efficiency_drift_pct;
  if(adv.km_gap_hr_efficiency_drift_pct!=null)updates.km_gap_hr_efficiency_drift_pct=adv.km_gap_hr_efficiency_drift_pct;
  if(adv.normalized_power_w!=null)updates.normalized_power=adv.normalized_power_w;
  updates.metrics_json=mj;

  const res=await client.from("activities").update(updates).eq("id",activity.id).eq("user_id",userId());
  if(!res.error){Object.assign(activity,updates);activity.metrics_json=mj;}
  return d;
}
async function maybePersistDerivedMetrics(activity,rows){
  const mj=safeJson(activity.metrics_json,{});
  if(mj.advanced_metrics_version==="web_v4")return derivedStreamSummary(rows,activity);
  return persistAdvancedMetrics(activity,rows);
}

async function enrichMetricsForActivities(rows){
  const todo=rows.filter(a=>safeJson(a.metrics_json,{}).advanced_metrics_version!=="web_v4");
  let done=0;
  for(const a of todo){
    try{
      const stream=await getWorkoutStream(a.id),built=buildStreamRows(stream);
      if(built.length)await persistAdvancedMetrics(a,built);
    }catch(err){console.warn("Advanced metric enrichment failed",a.id,err);}
    done++;
    if(done%5===0)setSync("Derived advanced metrics "+done+" / "+todo.length+"…");
  }
  return done;
}
function solveLinearSystem(matrix,vector){
  const n=vector.length,a=matrix.map((row,i)=>row.slice().concat([vector[i]]));
  for(let col=0;col<n;col++){
    let pivot=col;
    for(let r=col+1;r<n;r++)if(Math.abs(a[r][col])>Math.abs(a[pivot][col]))pivot=r;
    if(Math.abs(a[pivot][col])<1e-10)return null;
    [a[col],a[pivot]]=[a[pivot],a[col]];
    const div=a[col][col];for(let c=col;c<=n;c++)a[col][c]/=div;
    for(let r=0;r<n;r++){
      if(r===col)continue;
      const factor=a[r][col];if(!factor)continue;
      for(let c=col;c<=n;c++)a[r][c]-=factor*a[col][c];
    }
  }
  return a.map(row=>row[n]);
}
function fitRidgeRegression(samples,lambda=.8){
  if(samples.length<10)return null;
  const p=samples[0].x.length,means=[],stds=[];
  for(let j=0;j<p;j++){
    const vals=samples.map(s=>s.x[j]);const m=mean(vals)??0;
    const variance=mean(vals.map(v=>(v-m)*(v-m)))??0;
    means[j]=m;stds[j]=Math.sqrt(variance)>1e-8?Math.sqrt(variance):1;
  }
  const dim=p+1,xtx=Array.from({length:dim},()=>Array(dim).fill(0)),xty=Array(dim).fill(0);
  for(const sample of samples){
    const x=[1,...sample.x.map((v,j)=>(v-means[j])/stds[j])],y=sample.y;
    for(let r=0;r<dim;r++){
      xty[r]+=x[r]*y;
      for(let c=0;c<dim;c++)xtx[r][c]+=x[r]*x[c];
    }
  }
  for(let j=1;j<dim;j++)xtx[j][j]+=lambda;
  const beta=solveLinearSystem(xtx,xty);if(!beta)return null;
  const predict=x=>{
    let y=beta[0];
    for(let j=0;j<p;j++)y+=beta[j+1]*((x[j]-means[j])/stds[j]);
    return y;
  };
  return {beta,means,stds,predict};
}

function previousTrainingContext(activity){
  const t=new Date(activity.start_time).getTime();
  if(!Number.isFinite(t))return {load7:0,baseline28:0,recovery_hours:168};
  let l7=0,l28=0,last=null;
  for(const other of activities){
    const ot=new Date(other.start_time).getTime();
    if(!Number.isFinite(ot)||ot>=t)continue;
    const dt=(t-ot)/3600000,load=derivedLoad(other);
    if(dt<=7*24)l7+=load;
    if(dt<=28*24)l28+=load;
    if(last==null||ot>last)last=ot;
  }
  const recovery=last==null?168:clamp((t-last)/3600000,0,336);
  return {load7:l7,baseline28:l28/4,recovery_hours:Math.log1p(recovery)};
}
function fatigueFeatureVector(split,weather=null,useWeather=false,context=null){
  const gap=n(split.avg_gap_speed_mps),time=n(split.cumulative_duration_before_min),
    load=n(split.cumulative_hr_load_before),z45=n(split.cumulative_z45_before_min),
    dist=n(split.cumulative_distance_before_km);
  if([gap,time,load,z45,dist].some(v=>v==null))return null;
  const c=context||{load7:0,baseline28:0,recovery_hours:0};
  const base=[gap,gap*gap,time,load,z45,dist,n(c.load7,0),n(c.baseline28,0),n(c.recovery_hours,0)];
  if(!useWeather)return base;
  const temp=n(weather&&weather.mean_temp_c),humidity=n(weather&&weather.mean_humidity_pct);
  if(temp==null||humidity==null)return null;
  return [...base,temp,humidity];
}
function personalFatigueSamples(excludeActivityId=null,useWeather=false){
  const cutoff=daysAgo(180),samples=[],runIds=new Set(),dates=[];
  for(const a of activities){
    if(a.id===excludeActivityId||normalizeSport(a.sport_category||a.sport_type)!=="run"||new Date(a.start_time)<cutoff)continue;
    const splits=safeJson(a.metrics_json,{}).km_splits;
    if(!Array.isArray(splits))continue;
    const weather=weatherNumbers(a),prior=previousTrainingContext(a);
    for(const split of splits){
      const hr=n(split.avg_hr),x=fatigueFeatureVector(split,weather,useWeather,prior);
      if(hr==null||hr<90||hr>205||!x||x[0]<1.2||x[0]>7)continue;
      samples.push({x,y:hr,activity_id:a.id});runIds.add(a.id);dates.push(new Date(a.start_time).getTime());
    }
  }
  return {samples,runIds,dates};
}
function fitPersonalFatigueReference(excludeActivityId=null,useWeather=false){
  const {samples,runIds,dates}=personalFatigueSamples(excludeActivityId,useWeather);
  if(samples.length<20||runIds.size<3)return null;

  let fit=fitRidgeRegression(samples,useWeather?1.1:.8);if(!fit)return null;
  const residuals=samples.map(v=>v.y-fit.predict(v.x)),med=median(residuals)??0;
  const mad=median(residuals.map(r=>Math.abs(r-med)))??0,limit=Math.max(10,3*1.4826*mad);
  const clean=samples.filter((v,i)=>Math.abs(residuals[i]-med)<=limit);
  if(clean.length>=Math.max(18,Math.floor(samples.length*.65))){
    const robust=fitRidgeRegression(clean,useWeather?1.1:.8);if(robust)fit=robust;
  }
  const used=clean.length>=18?clean:samples;
  const rmse=Math.sqrt(mean(used.map(v=>Math.pow(v.y-fit.predict(v.x),2)))??0);
  const latest=dates.length?Math.max(...dates):0;
  const coeff=fit.beta.map(v=>Math.round(v*1000)/1000).join(",");
  const signature=[latest,samples.length,runIds.size,useWeather?"weather":"base",coeff,excludeActivityId||"global"].join("|");
  return {...fit,n_splits:samples.length,n_runs:runIds.size,rmse,signature,use_weather:useWeather};
}


const ML_FEATURE_LABELS=["GAP speed","GAP speed²","Prior duration","Cumulative HR load","Prior Z4/Z5","Prior distance","7-day pre-run load","28-day weekly baseline","Recovery interval","Temperature","Humidity"];
function mlTreeValue(tree,x){
  if(tree.value!=null)return tree.value;
  return mlTreeValue(x[tree.feature]<=tree.threshold?tree.left:tree.right,x);
}
function trainMlTree(X,y,idx,depth,minLeaf,maxDepth=2){
  const vals=idx.map(i=>y[i]),base=mean(vals)??0;
  if(depth>=maxDepth||idx.length<minLeaf*2)return {value:base};
  const baseSse=vals.reduce((v,z)=>v+(z-base)**2,0);
  let best=null,features=X[0].length;
  for(let j=0;j<features;j++){
    const sorted=idx.slice().sort((a,b)=>X[a][j]-X[b][j]);
    const candidates=[];
    for(let q=1;q<=9;q++){
      const k=Math.floor(q*sorted.length/10);
      if(k<minLeaf||k>sorted.length-minLeaf)continue;
      const lo=X[sorted[k-1]][j],hi=X[sorted[k]][j];
      if(hi>lo)candidates.push((lo+hi)/2);
    }
    for(const threshold of candidates){
      let ln=0,rn=0,ls=0,rs=0,lss=0,rss=0;
      for(const i of idx){
        const v=y[i];if(X[i][j]<=threshold){ln++;ls+=v;lss+=v*v;}else{rn++;rs+=v;rss+=v*v;}
      }
      if(ln<minLeaf||rn<minLeaf)continue;
      const gain=baseSse-(lss-ls*ls/ln+rss-rs*rs/rn);
      if(gain>1e-5&&(!best||gain>best.gain))best={feature:j,threshold,gain};
    }
  }
  if(!best)return {value:base};
  const left=[],right=[];
  for(const i of idx)(X[i][best.feature]<=best.threshold?left:right).push(i);
  return {feature:best.feature,threshold:best.threshold,gain:best.gain,
    left:trainMlTree(X,y,left,depth+1,minLeaf,maxDepth),right:trainMlTree(X,y,right,depth+1,minLeaf,maxDepth)};
}
function fitGradientBoostResidual(samples,ridge,options={}){
  if(!ridge||samples.length<24)return null;
  const X=samples.map(s=>s.x),y=samples.map(s=>s.y-ridge.predict(s.x));
  const pred=Array(samples.length).fill(0),trees=[],lr=.065;
  const rounds=options.rounds||28,minLeaf=Math.max(6,Math.floor(samples.length*.035));
  const importance=Array(X[0].length).fill(0),idx=X.map((_,i)=>i);
  const addImportance=t=>{
    if(t.value!=null)return;
    importance[t.feature]+=t.gain;
    addImportance(t.left);addImportance(t.right);
  };
  for(let it=0;it<rounds;it++){
    const residuals=y.map((v,i)=>v-pred[i]);
    const tree=trainMlTree(X,residuals,idx,0,minLeaf,2);
    if(tree.value!=null)break;
    addImportance(tree);
    trees.push(tree);
    for(let i=0;i<X.length;i++)pred[i]+=lr*mlTreeValue(tree,X[i]);
  }
  if(!trees.length)return null;
  return {trees,learning_rate:lr,importance,
    predict:x=>ridge.predict(x)+lr*trees.reduce((acc,tree)=>acc+mlTreeValue(tree,x),0)};
}
function mlSamplesSignature(samples){
  return samples.map(s=>[s.activity_id,s.y.toFixed(2),s.x.map(x=>x.toFixed(2)).join(",")].join(":")).join("|");
}
function mlErrorSummary(errors){
  if(!errors.length)return null;
  const byRun=new Map();
  for(const e of errors){
    if(!byRun.has(e.id))byRun.set(e.id,[]);
    byRun.get(e.id).push(e.error);
  }
  const perRun=Array.from(byRun.values()).map(v=>mean(v.map(x=>Math.abs(x))));
  return {
    mae:mean(errors.map(e=>Math.abs(e.error))),
    workout_mae:mean(perRun),
    rmse:Math.sqrt(mean(errors.map(e=>e.error*e.error))),
    bias:mean(errors.map(e=>e.error)),
    runs:byRun.size,points:errors.length
  };
}
async function computeMlModelReport(force=false){
  const weatherData=personalFatigueSamples(null,true);
  const basicData=personalFatigueSamples(null,false);
  const useWeather=weatherData.runIds.size>=8&&weatherData.samples.length>=Math.max(100,basicData.samples.length*.7);
  const data=useWeather?weatherData:basicData;
  const signature=mlSamplesSignature(data.samples)+"|"+useWeather;
  if(!force&&mlModelReport&&mlModelSignature===signature)return mlModelReport;
  if(mlAnalysisBusy)return mlModelReport;
  mlAnalysisBusy=true;
  const status=el("mlAnalysisStatus");
  const report={version:"hr_ml_v1",status:"insufficient",selected:"ridge",uses_weather:useWeather,
    n_runs:data.runIds.size,n_km_splits:data.samples.length,feature_importance:[]};
  try{
    if(status)status.textContent="Validating Ridge vs Gradient Boosting across whole workouts…";
    if(data.runIds.size<4||data.samples.length<24){
      report.reason="Need at least 4 runs and 24 reliable kilometre splits for a personal reference.";
    }else{
      const groups=Array.from(data.runIds),testRidge=[],testBoost=[],mlEligible=data.runIds.size>=8&&data.samples.length>=100;
      for(let k=0;k<groups.length;k++){
        const hold=groups[k],train=data.samples.filter(s=>s.activity_id!==hold),test=data.samples.filter(s=>s.activity_id===hold);
        if(train.length<20)continue;
        const ridge=fitPersonalFatigueReference(hold,useWeather);
        if(!ridge)continue;
        const boosting=mlEligible?fitGradientBoostResidual(train,ridge):null;
        for(const sample of test){
          testRidge.push({id:hold,error:sample.y-ridge.predict(sample.x)});
          if(boosting)testBoost.push({id:hold,error:sample.y-boosting.predict(sample.x)});
        }
        if(k%3===0)await new Promise(resolve=>setTimeout(resolve,0));
      }
      const ridge=mlErrorSummary(testRidge),boost=mlEligible?mlErrorSummary(testBoost):null;
      report.ridge_cv=ridge;
      report.boost_cv=boost;
      report.status=ridge?"ready":"insufficient";
      report.reason=mlEligible?null:"Gradient Boosting requires ≥8 independent runs and ≥100 valid kilometre splits.";
      // Require a material MAE improvement, no worse RMSE, and no material additional bias.
      if(ridge&&boost&&boost.runs===ridge.runs&&boost.workout_mae<=ridge.workout_mae*.95&&
        boost.rmse<=ridge.rmse*1.01&&Math.abs(boost.bias)<=Math.abs(ridge.bias)+.75){
        report.selected="gradient_boosting";
      }
      const allRidge=fitRidgeRegression(data.samples,useWeather?1.1:.8);
      const fullBoost=allRidge&&mlEligible?fitGradientBoostResidual(data.samples,allRidge):null;
      if(fullBoost){
        const tot=sum(fullBoost.importance);
        report.feature_importance=fullBoost.importance.map((gain,i)=>({
          name:ML_FEATURE_LABELS[i],importance:tot>0?100*gain/tot:0
        })).sort((a,b)=>b.importance-a.importance).slice(0,6);
      }
    }
    mlModelReport=report;mlModelSignature=signature;
    renderMlAnalysis();
    return report;
  }finally{
    mlAnalysisBusy=false;
    if(status&&report.status!=="ready")status.textContent=report.reason||"Not enough data yet.";
  }
}
function compactMlForAi(){
  const r=mlModelReport;if(!r)return {status:"not_calculated"};
  return {
    method:r.selected,validation:"leave_one_workout_out",runs:r.n_runs,km_splits:r.n_km_splits,
    weather_in_model:r.uses_weather,
    ridge_mae_bpm:r.ridge_cv&&Math.round(r.ridge_cv.workout_mae*100)/100,
    boosting_mae_bpm:r.boost_cv&&Math.round(r.boost_cv.workout_mae*100)/100,
    ridge_rmse_bpm:r.ridge_cv&&Math.round(r.ridge_cv.rmse*100)/100,
    boosting_rmse_bpm:r.boost_cv&&Math.round(r.boost_cv.rmse*100)/100,
    main_features:(r.feature_importance||[]).slice(0,4)
  };
}
function renderMlAnalysis(){
  const r=mlModelReport,host=el("mlAnalysisMetrics"),status=el("mlAnalysisStatus");
  if(!host||!status)return;
  if(!r){
    status.textContent="Computing personal model after advanced workout metrics have been enriched.";
    host.innerHTML="";return;
  }
  const ready=r.status==="ready";
  status.textContent=ready?
    "Leave-one-workout-out validation · "+r.n_runs+" runs / "+r.n_km_splits+" kilometre splits"+
      (r.uses_weather?" · weather-aware":" · insufficient weather coverage for weather-aware fitting"):
    (r.reason||"Not enough comparable running data.");
  const cards=[
    ["Selected model",ready?(r.selected==="gradient_boosting"?"Gradient Boosting":"Ridge"):"Insufficient data","Selected only when boosted model wins on held-out runs"],
    ["Ridge error",r.ridge_cv?fmt(r.ridge_cv.workout_mae,1," bpm"):"—","Average per-workout cross-validated MAE"],
    ["Boosting error",r.boost_cv?fmt(r.boost_cv.workout_mae,1," bpm"):"Not eligible","Same held-out workout comparison"],
    ["Training evidence",r.n_km_splits+" km",r.n_runs+" independent runs · 180-day window"]
  ];
  host.innerHTML=cards.map(c=>'<div class="analysis-card"><div class="eyebrow">'+escapeHtml(c[0])+'</div><div class="value">'+escapeHtml(c[1])+
    '</div><div class="note">'+escapeHtml(c[2])+"</div></div>").join("");
  const names=["Ridge","Gradient Boosting"],vals=[r.ridge_cv&&r.ridge_cv.workout_mae,r.boost_cv&&r.boost_cv.workout_mae];
  const valid=names.map((name,i)=>({name,value:vals[i]})).filter(x=>x.value!=null);
  Plotly.react("mlModelComparison",[{x:valid.map(x=>x.name),y:valid.map(x=>x.value),type:"bar"}],
    {margin:{l:52,r:15,t:15,b:45},yaxis:{title:"Held-out MAE (bpm)",rangemode:"tozero"},paper_bgcolor:"transparent",plot_bgcolor:"transparent"},
    {responsive:true,displaylogo:false});
  const imp=r.feature_importance||[],top=imp.slice(0,6).reverse();
  Plotly.react("mlFeatureImportance",[{x:top.map(x=>x.importance),y:top.map(x=>x.name),type:"bar",orientation:"h"}],
    {margin:{l:118,r:12,t:15,b:45},xaxis:{title:"Relative split-gain (%)",rangemode:"tozero"},paper_bgcolor:"transparent",plot_bgcolor:"transparent"},
    {responsive:true,displaylogo:false});
  el("mlAnalysisExplanation").textContent=
    "Tree split-gain describes the candidate Gradient Boosting model (even when Ridge wins); it is predictive importance, not causal attribution. The selected model predicts HR conditioned on GAP speed, prior effort and "+
    (r.uses_weather?"weather.":"available prior-effort metrics.")+
    " The current run is always excluded from its own HR reference. Read differences below about 2–3 bpm cautiously; training state, hydration and sensor noise also affect HR.";
}
function fatigueScoreSummary(splits){
  const valid=splits.filter(s=>n(s.fatigue_adjusted_efficiency_score)!=null);
  if(!valid.length)return null;
  const scores=valid.map(s=>n(s.fatigue_adjusted_efficiency_score)),residuals=valid.map(s=>n(s.hr_residual_vs_expected_bpm)).filter(Number.isFinite);
  const k=Math.max(1,Math.floor(scores.length/3)),first=mean(scores.slice(0,k)),last=mean(scores.slice(-k));
  return {
    mean:mean(scores),median:median(scores),last_third:last,min:Math.min(...scores),
    drift:first&&last!=null?100*(last/first-1):null,
    residual_bpm:mean(residuals)
  };
}


async function applyPersonalFatigueEfficiency(){
  const report=await computeMlModelReport();
  const cutoff=daysAgo(180),candidates=activities.filter(a=>
    normalizeSport(a.sport_category||a.sport_type)==="run"&&new Date(a.start_time)>=cutoff
  );
  let changed=0;
  for(const a of candidates){
    const ownWeather=weatherNumbers(a);
    const canWeather=ownWeather.mean_temp_c!=null&&ownWeather.mean_humidity_pct!=null;
    // Compare and train on identical feature spaces. For runs without weather,
    // use the established no-weather Ridge reference rather than fake imputation.
    const useWeather=!!(report&&report.uses_weather&&canWeather);
    let ridge=fitPersonalFatigueReference(a.id,useWeather);
    if(!ridge&&useWeather)ridge=fitPersonalFatigueReference(a.id,false);
    if(!ridge)continue;

    const useBoost=report&&report.selected==="gradient_boosting"&&
      ridge.use_weather===!!report.uses_weather;
    const heldOutTraining=personalFatigueSamples(a.id,ridge.use_weather).samples;
    const boosted=useBoost?fitGradientBoostResidual(heldOutTraining,ridge):null;
    const model=boosted||ridge,method=boosted?"gradient_boosting":"ridge";
    const signature=ridge.signature+"|"+method+"|hr_ml_v1_hist";
    const mj=safeJson(a.metrics_json,{}),splits=Array.isArray(mj.km_splits)?mj.km_splits.map(x=>({...x})):[];
    if(!splits.length)continue;
    if(mj.fatigue_model_version==="personal180_ml_loo_v5"&&mj.fatigue_model_signature===signature)continue;

    const priorContext=previousTrainingContext(a);
    for(const split of splits){
      const x=fatigueFeatureVector(split,ownWeather,ridge.use_weather,priorContext),actual=n(split.avg_hr);
      if(!x||actual==null||actual<90||actual>205)continue;
      const expected=clamp(model.predict(x),85,205);
      split.expected_hr_personal=Math.round(expected*10)/10;
      split.hr_residual_vs_expected_bpm=Math.round((actual-expected)*10)/10;
      split.fatigue_adjusted_efficiency_score=Math.round((100*expected/actual)*10)/10;
      split.weather_adjusted_reference=ridge.use_weather;
      split.hr_prediction_model=method;
    }
    const summary=fatigueScoreSummary(splits);if(!summary)continue;
    mj.km_splits=splits;
    mj.fatigue_adjusted_efficiency_score=Math.round(n(summary.median,summary.mean)*10)/10;
    mj.fatigue_adjusted_efficiency_mean=Math.round(n(summary.mean)*10)/10;
    mj.fatigue_adjusted_efficiency_last_third=Math.round(n(summary.last_third)*10)/10;
    mj.fatigue_adjusted_efficiency_drift_pct=summary.drift==null?null:Math.round(summary.drift*10)/10;
    mj.fatigue_hr_residual_bpm=summary.residual_bpm==null?null:Math.round(summary.residual_bpm*10)/10;
    mj.fatigue_reference_n_splits=ridge.n_splits;
    mj.fatigue_reference_n_runs=ridge.n_runs;
    mj.fatigue_reference_rmse_bpm=Math.round(ridge.rmse*10)/10;
    mj.fatigue_reference_uses_weather=ridge.use_weather?1:0;
    mj.fatigue_model_version="personal180_ml_loo_v5";
    mj.fatigue_model_signature=signature;
    mj.hr_prediction_model=method;
    mj.fatigue_efficiency_note=
      "100 = personal expected HR for GAP pace and prior fatigue, optionally weather-adjusted, using whole-run-excluded "+
      method+"; >100 means lower HR than expected. Predictive, not medical.";
    const res=await client.from("activities").update({metrics_json:mj}).eq("id",a.id).eq("user_id",userId());
    if(!res.error){a.metrics_json=mj;changed++;}
  }
  renderMlAnalysis();
  return changed;
}

async function enrichMetricsInBackground(){
  if(metricsEnrichmentRunning||!client||!userId())return;
  metricsEnrichmentRunning=true;
  try{
    const changed=await enrichMetricsForActivities(activities);
    setSync("Learning personal fatigue-adjusted HR reference…");
    const fatigueChanged=await applyPersonalFatigueEfficiency();
    if(changed||fatigueChanged){renderHistory();renderAnalysis();renderPlanner();}
  }finally{metricsEnrichmentRunning=false;setSync("Synced");}
}

async function getWorkoutStream(id){
  if(streamCache.has(id))return streamCache.get(id);
  const res=await client.from("activity_streams").select("stream_data").eq("activity_id",id).maybeSingle();
  if(res.error)throw new Error("Could not load workout stream: "+res.error.message);
  const stream=res.data?safeJson(res.data.stream_data,{}):{};
  streamCache.set(id,stream);return stream;
}

const WEATHER_VERSION="openmeteo_v3_peak_elevation";
function utcDateString(ms){return new Date(ms).toISOString().slice(0,10);}
function weatherNumbers(a){
  const m=safeJson(a&&a.metrics_json,{});
  return {
    start_temp_c:n(m.weather_start_temp_c),start_humidity_pct:n(m.weather_start_humidity_pct),
    end_temp_c:n(m.weather_end_temp_c),end_humidity_pct:n(m.weather_end_humidity_pct),
    min_temp_c:n(m.weather_min_temp_c),max_temp_c:n(m.weather_max_temp_c),
    min_humidity_pct:n(m.weather_min_humidity_pct),max_humidity_pct:n(m.weather_max_humidity_pct),
    mean_temp_c:n(m.weather_mean_temp_c),mean_humidity_pct:n(m.weather_mean_humidity_pct),
    peak_temp_c:n(m.weather_peak_temp_c),peak_humidity_pct:n(m.weather_peak_humidity_pct),
    peak_altitude_m:n(m.weather_peak_altitude_m),peak_time:m.weather_peak_time||null
  };
}
function nearestOrInterpolatedWeather(series,targetMs){
  if(!series||!series.times||!series.times.length)return {temp:null,humidity:null};
  const times=series.times;let hi=0;
  while(hi<times.length&&times[hi]<targetMs)hi++;
  if(hi<=0)return {temp:n(series.temp[0]),humidity:n(series.humidity[0])};
  if(hi>=times.length){const i=times.length-1;return {temp:n(series.temp[i]),humidity:n(series.humidity[i])};}
  const lo=hi-1,t0=times[lo],t1=times[hi],f=t1>t0?clamp((targetMs-t0)/(t1-t0),0,1):0;
  const interp=(arr)=>{
    const a=n(arr[lo]),b=n(arr[hi]);
    if(a==null)return b;if(b==null)return a;return a+(b-a)*f;
  };
  return {temp:interp(series.temp),humidity:interp(series.humidity)};
}
function seriesWindowValues(series,startMs,endMs,padMs){
  const out=[];
  if(!series||!series.times)return out;
  for(let i=0;i<series.times.length;i++){
    const t=series.times[i];if(t<startMs-padMs||t>endMs+padMs)continue;
    const temp=n(series.temp[i]),humidity=n(series.humidity[i]);
    if(temp!=null||humidity!=null)out.push({t,temp,humidity});
  }
  return out;
}
async function fetchOpenMeteoSeries(lat,lon,startMs,endMs,elevation=null){
  const startDate=utcDateString(startMs-3600000),endDate=utcDateString(endMs+3600000);
  const recent=endMs>=Date.now()-91*86400000;
  const modern=startMs>=Date.UTC(2022,0,1);
  const candidates=[];
  if(recent)candidates.push({url:"https://api.open-meteo.com/v1/forecast",label:"Open-Meteo forecast archive",fine:true});
  if(modern)candidates.push({url:"https://historical-forecast-api.open-meteo.com/v1/forecast",label:"Open-Meteo historical forecast",fine:true});
  candidates.push({url:"https://archive-api.open-meteo.com/v1/archive",label:"Open-Meteo historical weather",fine:false});

  let lastError=null;
  for(const c of candidates){
    try{
      const p=new URLSearchParams({
        latitude:String(lat),longitude:String(lon),start_date:startDate,end_date:endDate,
        hourly:"temperature_2m,relative_humidity_2m",timezone:"GMT",timeformat:"unixtime"
      });
      const recordedAltitude=n(elevation);
      // Open-Meteo supports downscaling to GPS altitude for mountain summits.
      if(recordedAltitude!=null&&recordedAltitude>=-400&&recordedAltitude<=9000)
        p.set("elevation",String(Math.round(recordedAltitude)));
      if(c.fine)p.set("minutely_15","temperature_2m,relative_humidity_2m");
      const response=await fetch(c.url+"?"+p.toString());
      if(!response.ok){lastError=new Error(c.label+" HTTP "+response.status);continue;}
      const data=await response.json();
      let src=data.minutely_15;
      let resolutionMin=15;
      if(!src||!Array.isArray(src.time)||src.time.length<2){src=data.hourly;resolutionMin=60;}
      if(!src||!Array.isArray(src.time)||!src.time.length)continue;
      const temp=src.temperature_2m||[],humidity=src.relative_humidity_2m||[];
      if(!temp.length&&!humidity.length)continue;
      return {
        times:src.time.map(v=>Number(v)*1000),temp,humidity,
        source:c.label,resolution_min:resolutionMin
      };
    }catch(err){lastError=err;}
  }
  if(lastError)throw lastError;
  return null;
}

async function workoutWeatherGeometry(activity){
  const stream=await getWorkoutStream(activity.id),pts=Array.isArray(stream&&stream.points)?stream.points:[];
  const gps=pts.filter(p=>n(p.lat)!=null&&n(p.lon)!=null);
  if(!gps.length)return null;
  const first=gps[0],last=gps[gps.length-1];
  const startMs=new Date(activity.start_time).getTime();
  if(!Number.isFinite(startMs))return null;
  const streamEnd=Math.max(0,...pts.map(p=>n(p.elapsed_s,0)));
  const endMs=startMs+(n(last.elapsed_s)??n(activity.duration_s)??streamEnd)*1000;
  let peak=null,bestAltitude=-Infinity;
  for(let i=0;i<gps.length;i++){
    const h=n(gps[i].alt);if(h==null)continue;
    const neighborhood=gps.slice(Math.max(0,i-2),Math.min(gps.length,i+3)).map(p=>n(p.alt)).filter(Number.isFinite);
    const smoothed=median(neighborhood);
    if(smoothed!=null&&smoothed>bestAltitude){bestAltitude=smoothed;peak=gps[i];}
  }
  const makeLocation=(p,t)=>({
    lat:n(p.lat),lon:n(p.lon),timeMs:t,alt:n(p.alt)
  });
  return {
    startMs,endMs,
    start:makeLocation(first,startMs+(n(first.elapsed_s)??0)*1000),
    end:makeLocation(last,endMs),
    peak:peak?makeLocation(peak,startMs+(n(peak.elapsed_s)??0)*1000):null,
    gps
  };
}
function nearbyWeatherStation(a,b){
  // Approximate horizontal distance in metres, only for choosing the nearest
  // of the three geographical sample locations.
  const y=(a.lat-b.lat)*111195,lonFactor=Math.cos(a.lat*Math.PI/180);
  const x=(a.lon-b.lon)*111195*lonFactor;
  return x*x+y*y;
}
async function fetchWeatherForActivity(activity){
  const geo=await workoutWeatherGeometry(activity);if(!geo)return null;
  const locations=[
    {name:"start",point:geo.start},
    {name:"finish",point:geo.end}
  ];
  if(geo.peak)locations.push({name:"peak",point:geo.peak});
  const stations=[];
  for(const loc of locations){
    const reused=stations.find(v=>nearbyWeatherStation(v.point,loc.point)<50*50&&
      (n(v.point.alt)==null||n(loc.point.alt)==null||Math.abs(v.point.alt-loc.point.alt)<40));
    let series=reused&&reused.series;
    if(!series)series=await fetchOpenMeteoSeries(loc.point.lat,loc.point.lon,geo.startMs,geo.endMs,loc.point.alt);
    if(!series)return null;
    stations.push({...loc,series});
  }
  const pointWeather=(loc)=>nearestOrInterpolatedWeather(
    stations.find(v=>v.name===loc.name).series,loc.point.timeMs
  );
  const first=pointWeather(stations[0]),end=pointWeather(stations[1]);
  const peak=geo.peak?pointWeather(stations[2]):null;

  // Estimate weather along the ACTUAL recorded route/times using the closest
  // available weather-model location; do not use the entire peak weather time
  // series to claim a minimum when the athlete was not at the summit.
  const sampleStep=Math.max(1,Math.floor(geo.gps.length/120)),values=[];
  for(let i=0;i<geo.gps.length;i+=sampleStep){
    const p=geo.gps[i],t=geo.startMs+(n(p.elapsed_s)??0)*1000;
    const nearest=stations.reduce((best,loc)=>
      !best||nearbyWeatherStation(loc.point,p)<nearbyWeatherStation(best.point,p)?loc:best,null);
    const sampled=nearestOrInterpolatedWeather(nearest.series,t);
    values.push({temp:sampled.temp,humidity:sampled.humidity});
  }
  values.push(first,end);
  if(peak)values.push(peak);
  const temps=values.map(x=>n(x.temp)).filter(Number.isFinite),hums=values.map(x=>n(x.humidity)).filter(Number.isFinite);
  if(!temps.length&&!hums.length)return null;
  return {
    weather_version:WEATHER_VERSION,
    weather_start_temp_c:first.temp,weather_start_humidity_pct:first.humidity,
    weather_end_temp_c:end.temp,weather_end_humidity_pct:end.humidity,
    weather_peak_temp_c:peak&&peak.temp,weather_peak_humidity_pct:peak&&peak.humidity,
    weather_peak_altitude_m:geo.peak&&geo.peak.alt,
    weather_peak_time:geo.peak?new Date(geo.peak.timeMs).toISOString():null,
    weather_min_temp_c:temps.length?Math.min(...temps):null,
    weather_max_temp_c:temps.length?Math.max(...temps):null,
    weather_mean_temp_c:temps.length?mean(temps):null,
    weather_min_humidity_pct:hums.length?Math.min(...hums):null,
    weather_max_humidity_pct:hums.length?Math.max(...hums):null,
    weather_mean_humidity_pct:hums.length?mean(hums):null,
    weather_resolution_min:Math.min(...stations.map(x=>x.series.resolution_min)),
    weather_start_source:stations[0].series.source,
    weather_end_source:stations[1].series.source,
    weather_peak_source:geo.peak?stations[2].series.source:null,
    weather_spatial_sampling:"nearest of start/finish/highest GPS point; route-time sampled approximation",
    weather_fetched_at:new Date().toISOString()
  };
}

function workoutHasWeather(activity){
  const m=safeJson(activity&&activity.metrics_json,{});
  return ["weather_start_temp_c","weather_end_temp_c","weather_mean_temp_c",
    "weather_start_humidity_pct","weather_mean_humidity_pct","weather_peak_temp_c"]
    .some(k=>n(m[k])!=null);
}
async function persistWeatherForActivity(activity,force=false){
  const existing=safeJson(activity.metrics_json,{});
  // Never re-fetch previously stored weather just because the parser was upgraded.
  if(workoutHasWeather(activity))return "skipped";
  const weather=await fetchWeatherForActivity(activity);
  if(!weather)return "no-gps";
  const mj={...existing,...weather};
  // Weather changes the personalized HR expectation, so force the fatigue score
  // to be learned again after the refresh.
  delete mj.fatigue_model_signature;
  const res=await client.from("activities").update({metrics_json:mj}).eq("id",activity.id).eq("user_id",userId());
  if(res.error)throw res.error;
  activity.metrics_json=mj;
  return "updated";
}
async function refreshAllWeather(force=false){
  const btn=el("refreshWeatherButton"),status=el("weatherRefreshStatus");
  if(btn)btn.disabled=true;
  let updated=0,skipped=0,failed=0,noGps=0;
  try{
    for(let i=0;i<activities.length;i++){
      const a=activities[i];
      if(status)status.textContent="Weather "+(i+1)+" / "+activities.length+": "+(a.name||a.sport_type||"Workout");
      if(workoutHasWeather(a)){skipped++;continue;}
      try{
        const r=await persistWeatherForActivity(a,false);
        if(r==="updated")updated++;else if(r==="no-gps")noGps++;else skipped++;
      }catch(err){failed++;console.warn("Weather refresh failed",a.id,err);}
      // Throttle only actual weather requests; existing records cost no API calls.
      if(i<activities.length-1)await new Promise(resolve=>setTimeout(resolve,90));
    }
    if(status)status.textContent="Weather: "+updated+" updated · "+skipped+" already current · "+noGps+" without GPS · "+failed+" failed.";
    if(updated){
      setSync("Relearning weather-aware HR efficiency…");
      await applyPersonalFatigueEfficiency();
      renderHistory();renderAnalysis();renderPlanner();
    }
  }finally{
    if(btn)btn.disabled=false;
    setSync("Synced");
  }
}
async function refreshMissingWeather(){
  return refreshAllWeather(false);
}

async function renderLatestWorkout(){
  if(!activities.length){
    el("homeWorkoutDetail").innerHTML='<div class="muted">Upload a workout to use the viewer.</div>';
    el("homeRouteMap").classList.add("hidden");el("homeStreamPlot").classList.add("hidden");return;
  }
  await showWorkout(activities[0].id,"home");
}


function workoutComparisonMetrics(a){
  const mj=safeJson(a.metrics_json,{}),w=weatherNumbers(a);
  const out=[
    ["Max HR",n(a.max_hr)," bpm",0],
    ["Pace",n(a.avg_pace_min_km)," min/km",null],
    ["Gradient-adjusted pace",n(a.avg_gap_pace_min_km)," min/km",null],
    ["Elevation gain",n(a.elevation_gain_m)," m",0],["Elevation loss",n(a.elevation_loss_m)," m",0],
    ["Training load",derivedLoad(a),"",1],
    ["Z1–Z2",n(a.easy_zone_fraction)!=null?n(a.easy_zone_fraction)*100:null,"%",0],
    ["Z4–Z5",n(a.hard_zone_fraction)!=null?n(a.hard_zone_fraction)*100:null,"%",0],
    ["HR drift",n(a.hr_efficiency_drift_pct),"%",1],
    ["GAP HR drift",n(a.gap_hr_efficiency_drift_pct),"%",1],
    ["Fatigue-adjusted HR score",n(mj.fatigue_adjusted_efficiency_score),"",1],
    ["HR residual vs expectation",n(mj.fatigue_hr_residual_bpm)," bpm",1],
    ["HR model",mj.hr_prediction_model==="gradient_boosting"?"Gradient Boosting":mj.hr_prediction_model==="ridge"?"Ridge":"—",null,null],
    ["Average power",n(a.average_power)," W",0],
    ["Normalized power",n(a.normalized_power)," W",0],
    ["Cadence",n(a.cadence_spm)," spm",0],
    ["Estimated steps",n(mj.estimated_total_steps),"",0],
    ["Best 5-min pace",n(mj.best_5min_pace_min_km)," min/km",null],
    ["Estimated VO₂max",n(a.own_vo2max_estimate)??n(a.estimated_vo2max),"",1],
    ["Apple VO₂max",n(a.apple_vo2max),"",1],
    ["Weather start temp",w.start_temp_c," °C",1],
    ["Weather start RH",w.start_humidity_pct,"%",0],
    ["Weather finish temp",w.end_temp_c," °C",1],
    ["Weather finish RH",w.end_humidity_pct,"%",0],
    ["Minimum temp",w.min_temp_c," °C",1],
    ["Maximum temp",w.max_temp_c," °C",1],
    ["Minimum humidity",w.min_humidity_pct,"%",0],
    ["Maximum humidity",w.max_humidity_pct,"%",0],
    ["Highest GPS altitude",w.peak_altitude_m," m",0],
    ["Highest point temp",w.peak_temp_c," °C",1],
    ["Highest point RH",w.peak_humidity_pct,"%",0]
  ];
  return out.map(([label,value,unit,digits])=>({label,value,unit,digits}));
}
function formattedCompareValue(value,unit="",digits=1){
  if(value==null||value==="")return "—";
  if(typeof value!=="number"||!Number.isFinite(value))return String(value);
  if(digits===null)return fmtPace(value);
  return value.toFixed(digits)+(unit||"");
}
function formattedCompareDelta(value,unit="",digits=1){
  if(value==null||!Number.isFinite(value))return "—";
  const prefix=value>0?"+":value<0?"−":"±",abs=Math.abs(value);
  return prefix+formattedCompareValue(abs,unit,digits);
}
function detailComparisonCard(descriptor,values,primaryClass=""){
  const {label,unit="",digits=1}=descriptor;
  const primary=values[0],other=values.slice(1),numbers=values.filter(Number.isFinite);
  const base=formattedCompareValue(primary,unit,digits);
  let detail="",description="";
  if(other.length===1){
    const v=other[0];
    if(Number.isFinite(primary)&&Number.isFinite(v)){
      detail="Δ "+formattedCompareDelta(v-primary,unit,digits);
      description="Second − primary: "+detail;
    }else{detail="Other: "+formattedCompareValue(v,unit,digits);description=detail;}
  }else if(other.length>=2){
    if(numbers.length){
      const average=mean(numbers),lo=Math.min(...numbers),hi=Math.max(...numbers);
      detail="Ø "+formattedCompareValue(average,unit,digits)+
        " · ΔØ "+(Number.isFinite(primary)?formattedCompareDelta(average-primary,unit,digits):"—");
      description="Range "+formattedCompareValue(lo,unit,digits)+" – "+formattedCompareValue(hi,unit,digits);
    }else{
      detail=values.filter(v=>v!=null&&v!=="").length+" values";
      description=[...new Set(values.map(v=>formattedCompareValue(v,unit,digits)))].join(" · ");
    }
  }
  const title=label+" · Primary "+base+(detail?" · "+detail:"")+(description?" · "+description:"");
  return '<div class="detail-item comparison-detail '+primaryClass+'" title="'+escapeHtml(title)+'">'+
    '<div class="detail-key">'+escapeHtml(label)+'</div>'+
    '<div class="detail-value">'+escapeHtml(base)+'</div>'+
    '<div class="detail-compare">'+escapeHtml(detail||"")+'</div>'+
    '<div class="detail-range">'+escapeHtml(other.length>=2?description:"")+'</div></div>';
}
function renderComparedWorkoutDetails(entries,target,derived={}){
  const a=entries[0].activity,all=entries.map(e=>e.activity);
  const detailId=target==="home"?"homeWorkoutDetail":"workoutDetail";
  const belowId=target==="home"?"homeWorkoutMetrics":"historyWorkoutMetrics";
  const primaryDescriptions=[
    {label:"Date",values:all.map(a=>localDate(a.start_time))},
    {label:"Type",values:all.map(a=>a.sport_type||a.sport_category||"—")},
    {label:"Distance",values:all.map(distanceKm),unit:" km",digits:2},
    {label:"Duration",values:all.map(durationMin),unit:" min",digits:0},
    {label:"Avg HR",values:all.map(a=>n(a.avg_hr)),unit:" bpm",digits:0}
  ];
  el(detailId).innerHTML='<h3>'+escapeHtml(a.name||"Workout")+
    (entries.length>1?' <span class="muted small-note">compared with '+(entries.length-1)+' other workout(s)</span>':'')+
    '</h3><div class="workout-primary-metrics">'+primaryDescriptions.map(d=>detailComparisonCard(d,d.values)).join("")+'</div>';
  const metrics=all.map(workoutComparisonMetrics);
  const descriptors=metrics[0];
  const grid=descriptors.map((desc,i)=>detailComparisonCard(desc,metrics.map(m=>m[i]?.value))).join("");
  const mj=safeJson(a.metrics_json,{}),rawSplits=Array.isArray(mj.km_splits)?mj.km_splits:[];
  const splits=rawSplits.length?
    '<details><summary>Primary workout: per-kilometre efficiency ('+rawSplits.length+' splits)</summary>'+
    '<div class="table-wrap"><table class="workout-table"><thead><tr><th>km</th><th>Pace</th><th>GAP</th><th>HR</th><th>Expected HR</th><th>Score</th><th>Prior load</th></tr></thead><tbody>'+
    rawSplits.map(r=>'<tr><td>'+escapeHtml(r.km)+'</td><td>'+escapeHtml(fmtPace(r.avg_pace_min_km))+'</td>'+
      '<td>'+escapeHtml(fmtPace(r.avg_gap_pace_min_km))+'</td><td>'+escapeHtml(fmt(r.avg_hr,0))+'</td>'+
      '<td>'+escapeHtml(fmt(r.expected_hr_personal,0))+'</td>'+
      '<td>'+escapeHtml(fmt(r.fatigue_adjusted_efficiency_score,1))+'</td>'+
      '<td>'+escapeHtml(fmt(r.cumulative_hr_load_before,1))+'</td></tr>').join("")+
    '</tbody></table></div></details>':"";
  el(belowId).innerHTML='<div class="workout-metrics-title">Training metrics & weather'+
    (entries.length>1?' <span class="muted small-note">· Δ = comparison minus primary'+
    (entries.length>2?' · Ø/range include all selected workouts':'')+'</span>':'')+'</div>'+
    '<div class="detail-grid">'+grid+'</div>'+
    '<p class="muted small-note">HR model scores are personalized predictive estimates. Weather comes from gridded models; mountain temperatures are approximate.</p>'+splits;
}
async function showWorkout(id,target="history"){
  const isHistory=target==="history";
  if(isHistory){
    if(!selectedActivityIds.length||!selectedActivityIds.includes(id))selectedActivityIds=[id];
    selectedActivityId=selectedActivityIds[0];
    id=selectedActivityId;
  }
  const ticket=isHistory?++workoutComparisonRequest:null;
  const ids=isHistory?selectedActivityIds.slice():[id];
  const chosen=ids.map(x=>activities.find(a=>a.id===x)).filter(Boolean);
  if(!chosen.length)return;
  try{
    const entries=await Promise.all(chosen.map(async activity=>({
      activity,rows:buildStreamRows(await getWorkoutStream(activity.id))
    })));
    if(isHistory&&ticket!==workoutComparisonRequest)return;
    const first=entries[0],derived=first.rows.length?await maybePersistDerivedMetrics(first.activity,first.rows):{};
    if(isHistory&&ticket!==workoutComparisonRequest)return;
    renderComparedWorkoutDetails(entries,target,derived);
    renderStreamViewer(first.rows,target,isHistory?entries.slice(1):[]);
    if(isHistory){renderWorkoutTable(filteredActivities());renderComparisonSelection();}
  }catch(err){
    const status=el(isHistory?"workoutDetail":"homeWorkoutDetail");
    if(!isHistory||ticket===workoutComparisonRequest)status.textContent="Could not load selected workout: "+(err.message||String(err));
    console.error(err);
  }
}

function nearestRowByX(rows,xKey,xValue){
  const x=n(xValue);if(x==null||!rows.length)return null;
  let best=null,bestD=Infinity;
  for(const r of rows){const v=n(r[xKey]);if(v==null)continue;const d=Math.abs(v-x);if(d<bestD){bestD=d;best=r;}}
  return best;
}
function nearestGpsRow(rows,latlng){
  if(!latlng)return null;
  let best=null,bestD=Infinity;
  const cos=Math.cos(latlng.lat*Math.PI/180);
  for(const r of rows){
    if(r.lat==null||r.lon==null)continue;
    const dy=r.lat-latlng.lat,dx=(r.lon-latlng.lng)*cos,d=dx*dx+dy*dy;
    if(d<bestD){bestD=d;best=r;}
  }
  return best;
}
function cursorReadoutHtml(row,key,def){
  const value=n(row&&row[key]);
  const selected=value==null?"—":(key==="pace_min_km"?fmtPace(value):fmt(value,1,def.unit?" "+def.unit:""));
  return '<strong>'+escapeHtml(def.label)+': '+escapeHtml(selected)+'</strong>'+
    '<span>Time '+escapeHtml(fmt(row&&row.time_min,1," min"))+'</span>'+
    '<span>Distance '+escapeHtml(fmt(row&&row.distance_km,2," km"))+'</span>'+
    '<span>HR '+escapeHtml(fmt(row&&row.heart_rate,0," bpm"))+'</span>'+
    '<span>Speed '+escapeHtml(fmt(row&&row.speed_kmh,1," km/h"))+'</span>'+
    '<span>Power '+escapeHtml(fmt(row&&row.power_w,0," W"))+'</span>'+
    '<span>Cadence '+escapeHtml(fmt(row&&row.cadence,0))+'</span>'+
    '<span>Grade '+escapeHtml(fmt(row&&row.grade_pct,1,"%"))+'</span>'+
    '<span>Altitude '+escapeHtml(fmt(row&&row.altitude_m,0," m"))+'</span>';
}
function updateViewerCursor(target,row){
  const state=viewerStates[target];if(!state||!row)return;
  state.cursorRow=row;
  const readout=el(state.readoutId);readout.classList.remove("hidden");readout.innerHTML=cursorReadoutHtml(row,state.key,state.def);

  if(state.map&&row.lat!=null&&row.lon!=null){
    if(!state.cursorMarker){
      const icon=L.divIcon({className:"workout-cursor-icon",html:'<div class="workout-cursor-dot"></div>',iconSize:[18,18],iconAnchor:[9,9]});
      state.cursorMarker=L.marker([row.lat,row.lon],{icon,draggable:true,zIndexOffset:1000}).addTo(state.map);
      state.cursorMarker.on("drag",e=>{
        const r=nearestGpsRow(state.rows,e.target.getLatLng());if(r)updateViewerCursor(target,r);
      });
      state.cursorMarker.on("dragend",e=>{
        const r=nearestGpsRow(state.rows,e.target.getLatLng());if(r)updateViewerCursor(target,r);
      });
    }else state.cursorMarker.setLatLng([row.lat,row.lon]);
  }

  const xv=n(row[state.xKey]),yv=n(row[state.key]);
  if(state.plotReady&&xv!=null){
    const plot=el(state.plotId),cursorIndex=state.cursorTraceIndex;
    if(cursorIndex!=null){
      if(yv!=null)Plotly.restyle(plot,{x:[[xv]],y:[[yv]]},[cursorIndex]);
      else Plotly.restyle(plot,{x:[[]],y:[[]]},[cursorIndex]);
    }
    Plotly.relayout(plot,{"shapes[0].x0":xv,"shapes[0].x1":xv});
  }
}
function bindPlotCursor(target){
  const state=viewerStates[target],plot=el(state.plotId);if(!state||!plot)return;
  if(typeof plot.removeAllListeners==="function"){plot.removeAllListeners("plotly_hover");plot.removeAllListeners("plotly_click");}
  plot.on("plotly_hover",ev=>{
    const cd=ev&&ev.points&&ev.points[0]&&ev.points[0].customdata;
    const idx=Array.isArray(cd)?n(cd[8]):null;
    if(idx!=null&&state.rows[idx])updateViewerCursor(target,state.rows[idx]);
  });
  plot.on("plotly_click",ev=>{
    const cd=ev&&ev.points&&ev.points[0]&&ev.points[0].customdata;
    const idx=Array.isArray(cd)?n(cd[8]):null;
    if(idx!=null&&state.rows[idx])updateViewerCursor(target,state.rows[idx]);
  });

  if(plot._wbPointerDown)plot.removeEventListener("pointerdown",plot._wbPointerDown,true);
  if(plot._wbPointerMove)plot.removeEventListener("pointermove",plot._wbPointerMove,true);
  if(plot._wbPointerUp)window.removeEventListener("pointerup",plot._wbPointerUp,true);
  let dragging=false;
  const move=e=>{
    const xa=plot._fullLayout&&plot._fullLayout.xaxis;if(!xa||typeof xa.p2d!=="function")return;
    const rect=plot.getBoundingClientRect(),px=e.clientX-rect.left-xa._offset,xv=xa.p2d(px);
    const row=nearestRowByX(state.rows,state.xKey,xv);if(row)updateViewerCursor(target,row);
  };
  plot._wbPointerDown=e=>{if(e.button!==0)return;dragging=true;move(e);};
  plot._wbPointerMove=e=>{if(dragging&&(e.buttons&1))move(e);};
  plot._wbPointerUp=()=>{dragging=false;};
  plot.addEventListener("pointerdown",plot._wbPointerDown,true);
  plot.addEventListener("pointermove",plot._wbPointerMove,true);
  window.addEventListener("pointerup",plot._wbPointerUp,true);
}
function quantizeLineColor(hex,step=16){
  const m=/^#([0-9a-f]{6})$/i.exec(String(hex||""));
  if(!m)return hex||"#64748b";
  const q=v=>clamp(Math.round(v/step)*step,0,255);
  const r=q(parseInt(m[1].slice(0,2),16)),g=q(parseInt(m[1].slice(2,4),16)),b=q(parseInt(m[1].slice(4,6),16));
  return "#"+[r,g,b].map(v=>v.toString(16).padStart(2,"0")).join("");
}
function buildColoredProfileTraces(rows,xKey,key,stats){
  const groups=new Map();
  for(let i=1;i<rows.length;i++){
    const a=rows[i-1],b=rows[i],x0=n(a[xKey]),x1=n(b[xKey]),y0=n(a[key]),y1=n(b[key]);
    if(x0==null||x1==null||y0==null||y1==null)continue;
    // Preserve genuine gaps/stops rather than drawing across missing sections.
    const dt=n(b.elapsed_s)-n(a.elapsed_s);
    if(dt!=null&&dt>30)continue;
    const color=quantizeLineColor(colorForStreamMetric(key,b[key],stats),key==="zone"?1:16);
    if(!groups.has(color))groups.set(color,{x:[],y:[]});
    const g=groups.get(color);
    g.x.push(x0,x1,null);g.y.push(y0,y1,null);
  }
  return Array.from(groups.entries()).map(([color,g])=>({
    x:g.x,y:g.y,mode:"lines",type:"scattergl",connectgaps:false,showlegend:false,hoverinfo:"skip",
    line:{color,width:3}
  }));
}
function renderStreamViewer(rows,target,comparisonEntries=[]){
  const mapId=target==="home"?"homeRouteMap":"routeMap",plotId=target==="home"?"homeStreamPlot":"streamPlot",
    metricId=target==="home"?"homeRouteMetric":"historyRouteMetric",xId=target==="home"?"homeRouteXAxis":"historyRouteXAxis",
    legendId=target==="home"?"homeRouteLegend":"historyRouteLegend",readoutId=target==="home"?"homeCursorReadout":"historyCursorReadout";
  const key=el(metricId).value||"pace_min_km",def=STREAM_METRICS[key],stats=streamMetricStats(rows,key),xKey=el(xId).value||"distance_km";
  const vals=rows.map(r=>n(r[key])).filter(Number.isFinite);

  if(vals.length){
    el(legendId).classList.remove("hidden");
    let range;
    if(key==="heart_rate")range="white/pale = low HR · dark red ≥180 bpm";
    else if(key==="zone")range="Z1 blue · Z2 green · Z3 yellow · Z4 orange · Z5 red · Z6 purple";
    else if(key==="pace_min_km"||key==="speed_kmh")range="derived from DistanceMeters / time · "+stats.lo.toFixed(1)+"–"+stats.hi.toFixed(1)+" "+def.unit+" (5th–95th percentile)";
    else range=stats.lo.toFixed(1)+"–"+stats.hi.toFixed(1)+" "+def.unit+" (5th–95th percentile)";
    el(legendId).innerHTML='<span class="legend-label">'+escapeHtml(def.label)+' (primary)</span><span class="legend-gradient" style="background:'+def.gradient+'"></span><span>'+escapeHtml(range)+'</span>'+
      comparisonEntries.map((entry,i)=>'<span class="comparison-legend-tag"><span class="comparison-swatch" style="background:'+comparisonColor(i+1)+'"></span>'+
        escapeHtml("#"+(i+2)+" "+(entry.activity.name||"workout"))+'</span>').join("");
  }else el(legendId).classList.add("hidden");

  const old=viewerStates[target];
  if(old&&old.map&&routeMaps[target]){routeMaps[target].remove();routeMaps[target]=null;}
  const state={rows,key,def,stats,xKey,mapId,plotId,legendId,readoutId,map:null,cursorMarker:null,plotReady:false};
  viewerStates[target]=state;

  const gps=rows.filter(r=>r.lat!=null&&r.lon!=null);
  const secondaryGps=comparisonEntries.map(e=>e.rows.filter(r=>r.lat!=null&&r.lon!=null))
    .find(coords=>coords.length>=2)||[];
  const mapBoundsGps=gps.length>=2?gps:secondaryGps;
  if(mapBoundsGps.length>=2){
    el(mapId).classList.remove("hidden");
    const map=L.map(mapId,{renderer:L.canvas({padding:.5})});routeMaps[target]=map;state.map=map;
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:19,attribution:"&copy; OpenStreetMap"}).addTo(map);
    // The primary workout exclusively determines the initial map viewport.
    // Overlay routes elsewhere must never zoom the primary route out.
    comparisonEntries.forEach((entry,i)=>{
      const coords=entry.rows.filter(r=>r.lat!=null&&r.lon!=null).map(r=>[r.lat,r.lon]);
      if(coords.length<2)return;
      const color=comparisonColor(i+1);
      L.polyline(coords,{color,weight:3,opacity:.28,interactive:false,
        dashArray:"5 7",lineCap:"round"}).addTo(map);
    });
    for(let i=1;i<gps.length;i++){
      const c=colorForStreamMetric(key,gps[i][key],stats);
      L.polyline([[gps[i-1].lat,gps[i-1].lon],[gps[i].lat,gps[i].lon]],{weight:5,opacity:comparisonEntries.length?1:.92,color:c,interactive:false}).addTo(map);
    }
    if(gps.length>=2){
      L.circleMarker([gps[0].lat,gps[0].lon],{radius:5,weight:2,color:"#166534",fillColor:"#ffffff",fillOpacity:1}).bindTooltip("Primary start").addTo(map);
      L.circleMarker([gps[gps.length-1].lat,gps[gps.length-1].lon],{radius:5,weight:2,color:"#991b1b",fillColor:"#ffffff",fillOpacity:1}).bindTooltip("Primary finish").addTo(map);
    }
    map.fitBounds(mapBoundsGps.map(r=>[r.lat,r.lon]),{padding:[18,18]});
    map.on("click",e=>{const r=nearestGpsRow(rows,e.latlng);if(r)updateViewerCursor(target,r);});
    let lastMapFollow=0;
    map.on("mousemove",e=>{
      const now=performance.now();if(now-lastMapFollow<35)return;lastMapFollow=now;
      const r=nearestGpsRow(rows,e.latlng);if(r)updateViewerCursor(target,r);
    });
    setTimeout(()=>map.invalidateSize(),0);
  }else{
    el(mapId).classList.add("hidden");
  }

  // IMPORTANT: downsample the full time series before checking metric validity.
  // This preserves recovery/stop gaps. Filtering first used to connect the end
  // of one interval directly to the beginning of another and visually stretched
  // intervals across the workout.
  const plotRows=rows;
  const validCount=plotRows.filter(r=>n(r[xKey])!=null&&n(r[key])!=null).length;
  const secondaryValid=comparisonEntries.some(e=>e.rows.filter(r=>n(r[xKey])!=null&&n(r[key])!=null).length>=2);
  if(validCount>=2||secondaryValid){
    el(plotId).classList.remove("hidden");
    const xs=plotRows.map(r=>n(r[xKey]));
    const ys=plotRows.map(r=>n(r[key]));
    const custom=plotRows.map(r=>[r.time_min,r.distance_km,r.altitude_m,r.heart_rate,r.speed_kmh,r.power_w,r.cadence,r.grade_pct,r.i]);
    const traces=buildColoredProfileTraces(plotRows,xKey,key,stats);
    // Secondary profiles are separate translucent continuous lines (no point cloud).
    // Keep the primary's dynamic colored segments and its cursor above them.
    comparisonEntries.forEach((entry,i)=>{
      const valid=entry.rows.filter(r=>n(r[xKey])!=null&&n(r[key])!=null);
      if(valid.length<2)return;
      const color=comparisonColor(i+1);
      traces.push({x:entry.rows.map(r=>n(r[xKey])),y:entry.rows.map(r=>n(r[key])),
        name:"#"+(i+2)+" "+(entry.activity.name||"Workout"),type:"scattergl",
        mode:"lines",connectgaps:false,showlegend:true,opacity:.4,
        line:{color,width:2},hovertemplate:escapeHtml(entry.activity.name||"Comparison")+
          "<br>%{x:.2f} · %{y:.2f}<extra></extra>"});
    });

    // Invisible hit-test layer: keeps precise hover/click interaction without
    // drawing thousands of visible sample points.
    const interactionTraceIndex=traces.length;
    traces.push({
      x:xs,y:ys,mode:"markers",type:"scattergl",connectgaps:false,showlegend:false,
      marker:{size:10,color:"rgba(0,0,0,0.001)"},
      customdata:custom,
      hovertemplate:"Time %{customdata[0]:.1f} min · Distance %{customdata[1]:.2f} km<br>"+escapeHtml(def.label)+": %{y:.2f} "+escapeHtml(def.unit)+"<br>Altitude %{customdata[2]:.0f} m · HR %{customdata[3]:.0f}<br>Speed %{customdata[4]:.1f} km/h · Power %{customdata[5]:.0f} W · Cadence %{customdata[6]:.0f} · Grade %{customdata[7]:.1f}%<extra></extra>"
    });

    // Cursor is the final WebGL trace, guaranteeing that it is painted over
    // both the colored profile lines and the invisible interaction layer.
    const cursorTraceIndex=traces.length;
    traces.push({
      x:[],y:[],mode:"markers",type:"scattergl",showlegend:false,hoverinfo:"skip",
      marker:{size:15,color:"#111827",line:{width:4,color:"#ffffff"}}
    });
    state.interactionTraceIndex=interactionTraceIndex;
    state.cursorTraceIndex=cursorTraceIndex;
    const firstValid=plotRows.find(r=>n(r[xKey])!=null&&n(r[key])!=null);
    const firstX=firstValid?n(firstValid[xKey]):0;
    Plotly.react(plotId,traces,{margin:{l:60,r:20,t:20,b:45},hovermode:"closest",dragmode:false,
      xaxis:{title:xKey==="distance_km"?"Distance (km)":"Time (min)"},
      yaxis:{title:def.label+" ("+def.unit+")",autorange:key==="pace_min_km"?"reversed":true},
      shapes:[{type:"line",x0:firstX,x1:firstX,yref:"paper",y0:0,y1:1,line:{color:"rgba(17,24,39,.35)",width:1,dash:"dot"}}],
      paper_bgcolor:"transparent",plot_bgcolor:"transparent",
      showlegend:comparisonEntries.length>0,legend:{orientation:"h",y:-.25,font:{size:10}}},
      {responsive:true,displaylogo:false,scrollZoom:false});
    state.plotReady=true;
    bindPlotCursor(target);
    if(firstValid)updateViewerCursor(target,firstValid);
  }else{
    el(plotId).classList.add("hidden");
    el(readoutId).classList.add("hidden");
  }
}


function analysisData() {
  const cutoff90 = daysAgo(90);
  const recent = activities.filter(a => new Date(a.start_time)>=cutoff90);
  const runs = recent.filter(a=>normalizeSport(a.sport_category||a.sport_type)==="run");
  const easyRuns = runs.filter(a => (n(a.easy_zone_fraction,0)>=.6 || n(a.hard_zone_fraction,0)<.12) && n(a.avg_pace_min_km)!=null && distanceKm(a)>=3);
  const slope = linearSlopePerWeek(easyRuns,a=>n(a.avg_pace_min_km));
  let trend = "Stable", trendNote = "Not enough comparable easy-run change to call a clear trend.";
  if (slope != null && slope < -.035) { trend="Improving"; trendNote="Comparable easy-run pace is getting faster by about "+Math.abs(slope).toFixed(2)+" min/km per week."; }
  else if (slope != null && slope > .035) { trend="Declining / fatigued"; trendNote="Comparable easy-run pace is getting slower by about "+slope.toFixed(2)+" min/km per week."; }

  const z = ["z1_s","z2_s","z3_s","z4_s","z5_s"].map(k=>sum(recent.map(a=>n(a[k],0))));
  const ztot=sum(z), easyPct=ztot?100*(z[0]+z[1])/ztot:null, hardPct=ztot?100*(z[3]+z[4])/ztot:null;
  const drifts = easyRuns.map(a=>n(a.hr_efficiency_drift_pct)).filter(x=>x!=null);
  const medDrift=median(drifts);
  let durability="Unknown",durabilityNote="Need more sufficiently long runs with HR/pace data.";
  if(medDrift!=null){ if(medDrift>=-3){durability="Good";durabilityNote="Median comparable-run HR efficiency drift is "+medDrift.toFixed(1)+"%.";}
    else if(medDrift>=-7){durability="Moderate";durabilityNote="Median HR efficiency drift is "+medDrift.toFixed(1)+"%; aerobic durability can improve.";}
    else {durability="Needs work";durabilityNote="Median HR efficiency drift is "+medDrift.toFixed(1)+"%; fatigue resistance is a likely limiter.";}}
  const c=trainingContext();
  const baseStatus = easyPct==null?"Unknown":easyPct>=70?"Good":easyPct>=60?"Moderate":"Low";
  const loadStatus = c.loadRatio>1.5?"High":c.loadRatio<.6?"Low":"Normal";
  const volumeStatus = c.runKm28Weekly>=15?"Established":c.runKm28Weekly>=8?"Building":"Low";

  const weatherRows=recent.map(a=>({a,w:weatherNumbers(a)})).filter(x=>x.w.mean_temp_c!=null||x.w.mean_humidity_pct!=null);
  const tempVals=weatherRows.map(x=>x.w.mean_temp_c).filter(Number.isFinite);
  const humidityVals=weatherRows.map(x=>x.w.mean_humidity_pct).filter(Number.isFinite);
  const weatherMedianTemp=median(tempVals),weatherMedianHumidity=median(humidityVals);
  const hotRows=weatherRows.filter(x=>n(x.w.mean_temp_c,-99)>=24);
  const humidRows=weatherRows.filter(x=>n(x.w.mean_humidity_pct,-1)>=75);
  const hotHumidRows=weatherRows.filter(x=>n(x.w.mean_temp_c,-99)>=20&&n(x.w.mean_humidity_pct,-1)>=70);
  const weatherAdjustedRuns=runs.filter(a=>n(safeJson(a.metrics_json,{}).fatigue_reference_uses_weather)===1);
  const hotFatigue=median(hotHumidRows.map(x=>n(safeJson(x.a.metrics_json,{}).fatigue_adjusted_efficiency_score)).filter(Number.isFinite));
  const mildFatigue=median(weatherRows.filter(x=>n(x.w.mean_temp_c,99)>=5&&n(x.w.mean_temp_c,99)<=18)
    .map(x=>n(safeJson(x.a.metrics_json,{}).fatigue_adjusted_efficiency_score)).filter(Number.isFinite));
  let weatherLabel=weatherRows.length?(weatherMedianTemp!=null?weatherMedianTemp.toFixed(1)+" °C":"Weather loaded"):"No weather";
  let weatherNote=weatherRows.length+" / "+recent.length+" recent workouts have weather.";
  if(weatherMedianHumidity!=null)weatherNote+=" Median RH "+weatherMedianHumidity.toFixed(0)+"%.";
  if(hotHumidRows.length)weatherNote+=" "+hotHumidRows.length+" warm/humid workout"+(hotHumidRows.length===1?"":"s")+".";
  if(weatherAdjustedRuns.length)weatherNote+=" Personal HR-efficiency reference includes temperature/humidity for "+weatherAdjustedRuns.length+" run"+(weatherAdjustedRuns.length===1?"":"s")+".";
  if(hotFatigue!=null&&mildFatigue!=null&&Math.abs(hotFatigue-mildFatigue)>=1.5){
    weatherNote+=" Fatigue-adjusted efficiency differs by "+Math.abs(hotFatigue-mildFatigue).toFixed(1)+" points between warm/humid and mild conditions.";
  }

  const evidence=[];
  if(easyPct!=null) evidence.push("Easy Z1–Z2 share over 90 days: "+easyPct.toFixed(1)+"%.");
  if(hardPct!=null) evidence.push("Hard Z4–Z5 share over 90 days: "+hardPct.toFixed(1)+"%.");
  evidence.push("7-day load / 28-day weekly baseline: "+c.loadRatio.toFixed(2)+"×.");
  evidence.push("Recent weekly-equivalent running volume: "+c.runKm28Weekly.toFixed(1)+" km.");
  if(easyRuns.length) evidence.push("Comparable easy runs used for pace trend: "+easyRuns.length+".");
  if(weatherRows.length){
    evidence.push("Weather coverage: "+weatherRows.length+" recent workouts; median "+(weatherMedianTemp==null?"—":weatherMedianTemp.toFixed(1)+" °C")+
      " / "+(weatherMedianHumidity==null?"—":weatherMedianHumidity.toFixed(0)+"% RH")+
      "; "+hotRows.length+" ≥24 °C and "+humidRows.length+" ≥75% RH.");
  }
  return {
    trend,trendNote,baseStatus,loadStatus,volumeStatus,durability,durabilityNote,easyPct,hardPct,z,c,easyRuns,evidence,slope,
    weather:{count:weatherRows.length,median_temp_c:weatherMedianTemp,median_humidity_pct:weatherMedianHumidity,
      hot_count:hotRows.length,humid_count:humidRows.length,warm_humid_count:hotHumidRows.length,
      weather_adjusted_runs:weatherAdjustedRuns.length,hot_fatigue_score:hotFatigue,mild_fatigue_score:mildFatigue,label:weatherLabel,note:weatherNote},
    generated_at:new Date().toISOString()
  };
}

function linearSlopePerWeek(rows,valueFn) {
  const pts=rows.map(a=>({x:new Date(a.start_time).getTime()/86400000,y:valueFn(a)})).filter(p=>Number.isFinite(p.x)&&Number.isFinite(p.y)).sort((a,b)=>a.x-b.x);
  if(pts.length<4)return null;
  const x0=pts[0].x, xs=pts.map(p=>p.x-x0), ys=pts.map(p=>p.y), xm=mean(xs), ym=mean(ys);
  const den=sum(xs.map(x=>(x-xm)*(x-xm))); if(!den)return null;
  const slope=sum(xs.map((x,i)=>(x-xm)*(ys[i]-ym)))/den;
  return slope*7;
}
function weeklyRunSeries() {
  const now=new Date(); now.setHours(0,0,0,0);
  const out=[];
  for(let w=11;w>=0;w--){
    const end=new Date(now.getTime()-w*7*86400000+86400000);
    const start=new Date(end.getTime()-7*86400000);
    const km=sum(activities.filter(a=>normalizeSport(a.sport_category||a.sport_type)==="run"&&new Date(a.start_time)>=start&&new Date(a.start_time)<end).map(distanceKm));
    out.push({date:start.toISOString().slice(0,10),km});
  } return out;
}
function renderAnalysis() {
  const d=analysisData();
  const cards=[
    ["Fitness trend",d.trend,d.trendNote],["Aerobic base",d.baseStatus,d.easyPct==null?"Insufficient HR-zone data":fmt(d.easyPct,1,"% easy Z1–Z2")],
    ["Durability",d.durability,d.durabilityNote],["Training load",d.loadStatus,d.c.loadRatio.toFixed(2)+"× acute/baseline"],
    ["Run volume",d.volumeStatus,d.c.runKm28Weekly.toFixed(1)+" km/week recent baseline"],
    ["Weather context",d.weather.label,d.weather.note]
  ];
  el("analysisCards").innerHTML=cards.map(c=>'<div class="analysis-card"><div class="eyebrow">'+escapeHtml(c[0])+'</div><div class="value">'+escapeHtml(c[1])+'</div><div class="note">'+escapeHtml(c[2])+"</div></div>").join("");
  const weaknesses=[];
  if(d.easyPct!=null&&d.easyPct<70) weaknesses.push("Aerobic-base volume: easy Z1–Z2 share is below 70%.");
  if(d.hardPct!=null&&d.hardPct>12) weaknesses.push("Intensity density: Z4–Z5 share is relatively high.");
  if(d.durability==="Needs work") weaknesses.push("Fatigue resistance / HR drift.");
  if(d.c.loadRatio>1.35) weaknesses.push("Recovery pressure from elevated acute load.");
  if(d.c.runKm28Weekly<10) weaknesses.push("Running consistency / weekly volume.");
  if(!weaknesses.length) weaknesses.push("No dominant limiter detected; prioritize consistent progressive training.");
  el("analysisEvidence").innerHTML =
    '<div class="evidence-box"><strong>Evidence</strong><ul>'+d.evidence.map(x=>"<li>"+escapeHtml(x)+"</li>").join("")+'</ul></div>'+
    '<div class="evidence-box"><strong>Likely limiters</strong><ul>'+weaknesses.map(x=>"<li>"+escapeHtml(x)+"</li>").join("")+"</ul></div>";
  const weekly=weeklyRunSeries();
  Plotly.react("weeklyVolumePlot",[{x:weekly.map(x=>x.date),y:weekly.map(x=>x.km),type:"bar",name:"Run km"}],
    {margin:{l:45,r:15,t:15,b:40},xaxis:{title:"Week"},yaxis:{title:"km"},paper_bgcolor:"transparent",plot_bgcolor:"transparent"});
  Plotly.react("zonePlot",[{x:["Z1","Z2","Z3","Z4","Z5"],y:d.z.map(x=>x/60),type:"bar"}],
    {margin:{l:45,r:15,t:15,b:40},xaxis:{title:"HR zone"},yaxis:{title:"minutes"},paper_bgcolor:"transparent",plot_bgcolor:"transparent"});
  renderMlAnalysis();
}




const AI_ADVANCED_KEYS=[
  "first_half_avg_hr","second_half_avg_hr","first_half_avg_gap_speed_mps","second_half_avg_gap_speed_mps",
  "first_half_eff_gap_speed_per_hr","second_half_eff_gap_speed_per_hr",
  "hr_efficiency_drift_pct","gap_hr_efficiency_drift_pct","km_gap_hr_efficiency_drift_pct",
  "fatigue_adjusted_efficiency_score","fatigue_adjusted_efficiency_last_third",
  "fatigue_adjusted_efficiency_drift_pct","fatigue_hr_residual_bpm",
  "fatigue_reference_n_runs","fatigue_reference_rmse_bpm",
  "weather_start_temp_c","weather_end_temp_c","weather_peak_temp_c","weather_peak_humidity_pct",
  "weather_peak_altitude_m","weather_mean_temp_c","weather_mean_humidity_pct",
  "weather_min_temp_c","weather_max_temp_c",
  "best_5min_pace_min_km","best_20min_pace_min_km","normalized_power_w","power_hr_efficiency",
  "moving_ratio","impact_load_index"
];
function scalarMetricsJson(a){
  const src=safeJson(a.metrics_json,{}),out={};
  for(const key of AI_ADVANCED_KEYS){
    const v=src[key];if(typeof v==="number"&&Number.isFinite(v))out[key]=Math.round(v*100)/100;
  }
  if(src.hr_prediction_model)out.hr_prediction_model=src.hr_prediction_model;
  return out;
}
function compactKmEfficiencyForAi(a){
  const splits=safeJson(a.metrics_json,{}).km_splits;
  if(!Array.isArray(splits))return [];
  const step=Math.max(1,Math.ceil(splits.length/8));
  const chosen=splits.filter((_,i)=>i===0||i===splits.length-1||i%step===0).slice(0,9);
  return chosen.map(s=>({
    km:n(s.km),avg_hr:n(s.avg_hr),gap_pace:n(s.avg_gap_pace_min_km),
    prior_load:n(s.cumulative_hr_load_before),prior_z45_min:n(s.cumulative_z45_before_min),
    fatigue_efficiency:n(s.fatigue_adjusted_efficiency_score),hr_residual_bpm:n(s.hr_residual_vs_expected_bpm)
  }));
}


function compactAnalysisForAi(d){
  return {
    fitness_trend:d.trend,fitness_trend_note:d.trendNote,aerobic_base:d.baseStatus,
    durability:d.durability,durability_note:d.durabilityNote,load_status:d.loadStatus,
    run_volume_status:d.volumeStatus,easy_zone_pct:d.easyPct,hard_zone_pct:d.hardPct,
    weekly_run_km:d.c&&d.c.runKm28Weekly,acute_load_ratio:d.c&&d.c.loadRatio,
    weather:d.weather?{
      median_temp_c:d.weather.median_temp_c,median_humidity_pct:d.weather.median_humidity_pct,
      hot_count:d.weather.hot_count,weather_adjusted_runs:d.weather.weather_adjusted_runs
    }:null,
    evidence:(d.evidence||[]).slice(0,7)
  };
}
function activityForAi(a,detailLevel="full"){
  if(detailLevel==="compact"){
    const w=weatherNumbers(a);
    return {
      detail_level:"compact",start_time:a.start_time,sport:a.sport_category||a.sport_type,
      distance_km:distanceKm(a),duration_min:durationMin(a),avg_hr:n(a.avg_hr),
      training_load_score:n(a.training_load_score)??n(a.trimp_score),
      vo2max:n(a.apple_vo2max)??n(a.own_vo2max_estimate)??n(a.estimated_vo2max),
      temp_c:w.mean_temp_c,humidity_pct:w.mean_humidity_pct
    };
  }
  return {
    detail_level:"full",start_time:a.start_time,name:a.name,sport:a.sport_category||a.sport_type,
    distance_km:distanceKm(a),duration_min:durationMin(a),elevation_gain_m:n(a.elevation_gain_m),elevation_loss_m:n(a.elevation_loss_m),
    avg_hr:n(a.avg_hr),max_hr:n(a.max_hr),pace_min_km:n(a.avg_pace_min_km),gap_pace_min_km:n(a.avg_gap_pace_min_km),
    training_load_score:n(a.training_load_score),trimp_score:n(a.trimp_score),easy_zone_fraction:n(a.easy_zone_fraction),hard_zone_fraction:n(a.hard_zone_fraction),
    z1_min:n(a.z1_s)!=null?n(a.z1_s)/60:null,z2_min:n(a.z2_s)!=null?n(a.z2_s)/60:null,z3_min:n(a.z3_s)!=null?n(a.z3_s)/60:null,
    z4_min:n(a.z4_s)!=null?n(a.z4_s)/60:null,z5_min:n(a.z5_s)!=null?n(a.z5_s)/60:null,
    apple_vo2max:n(a.apple_vo2max),estimated_vo2max:n(a.own_vo2max_estimate)??n(a.estimated_vo2max),
    average_power:n(a.average_power),normalized_power:n(a.normalized_power),cadence_spm:n(a.cadence_spm),
    hr_efficiency_drift_pct:n(a.hr_efficiency_drift_pct),gap_hr_efficiency_drift_pct:n(a.gap_hr_efficiency_drift_pct),
    km_gap_hr_efficiency_drift_pct:n(a.km_gap_hr_efficiency_drift_pct),
    advanced:scalarMetricsJson(a),km_efficiency:compactKmEfficiencyForAi(a)
  };
}
async function activitiesForAi(days){
  const rows=activities.filter(a=>new Date(a.start_time)>=daysAgo(days));
  await enrichMetricsForActivities(rows);
  await applyPersonalFatigueEfficiency();
  const fullCutoff=daysAgo(28);
  return rows.map(a=>activityForAi(a,new Date(a.start_time)>=fullCutoff?"full":"compact"));
}
function profileForAi(){
  const t=(profile&&profile.training_preferences)||{};
  return {
    profile_vo2max:n(profile&&profile.profile_vo2max),goals:profile&&profile.goals,
    training_preferences:{
      weekly_run_days:n(t.weekly_run_days),strength_sessions:n(t.strength_sessions),
      training_aggressiveness:n(t.training_aggressiveness),long_run_day:n(t.long_run_day)
    }
  };
}
async function loadAiHistory(){
  if(!client||!userId())return;
  const res=await client.from("analysis_snapshots").select("*").eq("user_id",userId()).order("created_at",{ascending:false}).limit(10);
  if(res.error){console.warn("Could not load AI history",res.error);aiHistory=[];return;}
  aiHistory=res.data||[];
  renderAiHistory();
}

function summarizeAiMemory(text){
  const raw=String(text||"").replace(/\r/g,""),sections=raw.split(/^##\s+/m).filter(Boolean),parts=[];
  for(const section of sections){
    const lines=section.split("\n"),title=(lines.shift()||"").trim();
    const paragraph=lines.join(" ").replace(/[#*_\`>]/g,"").replace(/\[[^\]]+\]\([^)]+\)/g,"").replace(/\s+/g," ").trim();
    if(!paragraph)continue;
    parts.push(title+": "+paragraph.slice(0,title.toLowerCase().includes("evidence")?110:220));
  }
  return (parts.length?parts.join(" | "):raw.replace(/[#*_]/g,"").replace(/\s+/g," ")).slice(0,850);
}
function aiHistoryPayload(){
  // Only the last ANALYSIS is used as memory. Historical evaluations and plans
  // remain stored in full in Supabase, but are not retransmitted to OpenAI.
  const last=aiHistory.find(h=>safeJson(h.deterministic,{}).evaluation_type==="analysis");
  if(!last)return [];
  const data=safeJson(last.deterministic,{});
  const summary=String(data.ai_memory_summary||summarizeAiMemory(last.llm_markdown||""));
  return [{created_at:last.created_at,evaluation_type:"analysis",summary:summary.slice(0,850)}];
}

function renderAiHistory(){
  const host=el("aiHistory");if(!host)return;
  if(!aiHistory.length){host.innerHTML="";return;}
  host.innerHTML='<div class="eyebrow">RECENT AI EVALUATIONS</div>'+
    aiHistory.slice(0,6).map(h=>{
      const d=safeJson(h.deterministic,{}),type=d.evaluation_type||"analysis";
      return '<details><summary>'+escapeHtml(localDate(h.created_at))+' · '+escapeHtml(type)+' · '+escapeHtml(h.model||"model")+
        '</summary><div class="history-body">'+(window.marked?marked.parse(h.llm_markdown||""):escapeHtml(h.llm_markdown||""))+'</div></details>';
    }).join("");
}
async function saveAiEvaluation(type,context,text,model,responseJson=null){
  if(!client||!userId())return;
  const deterministic={evaluation_type:type,...context,ai_memory_summary:summarizeAiMemory(text)};
  if(responseJson!=null)deterministic.response_json=responseJson;
  const res=await client.from("analysis_snapshots").insert({
    user_id:userId(),deterministic,llm_markdown:text||"",model:model||"unknown"
  });
  if(res.error){console.warn("Could not save AI evaluation",res.error);return;}
  await loadAiHistory();
}
async function askAiCoach() {
  if (!client) return;
  const d=analysisData();
  el("aiCoachOutput").textContent="Deriving 180-day metrics and analyzing…";
  el("aiCoachButton").disabled=true;
  try{
    const compact=await activitiesForAi(180);
    const result=await client.functions.invoke(cfg.COACH_FUNCTION||"coach",{body:{
      mode:"analysis",analysis:compactAnalysisForAi(d),ml_model:compactMlForAi(),race_predictions:conservativeRacePredictions(),activities:compact,history:aiHistoryPayload(),profile:profileForAi()
    }});
    if(result.error){el("aiCoachOutput").textContent="AI coach failed: "+result.error.message;return;}
    const text=(result.data&&result.data.text)||"No response returned.",model=(result.data&&result.data.model)||"unknown";
    el("aiCoachOutput").innerHTML=window.marked?marked.parse(text):escapeHtml(text);
    await saveAiEvaluation("analysis",{window_days:180,activity_count:compact.length,analysis:compactAnalysisForAi(d),ml_model:compactMlForAi()},text,model);
  }catch(err){
    el("aiCoachOutput").textContent="AI coach failed: "+(err.message||String(err));
  }finally{el("aiCoachButton").disabled=false;}
}
function validateAiPlan(raw,base){
  if(!Array.isArray(raw)||raw.length!==14)return null;
  const byDate=new Map(raw.map(w=>[String(w.date||""),w])),out=[];
  for(const b of base){
    if(b.locked){out.push({...b});continue;}
    const r=byDate.get(b.date);if(!r)return null;
    const rest=!!r.no_workout||String(r.sport_type||"").toLowerCase()==="none";
    const w=makePlannedWorkout(new Date(b.date+"T12:00:00"),{
      start_time:String(r.start_time||b.start_time||"18:00").slice(0,5),
      title:String(r.title||b.title||"Planned workout").slice(0,120),
      sport_type:rest?"None":String(r.sport_type||b.sport_type||"Run").slice(0,40),
      family:rest?"rest_or_mobility":String(r.family||b.family||"easy_aerobic").slice(0,50),
      duration_min:rest?0:clamp(n(r.duration_min,n(b.duration_min,0)),0,300),
      distance_km:rest?0:clamp(n(r.distance_km,n(b.distance_km,0)),0,80),
      zone:rest?"Rest":String(r.zone||b.zone||"").slice(0,100),
      pace:rest?"":String(r.pace||b.pace||"").slice(0,120),
      wattage:rest?"":String(r.wattage||b.wattage||"").slice(0,120),
      notes:String(r.notes||"").slice(0,500),
      no_workout:rest,locked:false,source:"ai_coach"
    });
    out.push(w);
  }
  return out;
}

function plannerIntensityTargets(){
  const c=trainingContext(),prefs=(profile&&profile.training_preferences)||{};
  const level=clamp(Math.round(n(prefs.training_aggressiveness,3)),1,5);
  const runDays=clamp(Math.round(n(prefs.weekly_run_days,3)),1,7);
  const weeklyKm=n(c.runKm28Weekly,0);
  // Aggressiveness controls training stimulus and allowed progressive adaptation,
  // NOT how many hard days to run.
  const gain=[0,.025,.05,.08,.11][level-1];
  const base7=n(c.runKm7,0),sustainableReference=weeklyKm+
    Math.min(Math.max(0,base7-weeklyKm)*.35,weeklyKm*.35);
  const moderatelyElevated=c.loadRatio>1.55||c.hardMin7>65;
  const totalWeek1=weeklyKm>0?Math.max(5,sustainableReference*(moderatelyElevated?1:(1+gain))):null;
  const totalWeek2=weeklyKm>0?Math.max(5,sustainableReference*(1+gain)*1.055):null;
  const sufficientBase=weeklyKm>=8&&runDays>=3;
  const overload=c.loadRatio>1.9||c.hardMin7>80;
  return {
    level,run_days_per_week:runDays,strength_per_week:clamp(Math.round(n(prefs.strength_sessions,1)),0,5),
    primary_goal:profile&&profile.goals&&profile.goals.primary_goal,
    target_minutes:n(profile&&profile.goals&&profile.goals.target_minutes),
    goal_date:profile&&profile.goals&&profile.goals.goal_date,
    recent_weekly_run_km:Math.round(weeklyKm*10)/10,
    recent_longest_run_km:Math.round(n(c.longestRun,0)*10)/10,
    acute_load_ratio:Math.round(c.loadRatio*100)/100,recent_z45_min:Math.round(c.hardMin7),
    hours_since_last_hard:Math.round(c.lastHardHours),
    target_week1_run_km:totalWeek1==null?null:Math.round(totalWeek1*10)/10,
    target_week2_run_km:totalWeek2==null?null:Math.round(totalWeek2*10)/10,
    desired_quality_per_week:level>=3&&sufficientBase&&!overload?1:0,
    desired_long_run_per_week:runDays>=2&&weeklyKm>=7?1:0,
    recovery_caution:overload,
    elevated_recent_load:c.loadRatio>1.55||c.hardMin7>65,
    guidance:level>=4?"Ambitious, progressive mix of one quality, one long aerobic and remaining easy sessions when recovery permits":
      level===3?"Balanced progressive mix with occasional controlled intensity":
      "Conservative volume and low-intensity base development"
  };
}
function aiPlanWorkloadChecks(candidate,constraints,reason=""){
  const problems=[];
  const weeks=[candidate.slice(0,7),candidate.slice(7,14)];
  const quality=weeks.map(w=>w.filter(x=>!x.no_workout&&planIsHard(x)).length);
  const longs=weeks.map(w=>w.filter(x=>!x.no_workout&&x.family==="long_run").length);
  const volume=weeks.map(w=>sum(w.filter(x=>!x.no_workout&&planSportCategory(x)==="run").map(x=>n(x.distance_km,0))));
  const reasons=String(reason||"").trim();
  if(constraints.level>=4&&!constraints.recovery_caution&&constraints.recent_weekly_run_km>=8){
    if(constraints.desired_quality_per_week&&quality[0]+quality[1]===0)
      problems.push("All quality sessions were removed despite level "+constraints.level+
        "; retain at least one controlled quality session in the second week or choose a conservative setting if recovery requires it.");
    if(constraints.desired_long_run_per_week&&longs[0]+longs[1]===0)
      problems.push("Both long aerobic runs were removed despite the athlete's endurance goal.");
  }
  const base=constraints.recent_weekly_run_km;
  if(base>=8){
    if(volume[0]>base*1.22+1&&volume[0]>base+4)
      problems.push("Week 1 running volume exceeds conservative progression limits.");
    if(volume[1]>Math.max(base*1.34+1,volume[0]*1.18+1)&&volume[1]>base+4)
      problems.push("Week 2 running volume exceeds progression limits.");
  }
  for(let start=0;start<=7;start++){
    if(candidate.slice(start,start+7).filter(planIsHard).length>2)
      problems.push("More than two hard sessions in a rolling seven-day window.");
  }
  return {problems,quality,longs,volume};
}
async function askAiPlanner(){
  if(!client)return;
  el("plannerAiButton").disabled=true;
  el("plannerAiStatus").textContent="Deriving the last 30 days and asking AI to evaluate the 14-day plan…";
  try{
    const base=currentPlan.length?currentPlan:buildFourteenDayPlan();
    const constraints=plannerIntensityTargets();
    const compact=await activitiesForAi(30);
    const result=await client.functions.invoke(cfg.COACH_FUNCTION||"coach",{body:{
      mode:"plan",ml_model:compactMlForAi(),race_predictions:conservativeRacePredictions(),activities:compact,
      planning_intensity:constraints,current_plan:base.map(({dateObj,...w})=>w),history:aiHistoryPayload(),profile:profileForAi()
    }});
    if(result.error)throw result.error;
    const candidate=validateAiPlan(result.data&&result.data.plan,base);
    if(!candidate)throw new Error("AI returned an invalid 14-day plan; existing plan was left unchanged.");
    const reason=String(result.data&&result.data.load_adjustment_reason||"");
    const checks=aiPlanWorkloadChecks(candidate,constraints,reason);
    if(checks.problems.length)throw new Error("AI plan rejected: "+checks.problems.join(" ")+
      " Deterministic plan remains unchanged.");
    const summary=String((result.data&&result.data.summary)||"AI reviewed and updated the plan."),model=(result.data&&result.data.model)||"unknown";
    currentPlan=candidate;
    await persistPlan(candidate,"ai_coach_v1",{ai_summary:summary,model});
    await saveAiEvaluation("planner",{window_days:30,activity_count:compact.length,current_plan:base.map(({dateObj,...w})=>w)},summary,model,
      {summary,plan:candidate.map(({dateObj,...w})=>w)});
    el("plannerAiStatus").textContent="AI-adjusted plan saved.";
    el("plannerAiSummary").classList.remove("hidden");
    el("plannerAiSummary").innerHTML=window.marked?marked.parse(summary):escapeHtml(summary);
    renderPlan();
  }catch(err){
    el("plannerAiStatus").textContent="AI plan update failed: "+(err.message||String(err));
  }finally{el("plannerAiButton").disabled=false;}
}

function openUpload(){ el("uploadStatus").textContent=""; el("tcxInput").value=""; el("uploadDialog").showModal(); }

function resolveDuplicateDecision(decision){
  if(!duplicateResolver)return;
  const r=duplicateResolver;duplicateResolver=null;
  if(el("duplicateDialog").open)el("duplicateDialog").close();
  r(decision);
}
function askDuplicateDecision(file,existing,matchType){
  el("duplicateInfo").innerHTML='<strong>'+escapeHtml(file.name)+'</strong><br>matches <strong>'+escapeHtml(existing.name||"Existing workout")+
    '</strong> · '+escapeHtml(localDate(existing.start_time))+'<br><span class="muted">Detected by '+escapeHtml(matchType)+'.</span>';
  el("duplicateDialog").showModal();
  return new Promise(resolve=>{duplicateResolver=resolve;});
}
async function importSelectedFiles() {
  const files=Array.from(el("tcxInput").files||[]);
  if(!files.length){el("uploadStatus").textContent="Choose at least one TCX, FIT or GPX file.";return;}
  el("importFilesButton").disabled=true;
  let ok=0, skipped=0, overwritten=0;
  for(let i=0;i<files.length;i++){
    el("uploadStatus").textContent="Importing "+(i+1)+" / "+files.length+": "+files[i].name;
    try {
      const result=await importTcx(files[i]);
      if(result==="skipped")skipped++;else if(result==="overwritten")overwritten++;else ok++;
    } catch(err){ console.error(err); toast(files[i].name+": "+err.message,5000); }
  }
  el("importFilesButton").disabled=false;
  await loadActivities(); streamCache.clear(); renderAll(); enrichMetricsInBackground();
  el("uploadStatus").textContent="Imported "+ok+(overwritten?" · overwritten "+overwritten:"")+(skipped?" · skipped "+skipped:"")+".";
  if(ok||overwritten){
    refreshMissingWeather().catch(err=>console.warn("Automatic weather enrichment failed",err));
    setTimeout(()=>el("uploadDialog").close(),900);
  }
}

async function sha256(file) {
  const buf=await file.arrayBuffer(); const hash=await crypto.subtle.digest("SHA-256",buf);
  return Array.from(new Uint8Array(hash)).map(b=>b.toString(16).padStart(2,"0")).join("");
}
function firstByLocal(node,name) {
  const all=node.getElementsByTagName("*");
  for(let i=0;i<all.length;i++) if(all[i].localName===name) return all[i];
  return null;
}
function textByLocal(node,name) { const x=firstByLocal(node,name); return x?x.textContent.trim():null; }
function haversine(a,b) {
  if(!a||!b||a.lat==null||a.lon==null||b.lat==null||b.lon==null)return 0;
  const R=6371000, p1=a.lat*Math.PI/180,p2=b.lat*Math.PI/180,dp=(b.lat-a.lat)*Math.PI/180,dl=(b.lon-a.lon)*Math.PI/180;
  const q=Math.sin(dp/2)**2+Math.cos(p1)*Math.cos(p2)*Math.sin(dl/2)**2; return 2*R*Math.atan2(Math.sqrt(q),Math.sqrt(1-q));
}
function zoneIndex(hr,z) {
  if(hr==null)return null;
  if(hr<=n(z.z1_max,130))return 1;if(hr<=n(z.z2_max,150))return 2;if(hr<=n(z.z3_max,165))return 3;if(hr<=n(z.z4_max,178))return 4;return 5;
}
function downsamplePoints(points,maxPoints=4000){
  if(points.length<=maxPoints)return points;
  const step=(points.length-1)/(maxPoints-1),out=[];
  for(let i=0;i<maxPoints;i++) out.push(points[Math.min(points.length-1,Math.round(i*step))]);
  return out;
}

let garminFitSdkPromise=null;
function fitSemicirclesToDegrees(value){
  const v=n(value);if(v==null)return null;
  return Math.abs(v)>180?v*(180/2147483648):v;
}
function fitTimestamp(value){
  if(value instanceof Date)return Number.isNaN(value.getTime())?null:value.toISOString();
  if(typeof value==="string"){
    const d=new Date(value);return Number.isNaN(d.getTime())?null:d.toISOString();
  }
  const raw=n(value);if(raw==null)return null;
  const epoch=raw>1e12?raw:raw>=1450000000?raw*1000:raw*1000+631065600000;
  const d=new Date(epoch);
  return Number.isNaN(d.getTime())?null:d.toISOString();
}
async function decodeFitWorkout(file){
  // Official FIT JavaScript SDK, pinned to avoid unannounced API changes.
  // Import only when a FIT file is selected; no API keys or GPS leave the browser.
  if(!garminFitSdkPromise)garminFitSdkPromise=import("https://esm.sh/@garmin/fitsdk@21.218.0")
    .catch(err=>{garminFitSdkPromise=null;throw new Error("Garmin FIT decoder could not be loaded (check internet/CDN): "+err.message);});
  const {Decoder,Stream}=await garminFitSdkPromise;
  const buf=await file.arrayBuffer(),stream=Stream.fromArrayBuffer(buf);
  if(!Decoder.isFIT(stream))throw new Error("Invalid FIT file header.");
  const decoder=new Decoder(stream);
  if(!decoder.checkIntegrity())throw new Error("FIT file failed its integrity/CRC check.");
  const {messages,errors}=decoder.read({convertDateTimesToDates:true,applyScaleAndOffset:true,
    convertTypesToStrings:true,expandSubFields:true,expandComponents:true,mergeHeartRates:true});
  const records=messages?.recordMesgs||messages?.record||[];
  const sessions=messages?.sessionMesgs||messages?.session||[];
  const session=Array.isArray(sessions)?sessions[0]:sessions;
  if(!Array.isArray(records)||!records.length)throw new Error("FIT contains no timestamped record samples.");
  const sport=String(session?.sport||"Workout");
  const points=records.map(r=>{
    const timestamp=fitTimestamp(r.timestamp||r.timeStamp);
    const lat=fitSemicirclesToDegrees(r.positionLat),lon=fitSemicirclesToDegrees(r.positionLong);
    return {
      time:timestamp,
      lat:lat!=null&&Math.abs(lat)<=90?lat:null,
      lon:lon!=null&&Math.abs(lon)<=180?lon:null,
      alt:n(r.enhancedAltitude)??n(r.altitude),
      distance_m:n(r.distance),hr:n(r.heartRate),
      cadence:n(r.cadence)??n(r.fractionalCadence),
      watts:n(r.power)??n(r.calculatedPower),
      speed_mps:n(r.enhancedSpeed)??n(r.speed),
      raw_speed_mps:n(r.enhancedSpeed)??n(r.speed)
    };
  }).filter(p=>p.time).sort((a,b)=>new Date(a.time)-new Date(b.time));
  if(points.length<2)throw new Error("FIT has fewer than two timestamped data points.");
  // Sanity-check units and monotonic time before deriving workout metrics.
  for(let i=1;i<points.length;i++){
    if(new Date(points[i].time)<new Date(points[i-1].time))throw new Error("FIT timestamps are not ordered.");
  }
  if(errors&&errors.length)console.warn("FIT decoder reported non-fatal data warnings",errors);
  return {sport,idText:fitTimestamp(session?.startTime)||points[0].time,points};
}
async function parseWorkoutFile(file) {
  const isFit=/\.fit$/i.test(file.name);
  let sport,idText,points;
  if(isFit){
    const parsed=await decodeFitWorkout(file);
    sport=parsed.sport;idText=parsed.idText;points=parsed.points;
  }else if(/\.gpx$/i.test(file.name)){
    const xml=new DOMParser().parseFromString(await file.text(),"application/xml");
    if(xml.querySelector("parsererror"))throw new Error("Invalid GPX XML.");
    const track=Array.from(xml.getElementsByTagName("*")).find(x=>x.localName==="trk");
    const typeNode=track&&firstByLocal(track,"type");
    const trackType=typeNode?typeNode.textContent.trim():"";
    sport=trackType||"Workout";idText=null;
    const nodes=Array.from(xml.getElementsByTagName("*")).filter(x=>x.localName==="trkpt");
    points=nodes.map(tp=>{
      const time=textByLocal(tp,"time");
      return {time,lat:n(tp.getAttribute("lat")),lon:n(tp.getAttribute("lon")),
        alt:n(textByLocal(tp,"ele")),distance_m:null,
        hr:n(textByLocal(tp,"hr")),cadence:n(textByLocal(tp,"cad")),
        watts:n(textByLocal(tp,"power")??textByLocal(tp,"watts")),
        raw_speed_mps:n(textByLocal(tp,"speed")),speed_mps:n(textByLocal(tp,"speed"))};
    }).filter(p=>p.time);
  }else if(/\.tcx$/i.test(file.name)){
    const xml=new DOMParser().parseFromString(await file.text(),"application/xml");
    if(xml.querySelector("parsererror"))throw new Error("Invalid TCX XML.");
    const activity=Array.from(xml.getElementsByTagName("*")).find(x=>x.localName==="Activity");
    if(!activity)throw new Error("No Activity element found.");
    sport=activity.getAttribute("Sport")||"Workout";
    idText=textByLocal(activity,"Id");
    const tpNodes=Array.from(xml.getElementsByTagName("*")).filter(x=>x.localName==="Trackpoint");
    points=[];
    for(const tp of tpNodes){
      const time=textByLocal(tp,"Time"),lat=n(textByLocal(tp,"LatitudeDegrees")),
        lon=n(textByLocal(tp,"LongitudeDegrees")),alt=n(textByLocal(tp,"AltitudeMeters")),
        dist=n(textByLocal(tp,"DistanceMeters"));
      const hrNode=firstByLocal(tp,"HeartRateBpm");
      const hr=hrNode?n(textByLocal(hrNode,"Value")):null;
      const cad=n(textByLocal(tp,"Cadence"))??n(textByLocal(tp,"RunCadence"));
      const watts=n(textByLocal(tp,"Watts")),speed=n(textByLocal(tp,"Speed"));
      points.push({time,lat,lon,alt,distance_m:dist,hr,cadence:cad,watts,
        raw_speed_mps:speed,speed_mps:speed});
    }
  }else throw new Error("Unsupported file type. Please select a .tcx, .fit or .gpx workout.");
  if(!points.length)throw new Error((isFit?"FIT":/\.gpx$/i.test(file.name)?"GPX":"TCX")+" contains no timestamped trackpoints.");
  const firstTime=new Date(points.find(p=>p.time)?.time||idText);
  if(Number.isNaN(firstTime.getTime()))throw new Error("Could not determine workout start time.");
  let cumulative=0;
  for(let i=0;i<points.length;i++){
    if(i>0){
      if(points[i].distance_m==null) cumulative+=haversine(points[i-1],points[i]);
      else cumulative=points[i].distance_m;
    } else cumulative=points[i].distance_m||0;
    points[i].distance_calc_m=cumulative;
    points[i].elapsed_s=points[i].time?(new Date(points[i].time)-firstTime)/1000:0;
  }
  // Build the same authoritative distance/time speed used by the viewer before
  // storing the stream. Keep raw_speed_mps separately for diagnostics.
  const speedRows=points.map((p,i)=>({i,elapsed_s:n(p.elapsed_s,i),distance_m:n(p.distance_calc_m)??n(p.distance_m),raw_speed_mps:n(p.raw_speed_mps),speed_mps:n(p.speed_mps)}));
  const derivedOk=deriveSpeedFromDistance(speedRows,4);
  for(let i=0;i<points.length;i++){
    if(derivedOk&&n(speedRows[i].distance_speed_mps)!=null)points[i].speed_mps=speedRows[i].distance_speed_mps;
  }

  const last=points[points.length-1];
  let distance=n(last.distance_m);
  if(distance==null||distance<=0)distance=n(last.distance_calc_m,0);
  const duration=Math.max(0,n(last.elapsed_s,0));
  let elevation=0;
  for(let i=1;i<points.length;i++){const d=n(points[i].alt)-n(points[i-1].alt);if(Number.isFinite(d)&&d>.5&&d<50)elevation+=d;}
  const hrs=points.map(p=>n(p.hr)).filter(x=>x!=null);
  const avgHr=mean(hrs), maxHr=hrs.length?Math.max(...hrs):null;
  const cadence=mean(points.map(p=>n(p.cadence)).filter(x=>x!=null));
  const avgPower=mean(points.map(p=>n(p.watts)).filter(x=>x!=null));
  const zones=[0,0,0,0,0], z=(profile&&profile.hr_zones)||defaultProfile().hr_zones;
  for(let i=1;i<points.length;i++){
    const dt=clamp(n(points[i].elapsed_s,0)-n(points[i-1].elapsed_s,0),0,30);
    const zi=zoneIndex(n(points[i].hr),z); if(zi)zones[zi-1]+=dt;
  }
  const zoneTotal=sum(zones), easy=zoneTotal?(zones[0]+zones[1])/zoneTotal:null, hard=zoneTotal?(zones[3]+zones[4])/zoneTotal:null;
  const load=sum(zones.map((s,i)=>s/60*(i+1)));
  const half=duration/2;
  function eff(part){
    let dd=0,hh=[],tt=0;
    for(let i=1;i<points.length;i++){
      const t=n(points[i].elapsed_s,0); if((part===0&&t>half)||(part===1&&t<=half))continue;
      const dt=clamp(t-n(points[i-1].elapsed_s,0),0,30); if(!dt)continue;
      const d=Math.max(0,n(points[i].distance_calc_m,0)-n(points[i-1].distance_calc_m,0));
      dd+=d;tt+=dt;if(n(points[i].hr)!=null)hh.push(n(points[i].hr));
    }
    const h=mean(hh);return h&&tt>0?(dd/tt)/h:null;
  }
  const e1=eff(0),e2=eff(1),drift=e1&&e2?100*(e2/e1-1):null;
  const fileHash=await sha256(file);
  const pace=distance>0&&duration>0?(duration/60)/(distance/1000):null;
  const derivedRows=buildStreamRows({points});
  const parsedActivityForMetrics={sport_type:sport,sport_category:normalizeSport(sport),max_hr:maxHr,start_time:firstTime.toISOString(),moving_time_s:duration,duration_s:duration};
  const streamDerived=derivedStreamSummary(derivedRows,parsedActivityForMetrics);
  const advanced=streamDerived.advanced||{};
  const streamPoints=downsamplePoints(points,4000);
  const startIso=firstTime.toISOString();
  const dedupe=[startIso.slice(0,19),normalizeSport(sport),Math.round(duration/10),Math.round(distance/10)].join("|");
  return {
    activity:{
      source:isFit?"fit_upload":/\.gpx$/i.test(file.name)?"gpx_upload":"tcx_upload",name:file.name.replace(/\.(tcx|fit|gpx)$/i,""),sport_type:sport,sport_category:normalizeSport(sport),
      start_time:startIso,original_start_time:idText||startIso,duration_s:duration,moving_time_s:duration,distance_m:distance,
      elevation_gain_m:elevation,elevation_loss_m:n(advanced.elevation_loss_m),avg_hr:avgHr,max_hr:maxHr,avg_pace_min_km:pace,avg_gap_pace_min_km:streamDerived.gap,
      training_load_score:load,trimp_score:(zones[0]/60*.8+zones[1]/60*1.5+zones[2]/60*2.7+zones[3]/60*5+zones[4]/60*7),
      easy_zone_fraction:easy,hard_zone_fraction:hard,z1_s:zones[0],z2_s:zones[1],z3_s:zones[2],z4_s:zones[3],z5_s:zones[4],
      own_vo2max_estimate:streamDerived.vo2,estimated_vo2max:streamDerived.vo2,average_power:avgPower,normalized_power:n(advanced.normalized_power_w),cadence_spm:cadence,
      hr_efficiency_drift_pct:n(advanced.hr_efficiency_drift_pct)??drift,gap_hr_efficiency_drift_pct:n(advanced.gap_hr_efficiency_drift_pct),
      km_gap_hr_efficiency_drift_pct:n(advanced.km_gap_hr_efficiency_drift_pct),elapsed_time_s:duration,file_sha256:fileHash,dedupe_key:dedupe,
      metrics_json:{parser:isFit?"workoutbuddy-web-fit-v1":/\.gpx$/i.test(file.name)?"workoutbuddy-web-gpx-v1":"workoutbuddy-web-tcx-v4",trackpoints:points.length,stored_trackpoints:streamPoints.length,
        ...advanced,
        max_speed_kmh:streamDerived.max_speed_kmh,max_power_w:streamDerived.max_power_w,max_cadence:streamDerived.max_cadence,
        avg_grade_pct:streamDerived.avg_grade_pct,min_grade_pct:streamDerived.min_grade_pct,max_grade_pct:streamDerived.max_grade_pct,
        vo2_estimate_method:"ACSM running oxygen cost + grade + HR-reserve heuristic",
        metrics_quality:"stream-derived; corrected distance/time speed; advanced metrics v4",
        speed_source:derivedOk?"distance_over_time_8s_centered":"tcx_speed_fallback"}
    },
    stream:{points:streamPoints}
  };
}

async function importTcx(file) {
  const parsed=await parseWorkoutFile(file);
  const fields="id,name,start_time,raw_file_path,file_sha256,dedupe_key,apple_vo2max,body_weight_kg,metrics_json";
  const byHash=await client.from("activities").select(fields).eq("user_id",userId()).eq("file_sha256",parsed.activity.file_sha256).limit(1);
  if(byHash.error)throw byHash.error;
  let existing=byHash.data&&byHash.data[0],matchType="identical file hash";
  if(!existing){
    const byKey=await client.from("activities").select(fields).eq("user_id",userId()).eq("dedupe_key",parsed.activity.dedupe_key).limit(1);
    if(byKey.error)throw byKey.error;
    existing=byKey.data&&byKey.data[0];matchType="same start time / sport / duration / distance";
  }
  if(!existing){
    // Cross-format exports (TCX vs FIT) can differ by seconds or GPS rounding.
    // Look for a close match, but never silently overwrite; user must choose.
    const start=new Date(parsed.activity.start_time).getTime();
    const nearby=await client.from("activities").select(fields+",sport_category,duration_s,distance_m")
      .eq("user_id",userId())
      .gte("start_time",new Date(start-90000).toISOString())
      .lte("start_time",new Date(start+90000).toISOString()).limit(30);
    if(nearby.error)throw nearby.error;
    existing=(nearby.data||[]).find(a=>
      normalizeSport(a.sport_category)===normalizeSport(parsed.activity.sport_category)&&
      Math.abs(n(a.duration_s,0)-n(parsed.activity.duration_s,0))<=120&&
      Math.abs(n(a.distance_m,0)-n(parsed.activity.distance_m,0))<=
        Math.max(120,.035*Math.max(n(a.distance_m,0),n(parsed.activity.distance_m,0)))
    );
    if(existing)matchType="near-identical workout timestamp, sport, duration and distance";
  }

  const year=new Date(parsed.activity.start_time).getFullYear();
  const safeName=file.name.replace(/[^A-Za-z0-9_.-]/g,"_");
  const newPath=userId()+"/"+year+"/"+parsed.activity.start_time.replace(/[:.]/g,"-")+"_"+safeName;

  if(existing){
    const decision=await askDuplicateDecision(file,existing,matchType);
    if(decision!=="overwrite")return "skipped";
    // Preserve correct raw-file extensions when overwriting via a different
    // export format; the old private file is kept as a backup.
    const incomingExt=(file.name.match(/\.(tcx|fit|gpx)$/i)||[])[1]?.toLowerCase();
    const oldExt=(String(existing.raw_file_path||"").match(/\.(tcx|fit|gpx)$/i)||[])[1]?.toLowerCase();
    const path=existing.raw_file_path&&oldExt===incomingExt?existing.raw_file_path:newPath;
    const up=await client.storage.from("workout-files").upload(path,file,{upsert:true,contentType:/\.fit$/i.test(file.name)?"application/octet-stream":(file.type||"application/xml")});
    if(up.error)throw up.error;

    parsed.activity.user_id=userId();parsed.activity.raw_file_path=path;
    // Preserve manual per-workout values unless this import explicitly provides them.
    if(parsed.activity.apple_vo2max==null&&existing.apple_vo2max!=null)parsed.activity.apple_vo2max=existing.apple_vo2max;
    if(parsed.activity.body_weight_kg==null&&existing.body_weight_kg!=null)parsed.activity.body_weight_kg=existing.body_weight_kg;
    parsed.activity.metrics_json={...safeJson(existing.metrics_json,{}),...safeJson(parsed.activity.metrics_json,{})};

    const upd=await client.from("activities").update(parsed.activity).eq("id",existing.id).eq("user_id",userId()).select("id").single();
    if(upd.error)throw upd.error;
    const st=await client.from("activity_streams").upsert({activity_id:existing.id,user_id:userId(),stream_data:parsed.stream},{onConflict:"activity_id"});
    if(st.error)throw st.error;
    streamCache.delete(existing.id);
    return "overwritten";
  }

  const up=await client.storage.from("workout-files").upload(newPath,file,{upsert:false,contentType:/\.fit$/i.test(file.name)?"application/octet-stream":(file.type||"application/xml")});
  if(up.error)throw up.error;
  parsed.activity.user_id=userId();parsed.activity.raw_file_path=newPath;
  const ins=await client.from("activities").insert(parsed.activity).select("id").single();
  if(ins.error)throw ins.error;
  const st=await client.from("activity_streams").insert({activity_id:ins.data.id,user_id:userId(),stream_data:parsed.stream});
  if(st.error)throw st.error;
  return "ok";
}

function parseRaceTimeInput(value){
  const raw=String(value||"").trim();if(!raw)return null;
  if(/^\d+(?:\.\d+)?$/.test(raw))return n(raw);
  const p=raw.split(":").map(x=>Number(x));if(p.some(x=>!Number.isFinite(x)||x<0))return null;
  if(p.length===3)return p[0]*60+p[1]+p[2]/60;
  if(p.length===2){
    // "1:45" is interpreted as 1 h 45 min; "45:00" as 45 min 00 s.
    return p[0]<10?p[0]*60+p[1]:p[0]+p[1]/60;
  }
  return null;
}
function formatRaceTimeInput(minutes){
  const v=n(minutes);if(v==null||v<=0)return "";
  const total=Math.round(v*60),h=Math.floor(total/3600),m=Math.floor((total%3600)/60),sec=total%60;
  return h>0?h+":"+String(m).padStart(2,"0")+":"+String(sec).padStart(2,"0"):m+":"+String(sec).padStart(2,"0");
}
function raceTimeText(minutes){return formatRaceTimeInput(minutes)||"—";}

function openSettings(){
  const p=profile||defaultProfile(),g=p.goals||{},t=p.training_preferences||{},z=p.hr_zones||{};
  el("profileName").value=p.display_name||"";el("profileBirthdate").value=p.birthdate||"";el("profileHeight").value=n(p.height_cm,178);
  el("profileWeight").value=n(p.current_weight_kg,75);el("profileVo2").value=n(p.profile_vo2max,45);
  el("goalType").value=g.primary_goal||"general";el("goalDate").value=g.goal_date||"";el("goalTargetTime").value=formatRaceTimeInput(n(g.target_minutes));
  el("weeklyRunDays").value=n(t.weekly_run_days,3);el("weeklyStrengthDays").value=n(t.strength_sessions,2);el("trainingAggressiveness").value=String(n(t.training_aggressiveness,3));el("longRunDay").value=String(t.long_run_day==null?6:t.long_run_day);
  el("z1Max").value=n(z.z1_max,130);el("z2Max").value=n(z.z2_max,150);el("z3Max").value=n(z.z3_max,165);el("z4Max").value=n(z.z4_max,178);el("z5Max").value=n(z.z5_max,220);
  el("settingsStatus").textContent="";el("settingsDialog").showModal();
}
async function saveSettings(event){
  event.preventDefault();
  const row={user_id:userId(),display_name:el("profileName").value.trim(),birthdate:el("profileBirthdate").value||null,
    height_cm:n(el("profileHeight").value),current_weight_kg:n(el("profileWeight").value),profile_vo2max:n(el("profileVo2").value),
    goals:{primary_goal:el("goalType").value,goal_date:el("goalDate").value||null,target_minutes:parseRaceTimeInput(el("goalTargetTime").value)},
    training_preferences:{...((profile&&profile.training_preferences)||{}),weekly_run_days:n(el("weeklyRunDays").value,3),strength_sessions:n(el("weeklyStrengthDays").value,2),training_aggressiveness:n(el("trainingAggressiveness").value,3),long_run_day:n(el("longRunDay").value,6)},
    hr_zones:{z1_max:n(el("z1Max").value,130),z2_max:n(el("z2Max").value,150),z3_max:n(el("z3Max").value,165),z4_max:n(el("z4Max").value,178),z5_max:n(el("z5Max").value,220)}
  };
  el("settingsStatus").textContent="Saving…";
  const res=await client.from("profiles").upsert(row).select().single();
  if(res.error){el("settingsStatus").textContent=res.error.message;return;}
  profile=res.data;profile.hr_zones=safeJson(profile.hr_zones,{});profile.goals=safeJson(profile.goals,{});profile.training_preferences=safeJson(profile.training_preferences,{});
  el("settingsStatus").textContent="Saved.";renderAll();setTimeout(()=>el("settingsDialog").close(),500);
}

window.addEventListener("DOMContentLoaded",boot);
})();