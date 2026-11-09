// Run against the unmodified frontend copy and the actual MySQL-backed API.
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const root = path.resolve(__dirname, '../storage/browser-check');
const modules = process.env.CODEX_BROWSER_MODULES || 'C:/Users/杨/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules';
const runtimeRequire = createRequire(path.join(modules, '_browser_check.cjs'));
const { chromium } = runtimeRequire('playwright');

(async () => {
  const session = JSON.parse(fs.readFileSync(path.join(root, 'session.json'), 'utf8'));
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const context = await browser.newContext({ viewport: { width: 1900, height: 1400 }, acceptDownloads: true });
  const p = await context.newPage();
  const errors = [], requests = [], downloads = [];
  p.on('pageerror', e => errors.push(e.message));
  p.on('response', r => {
    if (r.url().includes('/api/v1/compliance/agent-traces') || r.url().includes('/api/v1/tasks/')) {
      const u = new URL(r.url());
      requests.push({ path: u.pathname, status: r.status(), view: u.searchParams.get('view'), section: u.searchParams.get('section') });
    }
  });
  await p.addInitScript(s => {
    sessionStorage.setItem('accessToken', s.token);
    sessionStorage.setItem('authUser', JSON.stringify(s.user));
  }, session);
  try {
    await p.goto('http://127.0.0.1:1225/compliance/agent-traces?view=tools', { waitUntil: 'networkidle', timeout: 90000 });
    await p.locator('.tools-view').waitFor();
    const toggle = p.locator('.compliance-mode .el-switch');
    if (await toggle.evaluate(e => e.classList.contains('is-checked'))) await toggle.click();
    await p.locator('.tools-result').filter({ hasText: '价值分析' }).waitFor({ timeout: 45000 });
    await p.locator('.tool-table-scroll tbody tr').first().waitFor();
    const rows = await p.locator('.tool-table-scroll tbody tr').count();
    if (rows !== 7) throw new Error('Expected 7 database-backed calls, got ' + rows);
    await p.screenshot({ path: path.join(root, 'tools-real-mysql.png'), fullPage: true });
    const initial = await p.locator('.tools-result').innerText();
    await p.locator('.tool-table-scroll').getByRole('button', { name: 'CALL-01', exact: true }).click();
    await p.waitForFunction(() => document.querySelector('.tool-detail-heading em')?.textContent.includes('CALL-01'));
    await p.getByRole('button', { name: '原始事件', exact: true }).click();
    const drawer = p.getByRole('dialog', { name: '原始事件（脱敏）', exact: true });
    await drawer.waitFor();
    await p.waitForFunction(() => {
      const v = document.querySelector('.el-drawer pre')?.textContent;
      return v && v !== '加载中…' && v.includes('event_id');
    });
    await drawer.locator('.el-drawer__close-btn').click();
    await p.locator('.tools-query .el-select').nth(1).click();
    const options = p.getByRole('option');
    await options.nth(1).waitFor();
    const secondLabel = await options.nth(1).innerText();
    await options.nth(1).click();
    if ((await p.locator('.tools-result').innerText()) !== initial) throw new Error('Draft selection changed applied result');
    await p.getByRole('button', { name: '查询', exact: true }).click();
    await p.waitForFunction(label => document.querySelector('.tools-result')?.textContent.includes(label.split(' ')[0]), secondLabel);
    await p.locator('.tool-table-scroll tbody tr').first().waitFor();
    await p.screenshot({ path: path.join(root, 'tools-second-sample.png'), fullPage: true });

    await p.locator('.tools-query .el-select').nth(1).click();
    const searched = p.waitForResponse(r => {
      const u = new URL(r.url());
      return u.pathname.endsWith('/samples') && u.searchParams.get('q') === 'AUD-0008';
    });
    await p.locator('.tools-query .el-select').nth(1).locator('input').fill('AUD-0008');
    await searched;
    await p.getByRole('option').filter({ hasText: 'AUD-0008' }).click();
    await p.getByRole('button', { name: '查询', exact: true }).click();
    await p.waitForFunction(() => document.querySelector('.tools-result')?.textContent.includes('AUD-0008'));

    // Return to the first sample: execution currently only handles first-page deep links reliably.
    await p.locator('.tools-query .el-select').nth(1).click();
    await p.locator('.tools-query .el-select').nth(1).locator('input').fill('');
    await p.getByRole('option').filter({ hasText: 'AUD-0001' }).waitFor();
    await p.getByRole('option').first().click();
    await p.getByRole('button', { name: '查询', exact: true }).click();
    await p.waitForFunction(() => document.querySelector('.tools-result')?.textContent.includes('AUD-0001'));
    const beforeBatch = await p.locator('.tools-result').innerText();
    await p.locator('.tools-query .el-select').nth(0).click();
    const alternateBatch = await p.getByRole('option').nth(1).innerText();
    await p.getByRole('option').nth(1).click();
    await p.getByRole('button', { name: '查询', exact: true }).waitFor({ state: 'visible' });
    await p.waitForFunction(() => !document.querySelector('.tools-query .el-button')?.disabled);
    if ((await p.locator('.tools-result').innerText()) !== beforeBatch) throw new Error('Draft batch changed result before query');
    await p.getByRole('button', { name: '查询', exact: true }).click();
    await p.waitForFunction(label => document.querySelector('.tools-result')?.textContent.includes(label), alternateBatch.trim());
    await p.locator('.tools-query .el-select').nth(0).click();
    const originalBatch = await p.getByRole('option').first().innerText();
    await p.getByRole('option').first().click();
    // The workspace response must finish before the query uses its candidate scope.
    await p.waitForResponse(r => r.url().includes('/workspace?') && r.url().includes('batch_id=') && r.status() === 200);
    await p.getByRole('button', { name: '查询', exact: true }).click();
    await p.waitForFunction(label => document.querySelector('.tools-result')?.textContent.includes(label), originalBatch.trim());
    const pngDownload = p.waitForEvent('download', { timeout: 90000 });
    await p.getByRole('button', { name: '导出当前视图', exact: true }).click();
    const png = await pngDownload;
    await png.saveAs(path.join(root, 'browser-current-view.png'));
    downloads.push({ type: 'browser_png', filename: png.suggestedFilename() });
    await p.getByRole('button', { name: '导出结果包', exact: true }).click();
    const dialog = p.getByRole('dialog', { name: '导出结果包', exact: true });
    await dialog.waitFor();
    await dialog.getByRole('button', { name: '生成 ZIP', exact: true }).click();
    const zipButton = p.locator('.tools-download-button').first();
    await zipButton.waitFor({ timeout: 90000 });
    const zipDownload = p.waitForEvent('download', { timeout: 30000 });
    await zipButton.click();
    const zip = await zipDownload;
    await zip.saveAs(path.join(root, 'browser-result-package.zip'));
    downloads.push({ type: 'server_zip', filename: zip.suggestedFilename() });
    await p.getByRole('button', { name: '定位思维步骤', exact: true }).click();
    await p.waitForURL(/view=execution/, { timeout: 30000 });
    await p.locator('.execution-view').waitFor({ timeout: 30000 });
    await p.locator('.execution-view .ex-columns').waitFor({ timeout: 45000 });
    await p.waitForFunction(() => {
      const id = new URL(location.href).searchParams.get('eventId');
      return id && document.getElementById('execution-log-' + id)?.classList.contains('selected');
    }, null, { timeout: 45000 });
    await p.screenshot({ path: path.join(root, 'execution-location.png'), fullPage: true });
    await p.getByRole('button', { name: '16 网页抓取', exact: true }).click();
    await p.waitForFunction(() => {
      const row = document.querySelector('.execution-view tr.selected');
      return row && row.textContent.includes('STEP-16') && !row.id.includes('已脱敏');
    });
    const selectedId = await p.locator('.execution-view tr.selected').getAttribute('id');
    const eventId = selectedId.replace('execution-log-', '');
    const url = new URL(p.url());
    const verify = await context.request.get('http://127.0.0.1:1225/api/v1/compliance/agent-traces/batches/' +
      url.searchParams.get('batchId') + '/samples/' + url.searchParams.get('traceId'), {
      headers: { Authorization: 'Bearer ' + session.token },
      params: { view: 'execution', snapshot_id: url.searchParams.get('snapshotId'),
        projection_version: url.searchParams.get('projectionVersion'), section: 'selection', event_id: eventId }
    });
    if (!verify.ok()) throw new Error('Event identity was changed by redaction');
    await p.screenshot({ path: path.join(root, 'execution-event16.png'), fullPage: true });
    if (errors.length) throw new Error(errors.join('\n'));
    const failed = requests.filter(r => r.status >= 400);
    if (failed.length) throw new Error('Failed backend requests: ' + JSON.stringify(failed));
    const report = { database: 'actual_mysql', mode: 'real_http', initial_result: initial, calls: rows,
      raw_events: 'passed', sample_query: 'passed', sample_search: 'passed', batch_query: 'passed',
      first_page_step_location: 'passed', event_identity_after_redaction: 'passed', downloads, requests, page_errors: errors };
    fs.writeFileSync(path.join(root, 'browser-report.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify({ passed: true, calls: rows, requests: requests.length, downloads }));
  } catch (e) {
    await p.screenshot({ path: path.join(root, 'browser-failure.png'), fullPage: true }).catch(() => {});
    fs.writeFileSync(path.join(root, 'browser-failure.json'), JSON.stringify({ message: e.message, requests, errors }, null, 2));
    throw e;
  } finally {
    await browser.close();
  }
})().catch(e => { console.error(e.stack); process.exitCode = 1; });
