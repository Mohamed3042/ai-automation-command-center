const state = {
  view: new URLSearchParams(location.search).get('view') || 'overview',
  dashboardDays: 7,
  store: 'All stores',
  selectedWorkflow: 1,
  builderWorkflow: 1,
  builderDraft: null,
  selectedAlert: null,
};

const pageMeta = {
  overview: ['Operations pulse', 'Executive overview'],
  integrations: ['System fabric', 'Integration control plane'],
  automations: ['Workflow engine', 'Automation operations'],
  builder: ['Design studio', 'Workflow builder'],
  intelligence: ['Decision layer', 'AI intelligence'],
  reports: ['Scheduled outputs', 'Reports & distribution'],
  alerts: ['Response center', 'Alerts & escalation'],
};

const colors = ['#5b4ce8', '#0d9f88', '#2386dc', '#e99a12', '#e14c7b', '#8995a5', '#8355c7'];

function safe(value) {
  return String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
}

function money(value, compact = false) {
  if (compact) return new Intl.NumberFormat('en-US', {style:'currency', currency:'USD', notation:'compact', maximumFractionDigits:1}).format(value);
  return new Intl.NumberFormat('en-US', {style:'currency', currency:'USD', maximumFractionDigits:0}).format(value);
}

function number(value) { return new Intl.NumberFormat('en-US').format(Math.round(value)); }
function pct(value) { return `${Number(value).toFixed(1)}%`; }
function relativeTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  return date.toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit'});
}
function fileSize(value) { return value < 1024 ? `${value} B` : `${(value / 1024).toFixed(1)} KB`; }

function icon(name) {
  const paths = {
    revenue: '<path d="M4 19V9m5 10V5m5 14v-7m5 7V3"/>',
    orders: '<path d="M6 7h12l-1 14H7L6 7Zm3 0a3 3 0 0 1 6 0"/>',
    margin: '<circle cx="12" cy="12" r="9"/><path d="m8 15 3-3 2 2 4-5"/>',
    automation: '<path d="M13 2 4.5 13H11l-1 9 9-12h-7l1-8Z"/>',
    refresh: '<path d="M20 6v5h-5M4 18v-5h5"/><path d="M18.5 9A7 7 0 0 0 6 6.5L4 9m16 6-2 2.5A7 7 0 0 1 5.5 15"/>',
    play: '<path d="m8 5 11 7-11 7V5Z"/>',
    report: '<path d="M6 2h9l4 4v16H6z"/><path d="M14 2v5h5M9 12h7M9 16h7"/>',
    ai: '<path d="M9.5 4.5A3.5 3.5 0 0 0 6 8v1a3 3 0 0 0-2 2.8A3.2 3.2 0 0 0 7.2 15H8v1.5A3.5 3.5 0 0 0 11.5 20V4.5h-2ZM14.5 4.5A3.5 3.5 0 0 1 18 8v1a3 3 0 0 1 2 2.8 3.2 3.2 0 0 1-3.2 3.2H16v1.5a3.5 3.5 0 0 1-3.5 3.5V4.5h2Z"/>',
    bell: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9ZM10 21h4"/>',
    link: '<path d="M9 15 15 9M7 17l-2 2a3 3 0 0 1-4-4l4-4a3 3 0 0 1 4 0M17 7l2-2a3 3 0 1 1 4 4l-4 4a3 3 0 0 1-4 0"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v6l4 2"/>',
    download: '<path d="M12 3v12m-4-4 4 4 4-4M5 21h14"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
  };
  return `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.check}</svg>`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {headers: {'Content-Type':'application/json'}, ...options});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

function trend(delta, inverse = false) {
  const positive = inverse ? delta < 0 : delta > 0;
  const direction = delta === 0 ? 'neutral' : positive ? 'up' : 'down';
  const arrow = delta === 0 ? '•' : delta > 0 ? '↑' : '↓';
  return `<span class="trend ${direction}">${arrow} ${Math.abs(delta).toFixed(1)}%</span>`;
}

function statusPill(status) { return `<span class="status-pill ${safe(status.toLowerCase().replaceAll(' ', '-'))}">${safe(status)}</span>`; }
function badge(value) { return `<span class="badge ${safe(value.toLowerCase())}">${safe(value)}</span>`; }

function statCard(kpi) {
  const value = kpi.format === 'currency' ? money(kpi.value, true) : kpi.format === 'percent' ? pct(kpi.value) : number(kpi.value);
  return `<article class="card stat-card">
    <div class="stat-top"><span class="stat-label">${safe(kpi.label)}</span><span class="stat-icon">${icon(kpi.id)}</span></div>
    <div class="stat-value">${value}</div>
    <div class="stat-meta">${trend(kpi.delta)}<span>${safe(kpi.note)}</span></div>
  </article>`;
}

