/* WorkOutBuddy: provisional 60-day heart-rate zone estimation.
   Activity streams and profile data stay in the authenticated browser.
   Estimates are training heuristics, NOT measured LT1/LT2 or medical advice. */
(function (root) {
  "use strict";

  const WINDOW_DAYS = 60;
  const BIN_SECONDS = 30;
  const DAY_MS = 86400000;

  function num(x) {
    if (x === "" || x === null || x === undefined || typeof x === "boolean") return null;
    const v = Number(x);
    return Number.isFinite(v) ? v : null;
  }
  function clamp(x, lo, hi) { return Math.max(lo, Math.min(hi, x)); }
  function mean(xs) { return xs.length ? xs.reduce((s,x) => s+x,0)/xs.length : null; }
  function sportOf(a) {
    const s = String(a.sport_category || a.sport_type || "").toLowerCase();
    if (/run|jog|trail/.test(s)) return "run";
    if (/bike|cycl|ride|ergo/.test(s)) return "bike";
    return "other";
  }
  function ageAt(profile, timestamp) {
    const raw = profile && profile.birthdate;
    if (!raw || !/^\d{4}-\d\d-\d\d/.test(String(raw))) return null;
    const birth = new Date(String(raw).slice(0,10) + "T12:00:00Z");
    if (!Number.isFinite(birth.getTime())) return null;
    const age = (timestamp-birth.getTime())/(365.2425*DAY_MS);
    return age >= 16 && age <= 100 ? age : null;
  }
  function jsonObject(value) {
    if (value && typeof value === "object") return value;
    try { return JSON.parse(value) || {}; } catch (_) { return {}; }
  }
  function driftOf(a) {
    const m = jsonObject(a.metrics_json);
    return num(a.gap_hr_efficiency_drift_pct) ??
      num(m.gap_hr_efficiency_drift_pct) ??
      num(a.hr_efficiency_drift_pct) ??
      num(m.hr_efficiency_drift_pct);
  }
  function rollingMaxHr(bins, length, minCoverage) {
    if (bins.length < length) return null;
    let secs = 0, sumHr = 0, best = null;
    for (let i = 0; i < bins.length; i++) {
      secs += bins[i].secs;
      sumHr += bins[i].hrSeconds;
      if (i >= length) {
        secs -= bins[i-length].secs;
        sumHr -= bins[i-length].hrSeconds;
      }
      if (i >= length-1 && secs >= length*BIN_SECONDS*minCoverage) {
        const v = sumHr/secs;
        if (best === null || v > best) best = v;
      }
    }
    return best;
  }

  function summarizeStream(stream) {
    const points = jsonObject(stream).points;
    if (!Array.isArray(points) || points.length < 2) return null;
    const bins = [];
    const hrSecondsPerBpm = new Array(226).fill(0);
    // Use elapsed-time weights, not the number of samples, so FIT and TCX
    // sampling rates do not change the estimate. Do not bridge data gaps.
    for (let i=1; i<points.length; i++) {
      const left=points[i-1], right=points[i];
      const t0=num(left.elapsed_s), t1=num(right.elapsed_s);
      const h0=num(left.hr ?? left.heart_rate), h1=num(right.hr ?? right.heart_rate);
      if (t0===null || t1===null || h0===null || h1===null) continue;
      const dt=t1-t0;
      if (t0<0 || t1>48*3600 || dt<=0 || dt>30 || h0<40 || h1<40 || h0>225 || h1>225) continue;
      if (Math.abs(h1-h0)>Math.max(15,4*dt)) continue; // optical-sensor spikes
      const hr=(h0+h1)/2;
      hrSecondsPerBpm[Math.round(hr)] += dt;
      let cursor=t0;
      while(cursor<t1-0.000001) {
        const b=Math.floor(cursor/BIN_SECONDS);
        const stop=Math.min(t1,(b+1)*BIN_SECONDS);
        const seconds=stop-cursor;
        if (!bins[b]) bins[b]={secs:0,hrSeconds:0};
        bins[b].secs+=seconds;
        bins[b].hrSeconds+=seconds*hr;
        cursor=stop;
      }
    }
    const length=bins.length;
    if (!length) return null;
    for (let i=0;i<length;i++) if (!bins[i]) bins[i]={secs:0,hrSeconds:0};
    const seconds=bins.reduce((s,b)=>s+b.secs,0);
    if (seconds<60) return null;
    const weightedHr=bins.reduce((s,b)=>s+b.hrSeconds,0)/seconds;
    return {
      seconds, avgHr:weightedHr, hrSecondsPerBpm,
      peak1:rollingMaxHr(bins,2,.80),
      peak20:rollingMaxHr(bins,40,.82),
      peak30:rollingMaxHr(bins,60,.82)
    };
  }


  function heartRateDistribution(records) {
    const totals=new Array(226).fill(0);
    for(const record of records){
      const perBpm=record.hrSecondsPerBpm || [];
      for(let bpm=40;bpm<=225;bpm++)totals[bpm]+=perBpm[bpm]||0;
    }
    // A small Gaussian kernel makes low-sample-rate and integer-bpm
    // quantization less distracting. The underlying bars remain unsmoothed.
    const smoothed=totals.map((_,bpm)=>{
      let weighted=0,normalization=0;
      for(let offset=-8;offset<=8;offset++){
        const idx=bpm+offset;
        if(idx<40||idx>225)continue;
        const weight=Math.exp(-.5*(offset/2.2)**2);
        normalization+=weight;weighted+=weight*totals[idx];
      }
      return normalization ? weighted/normalization : 0;
    });
    const filled=[];
    for(let bpm=40;bpm<=225;bpm++)if(totals[bpm]>0)filled.push(bpm);
    if(!filled.length)return null;
    const low=Math.max(40,filled[0]-8), high=Math.min(225,filled[filled.length-1]+8);
    const bpm=[],minutes=[],smoothedMinutes=[];
    for(let i=low;i<=high;i++){
      bpm.push(i);
      minutes.push(Math.round(totals[i]/60*100)/100);
      smoothedMinutes.push(Math.round(smoothed[i]/60*100)/100);
    }
    const max=Math.max(...smoothedMinutes);
    // Two well-separated peaks with a clear intervening valley can show a
    // training-intensity distribution dip. It is NOT a physiological breakpoint.
    const peaks=[];
    for(let i=4;i<smoothedMinutes.length-4;i++){
      const value=smoothedMinutes[i];
      if(value >= max*.27 && value>=smoothedMinutes[i-1] && value>smoothedMinutes[i+1])
        peaks.push({i,bpm:bpm[i],value});
    }
    peaks.sort((a,b)=>b.value-a.value);
    let dipBpm=null,dipRelativeDepth=null;
    for(let i=0;i<peaks.length&&dipBpm===null;i++){
      for(let j=i+1;j<peaks.length;j++){
        const p=peaks[i],q=peaks[j],separation=Math.abs(p.i-q.i);
        if(separation<14||separation>65)continue;
        const a=Math.min(p.i,q.i),b=Math.max(p.i,q.i);
        let valley=a+4;
        for(let k=a+4;k<=b-4;k++)if(smoothedMinutes[k]<smoothedMinutes[valley])valley=k;
        const depth=1-smoothedMinutes[valley]/Math.min(p.value,q.value);
        if(depth>=.35){
          dipBpm=bpm[valley];dipRelativeDepth=Math.round(depth*100);
          break;
        }
      }
    }
    return {
      bpm,minutes,smoothedMinutes,dipBpm,dipRelativeDepth,
      workoutCount:records.length,
      recordedHours:Math.round(records.reduce((s,r)=>s+r.seconds,0)/360)/10
    };
  }

  function estimate(activities, streamsByActivity, profile, asOf) {
    const end = asOf == null ? Date.now() : new Date(asOf).getTime();
    if (!Number.isFinite(end)) throw new Error("Invalid estimation date");
    const recent=(activities || []).filter(a => {
      const t=new Date(a.start_time).getTime();
      return Number.isFinite(t) && t <= end && t >= end-WINDOW_DAYS*DAY_MS;
    });
    const records=[];
    for (const activity of recent) {
      const stream = streamsByActivity instanceof Map
        ? streamsByActivity.get(activity.id)
        : (streamsByActivity || {})[activity.id];
      const data=summarizeStream(stream);
      if (data) records.push({activity, sport:sportOf(activity), ...data, drift:driftOf(activity)});
    }
    const seconds=records.reduce((s,r)=>s+r.seconds,0);
    const resultBase={
      windowDays:WINDOW_DAYS,totalWorkouts:recent.length,
      streamWorkouts:records.length,missingStreams:recent.length-records.length,
      hrHours:Math.round(seconds/360)/10,
      runWorkouts:records.filter(r=>r.sport==="run").length,
      bikeWorkouts:records.filter(r=>r.sport==="bike").length
    };
    if (records.length<2 || seconds<2700) return {
      ...resultBase, available:false,
      reason:"At least two workouts and 45 minutes of valid HR stream data are needed."
    };

    const sustainedPeaks=records.map(r=>r.peak1).filter(v=>v!==null);
    if (!sustainedPeaks.length) return {
      ...resultBase,available:false,
      reason:"No continuous 1-minute heart-rate segments were found."
    };
    const observedPeak=Math.max(...sustainedPeaks);
    const age=ageAt(profile || {}, end);
    const agePrediction=age!==null ? Math.round(208-.7*age) : null;
    // The highest observed 1-minute HR is a LOWER BOUND on true HRmax.
    // A demographic prior, when available, helps avoid calling easy-session
    // peak HR "maximum". Its individual error is substantial.
    const hrMax=Math.round(clamp(
      Math.max(observedPeak+6, agePrediction===null ? observedPeak+9 : agePrediction),
      145,215
    ));
    if (observedPeak<115) return {
      ...resultBase,available:false,
      reason:"Recent workouts contain too little elevated HR to locate useful intensity boundaries."
    };

    // Prefer running for the aerobic/threshold proxies. Cycling and other
    // sports still contribute to observed peak HR and total data coverage.
    const runs=records.filter(r=>r.sport==="run");
    const bikes=records.filter(r=>r.sport==="bike");
    const basis=runs.length>=2 ? runs : bikes.length>=2 ? bikes : records;
    const basisSport=basis===runs ? "run" : basis===bikes ? "bike" : "mixed";
    const hard=basis.filter(r=>r.peak20!==null && r.peak20>=.82*hrMax)
      .sort((a,b)=>b.peak20-a.peak20).slice(0,3);
    const hardPeak=mean(hard.map(r=>r.peak20));
    const lt2Prior=.90*hrMax;
    const hardWeight=hard.length>=3?.72:hard.length===2?.60:hard.length===1?.40:0;
    const lt2=Math.round(clamp(
      (hardPeak===null ? lt2Prior : hardPeak*hardWeight+lt2Prior*(1-hardWeight)),
      .83*hrMax,.95*hrMax
    ));

    // Stable 30+ minute workouts with small aerobic decoupling provide weak
    // supporting evidence for LT1. Stability alone DOES NOT prove "below LT1".
    const easy=basis.filter(r =>
      r.peak30!==null && r.drift!==null &&
      r.drift>=-5 && r.drift<=5 &&
      r.avgHr>=.60*hrMax && r.avgHr<=.85*hrMax
    ).sort((a,b)=>b.avgHr-a.avgHr).slice(0,4);
    const easyCandidate=easy.length ? mean(easy.map(r=>r.avgHr)) : null;
    const lt1Prior=.77*hrMax;
    const easyWeight=easy.length>=3?.40:easy.length===2?.30:easy.length===1?.20:0;
    const lt1=Math.round(clamp(
      easyCandidate===null ? lt1Prior :
        easyWeight*easyCandidate+(1-easyWeight)*lt1Prior,
      .69*hrMax,Math.min(.84*hrMax,lt2-15)
    ));

    const z1=Math.round(.86*lt1);
    const z3=Math.round(lt1+.52*(lt2-lt1));
    const upper=[z1,lt1,z3,lt2,hrMax];
    // These are indicative bpm ranges, not independently determined
    // respiratory/lactate thresholds.
    const zones=upper.map((max,i)=>({
      zone:i+1,min:i===0?null:upper[i-1]+1,max,
      label:["Recovery","Aerobic endurance","Tempo","Threshold","High intensity"][i]
    }));
    const confidence=records.length>=8 && basis.length>=4 &&
      hard.length>=2 && easy.length>=2 && hrMax-observedPeak<=12
      ? "moderate" : "low";
    return {
      ...resultBase,available:true,
      zones,estimatedHrMax:hrMax,observedSustainedPeak:Math.round(observedPeak),
      aerobicThresholdProxy:lt1,thresholdProxy:lt2,
      hardEvidence:hard.length,aerobicEvidence:easy.length,
      ageBasedPriorUsed:agePrediction!==null,
      basisSport,confidence,
      distributions:{basis:heartRateDistribution(basis),all:heartRateDistribution(records)},
      // Interpret as approximate sensitivity, NEVER formal confidence limits.
      uncertaintyBpm:{
        aerobic:easy.length>=2?8:12,
        threshold:hard.length>=2?8:13,
        maximum:hrMax-observedPeak<=9?12:18
      },
      limitation:"Training-data estimates only. No workout can establish true HRmax, LT1 or LT2 without a validated maximal/threshold test; heat, terrain, fatigue and sensor errors affect HR."
    };
  }
  const api={estimate,summarizeStream};
  if (typeof module!=="undefined" && module.exports) module.exports=api;
  if (root) root.WorkoutBuddyHrZones=api;
})(typeof window!=="undefined" ? window : null);
