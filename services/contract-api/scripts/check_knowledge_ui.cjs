// Run against a disposable local knowledge_demo.py database, never real user documents.
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || '@playwright/test');

async function main() {
  const url = process.env.KNOWLEDGE_UI_URL || 'http://127.0.0.1:8082';
  const target = new URL(url);
  assert(['127.0.0.1', 'localhost'].includes(target.hostname), 'Local test server only');
  const token = process.env.CONTRACTOPS_LOCAL_TOKEN;
  assert(token, 'Set CONTRACTOPS_LOCAL_TOKEN for the disposable test server');
  for (let attempt = 0; attempt < 40; attempt++) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(1000) });
      if (response.ok) break;
    } catch {}
    if (attempt === 39) throw new Error('Disposable UI test server is not ready');
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  const python =
    process.env.KNOWLEDGE_UI_PYTHON || path.join(__dirname, '../.venv/Scripts/python.exe');
  const makeFixture = (days) =>
    execFileSync(python, [
      '-c',
      'import io,zipfile,sys; b=io.BytesIO(); z=zipfile.ZipFile(b,"w"); ' +
        'z.writestr("word/document.xml",\'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Payment due in ' +
        days +
        " days.</w:t></w:r></w:p></w:body></w:document>'); " +
        'z.close(); sys.stdout.buffer.write(b.getvalue())',
    ]);
  const fixture = makeFixture(30);
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.KNOWLEDGE_UI_BROWSER
      ? { executablePath: process.env.KNOWLEDGE_UI_BROWSER }
      : {}),
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.goto(url);
    await page.locator('#token').fill(token);
    await page.locator('#connect').click();
    await page.waitForFunction(() =>
      document.querySelector('#connectionLabel').textContent.includes('本地 SQLite'),
    );
    assert(await page.locator('#navContracts').isEnabled());
    assert(await page.locator('#navApprovals').isEnabled());
    await page.locator('#newBase').click();
    const name = 'UI 验收知识库 ' + Date.now();
    await page.locator('#newBaseName').fill(name);
    await page.locator('#newBaseDescription').fill('自动化验收专用，不使用用户合同');
    await page.locator('#saveBase').click();
    await page.waitForFunction(
      (value) => document.querySelector('#baseName').textContent === value,
      name,
    );
    await page.locator('#uploadType').selectOption('POLICY');
    await page.locator('#files').setInputFiles({
      name: 'payment-policy.docx',
      mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      buffer: fixture,
    });
    await page.locator('#upload').click();
    await page.locator('#documentRows .badge.READY').waitFor();
    assert.equal(await page.locator('#documentCount').textContent(), '1');
    assert.match(await page.locator('#documentRows').textContent(), /公司制度/);
    const detail = page.locator('#documentRows').getByRole('button', { name: '查看内容' });
    await detail.click();
    await page.locator('#recordDialog[open]').waitFor();
    assert.match(await page.locator('#record').textContent(), /file_hash/);
    assert.match(await page.locator('#recordChunks').textContent(), /Payment due in 30 days/);
    await page.locator('[data-record-tab=text]').click();
    await page.waitForFunction(() =>
      document.querySelector('#recordFullText').textContent.includes('Payment due in 30 days'),
    );
    assert.match(await page.locator('#recordFullText').textContent(), /Payment due in 30 days/);
    assert.equal(await page.locator('#recordVersions option').count(), 1);
    const previousId = await page.locator('#recordVersions').inputValue();
    await page.locator('#versionFile').setInputFiles({
      name: 'payment-policy-v2.docx',
      mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      buffer: makeFixture(45),
    });
    await page.locator('#uploadVersion').click();
    await page.waitForFunction(
      () => document.querySelectorAll('#recordVersions option').length === 2,
    );
    const currentId = await page.locator('#recordVersions').inputValue();
    assert.notEqual(previousId, currentId);
    await page.locator('[data-record-tab=text]').click();
    await page.waitForFunction(() =>
      document.querySelector('#recordFullText').textContent.includes('45 days'),
    );
    await page.locator('#recordVersions').selectOption(previousId);
    await page.waitForFunction(() =>
      document.querySelector('#recordSummary').textContent.includes('历史版本'),
    );
    await page.locator('[data-record-tab=text]').click();
    await page.waitForFunction(() =>
      document.querySelector('#recordFullText').textContent.includes('30 days'),
    );
    assert(await page.locator('#versionUploadBar').isHidden());
    await page.locator('#recordVersions').selectOption(currentId);
    await page.waitForFunction(() =>
      document.querySelector('#recordSummary').textContent.includes('当前版本'),
    );
    await page.locator('[data-record-tab=chunks]').click();
    await page.locator('#closeRecord').click();
    if (process.env.KNOWLEDGE_UI_SCREENSHOT) {
      await page.screenshot({ path: process.env.KNOWLEDGE_UI_SCREENSHOT, fullPage: true });
    }
    await page.locator('#documentQuery').fill('does-not-exist');
    await page.locator('#filter').click();
    await page.waitForFunction(() => !document.querySelector('#documentRows .badge'));
    await page.locator('#documentQuery').fill('');
    await page.locator('#filter').click();
    await page.locator('#documentRows .badge.READY').waitFor();
    await page.locator('[data-view=qa]').click();
    await page.locator('#qaBase').selectOption({ label: name });
    await page.locator('#question').fill('Payment');
    await page.locator('#ask').click();
    await page.locator('#sources .source').first().waitFor();
    assert.match(await page.locator('#sources').textContent(), /45 days/);
    await page.locator('#sources').getByRole('button', { name: '打开引用原文' }).first().click();
    await page.locator('#recordDialog[open]').waitFor();
    await page.waitForFunction(() => document.querySelector('#recordChunks .selected'));
    assert.equal(await page.locator('#recordChunks .selected').count(), 1);
    await page.locator('#closeRecord').click();
    await page.locator('[data-view=knowledge]').click();
    page.once('dialog', (dialog) => dialog.accept());
    await page.locator('#documentRows').getByRole('button', { name: '归档' }).click();
    await page.locator('#documentRows .badge.ARCHIVED').waitFor();
    await page.locator('[data-view=qa]').click();
    await page.locator('#ask').click();
    await page.waitForFunction(() => !document.querySelector('#sources .source'));
    await page.locator('[data-view=knowledge]').click();
    await page.locator('#files').setInputFiles({
      name: 'broken.docx',
      mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      buffer: Buffer.from('invalid docx'),
    });
    await page.locator('#upload').click();
    await page.locator('#documentRows .badge.FAILED').waitFor();
    await page.locator('#documentRows').getByRole('button', { name: '重试' }).click();
    await page.waitForFunction(() =>
      document.querySelector('#notice').textContent.includes('重试'),
    );
    if (process.env.KNOWLEDGE_UI_PDF_FIXTURE) {
      await page.locator('#files').setInputFiles(process.env.KNOWLEDGE_UI_PDF_FIXTURE);
      await page.locator('#upload').click();
      const filename = path.basename(process.env.KNOWLEDGE_UI_PDF_FIXTURE);
      const row = page.locator('#documentRows tr').filter({ hasText: filename });
      await row.locator('.badge.READY').waitFor({ timeout: 60000 });
      await row.getByRole('button', { name: '查看内容' }).click();
      await page.locator('#recordDialog[open]').waitFor();
      assert.match(await page.locator('#processingState').textContent(), /Inspector/);
      await page.locator('[data-record-tab=original]').click();
      await page.locator('#originalPageImage').waitFor();
      await page.waitForFunction(
        () => document.querySelector('#originalPageImage').naturalWidth > 0,
      );
      if (process.env.KNOWLEDGE_UI_SCREENSHOT)
        await page.screenshot({
          path: process.env.KNOWLEDGE_UI_SCREENSHOT.replace('.png', '-pdf.png'),
          fullPage: true,
        });
      await page.locator('#closeRecord').click();
      console.log(
        'REAL INSPECTOR UI PASS: asynchronous PDF ingestion, status, original-page rendering',
      );
    }
    await page.setViewportSize({ width: 390, height: 844 });
    assert(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      'Mobile page overflow',
    );
    if (process.env.KNOWLEDGE_UI_SCREENSHOT) {
      await page.screenshot({
        path: process.env.KNOWLEDGE_UI_SCREENSHOT.replace('.png', '-mobile.png'),
        fullPage: true,
      });
    }
    await page.locator('#token').fill('invalid-test-token');
    assert.equal(await page.locator('#baseCount').textContent(), '0');
    assert.equal(await page.locator('#documentRows .badge').count(), 0);
    assert(await page.locator('#newBase').isDisabled());
    assert.deepEqual(errors, []);
    console.log(
      'UI PASS: connect, create, upload, metadata, full text, immutable versions, filter, current-version cited retrieval, archive, retry, identity reset, mobile layout',
    );
  } finally {
    await browser.close();
  }
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