function lineChart(points, key = 'revenue', forecast = false) {
  if (!points.length) return '<div class="empty-state">No data in this period</div>';
  const width = 680, height = 210, left = 47, right = 14, top = 15, bottom = 30;
  const values = points.map(point => Number(point[key]));
  if (forecast) points.forEach(point => values.push(Number(point.lower), Number(point.upper)));
  const min = Math.min(...values) * .96, max = Math.max(...values) * 1.04;
  const x = index => left + index * (width - left - right) / Math.max(1, points.length - 1);
  const y = value => top + (max - value) * (height - top - bottom) / Math.max(1, max - min);
  const path = points.map((point, index) => `${index ? 'L' : 'M'} ${x(index).toFixed(1)} ${y(point[key]).toFixed(1)}`).join(' ');
  const area = `${path} L ${x(points.length - 1)} ${height - bottom} L ${x(0)} ${height - bottom} Z`;
  const grid = [0,1,2,3].map(index => {
    const gy = top + index * (height - top - bottom) / 3;
    const value = max - index * (max - min) / 3;
    return `<line class="chart-grid" x1="${left}" x2="${width-right}" y1="${gy}" y2="${gy}"/><text class="axis-label" x="0" y="${gy+3}">${money(value,true)}</text>`;
  }).join('');
  const labels = points.map((point,index) => index % Math.max(1, Math.ceil(points.length/6)) === 0 || index === points.length-1 ? `<text class="axis-label" text-anchor="middle" x="${x(index)}" y="${height-7}">${new Date(point.date+'T00:00:00').toLocaleDateString('en-US',{month:'short',day:'numeric'})}</text>` : '').join('');
  const dots = points.map((point,index) => `<circle class="chart-dot" cx="${x(index)}" cy="${y(point[key])}" r="2.7"><title>${point.date}: ${money(point[key])}</title></circle>`).join('');
  let band = '';
  if (forecast) {
    const upper = points.map((point,index) => `${index ? 'L' : 'M'} ${x(index)} ${y(point.upper)}`).join(' ');
    const lower = [...points].reverse().map((point,rev) => { const index = points.length-1-rev; return `L ${x(index)} ${y(point.lower)}`; }).join(' ');
    band = `<path d="${upper} ${lower} Z" fill="#dedafd" opacity=".58" stroke="none"/>`;
  }
  return `<svg class="chart-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none"><defs><linearGradient id="areaGradient" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#695aee" stop-opacity=".22"/><stop offset="1" stop-color="#695aee" stop-opacity="0"/></linearGradient></defs>${grid}${band}<path class="chart-area" d="${area}"/><path class="chart-line" d="${path}"/>${dots}${labels}</svg>`;
}

function viewToolbar(title, subtitle, actions = '') {
  return `<div class="view-toolbar"><div class="view-intro"><h2>${title}</h2><p>${subtitle}</p></div><div class="toolbar-actions">${actions}</div></div>`;
}

async function renderOverview() {
  const data = await api(`/api/dashboard?days=${state.dashboardDays}&store=${encodeURIComponent(state.store)}`);
  document.getElementById('nav-alert-count').textContent = data.open_alerts;
  const maxStoreRevenue = Math.max(...data.stores.map(item => item.revenue));
  const totalCategory = data.categories.reduce((sum,item) => sum + item.revenue, 0);
  const actions = `<select class="select-control" id="store-filter">${data.stores_filter.map(store => `<option ${store===state.store?'selected':''}>${safe(store)}</option>`).join('')}</select><select class="select-control" id="period-filter"><option value="1" ${state.dashboardDays===1?'selected':''}>Today</option><option value="7" ${state.dashboardDays===7?'selected':''}>Last 7 days</option><option value="30" ${state.dashboardDays===30?'selected':''}>Last 30 days</option></select>`;
  return `${viewToolbar('Today’s operating picture', `Governed metrics across commerce, finance, support, and store operations · refreshed ${data.data_freshness}`, actions)}
    <section class="stat-grid">${data.kpis.map(statCard).join('')}</section>
    <section class="two-column">
      <article class="card"><header class="card-header"><div class="card-title"><h3>Revenue trajectory</h3><p>Net channel sales · ${safe(data.scope.period_start)} to ${safe(data.scope.period_end)}</p></div><div class="legend"><span><i></i> Revenue</span></div></header><div class="chart-wrap">${lineChart(data.timeline)}</div></article>
      <article class="card"><header class="card-header"><div class="card-title"><h3>Category mix</h3><p>Contribution to gross revenue</p></div><button class="link-action" data-view="intelligence">Explore signals →</button></header><div class="donut-body"><div class="donut"><div class="donut-label"><strong>${money(totalCategory,true)}</strong><span>gross sales</span></div></div><div class="category-list">${data.categories.slice(0,6).map((item,index) => `<div class="category-row"><i style="background:${colors[index]}"></i><span>${safe(item.category)}</span><strong>${Math.round(item.revenue/totalCategory*100)}%</strong></div>`).join('')}</div></div></article>
    </section>
    <section class="two-column">
      <article class="card table-wrap"><header class="card-header"><div class="card-title"><h3>Store & channel performance</h3><p>Click a row to narrow the dashboard</p></div><button class="link-action" data-view="reports">Open reporting →</button></header><table class="data-table"><thead><tr><th>Location</th><th>Revenue contribution</th><th class="numeric">Orders</th><th class="numeric">Margin</th><th class="numeric">Return rate</th></tr></thead><tbody>${data.stores.map((item,index) => `<tr data-store="${safe(item.store)}"><td><div class="store-name"><span class="store-rank">${index+1}</span><strong>${safe(item.store)}</strong></div></td><td class="progress-cell"><div class="progress-track"><span style="width:${item.revenue/maxStoreRevenue*100}%"></span></div></td><td class="numeric">${number(item.orders)}</td><td class="numeric"><strong>${pct(item.margin_pct)}</strong></td><td class="numeric">${pct(item.return_rate)}</td></tr>`).join('')}</tbody></table></article>
      <article class="card"><header class="card-header"><div class="card-title"><h3>Operational activity</h3><p>Auditable machine and operator events</p></div><button class="link-action" data-view="automations">View runs →</button></header><div class="event-list">${data.events.slice(0,5).map(event => `<div class="event-item"><strong>${safe(event.title)}</strong><p>${safe(event.detail)}</p><time>${safe(event.actor)} · ${relativeTime(event.created_at)}</time></div>`).join('')}</div></article>
    </section>`;
}

