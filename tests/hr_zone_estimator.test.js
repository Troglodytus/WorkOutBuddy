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
