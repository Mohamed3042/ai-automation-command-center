import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import puppeteer from 'puppeteer-core';

const root = path.resolve(import.meta.dirname, '..');
const outputDir = path.join(root, 'examples');
const baseUrl = (process.argv[2] || 'http://127.0.0.1:4173').replace(/\/$/, '');
const executablePath = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const views = ['overview', 'integrations', 'automations', 'builder', 'intelligence', 'reports', 'alerts'];

fs.mkdirSync(outputDir, { recursive: true });
const browser = await puppeteer.launch({
  executablePath,
  headless: true,
  args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-background-networking'],
});

try {
  for (const view of views) {
    const page = await browser.newPage();
    await page.setViewport({ width: 1600, height: 1200, deviceScaleFactor: 1 });
    await page.goto(`${baseUrl}/?view=${view}&capture=1`, { waitUntil: 'networkidle0', timeout: 20_000 });
    const documentHeight = await page.evaluate(() => document.documentElement.scrollHeight);
    await page.setViewport({ width: 1600, height: Math.max(1200, Math.min(2200, documentHeight)), deviceScaleFactor: 1 });
    await page.evaluate(() => { window.scrollTo(0, 0); });
    await new Promise(resolve => setTimeout(resolve, 350));
    await page.screenshot({ path: path.join(outputDir, `${view}.png`), fullPage: false });
    await page.close();
    console.log(`Captured examples/${view}.png`);
  }

  const reportDir = path.join(root, 'data', 'reports');
  const weeklyReports = fs.readdirSync(reportDir).filter(name => name.startsWith('weekly-') && name.endsWith('.html')).sort();
  if (weeklyReports.length) {
    const report = weeklyReports.at(-1);
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 980, deviceScaleFactor: 1 });
    await page.goto(pathToFileURL(path.join(reportDir, report)).href, { waitUntil: 'load' });
    await page.screenshot({ path: path.join(outputDir, 'generated-weekly-report.png'), fullPage: false });
    await page.close();
    console.log('Captured examples/generated-weekly-report.png');
  }
} finally {
  await browser.close();
}