async function renderIntegrations() {
  const data = await api('/api/connectors');
  document.getElementById('nav-connector-count').textContent = data.metrics.connected;
  const ribbon = [
    ['Systems connected', data.metrics.connected, 'Six operational domains'],
    ['Healthy now', `${data.metrics.healthy}/${data.metrics.connected}`, 'One connector degraded'],
    ['Events today', number(data.metrics.events_today), 'Normalized records'],
    ['Average uptime', pct(data.metrics.average_uptime), 'Trailing 30 days'],
  ];
  return `${viewToolbar('Connected systems, one operating model', 'Live contract health, throughput, latency, and sync controls across the retail stack', `<button class="button" id="sync-all">${icon('refresh')} Sync all</button>`)}
    <section class="metric-ribbon">${ribbon.map(item => `<div class="ribbon-stat"><span>${item[0]}</span><strong>${item[1]}</strong><small>${item[2]}</small></div>`).join('')}</section>
    <article class="card"><header class="card-header"><div class="card-title"><h3>Live integration map</h3><p>${data.contracts.join(' · ')}</p></div><div class="legend"><span><i style="background:#16a36a"></i> Healthy</span><span><i style="background:#e99a12"></i> Degraded</span></div></header><div class="integration-map"><svg class="map-lines" viewBox="0 0 1000 410" preserveAspectRatio="none"><path d="M500 205 C360 205 330 72 150 72M500 205 C640 205 670 72 850 72M500 205 C330 205 320 205 140 205M500 205 C670 205 680 205 860 205M500 205 C360 205 350 342 180 342M500 205 C640 205 650 342 820 342"/><circle cx="360" cy="133" r="4"/><circle cx="640" cy="133" r="4"/><circle cx="340" cy="205" r="4"/><circle cx="660" cy="205" r="4"/><circle cx="370" cy="301" r="4"/><circle cx="630" cy="301" r="4"/></svg><div class="map-hub"><div><div class="brand-mark"><span></span><span></span><span></span></div><strong>RelayOps Core</strong><small>Event + data plane</small></div></div>${data.connectors.map((item,index) => `<div class="map-node n${index+1}"><div class="connector-icon" style="background:${safe(item.color)}">${safe(item.name.split(' ').map(x=>x[0]).join('').slice(0,2))}</div><div><strong>${safe(item.name)}</strong><small>${safe(item.category)} · ${number(item.records_today)} events</small></div><span class="health-dot ${safe(item.status)}"></span></div>`).join('')}</div></article>
    <section class="connector-grid">${data.connectors.map(item => `<article class="card connector-card"><div class="connector-card-top"><div class="connector-icon" style="background:${safe(item.color)}">${safe(item.name.split(' ').map(x=>x[0]).join('').slice(0,2))}</div><div class="connector-card-name"><strong>${safe(item.name)}</strong><span>${safe(item.system)} · ${safe(item.mode)}</span></div>${statusPill(item.status)}</div><div class="connector-stats"><div><span>Uptime</span><strong>${pct(item.uptime)}</strong></div><div><span>Latency</span><strong>${number(item.latency_ms)} ms</strong></div><div><span>Error rate</span><strong>${pct(item.error_rate)}</strong></div></div><div class="connector-foot"><span>Synced ${relativeTime(item.last_sync)}</span><button class="button subtle" data-sync="${item.id}">${icon('refresh')} Sync</button></div></article>`).join('')}</section>`;
}

async function renderAutomations() {
  const [data, scheduler] = await Promise.all([api('/api/workflows'), api('/api/scheduler')]);
  let selected = data.workflows.find(item => item.id === state.selectedWorkflow) || data.workflows[0];
  const hours = data.metrics.hours_saved;
  const ribbon = [
    ['Active workflows', data.metrics.active, `${data.workflows.length} persisted definitions`],
    ['Runs · 30 days', number(data.metrics.runs_30d), 'Observed executions only'],
    ['Observed success', pct(data.metrics.success_rate), data.metrics.runs_30d ? 'Measured run outcomes' : 'Awaiting first run'],
    ['Time returned', `${hours.hours}h`, `${hours.minutes} calculated minutes`],
  ];
  const nextJob = scheduler.jobs[0];
  return `${viewToolbar('Automations that execute real work', 'SQLite actions, measured runtimes, bounded retries, cron dispatch, and truthful row counts', `<button class="button" data-test-retry="${selected.id}" data-action="${safe(selected.steps[0].action)}">Test retry</button><button class="button primary" data-run="${selected.id}">${icon('play')} Run selected</button>`)}
    <section class="metric-ribbon">${ribbon.map(item => `<div class="ribbon-stat"><span>${item[0]}</span><strong>${item[1]}</strong><small>${item[2]}</small></div>`).join('')}</section>
    <article class="scheduler-strip"><div><span class="pulse-dot"></span><strong>Scheduler ${safe(scheduler.state.status)}</strong><small>Last tick ${scheduler.state.last_tick_at ? relativeTime(scheduler.state.last_tick_at) : 'pending'} · ${scheduler.state.jobs_fired} jobs fired</small></div><div><span>Next scheduled job</span><strong>${nextJob ? safe(nextJob.name) : 'No active schedules'}</strong><small>${nextJob?.next_run ? new Date(nextJob.next_run).toLocaleString() : '—'}${nextJob ? ` · ${safe(nextJob.cron)}` : ''}</small></div></article>
    <section class="workflow-layout">
      <article class="card"><header class="card-header"><div class="card-title"><h3>Workflow catalog</h3><p>${data.workflows.length} persisted automations · select to inspect</p></div><button class="button subtle" data-view="builder">Open builder</button></header><div class="workflow-list">${data.workflows.map(workflow => `<div class="workflow-card ${workflow.id===selected.id?'selected':''}" data-workflow="${workflow.id}"><div class="workflow-card-icon">${icon('automation')}</div><div><h4>${safe(workflow.name)}</h4><p>${safe(workflow.description)}</p><div class="workflow-meta"><span>${icon('clock')} ${safe(workflow.trigger)}</span><span>${workflow.steps.length} executable steps</span><span>${pct(workflow.success_rate)} observed</span></div></div><div class="workflow-actions">${statusPill(workflow.active ? 'healthy' : 'paused')}<br><button class="button subtle" data-run="${workflow.id}">${icon('play')} Run</button><button class="button subtle" data-toggle-workflow="${workflow.id}" data-active="${workflow.active?0:1}">${workflow.active?'Pause':'Enable'}</button></div></div>`).join('')}</div></article>
      <div>
        <article class="card"><header class="card-header"><div class="card-title"><h3>${safe(selected.name)}</h3><p>${safe(selected.failure_policy)}</p></div><button class="button subtle" data-edit-workflow="${selected.id}">Edit</button></header><div class="flow-canvas">${selected.steps.map(step => `<div class="flow-step"><span class="flow-step-num">${step.position}</span><div><strong>${safe(step.name)}</strong><span>${safe(step.connector_slug || 'RelayOps core')} · ${step.manual_minutes} manual min/${safe(step.estimate_basis.replace('_',' '))}</span></div><code>${safe(step.action)} · retry ${step.retry_limit}×</code></div>`).join('')}</div></article>
        <article class="card" style="margin-top:15px"><header class="card-header"><div class="card-title"><h3>Latest executions</h3><p>Measured clocks, actual records, and attempts</p></div></header><div class="run-list">${data.recent_runs.slice(0,7).map(run => `<div class="run-row"><div><strong>${safe(run.workflow_name)}</strong><span>${safe(run.run_key)} · ${number(run.records_processed)} row results</span></div><div class="run-row-right">${statusPill(run.status)}<small>${run.duration_ms == null ? 'running' : `${Number(run.duration_ms).toFixed(3)}ms`} · ${run.retries} retries</small></div></div>`).join('') || '<div class="empty-state">No executions yet. Run a workflow to create evidence.</div>'}</div></article>
      </div>
    </section>`;
}

