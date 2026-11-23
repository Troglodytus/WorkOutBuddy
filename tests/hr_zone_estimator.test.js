/* Run with: node --test tests/hr_zone_estimator.test.js */
const test=require("node:test");
const assert=require("node:assert/strict");
const {estimate,summarizeStream}=require("../docs/hr_zone_estimator.js");

const asOf=new Date("2026-11-23T12:00:00.000Z");
function workout(id,sport,minutes,hr,drift=-2,dateOffset=1){
  const start_time=new Date(asOf.getTime()-dateOffset*86400000).toISOString();
  const points=Array.from({length:minutes*60+1},(_,i)=>({elapsed_s:i,hr:hr+1.4*Math.sin(i/60)}));
  return {
    activity:{id:String(id),start_time,sport_category:sport,gap_hr_efficiency_drift_pct:drift},
    stream:{points}
  };
}
function runModel(rows,profile={birthdate:"1990-01-01"}){
  return estimate(rows.map(x=>x.activity),new Map(rows.map(x=>[x.activity.id,x.stream])),profile,asOf);
}
test("insufficient data produces no misleading zone boundaries",()=>{
  const r=runModel([workout(1,"run",30,130)]);
  assert.equal(r.available,false);
  assert.equal(r.totalWorkouts,1);
});
test("only the last 60 days are included",()=>{
  const r=runModel([workout(1,"run",55,130,-1,1),workout(2,"run",55,145,-1,2),workout(3,"run",55,170,-1,65)]);
  assert.equal(r.totalWorkouts,2);
  assert.equal(r.streamWorkouts,2);
});
test("the five boundaries increase monotonically and include no gaps",()=>{
  const rows=[
    ...Array.from({length:5},(_,i)=>workout(i,"run",55,138+i,-2,i+1)),
    ...Array.from({length:4},(_,i)=>workout(i+5,"run",42,166+i,-3,i+6)),
    ...Array.from({length:3},(_,i)=>workout(i+9,"bike",40,122,-3,i+11))
  ];
  const r=runModel(rows);
  assert.equal(r.available,true);
  assert.equal(r.basisSport,"run");
  assert.equal(r.runWorkouts,9);
  assert.equal(r.bikeWorkouts,3);
  assert.equal(r.zones.length,5);
  assert.equal(r.zones[0].min,null);
  for(let i=1;i<5;i++){
    assert.equal(r.zones[i].min,r.zones[i-1].max+1);
    assert.ok(r.zones[i].max>r.zones[i-1].max);
  }
  assert.equal(r.zones[4].max,r.estimatedHrMax);
  assert.ok(r.estimatedHrMax>r.observedSustainedPeak);
});
test("time-weighting ignores large data gaps and implausible spikes",()=>{
  const stream={points:[
    {elapsed_s:0,hr:120},{elapsed_s:10,hr:120},
    {elapsed_s:20,hr:121},{elapsed_s:21,hr:220},
    {elapsed_s:200,hr:125},{elapsed_s:205,hr:125}
  ]};
  const r=summarizeStream(stream);
  assert.equal(r,null); // only 25 valid seconds
});
test("missing streams are counted instead of silently replaced by average HR",()=>{
  const rows=[workout(1,"run",50,140),workout(2,"run",50,155),workout(3,"bike",60,120)];
  const r=estimate(rows.map(x=>x.activity),new Map(rows.slice(0,2).map(x=>[x.activity.id,x.stream])),{},asOf);
  assert.equal(r.totalWorkouts,3);
  assert.equal(r.streamWorkouts,2);
  assert.equal(r.missingStreams,1);
  assert.equal(r.available,true);
});

test("HR distribution conserves observed recording time",()=>{
  const rows=[workout(20,"run",48,125),workout(21,"run",48,160)];
  const estimateResult=runModel(rows);
  assert.equal(estimateResult.available,true);
  const histogram=estimateResult.distributions.all;
  const expectedHours=estimateResult.hrHours;
  const rawMinutes=histogram.minutes.reduce((a,b)=>a+b,0);
  assert.ok(Math.abs(rawMinutes/60-expectedHours)<.08);
  assert.equal(histogram.workoutCount,2);
  assert.equal(histogram.bpm.length,histogram.minutes.length);
  assert.equal(histogram.bpm.length,histogram.smoothedMinutes.length);
});
test("a clear between-peak HR frequency dip is descriptive and optional",()=>{
  const rows=[
    workout(31,"run",50,125),workout(32,"run",50,126),
    workout(33,"run",50,166),workout(34,"run",50,167)
  ];
  const result=runModel(rows);
  assert.equal(result.available,true);
  const distribution=result.distributions.basis;
  assert.ok(distribution.dipBpm>130 && distribution.dipBpm<165);
  assert.ok(distribution.dipRelativeDepth>=35);
  const steady=runModel([workout(41,"run",48,140),workout(42,"run",48,141)]);
  assert.equal(steady.distributions.basis.dipBpm,null);
});
test("all-sports plot includes other workouts without altering run-led thresholds",()=>{
  const rows=[
    workout(51,"run",47,145),workout(52,"run",48,169),
    workout(53,"bike",55,115)
  ];
  const result=runModel(rows);
  assert.equal(result.basisSport,"run");
  assert.equal(result.distributions.basis.workoutCount,2);
  assert.equal(result.distributions.all.workoutCount,3);
  assert.ok(result.distributions.all.recordedHours>result.distributions.basis.recordedHours);
});
