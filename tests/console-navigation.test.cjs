// Exercise the actual console handlers without a browser or extra dependencies.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const html = readFileSync('frontend/app.html', 'utf8');

function handler(start, end) {
  const from = html.indexOf(start);
  const to = html.indexOf(end, from);
  assert.ok(from >= 0 && to > from, 'Console handler boundaries exist');
  return html.slice(from, to);
}

test('diagnosis evidence export includes saved chart data and review, and reports failures', async () => {
  const exports=[],notices=[],requests=[];
  const job={id:'diagnosis-id',result:{status:'diagnosed'}};
  const evidence={available:true,series:[{service:'db',points:[[1,2]]}],edges:[]};
  const review={revision:1,outcome:'fault',root_causes:['db']};
  const uncertainty={available:false,eligible_incidents:0};
  const context=vm.createContext({api:async path=>{requests.push(path);return path.endsWith('/review')?{review}:path.endsWith('/uncertainty')?uncertainty:evidence;},download:(name,content)=>exports.push({name,data:JSON.parse(content)}),toast:text=>notices.push(text)});
  vm.runInContext(handler('  async function downloadEvidence(', '  async function rerun('),context);
  await context.downloadEvidence(job);
  assert.equal(requests.length,3);
  assert.deepEqual(exports[0],{name:'corca-diagnosis-id.json',data:{diagnosis:job,evidence,review,uncertainty}});
  context.api=async()=>{throw new Error('Connection lost');};
  await context.downloadEvidence(job);
  assert.equal(exports.length,1);
  assert.match(notices[0],/Evidence download failed: Connection lost/);
});

function formContext(config, topologyDirty = false) {
  const elements = new Map();
  const routes = [];
  const context = vm.createContext({
    config, topologyDirty,
    $: id => {
      if (!elements.has(id)) elements.set(id, {
        hidden: true, value: '2026-10-29T12:00',
        setAttribute() {}, focus() {}, scrollIntoView() {},
      });
      return elements.get(id);
    },
    navigate: view => routes.push(view),
    renderDiagnosisSetup() {}, renderDetail() {},
  });
  vm.runInContext(handler('  function openForm()', '  function closeForm()'), context);
  context.openForm();
  return {routes, elements};
}

test('new diagnosis stays on Diagnoses when telemetry or sensitivity is missing', () => {
  for (const config of [{prometheus: false, methods: []}, {prometheus: true, methods: []}]) {
    assert.deepEqual(formContext(config).routes, ['diagnoses']);
  }
});

test('unsaved service changes show in-place guidance instead of redirecting', () => {
  assert.deepEqual(formContext({prometheus: true, methods: ['baro']}, true).routes, ['diagnoses']);
});

test('configured workspace opens the incident form', () => {
  const {routes, elements} = formContext({prometheus: true, methods: ['baro']});
  assert.deepEqual(routes, ['diagnoses']);
  assert.equal(elements.get('new-form').hidden, false);
});

test('responder diagnosis prerequisites explain administrator setup and allow read-only navigation',()=>{
  for(const source of [false,true]){
    const controls={},routes=[];
    const context=vm.createContext({config:{prometheus:source,methods:[]},topologyDirty:false,sessionInfo:{user:{role:'responder'}},$:id=>controls[id]??=( {} ),navigate:view=>routes.push(view)});
    vm.runInContext(handler('  function renderDiagnosisSetup()', '  const views ='),context);
    context.renderDiagnosisSetup();
    assert.match(controls['diagnosis-setup-description'].textContent,/administrator needs to finish/);
    assert.equal(controls['diagnosis-setup-action'].textContent,source?'View service setup':'View connection');
    controls['diagnosis-setup-action'].onclick();
    assert.deepEqual(routes,[source?'services':'integrations']);
    context.sessionInfo.user.role='administrator';context.renderDiagnosisSetup();
    assert.equal(controls['diagnosis-setup-action'].textContent,source?'Review service setup':'Connect telemetry');
  }
});

