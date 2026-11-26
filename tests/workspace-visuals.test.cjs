const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const {readFileSync}=require('node:fs');
const context=vm.createContext({});
vm.runInContext(readFileSync('frontend/workspace-visuals.js','utf8'),context);
const visuals=context.CoRCAVisuals;
const incident=(changes={})=>({case_id:'one',fault_type:'DB',outcome:'fault',truth:['db'],predicted:['db'],status:'diagnosed',seconds:null,...changes});

test('rank includes every confirmed cause and treats unavailable results as misses',()=>{
  assert.equal(visuals.rank(incident()),1);
  assert.equal(visuals.rank(incident({truth:['db','cache'],predicted:['db','other','cache']})),3);
  assert.equal(visuals.rank(incident({truth:['db','missing']})),Infinity);
  assert.equal(visuals.rank(incident({status:'failed'})),Infinity);
  assert.equal(visuals.rank(incident({outcome:'healthy',truth:[]})),null);
});
test('statistics keep healthy cases and unrecorded timings out of fault accuracy and duration',()=>{
  const result=visuals.stats([incident({seconds:10}),incident({predicted:[],status:'failed'}),incident({outcome:'healthy',truth:[],seconds:20})]);
  assert.equal(result.n,2);assert.equal(result.top1,1);assert.equal(result.top3,1);
  assert.equal(result.miss,1);assert.equal(result.bins[4],1);
  assert.equal(result.times.length,2);assert.equal(result.median,15);assert.equal(result.p90,19);
  assert.equal(visuals.stats([]).median,null);
});
test('comparisons require frozen case labels, dataset, evidence, and scope',()=>{
  const run={dataset_id:'v1',scope:'baro tau6',evidence_kind:'controlled_drill',cases:[incident()]};
  assert.equal(visuals.comparable(run,{...run,cases:[incident({predicted:['wrong']})]}),true);
  for(const change of [{dataset_id:'v2'},{scope:'circa'},{evidence_kind:'synthetic'},{cases:[incident({truth:['other']})]},{cases:[]}])assert.equal(visuals.comparable(run,{...run,...change}),false);
  assert.match(visuals.interval(0,0),/Not measured/);
  assert.match(visuals.interval(1,1),/21–100%/);
});

test('cause-set metrics penalize extra causes, retain failures, and distinguish partial completion',()=>{
  const s=visuals.stats([incident({truth:['db','cache'],predicted:['db','wrong'],status:'partial_unexplained'}),incident({status:'failed'}),incident(),incident({outcome:'healthy',truth:[],predicted:[],status:'no_significant_anomaly'})]);
  assert.equal(s.n,3);
  assert.equal(s.setTotals.precision,1.5);assert.equal(s.setTotals.recall,1.5);assert.equal(s.setTotals.f1,1.5);
  assert.equal(s.setTotals.complete,1);
  const partial=visuals.stats([incident({status:'partial_timeout'})]);
  assert.equal(partial.setTotals.f1,1);assert.equal(partial.setTotals.complete,0);
});

test('service traces use configured paths and stop on cycles',()=>{
  const edges=[['checkout','cart'],['cart','db'],['db','cart']];
  assert.deepEqual(Array.from(visuals.dependencyPath(edges,'checkout','db')),['checkout','cart','db']);
  assert.deepEqual(Array.from(visuals.dependencyPath(edges,'checkout','unknown')),[]);
  assert.match(visuals.metricSummary({metric:'latency',points:[[1,null],[2,.31],[3,2.4]],baseline:[.29,.33]}),/2.4.*0.29–0.33/);
  assert.equal(visuals.metricSummary(null),'Metric preview not captured');
});

test('target markers only use explicit bounded check declarations',()=>{
  const targets=visuals.declaredTargets([{name:'Top-3 hit rate at least 85%'},{name:'Median time to result at most 120 s'},{name:'Top-1 hit rate >= 60%'}]);
  assert.equal(targets.top3,.85);assert.equal(targets.seconds,120);assert.equal(targets.top1,.6);
  assert.deepEqual(Object.keys(visuals.declaredTargets([{name:'Top-3 hit rate at least 185%'},{name:'Median time to result at most 0 s'},{name:'Median time to result at most 120 ms'},{name:'Top-3 hit rate at least 85% except case-1'}])),[]);
});

test('coverage distinguishes partial gaps from absent service data and incident gaps',()=>{
  const data={scope:['a','b','c'],series:[{service:'a',points:[[1,2],[2,null]],gaps:[[2,2]]},{service:'b',points:[[1,null]],gaps:[[1,5]],gaps_truncated:true}]};
  const c=visuals.evidenceCoverage(data,3);
  assert.equal(c.captured,1);assert.equal(c.total,3);assert.equal(c.gaps,2);assert.equal(c.after,1);assert.equal(c.truncated,true);
  assert.equal(visuals.evidenceCoverage({},3).captured,0);
});