function newWorkflowDraft() {
  return {id:null,name:'',description:'',owner:'Operations',active:true,trigger_type:'manual',schedule_cron:'',steps:[{name:'New executable step',action:'metrics.increment',connector_slug:'',config:{},retry_limit:2,retry_backoff_ms:5,manual_minutes:1,estimate_basis:'per_run'}]};
}

function collectBuilderDraft() {
  const steps = [...document.querySelectorAll('[data-builder-step]')].map(row => ({
    id: row.dataset.stepId ? Number(row.dataset.stepId) : undefined,
    name: row.querySelector('[data-field="name"]').value,
    action: row.querySelector('[data-field="action"]').value,
    connector_slug: row.querySelector('[data-field="connector_slug"]').value || null,
    config: JSON.parse(row.querySelector('[data-field="config"]').value || '{}'),
    retry_limit: Number(row.querySelector('[data-field="retry_limit"]').value),
    retry_backoff_ms: Number(row.querySelector('[data-field="retry_backoff_ms"]').value),
    manual_minutes: Number(row.querySelector('[data-field="manual_minutes"]').value),
    estimate_basis: row.querySelector('[data-field="estimate_basis"]').value,
  }));
  return {id:state.builderWorkflow==='new'?null:Number(state.builderWorkflow),name:document.getElementById('builder-name').value,description:document.getElementById('builder-description').value,owner:document.getElementById('builder-owner').value,active:document.getElementById('builder-active').checked,trigger_type:document.getElementById('builder-trigger').value,schedule_cron:document.getElementById('builder-cron').value,steps};
}

async function renderBuilder() {
  const data = await api('/api/workflows');
  const persisted = data.workflows.find(item => item.id === Number(state.builderWorkflow));
  const draft = state.builderDraft || persisted || newWorkflowDraft();
  const actionOptions = action => data.actions.map(item => `<option value="${safe(item.id)}" ${item.id===action?'selected':''}>${safe(item.id)}</option>`).join('');
  return `${viewToolbar('Build production workflows without leaving RelayOps', 'Definitions, ordering, retry policy, schedules, and manual-effort assumptions persist in SQLite', `<button class="button" id="builder-new">New workflow</button><button class="button primary" id="builder-save">Save workflow</button>`)}
    <section class="builder-layout"><article class="card builder-settings"><header class="card-header"><div class="card-title"><h3>Workflow definition</h3><p>Every action maps to executable application code</p></div>${statusPill(draft.active?'healthy':'paused')}</header><div class="builder-form">
      <label>Workflow<select id="builder-select"><option value="new" ${state.builderWorkflow==='new'?'selected':''}>New workflow</option>${data.workflows.map(item=>`<option value="${item.id}" ${item.id===Number(state.builderWorkflow)?'selected':''}>${safe(item.name)}</option>`).join('')}</select></label>
      <label>Name<input id="builder-name" value="${safe(draft.name)}" required></label><label>Owner<input id="builder-owner" value="${safe(draft.owner)}"></label>
      <label class="wide">Description<textarea id="builder-description" rows="3">${safe(draft.description)}</textarea></label>
      <label>Trigger<select id="builder-trigger"><option value="manual" ${draft.trigger_type==='manual'?'selected':''}>Manual</option><option value="event" ${draft.trigger_type==='event'?'selected':''}>Event</option><option value="cron" ${draft.trigger_type==='cron'?'selected':''}>Cron schedule</option></select></label>
      <label>Cron (UTC)<input id="builder-cron" value="${safe(draft.schedule_cron||'')}" placeholder="*/30 * * * *"></label>
      <label class="check-field"><input id="builder-active" type="checkbox" ${draft.active?'checked':''}> Enabled for event and scheduled execution</label>
    </div></article>
    <article class="card builder-steps"><header class="card-header"><div class="card-title"><h3>Executable steps</h3><p>Drag-free deterministic ordering · use arrows to reorder</p></div><button class="button subtle" id="builder-add-step">+ Add step</button></header><div class="step-editor-list">${draft.steps.map((step,index)=>`<div class="step-editor" data-builder-step data-step-id="${step.id||''}"><div class="step-editor-order"><strong>${index+1}</strong><button data-move-step="${index}" data-direction="-1" ${index===0?'disabled':''}>↑</button><button data-move-step="${index}" data-direction="1" ${index===draft.steps.length-1?'disabled':''}>↓</button></div><div class="step-editor-fields"><label>Name<input data-field="name" value="${safe(step.name)}"></label><label>Action<select data-field="action">${actionOptions(step.action)}</select></label><label>Connector slug<input data-field="connector_slug" value="${safe(step.connector_slug||'')}" placeholder="optional"></label><label>Retry limit<input data-field="retry_limit" type="number" min="0" max="5" value="${step.retry_limit}"></label><label>Backoff ms<input data-field="retry_backoff_ms" type="number" min="0" max="5000" value="${step.retry_backoff_ms}"></label><label>Manual minutes<input data-field="manual_minutes" type="number" min="0" step="0.05" value="${step.manual_minutes}"></label><label>Estimate basis<select data-field="estimate_basis"><option value="per_record" ${step.estimate_basis==='per_record'?'selected':''}>Per record</option><option value="per_run" ${step.estimate_basis==='per_run'?'selected':''}>Per run</option></select></label><label class="wide">Action config (JSON)<input data-field="config" value="${safe(JSON.stringify(step.config||{}))}"></label></div><button class="step-remove" data-remove-step="${index}" aria-label="Remove step">×</button></div>`).join('')}</div></article></section>`;
}

