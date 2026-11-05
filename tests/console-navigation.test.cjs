// Exercise the actual console handlers without a browser or extra dependencies.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const html = readFileSync('runtime/web/app.html', 'utf8');

function handler(start, end) {
  const from = html.indexOf(start);
  const to = html.indexOf(end, from);
  assert.ok(from >= 0 && to > from, 'Console handler boundaries exist');
  return html.slice(from, to);
}

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
    renderDiagnosisSetup() {},
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

test('each route selects its own page and records navigable history', () => {
  const views = ['overview', 'diagnoses', 'alerts', 'logs', 'services', 'integrations', 'guide', 'team', 'quality'];
  const pages = Object.fromEntries(views.map(view => ['view-' + view, {hidden: view !== 'overview'}]));
  const links = views.map(view => ({dataset: {view}, attributes: {},
    setAttribute(key, value) { this.attributes[key] = value; },
    removeAttribute(key) { delete this.attributes[key]; },
  }));
  const urls = [];
  const location = {search: '?view=overview', hash: ''};
  const context = vm.createContext({
    views, selected: null, config: null, discovery: null, location, helpOriginView:"overview", refreshAlerts() {}, loadHelp() {}, loadTeam() {}, loadQuality() {},
    $: id => pages[id],
    document: {querySelectorAll: () => links},
    window: {scrollTo() {}},
    history: {pushState(_state, _title, url) { urls.push(url); location.search = url; }},
  });
  vm.runInContext(handler('  function navigate(', '  function restoreLocation()'), context);
  for (const view of ['diagnoses', 'alerts', 'logs', 'services', 'integrations', 'guide', 'team', 'quality']) {
    context.navigate(view);
    assert.equal(pages['view-' + view].hidden, false);
    assert.equal(Object.values(pages).filter(page => !page.hidden).length, 1);
    assert.equal(links.find(link => link.dataset.view === view).attributes['aria-current'], 'page');
  }
  assert.deepEqual(urls, ['?view=diagnoses', '?view=alerts', '?view=logs', '?view=services', '?view=integrations', '?view=guide', '?view=team', '?view=quality']);
  context.navigate('services', false); // popstate restores a route without adding history
  assert.equal(pages['view-services'].hidden, false);
  assert.equal(urls.length, 8);
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
    h:(tag,attrs,...children)=>({tag,attrs,children}), fmtTime:()=> 'today', renderAlertConfiguration() {},
  });
  vm.runInContext(handler('  function renderAlerts(', '  async function refreshAlerts()'), context);
  const data = {delivery:{last_external_at:null,firing:0,resolved:0},alerts:[],matching:0,diagnoses_ready:false};
  context.renderAlerts(data);
  assert.equal(element('alerts-receiver-title').textContent, 'Connect your alerts');
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