test('cancelled and incomplete runs never claim a no-anomaly result',()=>{
  for(const job of [{status:'cancelled'},{status:'failed'},{status:'done',result:{status:'partial_timeout',root_causes:[]}}, {status:'done',result:{}}]){
    const state=visuals.diagnosisState(job);assert.equal(state.incomplete,true);assert.doesNotMatch(state.title,/No anomaly/);
  }
  assert.equal(visuals.diagnosisState({status:'cancelled',result:{status:'no_significant_anomaly'}}).badge,'Cancelled');
  assert.equal(visuals.diagnosisState({status:'done',result:{status:'no_significant_anomaly'}}).badge,'No anomaly');
  assert.equal(visuals.diagnosisState({status:'done',result:{status:'insufficient_evidence'}}).badge,'Insufficient data');
});

test('evaluation history orders completed runs and stays anchored at an older selection',()=>{
  const runs=Array.from({length:12},(_,i)=>({id:String(i+1),completed_at:`2026-10-${String(i+1).padStart(2,'0')}T00:00:00Z`,dataset_id:'fixed',scope:'same',evidence_kind:'synthetic',cases:[incident()]}));
  const receivedLate=runs[0];
  assert.equal(visuals.evaluationRuns([receivedLate,...runs.slice(1).reverse()])[0].id,'12');
  assert.deepEqual(Array.from(visuals.evaluationHistory(runs,runs[2]),r=>r.id),['1','2','3']);
  assert.deepEqual(Array.from(visuals.evaluationHistory(runs,runs[11]),r=>r.id),['5','6','7','8','9','10','11','12']);
  assert.equal(runs[0].id,'1','history sorting must not mutate received reports');
});

test('evaluation rerenders retain selected run, explicit no comparison, case, and incident page',()=>{
  function h(tag,props={},...children){
    const node={tag,props,dataset:{case:props['data-case']},children:children.flat().filter(c=>c!=null),events:{},classList:{add(){}},
      append(...items){this.children.push(...items.flat().filter(c=>c!=null));},replaceChildren(...items){this.children=items.flat().filter(c=>c!=null);},
      setAttribute(name,value){this.props[name]=String(value);},addEventListener(name,fn){this.events[name]=fn;},cloneNode(){return {...this};},scrollIntoView(){}};
    Object.defineProperty(node,'value',{get(){return this.selectedValue??(this.tag==='select'?this.children[0]?.props.value:'')??'';},set(value){this.selectedValue=String(value);}});
    return node;
  }
  context.document={createElementNS:(_ns,tag)=>h(tag),createTextNode:text=>text};
  const cases=Array.from({length:60},(_,i)=>incident({case_id:'case-'+(i+1)}));
  cases[0]={...cases[0],status:'failed',seconds:1};
  const runs=Array.from({length:12},(_,i)=>({id:String(i+1),name:'run '+(i+1),run_key:'run:'+(i+1),completed_at:`2026-10-${String(i+1).padStart(2,'0')}T00:00:00Z`,dataset_id:'fixed',scope:'same',evidence_kind:'synthetic',cases,gates:[],limitations:[]}));
  const state={runId:'3',compareId:'',caseId:'case-60',page:1},host=h('div');let refreshes=0;
  const all=node=>typeof node==='object'&&node?[node,...node.children.flatMap(all)]:[];
  const draw=()=>visuals.evaluations({host,h,runs,state,onRefresh:()=>refreshes++});
  draw();
  let nodes=all(host);
  assert.equal(nodes.find(n=>n.props['aria-label']==='Evaluation run').value,'3');
  assert.equal(nodes.find(n=>n.props['aria-label']==='Compare evaluation with').value,'');
  assert.equal(nodes.find(n=>n.props['aria-label']==='Inspect incident').value,'case-60');
  assert.match(JSON.stringify(host),/Showing 51–60 of 60/);
  assert.match(JSON.stringify(host),/Recorded completion times include failures/);
  const compare=nodes.find(n=>n.props['aria-label']==='Compare evaluation with');compare.value='2';compare.events.change();
  draw();nodes=all(host);
  assert.equal(nodes.find(n=>n.props['aria-label']==='Compare evaluation with').value,'2');
  assert.equal(state.page,1);assert.equal(state.caseId,'case-60');
  const run=nodes.find(n=>n.props['aria-label']==='Evaluation run');run.value='1';run.events.change();
  assert.equal(state.compareId,'','oldest run should not default to a later comparison');
  assert.equal(state.page,0);
  all(host).find(n=>n.tag==='button'&&n.children.includes('Refresh evaluations')).props.onclick();
  assert.equal(refreshes,1);
  visuals.evaluations({host,h,runs:[],state,onRefresh:()=>refreshes++});
  const refresh=all(host).find(n=>n.tag==='button'&&n.children.includes('Refresh evaluations'));
  assert.equal(refresh.props.disabled,false);refresh.props.onclick();assert.equal(refreshes,2);
});