test('search shortcut only captures slash when the search is visible and the user is not editing',()=>{
  let listener,focuses=0,prevented=0,visible=true,wide=true,helpOpen=false;
  const context=vm.createContext({document:{addEventListener:(_event,fn)=>listener=fn},matchMedia:()=>({matches:wide}),$:id=>id==='help-drawer'?{open:helpOpen}:{getClientRects:()=>visible?[{}]:[],focus:()=>focuses++}});
  vm.runInContext(handler('  document.addEventListener("keydown",event=>{if(event.key==="/"', '  $("job-search").addEventListener'),context);
  const press=target=>listener({key:'/',target:target||{tagName:'DIV'},preventDefault:()=>prevented++});
  visible=false;press();assert.equal(focuses,0);assert.equal(prevented,0);
  visible=true;wide=false;press();assert.equal(focuses,0);
  wide=true;helpOpen=true;press();assert.equal(focuses,0);
  helpOpen=false;press({tagName:'INPUT'});press({tagName:'DIV',isContentEditable:true});assert.equal(focuses,0);
  press();assert.equal(focuses,1);assert.equal(prevented,1);
});

test('each route selects its own page and records navigable history', () => {
  const views = JSON.parse(html.match(/const views = (\[[^\n]+\]);/)[1]);
  const pages = Object.fromEntries(views.map(view => ['view-' + view, {hidden: view !== 'overview'}]));
  Object.assign(pages, {'help-drawer':{open:false},'breadcrumb-group':{},'breadcrumb-page':{}});
  const links = views.map(view => ({dataset: {view}, attributes: {},
    setAttribute(key, value) { this.attributes[key] = value; },
    removeAttribute(key) { delete this.attributes[key]; }, closest() { return true; },
  }));
  const urls = [];
  const location = {search: '?view=overview', hash: ''};
  const context = vm.createContext({
    views, selected: null, config: null, discovery: null, location, helpOriginView:"overview", refreshAlerts() {}, loadHelp() {}, loadHomeAlerts() {}, loadTeam() {}, loadQuality() {}, openHelp() { pages["help-drawer"].open=true; },
    $: id => pages[id],
    document: {querySelectorAll: () => links},
    window: {scrollTo() {}},
    history: {pushState(_state, _title, url) { urls.push(url); location.search = url; }},
  });
  vm.runInContext(handler('  function navigate(', '  function restoreLocation()'), context);
  const routes = ['diagnoses', 'alerts', 'logs', 'services', 'integrations', 'team', 'quality', 'about'];
  for (const view of routes) {
    context.navigate(view);
    assert.equal(pages['view-' + view].hidden, false);
    assert.equal(views.filter(view => !pages["view-"+view].hidden).length, 1);
    assert.equal(links.find(link => link.dataset.view === view).attributes['aria-current'], 'page');
  }
  assert.deepEqual(urls, routes.map(view => '?view=' + view));
  assert.equal(pages['breadcrumb-page'].textContent, 'About CoRCA');
  context.navigate('services', false); // popstate restores a route without adding history
  assert.equal(pages['view-services'].hidden, false);
  assert.equal(urls.length, routes.length);
  context.navigate("guide");
  assert.equal(pages["help-drawer"].open,true);
  assert.equal(pages["view-services"].hidden,false,"help retains the underlying screen");
  assert.equal(urls.length,routes.length,"opening contextual help does not replace the route");
});

test('overview prioritizes the first incomplete prerequisite over existing history', () => {
  const context = vm.createContext({});
  vm.runInContext(handler('  function nextWorkspaceStep(', '  function renderOverview()'), context);
  const step = context.nextWorkspaceStep;
  const metadata = {prometheus: {topology: 'custom'}};
  assert.equal(step(null, null, false).complete, 0);
  assert.equal(step({prometheus: false, methods: []}, metadata, false).view, 'integrations');
  assert.equal(step({prometheus: true, methods: []}, {prometheus: {topology: 'default'}}, false).complete, 1);
  assert.equal(step({prometheus: true, methods: []}, metadata, false).complete, 2);
  assert.equal(step({prometheus: true, methods: ['baro']}, metadata, false).complete, 3);
  assert.equal(step({prometheus: true, methods: ['baro']}, metadata, true).complete, 1);
  assert.equal(step({prometheus:true,methods:['baro']},null,false).view,'integrations','unavailable metadata is not treated as disconnected monitoring');
});