async function renderIntelligence() {
  const data = await api('/api/intelligence');
  const forecast = data.forecast;
  const support = data.support;
  const firstRisk = forecast.inventory_risks[0];
  return `${viewToolbar('AI that earns its place in operations', 'Explainable models turn governed business data into exceptions, forecasts, and routed work', `<button class="button primary" id="refresh-ai">${icon('refresh')} Refresh models</button>`)}
    <div class="ai-banner"><div class="ai-orb">${icon('ai')}</div><div><strong>${safe(data.adapter.label)} is active</strong><p>${data.adapter.configured ? `Hosted classification is configured; deterministic fallback remains available${data.adapter.last_error ? ` · last fallback: ${safe(data.adapter.last_error)}` : ''}.` : 'Set RELAYOPS_LLM_API_KEY to activate the hosted OpenAI-compatible path; the deterministic fallback needs no key.'}</p></div><code>${safe(data.adapter.active)} · ${safe(data.adapter.model)}</code></div>
    <section class="three-column">
      <article class="card"><header class="card-header"><div class="card-title"><h3>Sales anomaly detection</h3><p>Robust deviation from 14-day baselines</p></div><span class="badge">${data.anomalies.length} signals</span></header><div class="anomaly-list">${data.anomalies.slice(0,4).map(item => `<div class="anomaly-row"><div><strong>${safe(item.store)} · ${safe(item.category)}</strong><p>${safe(item.model)}</p></div><div class="anomaly-value"><strong class="trend ${item.change_pct>0?'up':'down'}">${item.change_pct>0?'+':''}${pct(item.change_pct)}</strong><span>${money(item.actual)} actual</span></div></div>`).join('') || '<div class="empty-state">No significant anomalies</div>'}</div></article>
      <article class="card"><header class="card-header"><div class="card-title"><h3>Demand forecast</h3><p>${safe(forecast.model)}</p></div><span class="status-pill healthy">${pct(forecast.confidence*100)} confidence</span></header><div class="forecast-panel"><div class="forecast-summary"><div><strong>${money(forecast.projected_revenue,true)}</strong><span>Projected 14-day revenue</span></div><span class="trend ${forecast.trend_pct >= 0 ? 'up':'down'}">${forecast.trend_pct>=0?'↑':'↓'} ${Math.abs(forecast.trend_pct)}%</span></div><div class="forecast-chart">${lineChart(forecast.points,'revenue',true)}</div>${firstRisk ? `<div class="inventory-risk"><span>Highest inventory risk</span><strong>${safe(firstRisk.name)} · ${firstRisk.cover_days} days cover</strong></div>`:''}</div></article>
      <article class="card"><header class="card-header"><div class="card-title"><h3>Support auto-triage</h3><p>${safe(support.adapter)}</p></div><span class="status-pill healthy">Routing live</span></header><div class="metric-ribbon" style="border:0;box-shadow:none;grid-template-columns:1fr 1fr;margin:0"><div class="ribbon-stat"><span>Auto-routed</span><strong>${pct(support.metrics.automation_rate)}</strong><small>${support.metrics.auto_routed} of ${support.metrics.classified} cases</small></div><div class="ribbon-stat"><span>Median response</span><strong>${support.metrics.median_first_response_minutes}m</strong><small>Seeded resolved cases</small></div></div><div class="anomaly-list">${support.categories.map((item,index) => `<div class="anomaly-row"><div><strong>${safe(item.category)}</strong><p>Intent queue</p></div><div class="anomaly-value"><strong>${item.count}</strong><span>${Math.round(item.count/support.metrics.classified*100)}% of inbox</span></div></div>`).join('')}</div></article>
    </section>
    <article class="card table-wrap"><header class="card-header"><div class="card-title"><h3>Auto-categorized support inbox</h3><p>Category, confidence, priority, and routing state remain operator-visible</p></div><span class="badge">${support.tickets.length} conversations</span></header><table class="data-table"><thead><tr><th>Conversation</th><th>Channel</th><th>AI category</th><th class="numeric">Confidence</th><th>Priority</th><th>Status</th></tr></thead><tbody>${support.tickets.slice(0,8).map(ticket => `<tr><td><strong>${safe(ticket.subject)}</strong><br><span style="color:#8a94a4">${safe(ticket.customer)}</span></td><td>${safe(ticket.channel)}</td><td>${badge(ticket.category)}</td><td class="numeric">${ticket.confidence_pct}%</td><td>${statusPill(ticket.priority)}</td><td>${statusPill(ticket.status)}</td></tr>`).join('')}</tbody></table></article>`;
}

