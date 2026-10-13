// Only use a disposable local database. This creates synthetic documents and contracts.
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || '@playwright/test');

async function main() {
  const url = process.env.KNOWLEDGE_UI_URL || 'http://127.0.0.1:8082';
  const target = new URL(url);
  assert(['127.0.0.1', 'localhost'].includes(target.hostname));
  assert.equal(
    process.env.WORKBENCH_UI_DISPOSABLE,
    'yes',
    'Explicit disposable database acknowledgement required',
  );
  const token = process.env.CONTRACTOPS_LOCAL_TOKEN;
  assert(token);
  async function api(route, options = {}) {
    const response = await fetch(url + route, {
      ...options,
      headers: { Authorization: 'Bearer ' + token, ...options.headers },
    });
    assert(response.ok, route + ': ' + (await response.clone().text()));
    return response.json();
  }
  const python =
    process.env.KNOWLEDGE_UI_PYTHON || path.join(__dirname, '../.venv/Scripts/python.exe');
  const fixture = (content) =>
    execFileSync(python, [
      '-c',
      'import io,zipfile,sys; from xml.sax.saxutils import escape; b=io.BytesIO(); z=zipfile.ZipFile(b,"w"); ' +
        'z.writestr("word/document.xml",\'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>\'+escape(sys.argv[1])+\'</w:t></w:r></w:p></w:body></w:document>\'); z.close(); sys.stdout.buffer.write(b.getvalue())',
      content,
    ]);
  const stamp = Date.now(),
    name = '业务验收库 ' + stamp;
  const base = await api('/v1/knowledge/bases', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, description: '合成测试资料，不使用用户合同' }),
  });
  async function upload(content, kind, file) {
    const query = new URLSearchParams({
      file_name: file,
      document_type: kind,
      media_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    });
    return api('/v1/knowledge/bases/' + base.id + '/documents?' + query, {
      method: 'POST',
      body: fixture(content),
    });
  }
  const source = await upload(
    'Payment CNY 1,000.00. Delivery due on 2026-11-01. 合同付款期限30天。',
    'CONTRACT',
    '验收合同.docx',
  );
  await upload(
    'Payment policy: deposits require human approval. 付款制度：交付后付款。',
    'POLICY',
    '验收制度.docx',
  );
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.KNOWLEDGE_UI_BROWSER
      ? { executablePath: process.env.KNOWLEDGE_UI_BROWSER }
      : {}),
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } }),
      errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.goto(url);
    await page.locator('#token').fill(token);
    await page.locator('#connect').click();
    await page.waitForFunction(() => !document.querySelector('#localIdentityBar').hidden);
    await page.locator('[data-view=qa]').click();
    await page.locator('#qaBase').selectOption(base.id);
    await page
      .locator('#qaDocument option[value="' + source.id + '"]')
      .waitFor({ state: 'attached' });
    await page.locator('#qaDocument').selectOption(source.id);
    await page.locator('#question').fill('Payment');
    await page.locator('#ask').click();
    await page.waitForFunction(() =>
      document.querySelector('#qaMode').textContent.includes('模型未配置'),
    );
    assert.match(await page.locator('#sources').textContent(), /1,000/);
    await page.locator('#question').fill('那交付期限呢？');
    await page.locator('#ask').click();
    await page.locator('#qaHistory details').waitFor();
    await page.locator('#sources button').first().click();
    await page.locator('#recordDialog[open]').waitFor();
    await page.waitForFunction(() => document.querySelector('#recordChunks .selected'));
    await page.locator('#closeRecord').click();
    await page.locator('#newConversation').click();
    assert.equal(await page.locator('#qaHistory').textContent(), '');
    await page.locator('[data-view=contracts]').click();
    await page.locator('#createContractPanel summary').click();
    const title = '页面闭环采购合同 ' + stamp;
    await page.locator('#contractTitle').fill(title);
    await page.locator('#contractParty').fill('合成供应商');
    await page.locator('#contractAmount').fill('1000');
    await page.locator('#newContractDocument').selectOption(source.id);
    await page.locator('#createContract').click();
    await page.waitForFunction(
      (value) => document.querySelector('#businessTitle').textContent === value,
      title,
    );
    const contractId = await page
      .locator('#businessIdentity')
      .textContent()
      .then((value) => value.match(/[0-9a-f]{8}-[0-9a-f-]{27,}/)[0]);
    await page.locator('#contractCandidates input').first().check();
    await page.locator('#confirmCandidates').click();
    await page.waitForFunction(() => !document.querySelector('#ask').disabled);
    await page.locator('#reviewBase').selectOption(base.id);
    await page.locator('#generateReview').click();
    await page.waitForFunction(() =>
      document.querySelector('#reviewReport').textContent.includes('当前版本预审报告'),
    );
    assert.match(await page.locator('#reviewReport').textContent(), /未生成模型建议/);
    await page.locator('#reviewReport details').first().locator('summary').click();
    await page
      .locator('#reviewReport details')
      .first()
      .getByRole('button', { name: '打开引用原文' })
      .first()
      .click();
    await page.locator('#recordDialog[open]').waitFor();
    await page.locator('#closeRecord').click();
    const revisionQuery = new URLSearchParams({
      expected_version: source.state_version,
      file_name: '验收合同-v2.docx',
      media_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    });
    const revision = await api(
      '/v1/knowledge/documents/' + source.id + '/versions?' + revisionQuery,
      {
        method: 'POST',
        body: fixture('Payment CNY 2,000.00. Delivery due on 2026-12-01. 合同付款期限45天。'),
      },
    );
    await page.locator('#refreshContracts').click();
    await page.waitForFunction(() =>
      document.querySelector('#reviewReport').textContent.includes('已失效'),
    );
    await page.locator('#revisionDocument').selectOption(revision.id);
    await page.locator('#bindContractRevision').click();
    await page.waitForFunction(() =>
      document.querySelector('#businessSummary').textContent.includes('合同 v2'),
    );
    await page.locator('#compareContract').click();
    await page.waitForFunction(() =>
      document.querySelector('#contractDiff').textContent.includes('新增内容'),
    );
    assert.match(await page.locator('#contractDiff').textContent(), /移除内容/);
    assert.match(await page.locator('#contractDiff').textContent(), /2,000/);
    await page.locator('#generateReview').click();
    await page.waitForFunction(() =>
      document.querySelector('#reviewReport').textContent.includes('当前版本预审报告'),
    );
    await page.locator('#submitContract').click();
    await page.waitForFunction(() =>
      document.querySelector('#businessSummary').textContent.includes('审批中'),
    );
    await page.locator('#localIdentity').selectOption('reviewer');
    await page.waitForFunction(() => document.querySelector('#approvalRows button'));
    assert.equal(await page.locator('#qaHistory').textContent(), '');
    await page
      .locator('#approvalRows section')
      .filter({ hasText: contractId })
      .getByRole('button', { name: '查看合同、预审与处理' })
      .click();
    await page.locator('#contractApprovals').getByRole('button', { name: '领取审批任务' }).click();
    await page
      .locator('#contractApprovals textarea')
      .fill('已核对合同付款条款和制度依据，同意通过。');
    page.once('dialog', (dialog) => dialog.accept());
    await page
      .locator('#contractApprovals')
      .getByRole('button', { name: '通过', exact: true })
      .click();
    await page.waitForFunction(() =>
      document.querySelector('#businessSummary').textContent.includes('已通过'),
    );
    await page.waitForFunction(() =>
      document.querySelector('#contractApprovals').textContent.includes('已核对合同付款条款'),
    );
    assert.match(await page.locator('#contractApprovals').textContent(), /已核对合同付款条款/);
    await page.locator('[data-view=approvals]').click();
    await page.locator('#approvalFilter').selectOption('handled');
    await page.waitForFunction(() =>
      document.querySelector('#approvalRows').textContent.includes('已通过'),
    );
    await page
      .locator('#approvalRows section')
      .filter({ hasText: contractId })
      .getByRole('button')
      .click();
    await page.waitForFunction(
      () =>
        !document.querySelector('#view-contracts').hidden &&
        !document.querySelector('#localIdentity').disabled,
    );
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 1000 });
      assert(
        await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
        'Unexpected page overflow at ' + width,
      );
      if (process.env.WORKBENCH_UI_SCREENSHOT)
        await page.screenshot({
          path: process.env.WORKBENCH_UI_SCREENSHOT.replace('.png', '-' + width + '.png'),
          fullPage: true,
        });
    }
    assert.deepEqual(errors, []);
    console.log(
      'UI PASS: scoped QA, follow-up, fixed source, manual candidate check, existing-document link, pre-review evidence, revision diff/stale report, submit, reviewer claim/approve/comment, handled history, identity reset, desktop/mobile layout',
    );
  } finally {
    await browser.close();
  }
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