test('invalid metric JSON opens optional settings and preserves the form', () => {
  const elements = new Map([
    ['source-provider', {value:'prometheus'}], ['source-url', {value:'http://localhost:9090'}],
    ['source-queries', {value:'{bad', focus() { this.focused = true; }}],
    ['source-metric-settings', {open:false}],
  ]);
  const context = vm.createContext({URL, $: id => elements.get(id)});
  vm.runInContext(handler('  function readSource()', '  $("download-source").addEventListener'), context);
  assert.throws(() => context.readSource(), /valid JSON in Metric settings/);
  assert.equal(elements.get('source-metric-settings').open, true);
  assert.equal(elements.get('source-queries').focused, true);
  assert.equal(elements.get('source-queries').value, '{bad');
});

test('provider cards open matching setup, show saved state, and preserve a reselected draft', () => {
  const names = ['prometheus', 'grafana', 'datadog', 'dynatrace', 'newrelic'];
  const cards = names.map(provider => ({dataset:{provider}, state:{},
    setAttribute(key, value) { this[key] = value; },
    querySelector() { return this.state; },
    addEventListener(_name, action) { this.click = action; },
  }));
  const summary = {focus() { this.focused = true; }};
  const controls = {'source-provider':{value:'prometheus'}, 'source-setup':{hidden:true, open:false, querySelector:()=>summary, scrollIntoView() {}}};
  let resets = 0;
  const context = vm.createContext({selectedConnectionProvider:null, connectionFormLoaded:false,
    integration:{provider:'prometheus',prometheus:{configured:true}}, providerName:name=>name,
    document:{querySelectorAll:()=>cards}, $:id=>controls[id], configureProvider:()=>{resets++;},
  });
  vm.runInContext(handler('  function renderProviderCards()', '  function configureProvider()'), context);
  cards[2].click();
  assert.equal(controls['source-provider'].value, 'datadog');
  assert.equal(controls['source-setup'].hidden, false);
  assert.equal(summary.textContent, 'Connect datadog');
  assert.equal(cards[2]['aria-pressed'], 'true');
  assert.equal(cards[0].state.textContent, 'Connected');
  assert.equal(resets, 1);
  cards[2].click();
  assert.equal(resets, 1, 'reselecting does not discard the customer’s draft');
});

test('Alertmanager setup waits for authentication metadata and includes resolved delivery', () => {
  const controls = new Map();
  const element = id => {
    if (!controls.has(id)) controls.set(id, {value:'https://corca.example.com'});
    return controls.get(id);
  };
  const context = vm.createContext({URL, alertsReceiver:null, $:element});
  vm.runInContext(handler('  function renderAlertConfiguration()', '  function renderAlerts('), context);
  assert.equal(context.renderAlertConfiguration(), false);
  assert.equal(element('copy-alert').disabled, true);
  context.alertsReceiver = {requires_auth:true};
  assert.equal(context.renderAlertConfiguration(), true);
  assert.match(element('alert-snippet').textContent, /https:\/\/corca.example.com\/v1\/alerts/);
  assert.match(element('alert-snippet').textContent, /send_resolved: true/);
  assert.match(element('alert-snippet').textContent, /credentials_file: \/etc\/alertmanager\/corca-token/);
  element('alerts-base-url').value = 'https://user:secret@corca.example.com';
  assert.equal(context.renderAlertConfiguration(), false);
  assert.equal(element('copy-alert').disabled, true);
  assert.equal(element('alert-snippet').textContent, '');
  assert.ok(!element('alerts-config-error').textContent.includes('secret'));
});

