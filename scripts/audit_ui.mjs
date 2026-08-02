import puppeteer from 'puppeteer-core';

const baseUrl = (process.argv[2] || 'http://127.0.0.1:4173').replace(/\/$/, '');
const readOnly = process.argv.includes('--read-only');
const executablePath = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const views = ['overview', 'integrations', 'automations', 'builder', 'intelligence', 'reports', 'alerts'];
const browser = await puppeteer.launch({executablePath, headless:true, args:['--no-sandbox','--disable-setuid-sandbox','--disable-background-networking']});
const failures = [];

async function openPage(view) {
  const page = await browser.newPage();
  page.on('console', message => { if (message.type() === 'error') failures.push(`${view} console: ${message.text()}`); });
  page.on('pageerror', error => failures.push(`${view} pageerror: ${error.message}`));
  page.on('requestfailed', request => failures.push(`${view} request: ${request.url()} ${request.failure()?.errorText}`));
  await page.setViewport({width:1600,height:1200,deviceScaleFactor:1});
  await page.goto(`${baseUrl}/?view=${view}`, {waitUntil:'networkidle0',timeout:20_000});
  await page.waitForSelector('#view .card, #view .metric-ribbon', {timeout:10_000});
  const audit = await page.evaluate(() => ({
    title: document.querySelector('#page-title')?.textContent,
    loader: Boolean(document.querySelector('#view > .initial-loader')),
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    alertCount: document.querySelector('#nav-alert-count')?.textContent,
    textLength: document.querySelector('#view')?.innerText.length || 0,
  }));
  if (audit.loader || audit.overflow > 1 || audit.textLength < 80 || !audit.alertCount) failures.push(`${view} layout: ${JSON.stringify(audit)}`);
  return page;
}

try {
  for (const view of views) {
    const page = await openPage(view);
    await page.close();
  }

  if (!readOnly) {
  const builder = await openPage('builder');
  await builder.click('#builder-new');
  await builder.waitForSelector('#builder-name');
  await builder.type('#builder-name', 'UI audit workflow');
  await builder.type('#builder-description', 'Created and reordered through the live form-based workflow builder.');
  await builder.click('#builder-add-step');
  await builder.waitForSelector('[data-builder-step]:nth-child(2)');
  await builder.click('[data-builder-step]:first-child [data-move-step][data-direction="1"]');
  await builder.waitForSelector('[data-builder-step]:nth-child(2)');
  const saveResponse = builder.waitForResponse(response => response.url().endsWith('/api/workflows') && response.request().method() === 'POST');
  await builder.click('#builder-save');
  if ((await saveResponse).status() !== 201) failures.push('builder create did not return 201');
  await builder.waitForFunction(() => document.querySelector('#toast-stack')?.innerText.includes('Workflow saved'));
  await builder.close();

  const automations = await openPage('automations');
  const retryResponse = automations.waitForResponse(response => /\/api\/workflows\/\d+\/run$/.test(response.url()));
  await automations.click('[data-test-retry]');
  if ((await retryResponse).status() !== 201) failures.push('retry drill did not return 201');
  await automations.waitForFunction(() => document.querySelector('#toast-stack')?.innerText.includes('Retry policy verified'));
  await automations.close();

  const alerts = await openPage('alerts');
  const transitionButton = await alerts.$('[data-transition-alert]');
  if (transitionButton) {
    const transitionResponse = alerts.waitForResponse(response => /\/api\/alerts\/\d+\/transition$/.test(response.url()));
    await transitionButton.click();
    if ((await transitionResponse).status() !== 200) failures.push('alert transition did not return 200');
    await alerts.waitForSelector('#mute-source');
  }
  await alerts.click('#mute-source', {clickCount:3});
  await alerts.type('#mute-source', 'UI audit*');
  const muteResponse = alerts.waitForResponse(response => response.url().endsWith('/api/alerts/mutes') && response.request().method() === 'POST');
  await alerts.click('#mute-form button');
  if ((await muteResponse).status() !== 201) failures.push('mute create did not return 201');
  await alerts.close();
  }
} finally {
  await browser.close();
}

if (failures.length) {
  console.error('UI AUDIT FAILED');
  failures.forEach(failure => console.error(`  ${failure}`));
  process.exit(1);
}
console.log(`UI AUDIT PASSED · ${views.length} views${readOnly ? ' · read-only release pass' : ' + builder/retry/lifecycle/mute interactions'}`);
