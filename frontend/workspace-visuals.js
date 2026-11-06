/* Native charts and local interaction; no chart library or polling of metric previews. */
(() => {
  const colors=['#0b57d0','#78a9fa','#c5dafc','#e98200','#9299a6'];
  const rank=c=>{
    if(c.outcome!=='fault')return null;
    if(!['diagnosed','partial_unexplained','partial_timeout'].includes(c.status))return Infinity;
    const places=c.truth.map(name=>c.predicted.indexOf(name)+1);
    return places.length && places.every(n=>n>0) ? Math.max(...places) : Infinity;
  };
  const quantile=(values,q)=>{
    if(!values.length)return null;
    const a=[...values].sort((a,b)=>a-b),i=(a.length-1)*q;
    return a[Math.floor(i)]+(a[Math.ceil(i)]-a[Math.floor(i)])*(i%1);
  };
  function stats(cases){
    const faults=cases.filter(c=>c.outcome==='fault'),times=cases.map(c=>c.seconds).filter(n=>Number.isFinite(n)&&n>=0);
    const bins=[0,0,0,0,0];
    for(const c of faults){const r=rank(c);bins[!['diagnosed','partial_unexplained','partial_timeout','no_significant_anomaly'].includes(c.status) || (!c.predicted.length && c.status!=='no_significant_anomaly') ? 4 : r===1 ? 0 : r<=3 ? 1 : r<=5 ? 2 : 3]++;}
    return {faults,times,bins,n:faults.length,top1:faults.filter(c=>rank(c)<=1).length,top3:faults.filter(c=>rank(c)<=3).length,miss:faults.filter(c=>rank(c)>5).length,median:quantile(times,.5),p90:quantile(times,.9)};
  }
  function comparable(a,b){
    const labels=r=>JSON.stringify(r.cases.map(c=>[c.case_id,c.outcome,[...c.truth].sort(),c.fault_type]).sort((a,b)=>a[0].localeCompare(b[0])));
    return !!a.dataset_id && a.dataset_id===b.dataset_id && a.scope===b.scope && a.evidence_kind===b.evidence_kind && labels(a)===labels(b);
  }
  function interval(hits,n){
    if(!n)return 'Not measured';
    const z=1.96,p=hits/n,d=1+z*z/n,mid=(p+z*z/(2*n))/d,half=z*Math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d;
    return `95% Wilson range ${(Math.max(0,mid-half)*100).toFixed(0)}–${(Math.min(1,mid+half)*100).toFixed(0)}%`;
  }
  function declaredTargets(gates){
    const targets={};
    for(const gate of gates||[]){
      const rate=gate.name.match(/^Top-([13]) hit rate\s*(?:at least|≥|>=)\s*(\d+(?:\.\d+)?)\s*%$/i);
      const time=gate.name.match(/^Median time to result\s*(?:at most|≤|<=)\s*(\d+(?:\.\d+)?)\s*s$/i);
      if(rate&&Number(rate[2])<=100)targets['top'+rate[1]]=Number(rate[2])/100;
      if(time&&Number.isFinite(Number(time[1]))&&Number(time[1])>0)targets.seconds=Number(time[1]);
    }
    return targets;
  }
  const evaluationRuns=runs=>runs.filter(r=>r.cases?.length).sort((a,b)=>(Date.parse(b.completed_at)||0)-(Date.parse(a.completed_at)||0));
  function evaluationHistory(runs,run){
    const peers=evaluationRuns(runs).filter(r=>r===run||comparable(run,r)),index=peers.indexOf(run);
    return peers.slice(index,index+8).reverse();
  }
  function evaluations({host,h,runs,openDiagnosis,openTools,onRefresh,state={}}){
    host.className='reference-layout ref-evaluations';
    const usable=evaluationRuns(runs);
    const refresh=h('button',{type:'button',class:'btn',disabled:!onRefresh,onclick:onRefresh},'Refresh evaluations');
    if(!usable.length){host.replaceChildren(h('section',{class:'ref-eval-heading'},h('h1',{},'Is diagnosis getting better?'),h('div',{class:'ref-eval-controls'},h('button',{type:'button',class:'btn',onclick:()=>openTools?.()},'Pipeline tools'),refresh)),h('section',{class:'card'},h('h2',{},'No confirmed incidents yet'),h('p',{class:'hint'},'Review incident outcomes or publish a run with per-incident results. Accuracy charts appear when confirmed evidence is available.')));return;}
    const runSelect=h('select',{'aria-label':'Evaluation run'},...usable.map(r=>h('option',{value:String(r.id)},r.name)));
    if(usable.some(r=>String(r.id)===state.runId))runSelect.value=state.runId;
    const compare=h('select',{'aria-label':'Compare evaluation with'}),body=h('div',{});
    const runDescription=h('span',{}),runDate=h('span',{}),runMeta=h('div',{class:'ref-run-meta'},h('label',{},'Evaluation run ',runSelect),runDescription,runDate),pipeline=h('span',{});
    const header=h('section',{class:'ref-eval-heading'},h('div',{},h('h1',{},'Is diagnosis getting better?'),runMeta),h('div',{class:'ref-eval-controls'},h('label',{},'Compare with',compare),pipeline,refresh));
    host.replaceChildren(header,body);
    let selectedCase=state.caseId||null,page=state.page||0;
    function prepare(resetPage=false){
      const run=usable.find(r=>String(r.id)===runSelect.value),saved=state.runId===runSelect.value&&Object.hasOwn(state,'compareId'),old=saved?state.compareId:'';
      const peers=usable.filter(r=>r!==run&&comparable(run,r));
      compare.replaceChildren(h('option',{value:''},'No comparison'),...peers.map(r=>h('option',{value:String(r.id)},r.name)));
      const previous=usable.slice(usable.indexOf(run)+1).find(r=>comparable(run,r));
      compare.value=saved&&old==='' || peers.some(r=>String(r.id)===old) ? old : previous?String(previous.id):'';
      if(resetPage)page=0;render();
    }
    function render(){
      const run=usable.find(r=>String(r.id)===runSelect.value),previous=usable.find(r=>String(r.id)===compare.value),s=stats(run.cases),before=previous ? stats(previous.cases) : null;
      page=Math.max(0,Math.min(page,Math.max(0,Math.ceil(s.n/50)-1)));
      Object.assign(state,{runId:runSelect.value,compareId:compare.value,page});
      const targets=declaredTargets(run.gates),failedTimings=run.cases.some(c=>Number.isFinite(c.seconds)&&['failed','insufficient_evidence'].includes(c.status));
      const pct=(n,d)=>d ? (100*n/d).toFixed(1)+'%' : 'Not measured';
      const delta=(a,b,den,oldDen)=>before&&den&&oldDen ? `${(100*a/den-100*b/oldDen)>=0?'+':''}${(100*a/den-100*b/oldDen).toFixed(1)} pt` : 'No comparison';
      const range=(hits,n,prior,den=n,target=null)=>{
        const bounds=n?interval(hits,n).match(/[\d.]+/g).slice(1).map(Number):[0,0];
        return h('div',{class:'ref-mini-range','aria-label':n?`${pct(hits,n)}; ${interval(hits,n)}${target!=null?'; declared target '+target*100+'%':''}`:'Not measured'},h('span',{class:'ref-range-band',style:`left:${bounds[0]||0}%;width:${(bounds[1]||0)-(bounds[0]||0)}%`}),n?h('i',{class:'ref-range-point',style:`left:${100*hits/n}%`}):null,target!=null?h('i',{class:'ref-range-prior',style:`left:${100*target}%`,title:'Declared target '+target*100+'%'}):prior!=null&&den?h('i',{class:'ref-range-prior',style:`left:${100*prior/den}%`,title:'Previous run'}):null);
      };
      const kpi=(name,value,detail,change,chart,foot)=>h('section',{class:'card eval-kpi'},h('div',{class:'ref-kpi-label'},h('span',{},name),h('span',{class:'ref-kpi-change'},change)),h('strong',{class:'ref-kpi-value'},value),h('p',{},detail),chart,h('small',{},foot));
      const timingLow=s.times.length?Math.min(...s.times):0,timingHigh=s.times.length?Math.max(timingLow+1,...s.times):1,timingX=t=>Math.max(0,Math.min(100,100*(t-timingLow)/(timingHigh-timingLow)));
      const durationRange=s.times.length?h('div',{class:'ref-mini-range','aria-label':`Median ${s.median.toFixed(1)} seconds, interquartile range ${quantile(s.times,.25).toFixed(1)}–${quantile(s.times,.75).toFixed(1)} seconds`},h('span',{class:'ref-range-band',style:`left:${timingX(quantile(s.times,.25))}%;width:${timingX(quantile(s.times,.75))-timingX(quantile(s.times,.25))}%`}),h('i',{class:'ref-range-point',style:`left:${timingX(s.median)}%`}),targets.seconds!=null?h('i',{class:'ref-range-prior',style:`left:${timingX(targets.seconds)}%`,title:'Declared limit '+targets.seconds+' s'}):before?.median!=null?h('i',{class:'ref-range-prior',style:`left:${timingX(before.median)}%`}):null):h('div',{class:'ref-mini-range'});
      const metrics=h('div',{class:'eval-kpis'},kpi('Top-1 hit rate',pct(s.top1,s.n),`${s.top1} of ${s.n} incidents`,delta(s.top1,before?.top1,s.n,before?.n),range(s.top1,s.n,before?.top1,before?.n,targets.top1),interval(s.top1,s.n)+(targets.top1!=null?' · target '+targets.top1*100+'%':'')),kpi('Top-3 hit rate',pct(s.top3,s.n),`${s.top3} of ${s.n} incidents`,delta(s.top3,before?.top3,s.n,before?.n),range(s.top3,s.n,before?.top3,before?.n,targets.top3),interval(s.top3,s.n)+(targets.top3!=null?' · target '+targets.top3*100+'%':'')),kpi('True cause not found',s.n?`${s.miss} of ${s.n}`:'Not measured','Not all causes in the top 5 candidates',before?`${s.miss-before.miss>=0?'+':''}${s.miss-before.miss}`:'No comparison',range(s.miss,s.n,before?.miss,before?.n),'Lower is better · unavailable results included'),kpi('Median time to result',s.median===null?'Not measured':s.median.toFixed(1)+' s',s.times.length?`p90 ${s.p90.toFixed(1)} s · ${s.times.length} runs`:'No recorded timings',before?.median!=null&&s.median!=null?`${s.median-before.median>=0?'+':''}${(s.median-before.median).toFixed(1)} s`:'No comparison',durationRange,(targets.seconds!=null?'Limit '+targets.seconds+' s':'Measured timings only')+(failedTimings?' · Recorded completion times include failures.':'')));
      runDescription.textContent=`dataset ${run.dataset_id} · ${run.cases.length} labelled · ${run.evidence_kind.replaceAll('_',' ')}`;
      runDate.textContent=run.completed_at?new Date(run.completed_at).toLocaleString():'';
      pipeline.replaceChildren(run.pipeline_url?h('a',{class:'btn',href:run.pipeline_url,target:'_blank',rel:'noopener noreferrer'},'Open pipeline ↗'):h('button',{type:'button',class:'btn',onclick:()=>openTools?.()},'Pipeline tools'));
      const checks=run.gates||[],failed=checks.filter(g=>g.status==='fail').length;
      const banner=h('div',{class:'ref-gate-banner'},h('span',{class:'ref-gate-pill '+(failed?'fail':'')},failed?'Gate failed':checks.length?'Checks reported':'Not assessed'),h('span',{},checks.length?`${checks.filter(g=>g.status==='pass').length} of ${checks.length} checks pass. ${checks.filter(g=>g.status==='fail').map(g=>g.name).join('; ')}`:'No acceptance thresholds were declared.'),h('button',{type:'button',class:'link-button',onclick:()=>checkPanel.scrollIntoView({block:'start',behavior:'auto'})},'See checks'));
      const history=evaluationHistory(usable,run);
      const trend=h('section',{class:'card'},h('h3',{},'Where the true cause landed'),h('p',{class:'hint'},history.length>1?`Each column is one run of ${s.n} labelled incidents.`:'One run available. Compare matching labelled runs as they arrive.'));
      const columns=h('div',{class:'rank-columns'});
      for(const r of history){const st=stats(r.cases);const stack=h('div',{class:'rank-stack'});st.bins.forEach((count,i)=>stack.append(h('span',{style:`height:${st.n?count/st.n*100:0}%;background:${colors[i]}`,title:`${['Rank 1','Rank 2–3','Rank 4–5','Not found','Unavailable'][i]}: ${count}`})));columns.append(h('button',{type:'button',class:'rank-run','aria-pressed':String(r===run),'aria-label':`${r.name}: ${st.top3} of ${st.n} within top 3`,onclick:()=>{runSelect.value=String(r.id);prepare();}},stack,h('small',{},'#'+String(r.run_key||r.name).split(':').at(-1))));}
      trend.append(columns,h('div',{class:'quality-legend eval-legend'},...['Rank 1','Rank 2–3','Rank 4–5','Not found','Unavailable'].map((label,i)=>h('span',{},h('i',{class:'quality-swatch',style:`background:${colors[i]}`}),label))));
      const changes=h('section',{class:'card ref-changes'},h('h2',{},previous?'Changed since #'+String(previous.run_key||previous.name).split(':').at(-1):'Changed since the previous run'));
      if(previous){const rows=s.faults.map(c=>({c,old:previous.cases.find(o=>o.case_id===c.case_id)})).filter(r=>r.old&&rank(r.old)!==rank(r.c));
        for(const better of [true,false]){const items=rows.filter(r=>(rank(r.c)<rank(r.old))===better);changes.append(h('h3',{class:better?'ref-blue':'ref-amber'},`${better?'Got better':'Got worse'} · ${items.length}`));for(const {c,old} of items.slice(0,6))changes.append(h('div',{class:'case-change '+(better?'better':'worse')},h('span',{},h('span',{class:'mono'},c.case_id),' '+c.fault_type),h('strong',{},`${Number.isFinite(rank(old))?rank(old):'miss'} → ${Number.isFinite(rank(c))?rank(c):'miss'}`)));if(!items.length)changes.append(h('p',{class:'hint'},'No incidents '+(better?'improved.':'worsened.')));}
        changes.append(h('p',{class:'ref-change-note'},'Averages hide these. A run can hold steady overall while individual incidents change.'));
      }else changes.append(h('p',{class:'hint'},'No comparable run with matching dataset, labels, evidence source, and scope.'));
      const grid=h('section',{class:'card setup-section'},h('h3',{},'Every incident, every run'),h('p',{class:'hint'},`Columns are the ${s.n} labelled fault incidents. Select one to see its history.`));
      const caseSelect=h('select',{'aria-label':'Inspect incident',disabled:!s.faults.length},...s.faults.map(c=>h('option',{value:c.case_id},c.case_id+' · '+c.fault_type)));
      const details=h('div',{class:'case-history'}),cells=[];
      const visible=s.faults.slice(page*50,page*50+50),table=h('table',{class:'case-grid'},h('caption',{class:'sr'},'Rank of all confirmed causes by incident and run'));
      table.append(h('colgroup',{},h('col',{style:'width:47px'}),h('col',{span:visible.length||1})));
      const tbody=h('tbody',{});
      function showCase(c){selectedCase=c.case_id;state.caseId=selectedCase;caseSelect.value=c.case_id;for(const cell of cells)cell.setAttribute('aria-pressed',String(cell.dataset.case===selectedCase));details.replaceChildren(h('div',{class:'ref-case-name'},h('strong',{class:'mono'},c.case_id),h('small',{},c.fault_type+' · confirmed '+c.truth.join(', '))),h('div',{class:'ref-case-ranks'},h('span',{class:'eyebrow'},'Rank of the true cause, #'+String(history[0]?.run_key||'').split(':').at(-1)+' → #'+String(history.at(-1)?.run_key||'').split(':').at(-1)),h('p',{class:'mono'},history.map(r=>{const item=r.cases.find(x=>x.case_id===c.case_id);const value=item?rank(item):Infinity;return Number.isFinite(value)?value<=3?String(value):value<=5?'4–5':'>5':'miss';}).join(' → '))));if(c.diagnosis_id)details.append(h('button',{type:'button',class:'btn',onclick:()=>openDiagnosis(c.diagnosis_id)},'Open selected diagnosis'));}
      caseSelect.addEventListener('change',()=>showCase(s.faults.find(c=>c.case_id===caseSelect.value)));
      grid.append(h('label',{class:'ref-case-picker'},h('span',{class:'sr'},'Inspect incident'),caseSelect));
      for(const r of history){const row=h('tr',{},h('th',{scope:'row'},'#'+String(r.run_key||r.name).split(':').at(-1)));for(const c of visible){const item=r.cases.find(x=>x.case_id===c.case_id),st=stats([item]),index=st.bins.findIndex(n=>n>0);const cell=h('button',{type:'button',class:'case-cell','data-case':c.case_id,'aria-pressed':String(c.case_id===selectedCase),'aria-label':`${c.case_id}, ${r.name}: ${Number.isFinite(rank(item)) ? 'rank '+rank(item) : index===4 ? 'unavailable' : 'not found'}`,style:`background:${colors[Math.max(0,index)]}`,onclick:()=>showCase(c)});cells.push(cell);row.append(h('td',{},cell));}tbody.append(row);}table.append(tbody);grid.append(h('div',{class:'table-wrap'},table),h('p',{class:'hint'},s.n?`Showing ${page*50+1}–${Math.min(s.n,page*50+50)} of ${s.n} fault cases`:'No confirmed fault cases to chart.'),details);
      if(visible.length)showCase(s.faults.find(c=>c.case_id===selectedCase)||visible.find(c=>c.diagnosis_id)||visible[0]);
      if(s.n>50)grid.append(h('div',{class:'actions'},h('button',{type:'button',class:'btn',disabled:page===0,onclick:()=>{page--;render();}},'Previous incidents'),h('button',{type:'button',class:'btn',disabled:(page+1)*50>=s.n,onclick:()=>{page++;render();}},'Next incidents')));
      const types=h('section',{class:'card'},h('h3',{},'Top-3 hit rate by fault type'),h('p',{class:'hint ref-amber'},'Small cohorts · do not judge a fault type alone'));
      const groups=new Map();for(const c of s.faults){if(!groups.has(c.fault_type))groups.set(c.fault_type,[]);groups.get(c.fault_type).push(c);}
      for(const [name,items] of groups){const hits=items.filter(c=>rank(c)<=3).length;types.append(h('div',{class:'fault-bar'},h('span',{},name),h('div',{class:'quality-track',role:'img','aria-label':`${name}: ${hits} of ${items.length} in top 3`},h('div',{class:'quality-fill',style:`width:${100*hits/items.length}%`}),targets.top3!=null?h('i',{class:'ref-fault-target',style:`left:${targets.top3*100}%`,title:'Declared top-3 target '+targets.top3*100+'%'}):null),h('span',{class:'mono'},`${hits}/${items.length}`)));}
      const duration=h('section',{class:'card ref-duration'},h('div',{class:'ref-card-head'},h('h2',{},'Time to result'),h('span',{class:'hint'},`${s.times.length} runs · seconds`)));
      if(s.times.length){const low=Math.floor(Math.min(...s.times)/10)*10,high=Math.ceil(Math.max(low+10,...s.times)/10)*10,bins=Array(12).fill(0);for(const value of s.times)bins[Math.min(11,Math.floor((value-low)/(high-low)*12))]++;const max=Math.max(...bins),x=t=>30+(t-low)/(high-low)*390,y=n=>170-n/max*125,chart=svg('svg',{viewBox:'0 0 440 215',role:'img','aria-label':`Time to result distribution: ${s.times.length} measured runs, median ${s.median.toFixed(1)} seconds, p90 ${s.p90.toFixed(1)} seconds`});chart.append(svg('rect',{x:Math.max(30,Math.min(420,x(targets.seconds??s.median))),y:20,width:420-Math.max(30,Math.min(420,x(targets.seconds??s.median))),height:150,fill:'#fef7e0'}));bins.forEach((n,i)=>{const left=low+i*(high-low)/12;chart.append(svg('rect',{x:x(left)+2,y:y(n),width:390/12-3,height:170-y(n),fill:left>=(targets.seconds??s.median)?'#e98200':'#78a9fa'},svg('title',{},`${left.toFixed(1)}–${(left+(high-low)/12).toFixed(1)} s: ${n} runs`)));});if(targets.seconds!=null&&targets.seconds>=low&&targets.seconds<=high)chart.append(svg('line',{x1:x(targets.seconds),x2:x(targets.seconds),y1:14,y2:172,stroke:'#b3261e'}),svg('text',{x:x(targets.seconds)+4,y:14,'font-size':10,fill:'#b3261e'},'limit '+targets.seconds+' s'));for(const [value,label] of [[s.median,'median'],[s.p90,'p90']])chart.append(svg('line',{x1:x(value),x2:x(value),y1:30,y2:172,stroke:'#1f1f1f','stroke-dasharray':'3 3'}),svg('text',{x:Math.min(330,x(value)+4),y:label==='median'?30:55,'font-size':10,fill:'#1f1f1f'},`${label} ${value.toFixed(1)} s`));for(let i=0;i<=4;i++){const t=low+(high-low)*i/4;chart.append(svg('text',{x:x(t),y:193,'text-anchor':'middle','font-size':10,fill:'#5f6368'},number(t)));}duration.append(chart,h('p',{class:'hint'},(targets.seconds!=null?'Amber shows runs at or above the declared limit.':'Blue: below median · amber: at or above median.')+(failedTimings?' Recorded completion times include failures.':'')));
      }else duration.append(h('div',{class:'ref-empty'},'Timing was not captured for these cases.'));
      const checkPanel=h('section',{class:'card setup-section'},h('h3',{},'Acceptance checks'));
      for(const check of checks){const top=check.name.toLowerCase().includes('top-3'),time=check.name.toLowerCase().includes('time');checkPanel.append(h('div',{class:'ref-check-row'},h('span',{class:'ref-check-pill '+check.status},check.status==='pass'?'Pass':check.status==='fail'?'Fail':'Not run'),h('strong',{},check.name),top?range(s.top3,s.n,before?.top3,before?.n,targets.top3):time?durationRange.cloneNode(true):h('span',{}),h('p',{},check.criterion)));}
      if(!checks.length)checkPanel.append(h('p',{class:'hint'},'No acceptance thresholds were declared for this cohort.'));
      body.replaceChildren(banner,h('details',{class:'card setup-section',open:true},h('summary',{},'Evaluation scope & limitations'),h('p',{class:'hint'},run.scope),...(run.limitations||[]).map(note=>h('p',{class:'hint'},note))),metrics,h('div',{class:'eval-split'},trend,changes),grid,h('div',{class:'eval-split equal'},types,duration),checkPanel);

    }
    runSelect.addEventListener('change',()=>prepare(true));compare.addEventListener('change',render);prepare();
  }
  const svg=(tag,attrs={},...children)=>{const e=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [key,value] of Object.entries(attrs))e.setAttribute(key,String(value));for(const c of children)e.append(typeof c==='string'?document.createTextNode(c):c);return e;};
  let signalNumber=0;
  function signalChart(item,job,cursor,fmtClock){
    const points=item.points,values=points.map(p=>p[1]).filter(Number.isFinite),band=item.baseline||[],all=[...values,...band];
    const low=all.length?Math.min(...all):0,high=all.length?Math.max(low+1e-12,...all):1,start=job.window[0],end=job.window[1];
    const x=t=>12+(t-start)/(end-start)*606,y=v=>72-(v-low)/(high-low)*54;
    const chart=svg('svg',{viewBox:'0 0 630 104',role:'img','aria-label':`${item.service} ${item.metric}. Captured metric preview, with incident start and missing-data intervals.`});
    const pattern='signal-gap-'+signalNumber++;
    chart.append(svg('defs',{},svg('pattern',{id:pattern,width:7,height:7,patternUnits:'userSpaceOnUse'},svg('rect',{width:7,height:7,fill:'#fef7e0'}),svg('path',{d:'M-1,1 L1,-1 M0,7 L7,0 M6,8 L8,6',stroke:'#e98200','stroke-width':.8}))));
    if(band.length)chart.append(svg('rect',{x:12,y:y(band[1]),width:606,height:Math.max(1,y(band[0])-y(band[1])),fill:'#d3e3fd'}));
    for(const [a,b] of item.gaps||[]){chart.append(svg('rect',{x:x(a),y:10,width:Math.max(3,x(b)-x(a)),height:68,fill:`url(#${pattern})`,stroke:'#e98200','stroke-dasharray':'3 3'}));if(x(b)-x(a)>40)chart.append(svg('text',{x:(x(a)+x(b))/2,y:45,'text-anchor':'middle','font-size':10,fill:'#8a5300'},'no data'));}
    let d='',last=null;
    for(const [t,v] of points){if(v===null){last=null;continue;}const gap=last!==null&&(item.gaps||[]).some(([a,b])=>a<=t&&b>=last);d+=(last===null||gap?'M':'L')+x(t).toFixed(2)+','+y(v).toFixed(2)+' ';last=t;}
    chart.append(svg('path',{d,fill:'none',stroke:'#0b57d0','stroke-width':2}),svg('line',{x1:x(job.inject_time),x2:x(job.inject_time),y1:8,y2:79,stroke:'#5f6368','stroke-dasharray':'3 3'}),svg('line',{x1:x(cursor),x2:x(cursor),y1:8,y2:79,stroke:'#1f1f1f','data-replay-cursor':'','data-start':start,'data-end':end}),svg('text',{x:12,y:98,fill:'#5f6368','font-size':11},fmtClock(start)),svg('text',{x:618,y:98,fill:'#5f6368','font-size':11,'text-anchor':'end'},fmtClock(end)));
    return chart;
  }
  const number=value=>Number.isFinite(value)?Number(value.toPrecision(3)).toString():'Not recorded';
  function metricSummary(item){
    if(!item)return 'Metric preview not captured';
    const values=item.points.filter(p=>Number.isFinite(p[1])).map(p=>p[1]);
    return `${item.metric} ${values.length?number(Math.max(...values)):'unavailable'}${item.baseline?' · baseline '+item.baseline.map(number).join('–'):''}`;
  }
  function dependencyPath(edges,start,end){
    const queue=[[start]],seen=new Set();
    while(queue.length){const path=queue.shift(),node=path.at(-1);if(node===end)return path;if(seen.has(node))continue;seen.add(node);for(const [a,b] of edges)if(a===node&&!seen.has(b))queue.push([...path,b]);}
    return [];
  }
  function evidenceCoverage(data,incident){
    const series=data.series||[],scope=new Set([...(data.scope||[]),...series.map(s=>s.service),...(data.edges||[]).flat()]);
    const captured=new Set(series.filter(s=>s.points.some(p=>Number.isFinite(p[1]))).map(s=>s.service));
    const gaps=series.filter(s=>s.gaps?.length),after=gaps.filter(s=>s.gaps.some(([,end])=>end>=incident));
    return {captured:captured.size,total:scope.size,gaps:new Set(gaps.map(s=>s.service)).size,after:new Set(after.map(s=>s.service)).size,truncated:gaps.some(s=>s.gaps_truncated)};
  }
  function diagnosisState(job){
    if(job.status==='cancelled')return {badge:'Cancelled',title:'Diagnosis was cancelled',description:'Analysis was cancelled before a result was available. This run cannot tell whether an anomaly occurred.',incomplete:true};
    if(job.status==='failed')return {badge:'Failed',title:'Analysis did not complete',description:'This run cannot provide a diagnosis. Review the error and retry.',incomplete:true};
    if(job.result?.status==='insufficient_evidence')return {badge:'Insufficient data',title:'Too little data to identify a cause',description:'More usable telemetry is needed before CoRCA can rank likely causes.'};
    if(job.result?.status==='no_significant_anomaly')return {badge:'No anomaly',title:'No anomaly crossed the threshold',description:'CoRCA did not identify a likely cause in this window. This does not confirm the services are healthy.'};
    if(job.result?.root_causes?.length)return {badge:'Diagnosis ready'};
    return {badge:'Incomplete',title:'No diagnosis available',description:'Analysis stopped without a conclusive result. Review diagnosis details before running again.',incomplete:true};
  }
  async function diagnosis({host,h,job,api,fmtTime,fmtClock,review,logs,copyText,share,download,ask,rerun,back,draftText}){
    host.className='reference-layout ref-diagnosis';
    host.replaceChildren(h('p',{class:'hint'},'Loading saved evidence…'));
    let data,outcomeReview;
    try{const replies=await Promise.all([api(`/v1/diagnoses/${encodeURIComponent(job.id)}/evidence`),api(`/v1/diagnoses/${encodeURIComponent(job.id)}/review`)]);data=replies[0];outcomeReview=replies[1].review;}catch(error){if(host.isConnected)host.replaceChildren(h('p',{class:'error',role:'alert'},error.message),h('button',{type:'button',class:'btn',onclick:()=>diagnosis({host,h,job,api,fmtTime,fmtClock,review,logs,copyText,share,download,ask,rerun,back,draftText})},'Retry'));return;}
    if(!host.isConnected)return;
    const result=job.result||{},state=diagnosisState(job),roots=result.root_causes||[],rounds=result.rounds||[],unexplained=result.unexplained_services||[],series=data.series||[],edges=data.edges||[],alerts=data.alerts||[];
    const counts=new Map();for(const [caller] of edges)counts.set(caller,(counts.get(caller)||0)+1);
    const entry=alerts[0]?.service||[...counts].sort((a,b)=>b[1]-a[1])[0]?.[0]||roots[0]||series[0]?.service;
    const root=roots[0],gaps=series.filter(s=>s.gaps?.length),missing=result.dropped_metrics||[],scope=[...new Set([...series.map(s=>s.service),...(data.scope||[]),...edges.flat()])];
    let focused=entry,trace=entry?[entry]:[],mapOnly=new URLSearchParams(location.search).get('panel')==='map',cursor=job.inject_time;
    const first=series.find(s=>s.service===root),entrySignal=series.find(s=>s.service===entry),path=dependencyPath(edges,entry,root),latest=alerts[0];
    const noCause=job.status==='failed'?'The analysis did not complete.':result.status==='insufficient_evidence'?'Not enough usable metric data.':result.status==='no_significant_anomaly'?'Nothing crossed the configured diagnosis threshold.':'No cause was named before analysis stopped.';
    const fixture=!!result.fixture||alerts.some(a=>a.name.includes('Synthetic'));
    const title=latest&&entrySignal ? `${entry}: ${entrySignal.metric} peak ${number(Math.max(...entrySignal.points.filter(p=>p[0]>=job.inject_time&&Number.isFinite(p[1])).map(p=>p[1])))}, baseline ${entrySignal.baseline?.map(number).join('–')||'not recorded'}` : latest ? `${entry} · ${latest.name}` : root ? `Investigate ${root}` : state.title;
    const heading=h('h1',{},title),subtitle=h('p',{class:'ref-subtitle'},latest?'Start at the alerting service. Follow its dependencies and compare the saved evidence.':'Follow saved call relationships and compare metric signals. These relationships alone do not establish a cause.');
    const badge=h('span',{class:'ref-badge '+(alerts.some(a=>a.status==='firing')?'red':'')},alerts.some(a=>a.status==='firing')?'● Firing':state.badge);
    const metadata=h('div',{class:'ref-incident-meta'},badge,h('span',{class:'mono'},'Started '+fmtTime(job.inject_time)),h('span',{class:'mono'},'Diagnosis #'+job.id.slice(0,8)+(Number.isFinite(result.time_to_result_seconds)?' ready at '+fmtClock(job.created_at+result.time_to_result_seconds):'')),fixture?h('span',{class:'ref-badge'},'Synthetic fixture'):null);
    const draft=h('textarea',{'aria-label':'Incident status draft'});draft.value=`${entry||'Incident'} · ${fmtTime(job.inject_time)}. ${root?'CoRCA points to '+root+' first. '+(path.length?'Dependency path: '+path.join(' → ')+'. ':''):'No candidate cause was named. '}${unexplained.length?'Still unexplained: '+unexplained.join(', ')+'. ':''}${gaps.length?'Missing samples: '+gaps.map(s=>s.service).join(', ')+'. ':''}Next: ${root?'inspect '+root+' metrics and recent changes':'verify incident timing and service coverage'}. Findings need independent confirmation.`;
    if(typeof draftText==='string')draft.value=draftText;
    const tools=h('div',{class:'ref-incident-tools'},h('span',{class:'hint'},'● Saved evidence'),h('button',{type:'button',class:'btn',onclick:()=>copyText(draft.value)},'Copy update'),h('button',{type:'button',class:'btn',onclick:ask},'Ask CoRCA'));
    const tabButtons=[h('button',{type:'button',onclick:()=>switchView(false)},'Summary'),h('button',{type:'button',onclick:()=>switchView(true)},'Service map')];
    const tabs=h('div',{class:'ref-segment',role:'group','aria-label':'Diagnosis view'},...tabButtons);
    const pagehead=h('div',{class:'ref-diagnosis-heading'},h('div',{},metadata,heading,subtitle),h('div',{class:'ref-heading-right'},tools,tabs));
    const uncertaintyCount=unexplained.length+gaps.length;
    const strip=h('section',{class:'ref-summary-strip'},
      h('div',{},h('span',{class:'eyebrow'},'Look first'),h('strong',{class:'ref-root mono'},root||'No candidate'),h('p',{},root?metricSummary(first):noCause)),
      h('div',{},h('span',{class:'eyebrow'},'Why we think so'),h('strong',{},root?`${rounds.find(r=>r.service===root)?.explained?.length||0} linked service symptoms`:'No cause named'),h('p',{},path.length?path.join(' → '):root?'Review its captured signal and candidate score.':'Results still need an independently verified outcome.')),
      h('div',{},h('span',{class:'eyebrow'},'Do next'),h('strong',{},root?(first?.metric.includes('connection')?'Check the connection pool':'Inspect '+root):'Check timing and coverage'),h('p',{},unexplained.length?'Then inspect '+unexplained[0]+' logs.':'Review recent deployments and configuration.')),
      h('div',{},h('span',{class:'eyebrow'},'Not sure about'),h('strong',{},uncertaintyCount?`${uncertaintyCount} open questions`:'No gaps reported'),h('p',{},[gaps.length?`${gaps.length} service${gaps.length===1?'':'s'} with missing samples`:null,unexplained.length?`${unexplained.length} unexplained symptom${unexplained.length===1?'':'s'}`:null].filter(Boolean).join('. ')||'Confidence depends on the available telemetry.')));
    if(!root){
      const coverage=evidenceCoverage(data,job.inject_time),failed=state.incomplete,insufficient=result.status==='insufficient_evidence';
      strip.classList.add('ref-summary-outcome');
      strip.setAttribute('aria-label','Diagnosis takeaway');
      strip.replaceChildren(
        h('div',{class:'ref-takeaway'},h('span',{class:'eyebrow'},'What this run tells you'),h('strong',{},state.title),h('p',{},state.description)),
        h('div',{},h('span',{class:'eyebrow'},'Evidence available'),h('strong',{},coverage.total?`${coverage.captured} of ${coverage.total} services captured`:'No metric preview available'),h('p',{},coverage.gaps?`${coverage.gaps} service previews contain gaps. ${coverage.after?`${coverage.after} have gaps at or after incident start.`:coverage.truncated?'Only the first recorded gaps are shown.':'Recorded gaps are before incident start; they may affect the baseline.'}`:coverage.captured?'No gaps recorded in the saved metric previews.':'No usable metric preview to assess gaps.'),h('button',{type:'button',class:'link-button',onclick:()=>blind.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth',block:'start'})},'Review data coverage →')),
        h('div',{},h('span',{class:'eyebrow'},'Recommended next step'),h('strong',{},failed?'Review this run':insufficient?'Check the monitoring data':'Compare with the reported symptom'),h('p',{},failed?'Check diagnosis details, then run again when ready.':insufficient?'Check missing streams and incident timing before running again.':'Confirm when the symptom began. Analyze the affected service’s logs for errors that metrics may not explain.'),h('button',{type:'button',class:'btn primary',onclick:failed?()=>rerun():insufficient?()=>blind.scrollIntoView({block:'start'}):()=>logs(alerts[0]?.service||'')},failed?'Run again':insufficient?'Review missing data':'Analyze logs')));
    }
    const graph=h('div',{class:'ref-graph'}),traceBar=h('div',{class:'ref-trace'}),inspector=h('aside',{class:'card ref-inspector'});
    const graphTitle=h('h2',{},root?'How it spread':'Service dependencies'),traceLink=h('button',{type:'button',class:'link-button',onclick:()=>switchView(true)},'Trace it step by step');
    const mapHead=h('div',{class:'ref-card-head'},graphTitle,h('span',{class:'hint'},'Arrows show who calls whom.'),traceLink);
    const replay=h('input',{type:'range',min:job.window[0],max:job.window[1],step:1,value:cursor,'aria-label':'Replay incident evidence'}),clock=h('output',{},fmtClock(cursor));
    const replayBar=h('div',{class:'ref-replay'},h('span',{},'Replay'),replay,clock);
    const legend=h('div',{class:'ref-legend'},...[[colors[0],'Candidate cause'],['#d93025','Alerting symptom'],['#e98200','Missing data / unexplained'],['#9aa0a6','No candidate designation']].map(([color,text])=>h('span',{},h('i',{style:`--legend:${color}`}),text)));
    const graphCard=h('section',{class:'card ref-map-card'},mapHead,traceBar,graph,replayBar,legend);
    const evidence=h('section',{class:'card ref-evidence'},h('div',{class:'ref-card-head'},h('h2',{},'Evidence on one clock'),h('span',{class:'hint'},'Every signal shares the same time axis. The cursor follows Replay.')));
    const priority=[...new Set([root,...roots.slice(1).filter(name=>!gaps.some(s=>s.service===name)),entry,...roots.filter(name=>gaps.some(s=>s.service===name)),...unexplained,...series.map(s=>s.service)])],ordered=priority.map(name=>series.find(s=>s.service===name)).filter(Boolean),readouts=[];
    const valueAt=(item,t)=>{if((item.gaps||[]).some(([a,b])=>a<=t&&t<=b))return 'no data';const point=item.points.reduce((best,p)=>Math.abs(p[0]-t)<Math.abs(best[0]-t)?p:best,item.points[0]);return Number.isFinite(point?.[1])?number(point[1]):'not recorded';};
    const signals=h('div',{}),moreSignals=h('details',{class:'ref-more-signals'},h('summary',{},`More signals · ${Math.max(0,ordered.length-5)}`));
    ordered.forEach((item,i)=>{const value=h('output',{},valueAt(item,cursor)),chart=signalChart(item,job,cursor,fmtClock);chart.classList.add('ref-signal-chart');const tone=alerts.some(a=>a.service===item.service)?'#d93025':unexplained.includes(item.service)?'#e98200':'#0b57d0';chart.querySelector('path[stroke="#0b57d0"]')?.setAttribute('stroke',tone);if(unexplained.includes(item.service))chart.querySelector('path[stroke="#e98200"]')?.setAttribute('stroke-dasharray','4 3');const row=h('div',{class:'ref-signal-row'},h('div',{},h('strong',{class:'mono'},item.service),h('small',{},item.metric+' · ',value)),chart);readouts.push({item,value});(i<5?signals:moreSignals).append(row);});
    evidence.append(signals);if(ordered.length>5)evidence.append(moreSignals);
    if(!series.length)evidence.append(h('div',{class:'ref-empty'},'No captured metric samples for this diagnosis. Run a new diagnosis to capture its evidence.'));
    const taskCount=h('span',{class:'hint'}),tasks=[root?'Check '+root+' '+(first?.metric.includes('connection')?'connection pool':'metrics'):'Verify incident timing',root?'Look for recent changes to '+root:'Verify service coverage',unexplained.length?'Analyze '+unexplained[0]+' logs':'Review data gaps','Prepare a status update'];
    const taskDetails=[first?metricSummary(first):'Compare the earliest symptom with monitoring.', 'Deploys, configuration, and infrastructure changes.',unexplained.length?'Its symptoms are not explained by the candidates.':'Check missing or dropped metric streams.','Use the editable draft below.'];
    let checked;try{checked=JSON.parse(localStorage.getItem('corca:steps:'+job.id)||'[]');if(!Array.isArray(checked))checked=[];}catch{checked=[];}
    const next=h('section',{class:'card ref-next'},h('div',{class:'ref-card-head'},h('h2',{},'Next steps'),taskCount));
    const updateCount=()=>taskCount.textContent=checked.filter(i=>i>=0&&i<4).length+' of 4 done';updateCount();
    tasks.forEach((text,i)=>{const box=h('input',{type:'checkbox','aria-label':text});box.checked=checked.includes(i);box.addEventListener('change',()=>{checked=box.checked?[...new Set([...checked,i])]:checked.filter(n=>n!==i);try{localStorage.setItem('corca:steps:'+job.id,JSON.stringify(checked));}catch{}updateCount();});next.append(h('label',{class:'ref-task'},box,h('span',{},h('strong',{},text),h('small',{},taskDetails[i]))));});next.append(h('button',{type:'button',class:'btn primary',onclick:()=>logs(unexplained[0]||entry)},unexplained.length?'Analyze '+unexplained[0]+' logs':'Analyze a log file'));
    const blind=h('section',{class:'card '+(gaps.length||unexplained.length||missing.length?'ref-blind':'ref-coverage-card')},h('h2',{},'Data coverage'));
    const gapDetails=h('details',{class:'ref-gap-details'},h('summary',{},'Affected services & times'));
    for(const item of gaps)gapDetails.append(h('p',{},h('strong',{class:'mono'},item.service),': no samples '+fmtClock(item.gaps[0][0])+'–'+fmtClock(item.gaps.at(-1)[1])));
    for(const name of unexplained)gapDetails.append(h('p',{},h('strong',{class:'mono'},name),': unexplained symptom. No candidate was reported to explain it.'));
    if(gaps.length||unexplained.length)blind.append(h('p',{},`Gaps: ${gaps.length} service${gaps.length===1?'':'s'} · Unexplained symptoms: ${unexplained.length}`),gapDetails);
    if(!gaps.length&&!unexplained.length)blind.append(h('p',{},missing.length?`${missing.length} metric streams were dropped.`:'No gaps or unexplained services were reported.'));
    const covered=series.filter(s=>s.points.some(p=>p[1]!==null)).length,total=scope.length;
    blind.append(h('div',{class:'ref-coverage'},h('span',{},'Captured data'),h('span',{class:'mono'},`${covered} / ${total} services`)),h('div',{class:'quality-track'},h('div',{class:'quality-fill',style:`width:${total?covered/total*100:0}%`})));
    const status=h('section',{class:'card ref-status'},h('div',{class:'ref-card-head'},h('h2',{},'Status update'),h('span',{class:'hint'},'Draft · edit before copying')),draft,h('div',{class:'actions'},h('button',{type:'button',class:'btn',onclick:()=>copyText(draft.value)},'Copy text'),h('button',{type:'button',class:'btn',onclick:share},'Copy link'),h('button',{type:'button',class:'btn',onclick:download,'aria-label':'Download diagnosis evidence'},'↓')));
    const main=h('div',{class:'ref-investigation-main'},graphCard,evidence),side=h('aside',{class:'ref-investigation-side'},root?next:h('details',{class:'card ref-optional'},h('summary',{},'Investigation checklist'),next),blind,root?status:h('details',{class:'card ref-optional'},h('summary',{},'Draft a status update'),status)),columns=h('div',{class:'ref-investigation-columns'},main,side);
    const candidates=h('section',{class:'card ref-candidates'},h('div',{class:'ref-card-head'},h('h2',{},root?'Candidates':'Outcome review'),root?h('span',{class:'hint'},'Relative scores, not probabilities.'):null));
    const table=h('table',{},h('caption',{class:'sr'},'Candidate causes and independent outcome review'),h('thead',{},h('tr',{},...['#','Service','Relative score','Evidence','Explains the alert?','Your call'].map(text=>h('th',{scope:'col'},text))))),tbody=h('tbody',{}),maxScore=Math.max(0,...rounds.map(r=>Number.isFinite(r.score)?r.score:0));
    roots.forEach((name,i)=>{const round=rounds.find(r=>r.service===name),item=series.find(s=>s.service===name),score=round?.score,dep=dependencyPath(edges,entry,name);tbody.append(h('tr',{},h('td',{},String(i+1)),h('td',{},h('strong',{class:'mono'},name),h('small',{},item?metricSummary(item):'No captured metric')),h('td',{},h('div',{class:'ref-score'},h('div',{class:'quality-track'},h('div',{class:'quality-fill',style:`width:${maxScore&&Number.isFinite(score)?Math.max(0,score)/maxScore*100:0}%`})),h('span',{class:'mono'},number(score)))),h('td',{class:item?.gaps?.length?'ref-amber':'ref-blue'},item?.gaps?.length?'Data gap':item?'Captured signal':'Not captured'),h('td',{},round?.explained?.includes(entry)?'Yes'+(dep.length>2?', via '+dep[1]:', directly'):'Not reported'),h('td',{},h('div',{class:'ref-review-actions'},h('button',{type:'button',class:'btn ref-confirm',onclick:()=>review(name,'confirm')},outcomeReview?.outcome==='fault'&&outcomeReview.root_causes.includes(name)?'Confirmed':'Confirm'),h('button',{type:'button',class:'btn',onclick:()=>review(name,'rule_out')},outcomeReview&&outcomeReview.outcome!=='unconfirmed'&&!outcomeReview.root_causes.includes(name)?'Ruled out':'Rule out')))));});table.append(tbody);
    if(roots.length)candidates.append(h('div',{class:'ref-table-scroll'},table));else candidates.append(h('div',{class:'ref-empty'},'Confirm whether this was an incident or a healthy window.',job.status==='cancelled'?h('p',{class:'hint'},'Cancelled runs are not scored in evaluations. Run a completed analysis before reviewing its outcome.'):h('button',{type:'button',class:'btn',onclick:()=>review()},'Review outcome')));
    candidates.append(h('p',{class:'ref-card-foot'},root?'Confirm causes only with independent evidence.':'Only independently verified outcomes are included in evaluations.'));
    const technical=h('details',{class:'ref-technical'},h('summary',{},'Diagnosis details & evidence'),h('p',{class:'hint'},data.note),h('p',{class:'mono hint'},`Window ${fmtClock(job.window[0])}–${fmtClock(job.window[1])} · ${job.method.toUpperCase()} · ${result.processing_seconds?.toFixed(2)??'unknown'} s processing`),h('div',{class:'actions'},h('button',{type:'button',class:'btn',onclick:download},'Download evidence'),h('button',{type:'button',class:'btn',onclick:rerun},'Re-run diagnosis')));
    function inspect(name,reset=false){focused=name;trace=reset?[name]:trace.includes(name)?trace.slice(0,trace.indexOf(name)+1):[...trace,name];drawMap();}
    function drawMap(){
      traceBar.replaceChildren(h('span',{class:'hint'},'Your trace'),...trace.map(name=>h('button',{type:'button',class:'trace-chip',onclick:()=>inspect(name)},name)),h('button',{type:'button',class:'link-button',onclick:()=>inspect(entry,true)},'Start over'));
      const active=mapOnly?focused:entry,parents=[...new Set(edges.filter(e=>e[1]===active).map(e=>e[0]))],children=[...new Set(edges.filter(e=>e[0]===active).map(e=>e[1]))],grandchildren=[...new Set(edges.filter(e=>children.includes(e[0])).map(e=>e[1]))].filter(n=>n!==active&&!children.includes(n));
      const extras=mapOnly?[]:[...new Set(edges.filter(([a,b])=>grandchildren.includes(b)&&!children.includes(a)&&a!==active).map(([a])=>a))];
      const height=mapOnly?Math.max(430,Math.min(10,Math.max(parents.length,children.length))*70+90):Math.max(300,Math.min(10,Math.max(children.length,grandchildren.length+extras.length))*70+70),canvas=svg('svg',{viewBox:`0 0 800 ${height}`,role:'group','aria-label':'Configured service dependencies'}),pattern='map-gap-'+signalNumber++;
      canvas.append(svg('defs',{},svg('pattern',{id:pattern,width:8,height:8,patternUnits:'userSpaceOnUse'},svg('rect',{width:8,height:8,fill:'#fef7e0'}),svg('path',{d:'M0,8 L8,0',stroke:'#e98200','stroke-width':1}))));
      const coords=new Map();if(active)coords.set(active,[mapOnly?290:40,height/2-26]);
      const place=(names,x)=>names.slice(0,10).forEach((name,i)=>{if(!coords.has(name))coords.set(name,[x,50+i*70]);});
      if(mapOnly){place(parents,25);place(children,555);canvas.append(svg('text',{x:30,y:22,'font-size':10,fill:'#5f6368'},'ENTRY'),svg('text',{x:300,y:22,'font-size':10,fill:'#5f6368'},'SERVICES'),svg('text',{x:565,y:22,'font-size':10,fill:'#5f6368'},'DEPENDENCIES'));}
      else{place(children,295);place([...grandchildren,...extras],555);}
      for(const [a,b] of edges){if(!coords.has(a)||!coords.has(b))continue;const from=coords.get(a),to=coords.get(b),vertical=from[0]===to[0],x1=vertical?from[0]+85:from[0]+170,x2=vertical?to[0]+85:to[0],y1=vertical?from[1]:from[1]+25,y2=vertical?to[1]+50:to[1]+25,blue=!mapOnly&&path.includes(a)&&path[path.indexOf(a)+1]===b;canvas.append(svg('path',{d:`M${x1},${y1} L${x2},${y2}`,stroke:blue?'#0b57d0':'#a5a8ad','stroke-width':blue?2:1.4,fill:'none'}));const angle=Math.atan2(y2-y1,x2-x1),size=8;canvas.append(svg('path',{d:`M${x2-size*Math.cos(angle-.5)},${y2-size*Math.sin(angle-.5)} L${x2},${y2} L${x2-size*Math.cos(angle+.5)},${y2-size*Math.sin(angle+.5)}`,stroke:blue?'#0b57d0':'#a5a8ad',fill:'none'}));}
      for(const [name,[x,y]] of coords){const gap=gaps.some(s=>s.service===name),unknown=unexplained.includes(name),alerting=alerts.some(a=>a.service===name),candidate=roots.includes(name),caution=gap||unknown,tone=caution?'#e98200':alerting?'#d93025':candidate?'#0b57d0':'#bdc1c6',fill=gap?`url(#${pattern})`:unknown?'#fff':alerting?'#f9dedc':candidate?'#d3e3fd':'#fff';const node=svg('g',{role:'button',tabindex:0,'aria-label':'Inspect service '+name,class:'service-node'});if(mapOnly&&name===focused)node.append(svg('rect',{x:x-5,y:y-5,width:180,height:60,rx:13,fill:'none',stroke:'#1f1f1f','stroke-width':2}));node.append(svg('rect',{x,y,width:170,height:50,rx:10,fill,stroke:tone,'stroke-width':1.7,...(caution?{'stroke-dasharray':'5 3'}:{})}),svg('text',{x:x+12,y:y+21,'font-size':15,fill:'#1f1f1f'},name.length>21?name.slice(0,19)+'…':name),svg('text',{x:x+12,y:y+39,'font-size':12,fill:gap||unknown?'#8a5300':'#5f6368'},gap?'data gap':unknown?'unexplained':alerting?'alerting symptom':candidate?'#'+(roots.indexOf(name)+1)+' candidate':'not ranked'));node.addEventListener('click',()=>{if(!mapOnly)switchView(true);inspect(name);});node.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();if(!mapOnly)switchView(true);inspect(name);}});canvas.append(node);}
      graph.replaceChildren(canvas);if(!active)graph.replaceChildren(h('div',{class:'ref-empty'},'No saved service topology for this diagnosis.'));
      const item=series.find(s=>s.service===focused),deps=edges.filter(e=>e[0]===focused).map(e=>e[1]);
      inspector.replaceChildren(h('p',{class:'eyebrow'},`Step ${trace.length} · inspecting`),h('h2',{class:'mono'},focused||'No service'),h('span',{class:'ref-badge '+(focused===entry&&alerts.length?'red':'')},focused===entry&&alerts.length?'Alerting · start here':roots.includes(focused)?'Candidate cause':'Service dependency'));
      if(item){const spark=signalChart(item,job,cursor,fmtClock);spark.querySelector('path[stroke="#0b57d0"]')?.setAttribute('stroke',focused===entry&&alerts.length?'#d93025':unexplained.includes(focused)?'#e98200':'#0b57d0');inspector.append(h('p',{class:'ref-metric-description'},metricSummary(item)),spark);}else inspector.append(h('p',{class:'ref-empty'},'No captured metric preview.'));
      inspector.append(h('div',{class:'ref-inspector-note'},focused===entry?'Start here. Follow a dependency and compare its signal. The map alone does not confirm the cause.':'Compare this service with its callers. A candidate still needs independent confirmation.'),h('p',{class:'eyebrow'},'It depends on'));
      for(const name of deps)inspector.append(h('div',{class:'ref-dependency'},h('div',{},h('span',{class:'mono'},name),h('small',{},gaps.some(s=>s.service===name)?'Missing metric samples':roots.includes(name)?'#'+(roots.indexOf(name)+1)+' candidate':'Not ranked as a cause')),h('button',{type:'button',class:'btn '+(name===path[path.indexOf(focused)+1]?'primary':''),onclick:()=>inspect(name)},'Follow')));
      if(!deps.length)inspector.append(h('p',{class:'hint'},'No downstream dependency configured. This does not confirm a root cause.'));
      if(parents.length>10||children.length>10)inspector.append(h('p',{class:'hint'},'Map preview shows up to 10 neighbors per column. All direct dependencies are listed above.'));
      inspector.append(h('div',{class:'ref-inspector-actions'},h('button',{type:'button',class:'btn',onclick:()=>{switchView(false);evidence.scrollIntoView({block:'start'});}},'Full evidence'),h('button',{type:'button',class:'btn',onclick:()=>logs(focused)},'Analyze its logs')));
    }
    replay.addEventListener('input',()=>{cursor=Number(replay.value);clock.textContent=fmtClock(cursor);replay.setAttribute('aria-valuetext',fmtTime(cursor));for(const {item,value} of readouts)value.textContent=valueAt(item,cursor);for(const line of host.querySelectorAll('[data-replay-cursor]')){const x=12+(cursor-Number(line.dataset.start))/(Number(line.dataset.end)-Number(line.dataset.start))*606;line.setAttribute('x1',x);line.setAttribute('x2',x);}});
    function switchView(value){mapOnly=value;const crumb=document.getElementById('breadcrumb-page');if(crumb)crumb.replaceChildren(h('button',{type:'button',class:'link-button',onclick:back},'Diagnoses'),h('span',{},' / #'+job.id.slice(0,8)+(value?' / Service map':'')));host.classList.toggle('is-map',value);heading.textContent=value?(root?'Trace the root cause':'Explore service dependencies'):title;subtitle.hidden=!value;metadata.hidden=value;strip.hidden=value;side.hidden=value;evidence.hidden=value;candidates.hidden=value||job.status==='cancelled';technical.hidden=value;replayBar.hidden=value;mapHead.hidden=value;traceBar.hidden=!value;inspector.hidden=!value;tools.hidden=value;for(const [i,button] of tabButtons.entries())button.setAttribute('aria-pressed',String(value===(i===1)));if(value)main.append(inspector);else inspector.remove();const url=new URL(location.href);if(value)url.searchParams.set('panel','map');else url.searchParams.delete('panel');history.replaceState(null,'',url);drawMap();}
    host.replaceChildren(pagehead,strip,columns,candidates,technical);switchView(mapOnly);
  }
  globalThis.CoRCAVisuals={rank,stats,comparable,interval,declaredTargets,evaluationRuns,evaluationHistory,dependencyPath,metricSummary,evidenceCoverage,diagnosisState,evaluations,diagnosis};
})();