test('Alerts prioritizes firing events and keeps setup out of the main workflow', () => {
  const controls = new Map();
  const element = id => {
    if (!controls.has(id)) controls.set(id, {value:'', replaceChildren(...children) { this.children = children; }});
    return controls.get(id);
  };
  element('alerts-filter').value = 'firing';
  const context = vm.createContext({alertsReceiver:null, $:element,
    h:(tag,attrs,...children)=>({tag,attrs,children}), document:{querySelectorAll:()=>[]}, fmtTime:()=> 'today', fmtClock:()=> '12:00', renderAlertConfiguration() {},
  });
  vm.runInContext(handler('  function renderAlerts(', '  function applyAlertResult('), context);
  const data = {delivery:{last_external_at:null,firing:0,resolved:0},alerts:[],matching:0,diagnoses_ready:false};
  context.renderAlerts(data);
  assert.equal(element('alerts-receiver-title').textContent, 'Connect your alerts');
  assert.equal(element('alerts-delivery-state').textContent, 'Awaiting first alert');
  assert.match(element('alerts-receiver-detail').textContent, /separate connections/);
  assert.equal(element('alerts-diagnosis-setup').hidden, true);
  assert.equal(element('alerts-list-controls').hidden, true);
  data.delivery = {last_external_at:1,firing:1,resolved:2};
  context.renderAlerts(data);
  assert.equal(element('alerts-receiver-title').textContent, '1 firing alert');
  assert.equal(element('alerts-connect').textContent, 'Connection settings');
  assert.equal(element('alerts-diagnosis-setup').hidden, false);
  data.delivery.firing = 0; data.diagnoses_ready = true;
  context.renderAlerts(data);
  assert.equal(element('alerts-receiver-title').textContent, 'No firing alerts');
  assert.equal(element('alerts-diagnosis-setup').hidden, true);
  assert.match(JSON.stringify(element('alerts-feed').children), /Choose All alerts/);
  element('alerts-search').value = 'missing';context.renderAlerts(data);
  assert.match(JSON.stringify(element('alerts-feed').children), /No alerts match your search/);
  data.alerts=[{name:'Missing end time',service:'cart',severity:'info',status:'resolved',starts_at:1,ends_at:null}];
  context.renderAlerts(data);
  assert.match(JSON.stringify(element('alerts-feed').children), /Not reported/);
  assert.doesNotMatch(JSON.stringify(element('alerts-feed').children), /Since 12:00/);
  data.alerts[0].status='firing';context.renderAlerts(data);
  assert.match(JSON.stringify(element('alerts-feed').children), /Since 12:00/);
});

test('empty evaluations distinguish diagnosis results from published quality evidence', () => {
  function h(tag, props, ...children) {
    return {tag,props:props || {},children:children.flat(),append(...more){this.children.push(...more);}};
  }
  const routes=[];
  const context=vm.createContext({h,navigate:view=>routes.push(view)});
  vm.runInContext(handler('  function renderQualityEmpty(', '  async function loadQuality('),context);
  const diagnosis=context.renderQualityEmpty('diagnosis');
  assert.match(JSON.stringify(diagnosis),/confirmed outcomes/);
  assert.match(JSON.stringify(diagnosis),/publish a benchmark report/);
  const action=diagnosis.children.find(child=>child.tag==='button');
  assert.equal(action.children[0],'View diagnoses');
  action.props.onclick();
  assert.deepEqual(routes,['diagnoses']);
});