async function renderReports() {
  const data = await api('/api/reports');
  return `${viewToolbar('Scheduled reports with real artifacts', 'Daily and weekly outputs render from governed data, persist to disk, and retain delivery metadata', `<select id="report-type" class="select-control"><option value="daily">Daily trading brief</option><option value="weekly">Weekly operations review</option><option value="inventory">Inventory exception pack</option></select><button class="button primary" id="generate-report">${icon('report')} Generate now</button>`)}
    <section class="metric-ribbon"><div class="ribbon-stat"><span>Active schedules</span><strong>${data.metrics.active_schedules}</strong><small>Daily, weekly, inventory</small></div><div class="ribbon-stat"><span>Artifacts retained</span><strong>${data.metrics.generated_30d}</strong><small>HTML + CSV written to disk</small></div><div class="ribbon-stat"><span>Scheduler success</span><strong>${data.metrics.scheduler_success_rate==null?'—':pct(data.metrics.scheduler_success_rate)}</strong><small>${data.metrics.scheduled_runs} observed scheduled runs</small></div><div class="ribbon-stat"><span>Storage used</span><strong>${fileSize(data.metrics.storage_bytes)}</strong><small>Local report vault</small></div></section>
    <section class="report-schedules">${data.schedules.map(schedule => `<article class="card schedule-card"><div class="schedule-top"><div class="schedule-icon">${icon('clock')}</div><span class="toggle ${schedule.active?'active':''}"></span></div><h3>${safe(schedule.name)}</h3><p>${safe(schedule.frequency)} · ${safe(schedule.recipients)}</p><div class="schedule-meta"><div><span>Next run</span><strong>${new Date(schedule.next_run).toLocaleString('en-US',{weekday:'short',hour:'2-digit',minute:'2-digit'})}</strong></div><div><span>Cron · ${safe(schedule.timezone)}</span><strong>${safe(schedule.cron_expr)}</strong></div></div></article>`).join('')}</section>
    <article class="card table-wrap"><header class="card-header"><div class="card-title"><h3>Generated report library</h3><p>Downloadable files written by schedules, workflows, and operators</p></div><span class="status-pill healthy">Vault available</span></header><table class="data-table"><thead><tr><th>Report</th><th>Period</th><th>Created / source</th><th class="numeric">Rows</th><th class="numeric">Size</th><th class="numeric">Action</th></tr></thead><tbody>${data.reports.map(report => `<tr><td><div class="file-cell"><span class="file-icon">${safe(report.format)}</span><div><strong>${safe(report.name)}</strong><span>${safe(report.path)}</span></div></div></td><td>${safe(report.period_start)} → ${safe(report.period_end)}</td><td>${new Date(report.created_at).toLocaleString('en-US',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}<br><span style="color:#8a94a4">${safe(report.generated_by)}</span></td><td class="numeric">${report.row_count}</td><td class="numeric">${fileSize(report.size_bytes)}</td><td class="numeric"><a class="button subtle" href="/api/reports/download/${encodeURIComponent(report.path)}">${icon('download')} Download</a></td></tr>`).join('')}</tbody></table></article>`;
}

async function renderAlerts() {
  const data = await api('/api/alerts');
  const openCount = Object.values(data.counts).reduce((sum,value)=>sum+value,0);
  const selected = data.alerts.find(item=>item.id===state.selectedAlert) || data.alerts[0];
  if (selected) state.selectedAlert = selected.id;
  document.getElementById('nav-alert-count').textContent = openCount;
  return `${viewToolbar('Exceptions routed to accountable owners', 'Strict acknowledgement, investigation, resolution, mute, and scheduled escalation controls', `<button class="button primary" id="evaluate-alerts">${icon('bell')} Evaluate rules</button>`)}
    <section class="metric-ribbon"><div class="ribbon-stat"><span>Active alerts</span><strong>${openCount}</strong><small>${data.lifecycle.acknowledged} acknowledged</small></div><div class="ribbon-stat"><span>Investigating</span><strong>${data.lifecycle.investigating}</strong><small>Owned active work</small></div><div class="ribbon-stat"><span>Resolved</span><strong>${data.lifecycle.resolved}</strong><small>Retained audit history</small></div><div class="ribbon-stat"><span>Delivery receipts</span><strong>${data.deliveries.length}</strong><small>All escalation attempts</small></div></section>
    <section class="alert-layout"><article class="card"><header class="card-header"><div class="card-title"><h3>Exception queue</h3><p>Select an alert to inspect its lifecycle</p></div><span class="status-pill healthy">Escalation active</span></header><div class="alert-list">${data.alerts.map(alert => `<div class="alert-item ${alert.id===selected?.id?'selected':''}" data-alert-select="${alert.id}"><span class="severity-bar ${safe(alert.severity)}"></span><div class="alert-copy"><strong>${safe(alert.title)}</strong><p>${safe(alert.message)}</p><div class="alert-meta"><span>${safe(alert.source)}</span><span>${statusPill(alert.status)}</span><span>Level ${alert.escalation_level}</span>${alert.muted_until?`<span>Muted until ${relativeTime(alert.muted_until)}</span>`:alert.escalates_at?`<span>Escalates ${relativeTime(alert.escalates_at)}</span>`:''}</div></div><div class="alert-actions">${statusPill(alert.severity)}<br>${alert.next_status?`<button class="button subtle" data-transition-alert="${alert.id}" data-status="${safe(alert.next_status)}">${icon('check')} ${safe(alert.next_status)}</button>`:''}</div></div>`).join('')}</div></article><div>
      <article class="card"><header class="card-header"><div class="card-title"><h3>${selected?safe(selected.title):'Escalation timeline'}</h3><p>Immutable state, delivery, mute, and escalation events</p></div>${selected?statusPill(selected.status):''}</header><div class="timeline-list">${selected?.timeline.map(item=>`<div class="timeline-item"><i></i><div><strong>${safe(item.event_type.replaceAll('_',' '))}</strong><p>${safe(item.detail)}</p><time>${safe(item.actor)} · ${new Date(item.created_at).toLocaleString()}</time></div></div>`).join('')||'<div class="empty-state">Select an alert to see its timeline.</div>'}</div></article>
      <article class="card" style="margin-top:15px"><header class="card-header"><div class="card-title"><h3>Mute policy</h3><p>Suppress matching deliveries without hiding alerts</p></div></header><form class="mute-form" id="mute-form"><input id="mute-source" value="Inventory*" placeholder="Source pattern"><select id="mute-severity"><option value="*">All severity</option><option>critical</option><option>high</option><option>medium</option><option>low</option></select><input id="mute-duration" type="number" value="60" min="1" max="10080" aria-label="Duration minutes"><input id="mute-reason" value="Planned maintenance" placeholder="Reason"><button class="button" type="submit">Create mute</button></form><div class="mute-list">${data.mutes.map(mute=>`<div><span>${safe(mute.source_pattern)} · ${safe(mute.severity)}</span><strong>${mute.active?'Active':'Disabled'} until ${relativeTime(mute.ends_at)}</strong><button class="link-action" data-toggle-mute="${mute.id}" data-active="${mute.active?0:1}">${mute.active?'Disable':'Enable'}</button></div>`).join('')||'<p>No mute rules configured.</p>'}</div></article>
    </div></section>`;
}

const renderers = {overview:renderOverview, integrations:renderIntegrations, automations:renderAutomations, builder:renderBuilder, intelligence:renderIntelligence, reports:renderReports, alerts:renderAlerts};

async function render() {
  window.scrollTo(0, 0);
  if (!renderers[state.view]) state.view = 'overview';
  document.querySelectorAll('.nav-item').forEach(item => item.classList.toggle('active', item.dataset.view === state.view));
  document.getElementById('eyebrow').textContent = pageMeta[state.view][0];
  document.getElementById('page-title').textContent = pageMeta[state.view][1];
  document.title = `${pageMeta[state.view][1]} — RelayOps`;
  document.getElementById('view').innerHTML = '<div class="initial-loader"><span></span><p>Loading operating picture…</p></div>';
  try {
    const [content, globalAlerts, health] = await Promise.all([renderers[state.view](), api('/api/alerts'), api('/api/health')]);
    document.getElementById('view').innerHTML = content;
    document.getElementById('nav-alert-count').textContent = Object.values(globalAlerts.counts).reduce((sum,value)=>sum+value,0);
    document.getElementById('provider-sidebar-label').textContent = health.llm.configured ? 'Hosted provider configured' : 'Provider fallback ready';
    document.getElementById('provider-sidebar-detail').textContent = health.llm.configured ? `${health.llm.model} · fallback ready` : 'Zero-key offline mode';
    bindControls();
  } catch (error) {
    document.getElementById('view').innerHTML = `<article class="card empty-state"><strong>Unable to load this view</strong><p>${safe(error.message)}</p><button class="button" onclick="render()">Try again</button></article>`;
    toast('View failed to load', error.message, true);
  }
}

function navigate(view) {
  state.view = view;
  const url = new URL(location.href);
  url.searchParams.set('view', view);
  history.pushState({}, '', url);
  window.scrollTo(0,0);
  render();
}

function toast(title, detail, isError = false) {
  const element = document.createElement('div');
  element.className = `toast${isError?' error':''}`;
  element.innerHTML = `<span class="toast-dot"></span><div><strong>${safe(title)}</strong><span>${safe(detail)}</span></div>`;
  document.getElementById('toast-stack').appendChild(element);
  setTimeout(() => element.remove(), 4200);
}

async function buttonTask(button, task) {
  const original = button.innerHTML;
  button.disabled = true;
  button.innerHTML = '<span class="initial-loader" style="min-height:0"><span style="width:14px;height:14px"></span></span>';
  try { await task(); } catch (error) { toast('Action failed', error.message, true); }
  finally { button.disabled = false; button.innerHTML = original; }
}

function bindControls() {
  const period = document.getElementById('period-filter');
  if (period) period.onchange = event => { state.dashboardDays = Number(event.target.value); render(); };
  const store = document.getElementById('store-filter');
  if (store) store.onchange = event => { state.store = event.target.value; render(); };
  const generate = document.getElementById('generate-report');
  if (generate) generate.onclick = () => buttonTask(generate, async () => { const type = document.getElementById('report-type').value; const result = await api('/api/reports/generate',{method:'POST',body:JSON.stringify({report_type:type})}); toast('Report generated', `${result.artifacts.length} files written to the report vault`); await render(); });
  const evaluate = document.getElementById('evaluate-alerts');
  if (evaluate) evaluate.onclick = () => buttonTask(evaluate, async () => { const result = await api('/api/alerts/evaluate',{method:'POST',body:'{}'}); toast('Rules evaluated', `${result.alerts_created} alerts created · ${result.deliveries_recorded} deliveries`); await render(); });
  const refreshAi = document.getElementById('refresh-ai');
  if (refreshAi) refreshAi.onclick = () => buttonTask(refreshAi, async () => { await api('/api/intelligence/refresh',{method:'POST',body:'{}'}); toast('Models refreshed','Anomalies, forecasts, and inbox categories recalculated offline'); await render(); });
  const syncAll = document.getElementById('sync-all');
  if (syncAll) syncAll.onclick = () => buttonTask(syncAll, async () => { const connectors = await api('/api/connectors'); await Promise.all(connectors.connectors.map(item => api(`/api/connectors/${item.id}/sync`,{method:'POST',body:'{}'}))); toast('All systems synchronized','Six connector checkpoints updated'); await render(); });
  const builderSelect = document.getElementById('builder-select');
  if (builderSelect) builderSelect.onchange = event => { state.builderWorkflow = event.target.value === 'new' ? 'new' : Number(event.target.value); state.builderDraft = null; render(); };
  const builderNew = document.getElementById('builder-new');
  if (builderNew) builderNew.onclick = () => { state.builderWorkflow = 'new'; state.builderDraft = newWorkflowDraft(); render(); };
  const builderSave = document.getElementById('builder-save');
  if (builderSave) builderSave.onclick = () => buttonTask(builderSave, async () => { const payload = collectBuilderDraft(); const path = payload.id ? `/api/workflows/${payload.id}` : '/api/workflows'; const saved = await api(path,{method:payload.id?'PUT':'POST',body:JSON.stringify(payload)}); state.builderWorkflow=saved.id; state.builderDraft=null; state.selectedWorkflow=saved.id; toast('Workflow saved',`${saved.steps.length} executable steps persisted`); await render(); });
  const muteForm = document.getElementById('mute-form');
  if (muteForm) muteForm.onsubmit = async event => { event.preventDefault(); const button=muteForm.querySelector('button'); await buttonTask(button,async()=>{await api('/api/alerts/mutes',{method:'POST',body:JSON.stringify({source_pattern:document.getElementById('mute-source').value,severity:document.getElementById('mute-severity').value,duration_minutes:Number(document.getElementById('mute-duration').value),reason:document.getElementById('mute-reason').value})});toast('Mute created','Matching deliveries will pause while alerts remain visible');await render();}); };
}

document.addEventListener('click', async event => {
  const nav = event.target.closest('[data-view]');
  if (nav) { event.preventDefault(); navigate(nav.dataset.view); return; }
  const store = event.target.closest('[data-store]');
  if (store) { state.store = store.dataset.store; render(); return; }
  const workflow = event.target.closest('[data-workflow]');
  if (workflow && !event.target.closest('[data-run],[data-toggle-workflow]')) { state.selectedWorkflow = Number(workflow.dataset.workflow); render(); return; }
  const run = event.target.closest('[data-run]');
  if (run) { await buttonTask(run, async () => { const result = await api(`/api/workflows/${run.dataset.run}/run`,{method:'POST',body:'{}'}); toast('Workflow completed', `${result.records_processed} row results · ${Number(result.duration_ms).toFixed(3)}ms`); await render(); }); return; }
  const retry = event.target.closest('[data-test-retry]');
  if (retry) { await buttonTask(retry, async () => { const result = await api(`/api/workflows/${retry.dataset.testRetry}/run`,{method:'POST',body:JSON.stringify({failure_injection:{action:retry.dataset.action,fail_attempts:1,message:'Operator retry drill'}})}); toast('Retry policy verified',`${result.retries} retry executed · final status ${result.status}`); await render(); }); return; }
  const toggleWorkflow = event.target.closest('[data-toggle-workflow]');
  if (toggleWorkflow) { await buttonTask(toggleWorkflow,async()=>{const active=toggleWorkflow.dataset.active==='1';await api(`/api/workflows/${toggleWorkflow.dataset.toggleWorkflow}/toggle`,{method:'POST',body:JSON.stringify({active})});toast(active?'Workflow enabled':'Workflow paused','Schedule eligibility updated');await render();});return; }
  const editWorkflow = event.target.closest('[data-edit-workflow]');
  if (editWorkflow) { state.builderWorkflow=Number(editWorkflow.dataset.editWorkflow);state.builderDraft=null;navigate('builder');return; }
  const addStep = event.target.closest('#builder-add-step');
  if (addStep) { try { state.builderDraft=collectBuilderDraft(); state.builderDraft.steps.push({name:'New executable step',action:'metrics.increment',connector_slug:'',config:{},retry_limit:2,retry_backoff_ms:5,manual_minutes:1,estimate_basis:'per_run'}); await render(); } catch(error){toast('Invalid step configuration',error.message,true);} return; }
  const moveStep = event.target.closest('[data-move-step]');
  if (moveStep) { try { state.builderDraft=collectBuilderDraft(); const from=Number(moveStep.dataset.moveStep),to=from+Number(moveStep.dataset.direction); const [step]=state.builderDraft.steps.splice(from,1);state.builderDraft.steps.splice(to,0,step);await render(); } catch(error){toast('Invalid step configuration',error.message,true);} return; }
  const removeStep = event.target.closest('[data-remove-step]');
  if (removeStep) { try { state.builderDraft=collectBuilderDraft(); if(state.builderDraft.steps.length===1) throw new Error('A workflow needs at least one step');state.builderDraft.steps.splice(Number(removeStep.dataset.removeStep),1);await render(); } catch(error){toast('Cannot remove step',error.message,true);} return; }
  const sync = event.target.closest('[data-sync]');
  if (sync) { await buttonTask(sync, async () => { const result = await api(`/api/connectors/${sync.dataset.sync}/sync`,{method:'POST',body:'{}'}); toast('Connector synchronized', `${result.connector} · ${result.records_synced} records`); await render(); }); return; }
  const alertSelect = event.target.closest('[data-alert-select]');
  if (alertSelect && !event.target.closest('[data-transition-alert]')) { state.selectedAlert=Number(alertSelect.dataset.alertSelect);await render();return; }
  const transition = event.target.closest('[data-transition-alert]');
  if (transition) { await buttonTask(transition,async()=>{await api(`/api/alerts/${transition.dataset.transitionAlert}/transition`,{method:'POST',body:JSON.stringify({status:transition.dataset.status,note:`Operator moved alert to ${transition.dataset.status}`})});toast('Alert lifecycle advanced',`Status is now ${transition.dataset.status}`);await render();});return; }
  const toggleMute = event.target.closest('[data-toggle-mute]');
  if (toggleMute) { await buttonTask(toggleMute,async()=>{await api(`/api/alerts/mutes/${toggleMute.dataset.toggleMute}/toggle`,{method:'POST',body:JSON.stringify({active:toggleMute.dataset.active==='1'})});toast('Mute policy updated','Scheduler will apply the new rule state');await render();}); }
});

window.addEventListener('popstate', () => { state.view = new URLSearchParams(location.search).get('view') || 'overview'; render(); });
if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
window.scrollTo(0, 0);
render();
