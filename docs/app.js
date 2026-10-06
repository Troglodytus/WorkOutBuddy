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
let routeMaps = {history:null, home:null};
let viewerStates = {history:null, home:null};
let currentPlan = [];
let duplicateResolver = null;
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
    goals: {primary_goal:"general",goal_date:null},
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
  initWorkoutViewerControls();
  el("regeneratePlanButton").addEventListener("click", renderPlan);
  el("plannerRegenerateButton").addEventListener("click", renderPlan);
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
  await Promise.all([loadProfile(), loadActivities()]);
  renderAll();
  setSync("Synced");
}

function openView(name) {
  qsa(".tab").forEach(b => b.classList.toggle("active", b.dataset.view === name));
  qsa(".view").forEach(v => v.classList.remove("active"));
  const target = el(name + "View");
  if (target) target.classList.add("active");
  if (name === "history") renderHistory();
  if (name === "planner") renderPlanner();
  if (name === "analysis") renderAnalysis();
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

async function loadActivities() {
  if (!userId()) return;
  const res = await client.from("activities").select("*").order("start_time",{ascending:false}).limit(2000);
  if (res.error) { toast("Could not load activities: " + res.error.message,5000); activities = []; return; }
  activities = res.data || [];
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
  currentPlan=out;return out;
}
function renderPlan(){
  const plan=buildFourteenDayPlan();
  el("weekPlan").innerHTML=plan.slice(0,7).map((p,i)=>
    '<div class="day-card '+(i===0?"today ":"")+(p.locked?"manual":"")+'" data-plan-date="'+escapeHtml(p.date)+'">'+
    '<div class="day-date">'+escapeHtml(shortDay(p.dateObj))+(p.locked?' · manual':'')+'</div>'+
    '<div class="day-title">'+escapeHtml(p.title)+'</div><div class="day-meta">'+escapeHtml(planMeta(p))+"</div></div>"
  ).join("");
  qsa("#weekPlan [data-plan-date]").forEach(card=>card.addEventListener("click",()=>openPlanEditor(card.dataset.planDate)));
  renderPlanner();
}
function renderPlanner(){
  const plan=currentPlan.length?currentPlan:buildFourteenDayPlan();
  const runKm=sum(plan.filter(w=>planSportCategory(w)==="run").map(w=>n(w.distance_km,0)));
  const rideMin=sum(plan.filter(w=>planSportCategory(w)==="bike").map(w=>n(w.duration_min,0)));
  const totalMin=sum(plan.map(w=>n(w.duration_min,0)));
  const manual=plan.filter(w=>w.locked).length;
  el("plannerSummary").textContent="Next 14 days: "+runKm.toFixed(1)+" run km · "+Math.round(rideMin)+" ride min · "+Math.round(totalMin)+" total min · "+manual+" manual override"+(manual===1?"":"s");
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
  try{await persistTrainingPreferences({manual_plan_overrides:overrides});el("planEditDialog").close();renderPlan();toast("Manual override saved; following schedule recalculated.");}
  catch(err){el("planEditStatus").textContent=err.message||String(err);}
}
async function clearPlanOverride(){
  const dateKey=el("planEditDate").value,pref=(profile&&profile.training_preferences)||{},overrides={...(safeJson(pref.manual_plan_overrides,{})||{})};
  if(!overrides[dateKey]){el("planEditDialog").close();return;}delete overrides[dateKey];
  try{await persistTrainingPreferences({manual_plan_overrides:overrides});el("planEditDialog").close();renderPlan();toast("Manual override cleared; schedule recalculated.");}
  catch(err){el("planEditStatus").textContent=err.message||String(err);}
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

function activityMetricKeys() {
  const preferred=[
    "start_time","distance_km","duration_min","moving_time_min","avg_pace_min_km","avg_gap_pace_min_km",
    "avg_hr","max_hr","elevation_gain_m","elevation_loss_m","derived_load","training_load_score","trimp_score",
    "apple_vo2max","own_vo2max_estimate","estimated_vo2max","body_weight_kg","average_power","normalized_power","cadence_spm",
    "hr_efficiency_drift_pct","gap_hr_efficiency_drift_pct","km_gap_hr_efficiency_drift_pct",
    "easy_zone_fraction","hard_zone_fraction","z1_min","z2_min","z3_min","z4_min","z5_min"
  ];
  const keys=new Set(preferred);
  const skip=new Set(["id","user_id","source_external_id","raw_file_path","file_sha256","dedupe_key","metrics_json","created_at","updated_at","original_start_time","name","sport_type","sport_category","source","local_timezone"]);
  for(const a of activities){
    for(const [k,v] of Object.entries(a)){
      if(skip.has(k)||keys.has(k)) continue;
      if(n(v)!=null) keys.add(k);
    }
    const mj=safeJson(a.metrics_json,{});
    for(const [k,v] of Object.entries(mj)){
      if(n(v)!=null) keys.add("metrics."+k);
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

function renderWorkoutTable(rows){
  const wrap=el("historyTableWrap");
  if(!rows.length){wrap.innerHTML='<div class="muted" style="padding:14px">No workouts match these filters.</div>';return;}
  const head=["Date","Sport","Name","Distance","Duration","Pace","GAP","Avg HR","Max HR","Elevation","Power","Cadence","Load","VO₂ est.","Apple VO₂max","Weight"];
  const body=rows.map(a=>{
    const selected=a.id===selectedActivityId?" selected":"";
    return '<tr class="workout-table-row'+selected+'" data-activity-id="'+escapeHtml(a.id)+'">'+
      '<td>'+escapeHtml(localDate(a.start_time))+'</td><td>'+escapeHtml(a.sport_type||a.sport_category||"—")+'</td><td>'+escapeHtml(a.name||"Workout")+'</td>'+
      '<td>'+escapeHtml(fmt(distanceKm(a),2," km"))+'</td><td>'+escapeHtml(fmt(durationMin(a),0," min"))+'</td><td>'+escapeHtml(fmtPace(a.avg_pace_min_km))+'</td>'+
      '<td>'+escapeHtml(fmtPace(a.avg_gap_pace_min_km))+'</td><td>'+escapeHtml(fmt(a.avg_hr,0))+'</td><td>'+escapeHtml(fmt(a.max_hr,0))+'</td>'+
      '<td>'+escapeHtml(fmt(a.elevation_gain_m,0," m"))+'</td><td>'+escapeHtml(fmt(a.average_power,0," W"))+'</td><td>'+escapeHtml(fmt(a.cadence_spm,0))+'</td>'+
      '<td>'+escapeHtml(fmt(derivedLoad(a),1))+'</td><td>'+escapeHtml(fmt(a.own_vo2max_estimate??a.estimated_vo2max,1))+'</td>'+
      '<td><input class="inline-number manual-cell" data-field="apple_vo2max" data-id="'+escapeHtml(a.id)+'" type="number" min="15" max="90" step="0.1" value="'+escapeHtml(n(a.apple_vo2max)!=null?n(a.apple_vo2max):"")+'"></td>'+
      '<td><input class="inline-number manual-cell" data-field="body_weight_kg" data-id="'+escapeHtml(a.id)+'" type="number" min="30" max="250" step="0.1" value="'+escapeHtml(n(a.body_weight_kg)!=null?n(a.body_weight_kg):"")+'"></td></tr>';
  }).join("");
  wrap.innerHTML='<table class="workout-table"><thead><tr>'+head.map(h=>"<th>"+escapeHtml(h)+"</th>").join("")+"</tr></thead><tbody>"+body+"</tbody></table>";
  qsa("#historyTableWrap .workout-table-row").forEach(r=>r.addEventListener("click",e=>{
    if(e.target.closest("input")) return;
    showWorkout(r.dataset.activityId,"history");
  }));
  qsa("#historyTableWrap .manual-cell").forEach(inp=>{
    inp.addEventListener("click",e=>e.stopPropagation());
    inp.addEventListener("change",()=>saveManualActivityValue(inp.dataset.id,inp.dataset.field,inp.value));
  });
}

async function saveManualActivityValue(id,field,raw){
  const value=raw===""?null:n(raw);
  if(value!=null&&field==="apple_vo2max"&&(value<15||value>90)){toast("VO₂max must be between 15 and 90.");renderHistory();return;}
  if(value!=null&&field==="body_weight_kg"&&(value<30||value>250)){toast("Weight must be between 30 and 250 kg.");renderHistory();return;}
  const res=await client.from("activities").update({[field]:value}).eq("id",id).eq("user_id",userId());
  if(res.error){toast("Could not save value: "+res.error.message,5000);renderHistory();return;}
  const a=activities.find(x=>x.id===id);if(a)a[field]=value;
  toast(field==="apple_vo2max"?"Apple VO₂max saved.":"Weight saved.");
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
function buildStreamRows(stream){
  const pts=Array.isArray(stream&&stream.points)?stream.points:[];
  if(!pts.length)return [];
  const rows=pts.map((p,i)=>({
    i,elapsed_s:n(p.elapsed_s,i),distance_m:n(p.distance_calc_m)??n(p.distance_m),lat:n(p.lat),lon:n(p.lon),altitude_m:n(p.alt),
    heart_rate:n(p.hr),power_w:n(p.watts),cadence:n(p.cadence),speed_mps:n(p.speed_mps)
  }));

  for(let i=0;i<rows.length;i++){
    if(rows[i].distance_m==null) rows[i].distance_m=i?rows[i-1].distance_m:null;
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

  // Smooth barometric/GPS altitude first, then estimate grade over an ~80 m
  // centered distance window. This avoids the alternating ±grade spikes caused
  // by taking a derivative over only a few metres.
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
function derivedStreamSummary(rows,activity){
  const vo2=gradientNormalizedVo2(rows,activity),gap=gradientAdjustedPace(rows);
  const speeds=rows.map(r=>n(r.speed_kmh)).filter(Number.isFinite),powers=rows.map(r=>n(r.power_w)).filter(Number.isFinite);
  const cads=rows.map(r=>n(r.cadence)).filter(Number.isFinite),grades=rows.map(r=>n(r.grade_pct)).filter(Number.isFinite);
  return {vo2,gap,max_speed_kmh:speeds.length?Math.max(...speeds):null,max_power_w:powers.length?Math.max(...powers):null,
    max_cadence:cads.length?Math.max(...cads):null,avg_grade_pct:mean(grades),min_grade_pct:grades.length?Math.min(...grades):null,max_grade_pct:grades.length?Math.max(...grades):null};
}
async function maybePersistDerivedMetrics(activity,rows){
  const d=derivedStreamSummary(rows,activity),updates={},mj={...safeJson(activity.metrics_json,{})};
  if(d.vo2!=null&&(n(activity.own_vo2max_estimate)==null||Math.abs(n(activity.own_vo2max_estimate)-d.vo2)>.15)){
    updates.own_vo2max_estimate=Math.round(d.vo2*10)/10;updates.estimated_vo2max=Math.round(d.vo2*10)/10;
  }
  if(d.gap!=null&&(n(activity.avg_gap_pace_min_km)==null||Math.abs(n(activity.avg_gap_pace_min_km)-d.gap)>.01))updates.avg_gap_pace_min_km=d.gap;
  for(const k of ["max_speed_kmh","max_power_w","max_cadence","avg_grade_pct","min_grade_pct","max_grade_pct"])if(d[k]!=null)mj[k]=d[k];
  mj.vo2_estimate_method="ACSM running oxygen cost + grade + HR-reserve heuristic";
  updates.metrics_json=mj;
  if(Object.keys(updates).length){
    const res=await client.from("activities").update(updates).eq("id",activity.id).eq("user_id",userId());
    if(!res.error)Object.assign(activity,updates);
  }
  return d;
}

async function getWorkoutStream(id){
  if(streamCache.has(id))return streamCache.get(id);
  const res=await client.from("activity_streams").select("stream_data").eq("activity_id",id).maybeSingle();
  const stream=res.data?safeJson(res.data.stream_data,{}):{};
  streamCache.set(id,stream);return stream;
}
async function renderLatestWorkout(){
  if(!activities.length){
    el("homeWorkoutDetail").innerHTML='<div class="muted">Upload a workout to use the viewer.</div>';
    el("homeRouteMap").classList.add("hidden");el("homeStreamPlot").classList.add("hidden");return;
  }
  await showWorkout(activities[0].id,"home");
}
async function showWorkout(id,target="history") {
  if(target==="history")selectedActivityId=id;
  const a=activities.find(x=>x.id===id);if(!a)return;
  const detailId=target==="home"?"homeWorkoutDetail":"workoutDetail";
  const stream=await getWorkoutStream(id),rows=buildStreamRows(stream),derived=rows.length?await maybePersistDerivedMetrics(a,rows):{};
  const items=[
    ["Date",localDate(a.start_time)],["Sport",a.sport_type||a.sport_category],["Distance",fmt(distanceKm(a),2," km")],["Duration",fmt(durationMin(a),0," min")],
    ["Average HR",fmt(a.avg_hr,0," bpm")],["Max HR",fmt(a.max_hr,0," bpm")],["Pace",fmtPace(a.avg_pace_min_km)],["Gradient-adjusted pace",fmtPace(a.avg_gap_pace_min_km)],
    ["Elevation",fmt(a.elevation_gain_m,0," m")],["Training load",fmt(derivedLoad(a),1)],["Average power",fmt(a.average_power,0," W")],["Normalized power",fmt(a.normalized_power,0," W")],
    ["Cadence",fmt(a.cadence_spm,0)],["HR drift",fmt(a.hr_efficiency_drift_pct,1,"%")],["Gradient-normalized VO₂max",fmt(a.own_vo2max_estimate??derived.vo2,1)],["Apple VO₂max",fmt(a.apple_vo2max,1)]
  ];
  el(detailId).innerHTML='<h3>'+escapeHtml(a.name||"Workout")+'</h3><div class="detail-grid">'+items.map(i=>
    '<div class="detail-item"><div class="detail-key">'+escapeHtml(i[0])+'</div><div class="detail-value">'+escapeHtml(i[1])+'</div></div>'
  ).join("")+'</div><div class="vo2-method">VO₂max estimate is a heuristic for running: pointwise ACSM oxygen cost is grade-corrected and scaled by heart-rate reserve. It is useful for within-person trends, not a substitute for a laboratory VO₂max test.</div>';
  renderStreamViewer(rows,target);
  if(target==="history")renderWorkoutTable(filteredActivities());
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
    const plot=el(state.plotId);
    if(yv!=null) Plotly.restyle(plot,{x:[[xv]],y:[[yv]]},[2]);
    else Plotly.restyle(plot,{x:[[]],y:[[]]},[2]);
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
function renderStreamViewer(rows,target){
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
    else range=stats.lo.toFixed(1)+"–"+stats.hi.toFixed(1)+" "+def.unit+" (5th–95th percentile)";
    el(legendId).innerHTML='<span class="legend-label">'+escapeHtml(def.label)+'</span><span class="legend-gradient" style="background:'+def.gradient+'"></span><span>'+escapeHtml(range)+'</span>';
  }else el(legendId).classList.add("hidden");

  const old=viewerStates[target];
  if(old&&old.map&&routeMaps[target]){routeMaps[target].remove();routeMaps[target]=null;}
  const state={rows,key,def,stats,xKey,mapId,plotId,legendId,readoutId,map:null,cursorMarker:null,plotReady:false};
  viewerStates[target]=state;

  const gps=rows.filter(r=>r.lat!=null&&r.lon!=null);
  if(gps.length>=2){
    el(mapId).classList.remove("hidden");
    const map=L.map(mapId,{renderer:L.canvas({padding:.5})});routeMaps[target]=map;state.map=map;
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:19,attribution:"&copy; OpenStreetMap"}).addTo(map);
    for(let i=1;i<gps.length;i++){
      const c=colorForStreamMetric(key,gps[i][key],stats);
      L.polyline([[gps[i-1].lat,gps[i-1].lon],[gps[i].lat,gps[i].lon]],{weight:5,opacity:.92,color:c,interactive:false}).addTo(map);
    }
    L.circleMarker([gps[0].lat,gps[0].lon],{radius:5,weight:2,color:"#166534",fillColor:"#ffffff",fillOpacity:1}).bindTooltip("Start").addTo(map);
    L.circleMarker([gps[gps.length-1].lat,gps[gps.length-1].lon],{radius:5,weight:2,color:"#991b1b",fillColor:"#ffffff",fillOpacity:1}).bindTooltip("Finish").addTo(map);
    map.fitBounds(gps.map(r=>[r.lat,r.lon]),{padding:[18,18]});
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
  if(validCount>=2){
    el(plotId).classList.remove("hidden");
    const xs=plotRows.map(r=>n(r[xKey]));
    const ys=plotRows.map(r=>n(r[key]));
    const custom=plotRows.map(r=>[r.time_min,r.distance_km,r.altitude_m,r.heart_rate,r.speed_kmh,r.power_w,r.cadence,r.grade_pct,r.i]);
    const colors=plotRows.map(r=>colorForStreamMetric(key,r[key],stats));
    const traces=[
      {x:xs,y:ys,mode:"lines",type:"scattergl",connectgaps:false,showlegend:false,hoverinfo:"skip",line:{color:"rgba(83,97,113,.28)",width:1.2}},
      {x:xs,y:ys,mode:"markers",type:"scattergl",connectgaps:false,showlegend:false,marker:{size:5,color:colors},customdata:custom,
       hovertemplate:"Time %{customdata[0]:.1f} min · Distance %{customdata[1]:.2f} km<br>"+escapeHtml(def.label)+": %{y:.2f} "+escapeHtml(def.unit)+"<br>Altitude %{customdata[2]:.0f} m · HR %{customdata[3]:.0f}<br>Speed %{customdata[4]:.1f} km/h · Power %{customdata[5]:.0f} W · Cadence %{customdata[6]:.0f} · Grade %{customdata[7]:.1f}%<extra></extra>"},
      {x:[],y:[],mode:"markers",type:"scatter",showlegend:false,hoverinfo:"skip",marker:{size:12,color:"#111827",line:{width:3,color:"#ffffff"}}}
    ];
    const firstValid=plotRows.find(r=>n(r[xKey])!=null&&n(r[key])!=null);
    const firstX=firstValid?n(firstValid[xKey]):0;
    Plotly.react(plotId,traces,{margin:{l:60,r:20,t:20,b:45},hovermode:"closest",dragmode:false,
      xaxis:{title:xKey==="distance_km"?"Distance (km)":"Time (min)"},
      yaxis:{title:def.label+" ("+def.unit+")",autorange:key==="pace_min_km"?"reversed":true},
      shapes:[{type:"line",x0:firstX,x1:firstX,yref:"paper",y0:0,y1:1,line:{color:"rgba(17,24,39,.35)",width:1,dash:"dot"}}],
      paper_bgcolor:"transparent",plot_bgcolor:"transparent",showlegend:false},
      {responsive:true,displaylogo:false,scrollZoom:false});
    state.plotReady=true;
    bindPlotCursor(target);
    updateViewerCursor(target,firstValid);
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
  const evidence=[];
  if(easyPct!=null) evidence.push("Easy Z1–Z2 share over 90 days: "+easyPct.toFixed(1)+"%.");
  if(hardPct!=null) evidence.push("Hard Z4–Z5 share over 90 days: "+hardPct.toFixed(1)+"%.");
  evidence.push("7-day load / 28-day weekly baseline: "+c.loadRatio.toFixed(2)+"×.");
  evidence.push("Recent weekly-equivalent running volume: "+c.runKm28Weekly.toFixed(1)+" km.");
  if(easyRuns.length) evidence.push("Comparable easy runs used for pace trend: "+easyRuns.length+".");
  return {trend,trendNote,baseStatus,loadStatus,volumeStatus,durability,durabilityNote,easyPct,hardPct,z,c,easyRuns,evidence,slope,generated_at:new Date().toISOString()};
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
    ["Run volume",d.volumeStatus,d.c.runKm28Weekly.toFixed(1)+" km/week recent baseline"]
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
}

async function askAiCoach() {
  if (!client) return;
  const d=analysisData();
  el("aiCoachOutput").textContent="Analyzing…";
  el("aiCoachButton").disabled=true;
  const compact=activities.slice(0,60).map(a=>({
    start_time:a.start_time,sport:a.sport_category||a.sport_type,distance_km:distanceKm(a),duration_min:durationMin(a),
    avg_hr:n(a.avg_hr),pace_min_km:n(a.avg_pace_min_km),elevation_gain_m:n(a.elevation_gain_m),
    easy_zone_fraction:n(a.easy_zone_fraction),hard_zone_fraction:n(a.hard_zone_fraction),
    hr_drift_pct:n(a.hr_efficiency_drift_pct),apple_vo2max:n(a.apple_vo2max),load:derivedLoad(a)
  }));
  const result=await client.functions.invoke(cfg.COACH_FUNCTION||"coach",{body:{analysis:d,activities:compact,profile:{
    profile_vo2max:n(profile&&profile.profile_vo2max),goals:profile&&profile.goals,training_preferences:profile&&profile.training_preferences
  }}});
  el("aiCoachButton").disabled=false;
  if(result.error){el("aiCoachOutput").textContent="AI coach failed: "+result.error.message;return;}
  const text=(result.data&&result.data.text)||"No response returned.";
  el("aiCoachOutput").innerHTML=window.marked?marked.parse(text):escapeHtml(text);
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
  if(!files.length){el("uploadStatus").textContent="Choose at least one TCX file.";return;}
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
  await loadActivities(); streamCache.clear(); renderAll();
  el("uploadStatus").textContent="Imported "+ok+(overwritten?" · overwritten "+overwritten:"")+(skipped?" · skipped "+skipped:"")+".";
  if(ok||overwritten)setTimeout(()=>el("uploadDialog").close(),900);
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
async function parseTcx(file) {
  const xml=new DOMParser().parseFromString(await file.text(),"application/xml");
  if(xml.querySelector("parsererror")) throw new Error("Invalid TCX XML.");
  const activity=Array.from(xml.getElementsByTagName("*")).find(x=>x.localName==="Activity");
  if(!activity)throw new Error("No Activity element found.");
  const sport=activity.getAttribute("Sport")||"Workout";
  const idText=textByLocal(activity,"Id");
  const tpNodes=Array.from(xml.getElementsByTagName("*")).filter(x=>x.localName==="Trackpoint");
  const points=[];
  for(const tp of tpNodes){
    const time=textByLocal(tp,"Time");
    const lat=n(textByLocal(tp,"LatitudeDegrees"));
    const lon=n(textByLocal(tp,"LongitudeDegrees"));
    const alt=n(textByLocal(tp,"AltitudeMeters"));
    const dist=n(textByLocal(tp,"DistanceMeters"));
    const hrNode=firstByLocal(tp,"HeartRateBpm");
    const hr=hrNode?n(textByLocal(hrNode,"Value")):null;
    const cad=n(textByLocal(tp,"Cadence")) ?? n(textByLocal(tp,"RunCadence"));
    const watts=n(textByLocal(tp,"Watts"));
    const speed=n(textByLocal(tp,"Speed"));
    points.push({time,lat,lon,alt,distance_m:dist,hr,cadence:cad,watts,speed_mps:speed});
  }
  if(!points.length)throw new Error("TCX contains no trackpoints.");
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
  const streamDerived=derivedStreamSummary(derivedRows,{sport_type:sport,sport_category:normalizeSport(sport),max_hr:maxHr});
  const streamPoints=downsamplePoints(points,4000);
  const startIso=firstTime.toISOString();
  const dedupe=[startIso.slice(0,19),normalizeSport(sport),Math.round(duration/10),Math.round(distance/10)].join("|");
  return {
    activity:{
      source:"tcx_upload",name:file.name.replace(/\.tcx$/i,""),sport_type:sport,sport_category:normalizeSport(sport),
      start_time:startIso,original_start_time:idText||startIso,duration_s:duration,moving_time_s:duration,distance_m:distance,
      elevation_gain_m:elevation,avg_hr:avgHr,max_hr:maxHr,avg_pace_min_km:pace,avg_gap_pace_min_km:streamDerived.gap,
      training_load_score:load,easy_zone_fraction:easy,hard_zone_fraction:hard,z1_s:zones[0],z2_s:zones[1],z3_s:zones[2],z4_s:zones[3],z5_s:zones[4],
      own_vo2max_estimate:streamDerived.vo2,estimated_vo2max:streamDerived.vo2,average_power:avgPower,cadence_spm:cadence,
      hr_efficiency_drift_pct:drift,file_sha256:fileHash,dedupe_key:dedupe,
      metrics_json:{parser:"workoutbuddy-web-tcx-v2",trackpoints:points.length,stored_trackpoints:streamPoints.length,
        max_speed_kmh:streamDerived.max_speed_kmh,max_power_w:streamDerived.max_power_w,max_cadence:streamDerived.max_cadence,
        avg_grade_pct:streamDerived.avg_grade_pct,min_grade_pct:streamDerived.min_grade_pct,max_grade_pct:streamDerived.max_grade_pct,
        vo2_estimate_method:"ACSM running oxygen cost + grade + HR-reserve heuristic"}
    },
    stream:{points:streamPoints}
  };
}

async function importTcx(file) {
  const parsed=await parseTcx(file);
  const fields="id,name,start_time,raw_file_path,file_sha256,dedupe_key,apple_vo2max,body_weight_kg,metrics_json";
  const byHash=await client.from("activities").select(fields).eq("user_id",userId()).eq("file_sha256",parsed.activity.file_sha256).limit(1);
  if(byHash.error)throw byHash.error;
  let existing=byHash.data&&byHash.data[0],matchType="identical file hash";
  if(!existing){
    const byKey=await client.from("activities").select(fields).eq("user_id",userId()).eq("dedupe_key",parsed.activity.dedupe_key).limit(1);
    if(byKey.error)throw byKey.error;
    existing=byKey.data&&byKey.data[0];matchType="same start time / sport / duration / distance";
  }

  const year=new Date(parsed.activity.start_time).getFullYear();
  const safeName=file.name.replace(/[^A-Za-z0-9_.-]/g,"_");
  const newPath=userId()+"/"+year+"/"+parsed.activity.start_time.replace(/[:.]/g,"-")+"_"+safeName;

  if(existing){
    const decision=await askDuplicateDecision(file,existing,matchType);
    if(decision!=="overwrite")return "skipped";
    const path=existing.raw_file_path||newPath;
    const up=await client.storage.from("workout-files").upload(path,file,{upsert:true,contentType:file.type||"application/xml"});
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

  const up=await client.storage.from("workout-files").upload(newPath,file,{upsert:false,contentType:file.type||"application/xml"});
  if(up.error)throw up.error;
  parsed.activity.user_id=userId();parsed.activity.raw_file_path=newPath;
  const ins=await client.from("activities").insert(parsed.activity).select("id").single();
  if(ins.error)throw ins.error;
  const st=await client.from("activity_streams").insert({activity_id:ins.data.id,user_id:userId(),stream_data:parsed.stream});
  if(st.error)throw st.error;
  return "ok";
}

function openSettings(){
  const p=profile||defaultProfile(),g=p.goals||{},t=p.training_preferences||{},z=p.hr_zones||{};
  el("profileName").value=p.display_name||"";el("profileBirthdate").value=p.birthdate||"";el("profileHeight").value=n(p.height_cm,178);
  el("profileWeight").value=n(p.current_weight_kg,75);el("profileVo2").value=n(p.profile_vo2max,45);
  el("goalType").value=g.primary_goal||"general";el("goalDate").value=g.goal_date||"";
  el("weeklyRunDays").value=n(t.weekly_run_days,3);el("weeklyStrengthDays").value=n(t.strength_sessions,2);el("trainingAggressiveness").value=String(n(t.training_aggressiveness,3));el("longRunDay").value=String(t.long_run_day==null?6:t.long_run_day);
  el("z1Max").value=n(z.z1_max,130);el("z2Max").value=n(z.z2_max,150);el("z3Max").value=n(z.z3_max,165);el("z4Max").value=n(z.z4_max,178);el("z5Max").value=n(z.z5_max,220);
  el("settingsStatus").textContent="";el("settingsDialog").showModal();
}
async function saveSettings(event){
  event.preventDefault();
  const row={user_id:userId(),display_name:el("profileName").value.trim(),birthdate:el("profileBirthdate").value||null,
    height_cm:n(el("profileHeight").value),current_weight_kg:n(el("profileWeight").value),profile_vo2max:n(el("profileVo2").value),
    goals:{primary_goal:el("goalType").value,goal_date:el("goalDate").value||null},
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