test('Quality charts keep score scales, missing evidence, and check counts distinct', () => {
  function h(tag, props, ...children) {
    return {tag, props:props || {}, children:children.flat().filter(child=>child!=null && child!==false),
      append(...more) { this.children.push(...more); }};
  }
  const context=vm.createContext({h,qualityMetricNames:{complete_exact_match:'Complete cause match',healthy_false_positive:'Healthy false alarms',mean_f1:'F1',unavailable_rate:'Unavailable'}});
  vm.runInContext(handler('  function formatQualityMetric(', '  async function loadQuality('),context);
  const chart=context.renderQualityCharts({case_count:188,metrics:[
    {name:'complete_exact_match',value:.8,samples:10},{name:'healthy_false_positive',value:0,samples:5},
    {name:'mean_f1',value:.75,samples:10},{name:'unavailable_rate',value:null,samples:0},
    {name:'response_seconds',value:2.5,samples:10}],gates:[{status:'pass'},{status:'pass'},{status:'fail'},{status:'not_run'}]});
  function nodes(node) { return typeof node==='object' && node ? [node,...node.children.flatMap(nodes)] : []; }
  const all=nodes(chart);
  assert.equal(all.filter(node=>node.props.class==='quality-track').length,3,'missing measurements and seconds are not plotted as rates');
  assert.deepEqual(all.filter(node=>node.props.class==='quality-fill').map(node=>node.props.style),['width:80%','width:0%','width:75%']);
  const donut=all.find(node=>node.props.class==='quality-donut');
  assert.match(donut.props['aria-label'],/4 declared checks: 2 passed, 1 failed, 1 not run/);
  assert.match(donut.props.style,/50%.*75%.*100%/);
  assert.equal(context.formatQualityMetric({name:'mean_f1',value:.75}),'0.750');
  assert.equal(context.formatQualityMetric({name:'response_seconds',value:2.5}),'2.50 s');
  assert.equal(context.formatQualityMetric({name:'mean_f1',value:null}),'Not measured');
  const missing=context.renderQualityCharts({metrics:[{name:'mean_f1',value:null,samples:0}],gates:[]});
  assert.equal(nodes(missing).filter(node=>['quality-track','quality-donut'].includes(node.props.class)).length,0);
  assert.match(JSON.stringify(missing),/No acceptance checks declared/);
});

test('alert polling buffers changes, honors pause, and applies explicit refreshes', async () => {
  const controls = new Map();
  const element = id => {
    if (!controls.has(id)) controls.set(id,{hidden:false,value:''});
    return controls.get(id);
  };
  element('alerts-filter').value='firing';
  let current={alerts:[{id:'first'}]}, reads=0;
  const rendered=[];
  const context=vm.createContext({
    alertsTimer:null,alertsSequence:0,alertsSnapshot:'',alertsReceiver:null,alertsPaused:false,alertsQuery:'',pendingAlerts:null,
    $:element,document:{hidden:false},URLSearchParams,JSON,Date,
    clearTimeout() {},setTimeout() {return 1;},fmtClock:()=> '12:00',
    api:async()=>{reads++;return current;},renderAlerts:data=>rendered.push(data),
  });
  vm.runInContext(handler('  function applyAlertResult(', '  $("alerts-apply").addEventListener'),context);
  await context.refreshAlerts();
  assert.equal(rendered.length,1);
  assert.match(element('alerts-poll-state').textContent,/Receiver ready/);
  current={alerts:[{id:'second'},{id:'first'}]};
  await context.refreshAlerts();
  assert.equal(rendered.length,1,'background polling keeps visible rows stable');
  assert.equal(element('alerts-apply').hidden,false);
  assert.equal(context.pendingAlerts.result.alerts.length,2);
  context.applyAlertResult(context.pendingAlerts.result,context.pendingAlerts.snapshot,context.pendingAlerts.query);
  assert.equal(rendered.length,2);
  assert.equal(element('alerts-apply').hidden,true);
  context.alertsPaused=true;
  await context.refreshAlerts();
  assert.equal(reads,2,'paused polling does not issue requests');
  current={alerts:[]};
  await context.refreshAlerts(true);
  assert.equal(rendered.length,3,'a manual refresh remains available while paused');
  assert.equal(context.pendingAlerts,null);
});

test('an older alert response cannot overwrite a newer filter result', async () => {
  const controls=new Map();const element=id=>{if(!controls.has(id))controls.set(id,{hidden:false,value:''});return controls.get(id);};
  element('alerts-filter').value='firing';
  let releaseOld;
  const old=new Promise(resolve=>{releaseOld=resolve;});let calls=0;
  const rendered=[];
  const context=vm.createContext({alertsTimer:null,alertsSequence:0,alertsSnapshot:'',alertsPaused:false,alertsQuery:'',pendingAlerts:null,
    $:element,document:{hidden:false},URLSearchParams,JSON,Date,clearTimeout(){},setTimeout(){return 1;},fmtClock:()=>'',
    api:()=>++calls===1 ? old : Promise.resolve({alerts:[{id:'resolved-result'}]}),renderAlerts:data=>rendered.push(data),
  });
  vm.runInContext(handler('  function applyAlertResult(', '  $("alerts-apply").addEventListener'),context);
  const first=context.refreshAlerts();
  element('alerts-filter').value='resolved';await context.refreshAlerts(true);
  releaseOld({alerts:[{id:'stale-firing-result'}]});await first;
  assert.equal(rendered.length,1);
  assert.equal(rendered[0].alerts[0].id,'resolved-result');
  assert.equal(context.pendingAlerts,null);
});

test('log chart preserves empty time buckets and reports zero counts honestly', () => {
  function h(tag,props,...children) {return {tag,props:props||{},children:children.flat(),append(...more){this.children.push(...more);},setAttribute(key,value){this.props[key]=value;},replaceChildren(...more){this.children=more;}};}
  const target={replaceChildren(...children){this.children=children;}};
  const context=vm.createContext({h,$:()=>target,fmtTime:String,fmtClock:String});
  vm.runInContext(handler('  function renderLogChart(', '  function renderLogReport('),context);
  const report={timeline:[{start:0,errors:3},{start:600,errors:1}],timeline_interval_seconds:60,comparison:{incident_start:660}};
  context.renderLogChart(report);
  function nodes(node){return typeof node==='object' && node ? [node,...node.children.flatMap(nodes)] : [];}
  let all=target.children.flatMap(nodes);
  const bars=all.filter(node=>node.props.class==='log-bar');
  assert.equal(bars.length,11,'two observed buckets ten minutes apart retain the intervening nine minutes');
  assert.equal(bars.filter(node=>node.props.style==='height:0%').length,9);
  assert.equal(all.find(node=>node.props.class==='log-incident').props.style,'left:100%','the marker stays at the real incident time');
  const slider=all.find(node=>node.props.class==='chart-scrubber');
  slider.props.oninput({target:{value:'10'}});
  assert.equal(slider.value,'10');
  assert.match(slider.props['aria-valuetext'],/1 errors/);
  const bucketButtons=all.filter(node=>node.props.class==='log-bucket');
  assert.equal(bucketButtons.at(-1).props['aria-pressed'],'true');
  assert.equal(bucketButtons[0].props['aria-pressed'],'false');
  assert.equal(bars[0].props.style,'height:100%','selection never changes the plotted counts');
  report.timeline=[{start:0,errors:0}];report.comparison=null;
  context.renderLogChart(report);all=target.children.flatMap(nodes);
  assert.equal(all.filter(node=>node.props.class==='log-bars').length,0);
  assert.match(JSON.stringify(target),/One time bucket recorded/);
  assert.ok(all.some(node=>node.tag==='dd' && node.children.includes('0')));
});

test('log service highlights include the highest error count and preserve the exact table order',()=>{
  function h(tag,props,...children){return {tag,props:props||{},children:children.flat().filter(v=>v!=null),append(...more){this.children.push(...more);}};}
  const controls=new Map(),element=id=>{
    if(!controls.has(id))controls.set(id,{replaceChildren(...children){this.children=children;this.firstElementChild=children[0];}});
    return controls.get(id);
  };
  const services=Array.from({length:6},(_,i)=>({name:'busy-'+i,entries:1000,errors:0,warnings:0}));
  services.push({name:'failing-service',entries:50,errors:50,warnings:0});
  const order=services.map(s=>s.name);
  const context=vm.createContext({h,$:element,renderLogChart(){},logTable:(headings,rows)=>h('table',{},JSON.stringify(rows)),fmtTime:String});
  vm.runInContext(handler('  function renderLogReport(', '  $("logs-file").addEventListener'),context);
  context.renderLogReport({filename:'test.log',entries:6050,errors:50,warnings:0,notes:[],services,patterns:[],timestamped:6050,timeline_interval_seconds:60,timeline:[]});
  const bars=element('logs-services').children[0];
  assert.match(JSON.stringify(bars),/failing-service/);
  assert.equal(bars.children.filter(node=>node.props?.class==='service-error').length,5);
  assert.deepEqual(services.map(s=>s.name),order);
});

test('outcome reviews start unconfirmed and save independent labels without hidden causes', async () => {
  function h(tag,props,...children) {
    return {tag,props:props || {},children:children.flat().filter(Boolean),events:{},isConnected:true,
      value:'',hidden:false,addEventListener(name,fn){this.events[name]=fn;},
      replaceChildren(...nodes){this.children=nodes;},append(...nodes){this.children.push(...nodes);},prepend(...nodes){this.children.unshift(...nodes);},focus(){}};
  }
  const host=h('div'),requests=[];
  let saved=null,reviewPage;
  const context=vm.createContext({h,encodeURIComponent,Promise,
    api:async (url,options)=>{
      if(options){requests.push(JSON.parse(options.body));if(saved)throw Error('Another reviewer changed this outcome. Reload before saving.');return {};}
      return url.endsWith('/review') ? {review:saved} : {method:'baro',inject_time:1000,result:{root_causes:['candidate']}};
    },fmtTime:()=> 'incident time',busy(){},toast(){},$:()=>({hidden:false}),qualityReviewState:{before:10},loadOutcomeEvaluations:async before=>{reviewPage=before;}});
  vm.runInContext(handler('  async function openOutcomeReview(', '  async function loadOutcomeEvaluations('),context);
  await context.openOutcomeReview('abc',host);
  function all(node){return [node,...node.children.filter(child=>typeof child==='object').flatMap(all)];}
  let nodes=all(host),outcome=nodes.find(n=>n.props['aria-label']==='Confirmed outcome');
  let causes=nodes.find(n=>n.props['aria-label']==='Confirmed causes');
  assert.equal(outcome.value,'unconfirmed');assert.equal(causes.disabled,true);
  outcome.value='fault';outcome.events.change();assert.equal(causes.required,true);
  causes.value='confirmed-db\nmissing-service';
  outcome.value='healthy';outcome.events.change();
  nodes.find(n=>n.props['aria-label']==='Evidence source').value='controlled_drill';
  nodes.find(n=>n.props['aria-label']==='Confirmation evidence').value='Verified independently after a drill.';
  await nodes.find(n=>n.tag==='form').events.submit({preventDefault(){}});
  assert.deepEqual(requests[0].root_causes,[]);assert.equal(requests[0].outcome,'healthy');
  assert.equal(requests[0].revision,0);
  assert.equal(reviewPage,10,'saving a review retains the current queue page');
  saved={outcome:'fault',root_causes:['actual-db'],evidence_kind:'real_incident',evidence:'Verified postmortem record.',revision:3,reviewer:'Reviewer',reviewed_at:1000};
  await context.openOutcomeReview('abc',host);nodes=all(host);
  assert.equal(nodes.find(n=>n.props['aria-label']==='Confirmed causes').value,'actual-db');
  await nodes.find(n=>n.tag==='form').events.submit({preventDefault(){}});
  assert.equal(requests[1].revision,3);
  assert.match(nodes.find(n=>n.props.role==='alert').textContent,/Another reviewer/);
  assert.equal(nodes.find(n=>n.props['aria-label']==='Confirmation evidence').value,'Verified postmortem record.');
});


test('review queue pagination stays visible and detection metrics have a direct entry',async()=>{
  function h(tag,props={},...children){return {tag,props,children:children.flat().filter(c=>c!=null),events:{},isConnected:true,
    addEventListener(name,fn){this.events[name]=fn;},replaceChildren(...items){this.children=items;},append(...items){this.children.push(...items);},scrollIntoView(){this.scrolled=true;}};}
  const host=h('div'),requests=[],reviewed=[];
  const context=vm.createContext({h,$:id=>id==='quality-kind'?{value:'diagnosis'}:host,outcomeSequence:0,qualityReviewState:{open:false,before:null},qualityDashboardState:{},qualityCaseReports:[],updateOutcomeDashboard:null,
    api:async path=>{requests.push(path);return {cohorts:[],pending_count:2,pending:[{id:path.includes('before=')?'older':'latest',method:'baro',inject_time:1}],recent:[],next_before:path.includes('before=')?null:10};},
    statusOf:()=>({label:'Done'}),fmtTime:String,openOutcomeReview:async id=>reviewed.push(id),CoRCAVisuals:{evaluations(){}},
  });
  vm.runInContext(handler('  async function loadOutcomeEvaluations(', '  function renderQualityEmpty('),context);
  const nodes=node=>typeof node==='object'&&node?[node,...node.children.flatMap(nodes)]:[];
  const button=text=>nodes(host).find(n=>n.tag==='button'&&n.children.includes(text));
  await context.loadOutcomeEvaluations();
  button('Detection metrics').props.onclick();
  let detection=nodes(host).find(n=>n.tag==='details'&&n.children.some(c=>c?.tag==='summary'&&c.children.includes('Cause-set F1, anomaly detection & false alarms')));
  assert.equal(detection.open,true);assert.equal(detection.scrolled,true);
  assert.match(JSON.stringify(detection),/No confirmed outcomes yet/);
  button('Review incidents').props.onclick();
  await button('Earlier unconfirmed diagnoses').props.onclick();
  const tools=nodes(host).find(n=>n.props.class==='ref-eval-evidence');
  assert.equal(tools.props.hidden,false);assert.equal(tools.props.open,true);
  assert.equal(context.qualityReviewState.before,10);assert.match(requests.at(-1),/before=10/);
  await button('Review outcome').props.onclick();assert.deepEqual(reviewed,['older']);
});

test('evaluation refresh retains loaded history and requests only diagnosis reports',async()=>{
  function h(tag,props={},...children){return {tag,props,children,append(...items){this.children.push(...items);}};}
  const list={replaceChildren(){},append(){}},requests=[];
  const report=id=>({id,kind:'diagnosis',cases:[{case_id:'one'}],name:'run '+id,metrics:[],gates:[],limitations:[],evidence_kind:'synthetic',case_count:1,completed_at:'2026-11-01T00:00:00Z'});
  let reply={runs:[report(1)],next_before:20};
  const context=vm.createContext({h,$:id=>id==='quality-runs'?list:{},loadOutcomeEvaluations(){},qualityCaseReports:[],qualityBefore:null,qualitySequence:0,updateOutcomeDashboard:null,
    api:async path=>{requests.push(path);return reply;},renderQualityCharts:()=>h('div'),qualityMetricNames:{},fmtTime:String,renderQualityEmpty:()=>h('div')});
  vm.runInContext(handler('  async function loadQuality(', '  $("quality-refresh")'),context);
  await context.loadQuality();
  reply={runs:[report(2)],next_before:null};await context.loadQuality(true);
  assert.deepEqual(Array.from(context.qualityCaseReports,r=>r.id),['published:2','published:1']);
  assert.deepEqual(requests,['/v1/evaluations?kind=diagnosis','/v1/evaluations?kind=diagnosis&before=20']);
  assert.doesNotMatch(html,/quality-tab-help|quality-tab-release|id="quality-kind"/);
});

test('customer evaluation upload rejects non-diagnosis reports before publication',async()=>{
  for(const kind of ['help','release']){
    const controls={},requests=[];let submit;
    const context=vm.createContext({$:id=>controls[id]??=(id==='quality-file'?{files:[{size:50,text:async()=>JSON.stringify({kind})}]}:{addEventListener:(_,fn)=>submit=fn}),busy(){},api:async(...args)=>requests.push(args)});
    vm.runInContext(handler('  $("quality-upload-form").addEventListener', '  /* ---------- individual access'),context);
    await submit({preventDefault(){}});
    assert.equal(requests.length,0);assert.match(controls['quality-error'].textContent,/Choose a diagnosis evaluation report/);
  }
});
