/* Shared workspace UI. No service/model credentials or persistent browser storage. */
const business = {
  current: null,
  report: null,
  docs: [],
  offset: 0,
  count: 0,
  history: [],
  pending: new Map(),
  sourceFingerprint: '',
  disabledBeforeBusy: new WeakMap(),
  controls() {
    for (const button of document.querySelectorAll(
      '.business-action,#refreshContracts,#loadTasks,#compareContract,#openContractSource,#contractsPrevious,#contractsNext',
    )) {
      if (state.busy) {
        if (!this.disabledBeforeBusy.has(button))
          this.disabledBeforeBusy.set(button, button.disabled);
        button.disabled = true;
      } else if (this.disabledBeforeBusy.has(button)) {
        button.disabled = this.disabledBeforeBusy.get(button);
        this.disabledBeforeBusy.delete(button);
      }
    }
    if (!state.busy) {
      this.buttons();
      $('createContract').disabled = !state.connected || !writer();
      $('contractsPrevious').disabled = this.offset === 0;
      $('contractsNext').disabled = this.count < 30;
      $('compareContract').disabled = !this.current;
      $('openContractSource').disabled = !this.current?.document_id;
    }
  },
  isApprover() {
    return state.session?.roles.some((role) =>
      ['APPROVER', 'LEGAL_ADMIN', 'FINANCE_APPROVER', 'BUSINESS_APPROVER', 'TENANT_ADMIN'].includes(
        role,
      ),
    );
  },
  editable() {
    return (
      writer() &&
      this.current &&
      ['DRAFT', 'CHANGES_REQUESTED', 'REJECTED'].includes(this.current.contract.status)
    );
  },
  async write(path, payload) {
    const cacheKey = path + JSON.stringify(payload),
      key = this.pending.get(cacheKey) || crypto.randomUUID();
    this.pending.set(cacheKey, key);
    const value = await api(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
      body: JSON.stringify(payload),
    });
    this.pending.delete(cacheKey);
    return value;
  },
  reset() {
    this.current = null;
    this.report = null;
    this.docs = [];
    this.offset = 0;
    this.count = 0;
    this.pending.clear();
    this.sourceFingerprint = '';
    this.clearConversation();
    $('localIdentityBar').hidden = true;
    $('businessDetail').hidden = true;
    for (const id of [
      'contractRows',
      'contractCandidates',
      'reviewReport',
      'contractApprovals',
      'contractDiff',
      'approvalRows',
    ])
      $(id).replaceChildren();
    for (const id of ['newContractDocument', 'revisionDocument', 'reviewBase', 'qaDocument'])
      $(id).replaceChildren();
    $('businessTitle').textContent = '';
    $('businessSummary').textContent = '';
    $('businessIdentity').textContent = '';
    $('createContractForm').reset();
  },
  clearConversation() {
    this.history = [];
    $('qaHistory').replaceChildren();
    $('qaMode').textContent = '';
    $('answer').textContent = '';
    $('sources').replaceChildren();
  },
  fill(select, options, placeholder) {
    const selected = select.value;
    select.replaceChildren(new Option(placeholder, ''));
    for (const [id, name] of options) select.append(new Option(name, id));
    if (options.some(([id]) => id === selected)) select.value = selected;
  },
  async loadSources() {
    this.docs = await api('/knowledge/documents?limit=100');
    const fingerprint = JSON.stringify(
      this.docs.map((d) => [d.document_id, d.version_id, d.status]),
    );
    if (this.sourceFingerprint && fingerprint !== this.sourceFingerprint) this.clearConversation();
    this.sourceFingerprint = fingerprint;
    const options = this.docs
      .filter((d) => d.document_id && d.document_type === 'CONTRACT' && d.searchable)
      .map((d) => [d.document_id, d.file_name + ' · v' + d.version_number]);
    this.fill($('newContractDocument'), options, '选择当前已解析的合同资料');
    this.fill($('revisionDocument'), options, '选择修订后的合同资料');
    this.fill(
      $('reviewBase'),
      state.bases.map((b) => [b.id, b.name]),
      '选择参考制度所在知识库',
    );
    await this.loadQADocuments();
  },
  async loadQADocuments() {
    const docs = $('qaBase').value
      ? await api('/knowledge/bases/' + $('qaBase').value + '/documents?limit=100&status=READY')
      : this.docs;
    this.fill(
      $('qaDocument'),
      docs
        .filter((d) => (d.document_id || d.id) && d.searchable)
        .map((d) => [d.document_id || d.id, d.file_name + ' · v' + d.version_number]),
      '范围内全部资料',
    );
    this.scopeHint();
  },
  scopeHint() {
    const base = $('qaBase').selectedOptions[0]?.textContent || '全部可访问资料',
      doc = $('qaDocument').selectedOptions[0]?.textContent || '范围内全部资料';
    $('qaScopeHint').textContent =
      '本次范围：' + base + ' / ' + doc + '。每轮重新鉴权；最多保留6轮当前页面会话。';
  },
  source(source, label = '打开引用原文') {
    const box = text('div', '', 'source');
    box.append(
      text(
        'strong',
        source.file_name +
          ' · v' +
          source.version_number +
          ' · ' +
          (source.page_number === null
            ? 'DOCX 标题来源'
            : '页 ' +
              (source.source_pages?.length ? source.source_pages.join('、') : source.page_number)),
      ),
      text('pre', source.excerpt),
    );
    if (source.document_id) {
      const button = text('button', label, 'row-action');
      button.onclick = () => recordRun(() => openRecord(source.document_id, source));
      box.append(button);
    }
    box.append(text('small', '片段 ' + source.chunk_id + ' · SHA-256 ' + source.content_hash));
    return box;
  },
  async onConnected() {
    $('localIdentityBar').hidden = state.session.mode !== 'LOCAL_SQLITE';
    $('localIdentity').value = state.session.local_identity || 'owner';
    $('contractDepartment').value = state.session.department_ids?.[0] || '';
    $('createContractPanel').hidden = !writer();
    $('navContracts').title = '读取持久化合同台账';
    $('navApprovals').title = '人工审批与操作记录';
    $('qaModelHint').textContent = state.session.model_configured
      ? '已配置模型：先鉴权检索，再组织带依据的回答；异常时降级为原文检索。'
      : '模型未配置：当前为原文检索模式。配置后可启用带引用的模型回答，不会模拟生成结果。';
    $('approvalFilter').value = this.isApprover() ? 'pending' : 'all';
    await this.loadSources();
  },
  async ask() {
    const question = $('question').value.trim();
    if (!question) throw new Error('请填写问题');
    $('answer').textContent = '';
    $('sources').replaceChildren();
    const payload = { question, limit: 6, history: this.history.slice(-6).map((t) => t.question) };
    if ($('qaBase').value) payload.base_id = $('qaBase').value;
    if ($('qaDocument').value) payload.document_id = $('qaDocument').value;
    const result = await api('/knowledge/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    this.history.push({ question, answer: result.answer, mode: result.mode });
    this.history = this.history.slice(-6);
    $('qaHistory').replaceChildren();
    for (const turn of this.history.slice(0, -1)) {
      const box = text('details', '', 'qa-turn');
      box.append(
        text('summary', turn.question + ' · ' + (turn.mode === 'RAG' ? '模型回答' : '原文检索')),
        text('pre', turn.answer),
      );
      $('qaHistory').append(box);
    }
    const mode =
      result.mode === 'RAG'
        ? '模型依据回答'
        : result.fallback_reason === 'MODEL_NOT_CONFIGURED'
          ? '原文检索模式 · 模型未配置'
          : result.fallback_reason
            ? '原文检索模式 · 模型不可用或输出未通过引用校验'
            : '原文检索模式';
    $('qaMode').textContent = mode + ' · ' + result.status;
    $('answer').textContent = result.answer;
    result.citations.forEach((source, index) => {
      const box = this.source(source);
      box.prepend(text('strong', '[' + (index + 1) + '] '));
      $('sources').append(box);
    });
    this.scopeHint();
    notify('本轮已按当前权限重新检索；模型不能操作合同或审批。');
  },
  async refresh() {
    const rows = await api('/workspace/contracts?limit=30&offset=' + this.offset);
    this.count = rows.length;
    $('contractRows').replaceChildren();
    for (const contract of rows) {
      const row = document.createElement('tr'),
        title = text('td', contract.title);
      title.append(text('small', contract.counterparty_name || '对方待确认'));
      const action = document.createElement('td'),
        button = text('button', '查看合同与预审', 'row-action');
      button.onclick = () => run(() => this.open(contract.id));
      action.append(button);
      row.append(
        title,
        text('td', contract.amount === null ? '待确认' : contract.amount + ' ' + contract.currency),
        text('td', 'v' + (contract.versions.at(-1)?.version_number || 0)),
        text('td', this.status(contract.status)),
        action,
      );
      $('contractRows').append(row);
    }
    if (!rows.length) {
      const row = document.createElement('tr'),
        cell = text('td', '暂无合同。先上传合同资料，再创建台账并关联。', 'empty');
      cell.colSpan = 5;
      row.append(cell);
      $('contractRows').append(row);
    }
    $('contractsPage').textContent =
      '第 ' + (this.offset / 30 + 1) + ' 页 · 本页 ' + rows.length + ' 份';
    $('contractsPrevious').disabled = this.offset === 0;
    $('contractsNext').disabled = rows.length < 30;
  },
  status(status) {
    return (
      {
        DRAFT: '草稿',
        IN_APPROVAL: '审批中',
        APPROVED: '已通过',
        REJECTED: '已驳回',
        CHANGES_REQUESTED: '退回修改',
        IN_PROGRESS: '进行中',
        READY: '待领取',
        CLAIMED: '已领取',
        PENDING: '待前序',
        SKIPPED: '已跳过',
      }[status] || status
    );
  },
  async create() {
    const documentId = $('newContractDocument').value;
    if (!documentId) throw new Error('请先选择已入库的合同资料');
    const amount = $('contractAmount').value;
    const contract = await this.write('/contracts', {
      title: $('contractTitle').value.trim(),
      contract_type: 'KNOWLEDGE_LINKED',
      counterparty_name: $('contractParty').value.trim() || null,
      department_id: $('contractDepartment').value.trim(),
      amount: amount || null,
      currency: amount ? $('contractCurrency').value.toUpperCase() : null,
      valid_from: $('contractFrom').value || null,
      valid_until: $('contractUntil').value || null,
    });
    try {
      await this.write('/workspace/contracts/' + contract.id + '/documents', {
        document_id: documentId,
        expected_version: contract.state_version,
      });
    } catch (error) {
      await this.refresh();
      await this.open(contract.id);
      throw new Error('台账已创建，文件关联未完成，可在详情继续关联：' + error.message);
    }
    $('createContractPanel').open = false;
    await this.refresh();
    await this.open(contract.id);
    notify('台账已创建并引用已有原件；没有复制上传文件。');
  },
  async open(id) {
    this.current = await api('/workspace/contracts/' + id);
    this.report = null;
    const { contract, document_id, candidates, confirmation, approvals } = this.current;
    $('businessDetail').hidden = false;
    $('businessTitle').textContent = contract.title;
    $('businessSummary').textContent =
      this.status(contract.status) +
      ' · ' +
      (contract.counterparty_name || '交易对方待确认') +
      ' · ' +
      (contract.amount === null ? '金额待确认' : contract.amount + ' ' + contract.currency) +
      ' · 合同 v' +
      (contract.versions.at(-1)?.version_number || 0);
    $('businessIdentity').textContent =
      '合同 ID：' + contract.id + '；当前文件：' + (document_id || '尚未关联') + '。';
    $('contractDiff').replaceChildren();
    $('openContractSource').disabled = !document_id;
    $('bindingBar').hidden = !this.editable();
    $('confirmCandidates').hidden = !this.editable();
    $('contractCandidates').replaceChildren();
    for (const candidate of candidates) {
      const row = text('div', '', 'check-item'),
        check = document.createElement('input');
      check.type = 'checkbox';
      check.value = candidate.id;
      check.className = 'candidate-check';
      check.id = 'candidate-' + candidate.id;
      check.checked = confirmation.candidate_ids?.includes(candidate.id) || false;
      check.disabled = !this.editable();
      const label = text(
        'label',
        candidate.title + '：' + (candidate.normalized_value || candidate.source.excerpt),
      );
      label.htmlFor = check.id;
      label.append(text('small', ' · 规则候选 / 未自动写入台账'));
      const button = text('button', '核对原文', 'row-action');
      button.onclick = () =>
        recordRun(() => openRecord(candidate.source.document_id, candidate.source));
      row.append(check, label, button);
      $('contractCandidates').append(row);
    }
    if (!candidates.length)
      $('contractCandidates').append(
        text('p', '未识别到规则候选；不代表原文没有金额或日期。', 'muted'),
      );
    await this.loadReport();
    this.renderApprovals(approvals);
    this.buttons();
    showView('contracts');
  },
  async loadReport() {
    this.report = null;
    $('reviewReport').replaceChildren();
    try {
      this.report = await api('/workspace/contracts/' + this.current.contract.id + '/review');
    } catch (error) {
      if (!error.message.includes('尚无预审报告')) throw error;
    }
    const report = this.report;
    if (!report) {
      $('reviewReport').append(
        text('p', '尚无预审报告。选择制度库后生成；没有制度时会明确提示依据不足。', 'muted'),
      );
      return;
    }
    if (state.bases.some((b) => b.id === report.policy_base_id))
      $('reviewBase').value = report.policy_base_id;
    const banner = text(
      'div',
      (report.status === 'STALE'
        ? '已失效：合同、制度或核对信息已变化，请重新生成。'
        : '当前版本预审报告') +
        ' · ' +
        (report.mode === 'RULES_WITH_MODEL'
          ? '规则与模型语义建议'
          : '规则与原文依据 · 未生成模型建议'),
      'banner' + (report.status === 'STALE' ? ' review-stale' : ''),
    );
    $('reviewReport').append(
      banner,
      text('strong', report.summary),
      text('p', report.disclaimer, 'muted'),
    );
    const checks = document.createElement('ul');
    for (const check of report.checks) checks.append(text('li', check));
    $('reviewReport').append(checks);
    if (report.semantic_suggestions)
      $('reviewReport').append(text('pre', report.semantic_suggestions));
    for (const pair of report.pairs) {
      const box = text('details', '', 'report-sources');
      box.append(
        text(
          'summary',
          pair.topic +
            ' · 合同 ' +
            pair.contract_sources.length +
            ' 处 / 制度 ' +
            pair.policy_sources.length +
            ' 处',
        ),
        text('p', pair.note, 'muted'),
      );
      for (const source of pair.contract_sources) {
        const card = this.source(source);
        card.prepend(text('strong', '合同原文'));
        box.append(card);
      }
      for (const source of pair.policy_sources) {
        const card = this.source(source);
        card.prepend(text('strong', '制度原文'));
        box.append(card);
      }
      $('reviewReport').append(box);
    }
    const references = text('details', '', 'report-sources');
    references.append(text('summary', '报告引用目录（模型编号对应这里）'));
    report.citations.forEach((source, index) => {
      const card = this.source(source);
      card.prepend(text('strong', '[' + (index + 1) + '] '));
      references.append(card);
    });
    $('reviewReport').append(references);
  },
  buttons() {
    $('submitContract').disabled =
      state.busy ||
      !writer() ||
      this.current?.contract.status !== 'DRAFT' ||
      this.report?.status !== 'CURRENT';
    $('generateReview').disabled =
      state.busy ||
      !this.current?.document_id ||
      !$('reviewBase').value ||
      !(writer() || this.isApprover());
    $('bindContractRevision').disabled =
      state.busy || !this.editable() || !$('revisionDocument').value;
    $('confirmCandidates').disabled = state.busy || !this.editable();
  },
  renderApprovals(instances) {
    $('contractApprovals').replaceChildren();
    if (!instances.length) $('contractApprovals').append(text('p', '尚未提交审批。', 'muted'));
    for (const instance of instances) {
      const timeline = text('div', '', 'timeline');
      timeline.append(
        text('strong', instance.policy_snapshot.name + ' · ' + this.status(instance.status)),
        text(
          'small',
          '申请 ' + date(instance.created_at) + ' · 合同版本 ' + instance.contract_version_id,
        ),
      );
      for (const step of instance.steps) {
        const card = text('section', '', 'step-card');
        card.append(
          text('strong', step.step_order + '. ' + step.name + ' · ' + this.status(step.status)),
          text('p', '所需角色：' + step.required_role, 'muted'),
        );
        if (step.decided_at)
          card.append(
            text(
              'p',
              '审批人 ' +
                step.decided_by +
                ' · ' +
                date(step.decided_at) +
                ' · 意见：' +
                (step.decision_comment || '未填写'),
            ),
          );
        const eligible =
          state.session.roles.includes(step.required_role) ||
          state.session.roles.includes('TENANT_ADMIN');
        if (step.status === 'READY' && eligible) {
          const button = text('button', '领取审批任务', 'primary business-action');
          button.onclick = () =>
            run(async () => {
              await this.write('/approval-steps/' + step.id + ':claim', {
                expected_version: step.state_version,
              });
              await this.open(instance.contract_id);
            });
          card.append(button);
        }
        if (step.status === 'CLAIMED' && step.assigned_to === state.session.user_id && eligible) {
          const comment = document.createElement('textarea');
          comment.maxLength = 2000;
          comment.placeholder = '填写审批意见（建议写明依据和需要修改的内容）';
          comment.setAttribute('aria-label', '审批意见');
          card.append(comment);
          const actions = text('div', '', 'business-actions');
          for (const [decision, label] of [
            ['APPROVE', '通过'],
            ['REJECT', '驳回'],
            ['REQUEST_CHANGES', '退回修改'],
          ]) {
            const button = text(
              'button',
              label,
              'business-action' + (decision === 'APPROVE' ? ' primary' : ''),
            );
            button.disabled = decision === 'APPROVE' && this.report?.status !== 'CURRENT';
            button.onclick = () =>
              run(async () => {
                if (!confirm('确认' + label + '该合同？这是人工审批决定。')) return;
                await this.write('/approval-steps/' + step.id + ':decide', {
                  expected_version: step.state_version,
                  decision,
                  comment: comment.value || null,
                });
                await this.refresh();
                await this.open(instance.contract_id);
                notify('审批操作已保存，合同状态与审批结果同步更新。');
              });
            actions.append(button);
          }
          card.append(actions);
        }
        timeline.append(card);
      }
      $('contractApprovals').append(timeline);
    }
  },
  async loadApprovals() {
    $('approvalRows').replaceChildren();
    const filter = $('approvalFilter').value;
    if (filter === 'pending') {
      if (!this.isApprover()) {
        $('approvalRows').append(
          text('p', '当前是经办人。可查看审批过程，或切换为本地审批人身份。'),
        );
        return;
      }
      const tasks = await api('/approval-tasks?limit=50');
      const instances = await Promise.all(
        [...new Set(tasks.map((t) => t.instance_id))].map((id) => api('/approval-instances/' + id)),
      );
      for (const task of tasks) {
        const instance = instances.find((i) => i.id === task.instance_id),
          box = text('section', '', 'step-card');
        box.append(
          text('strong', task.name + ' · ' + this.status(task.status)),
          text('p', '合同 ' + instance.contract_id),
        );
        const button = text('button', '查看合同、预审与处理', 'primary row-action');
        button.onclick = () => run(() => this.open(instance.contract_id));
        box.append(button);
        $('approvalRows').append(box);
      }
      if (!tasks.length) $('approvalRows').append(text('p', '当前身份暂无可处理待办。', 'muted'));
    } else {
      const instances = await api('/workspace/approval-history?handled=' + (filter === 'handled'));
      for (const instance of instances) {
        const box = text('section', '', 'step-card');
        box.append(
          text('strong', this.status(instance.status) + ' · ' + instance.policy_snapshot.name),
          text('p', '合同 ' + instance.contract_id + ' · ' + date(instance.updated_at)),
        );
        const button = text('button', '查看审批过程与原文', 'row-action');
        button.onclick = () => run(() => this.open(instance.contract_id));
        box.append(button);
        $('approvalRows').append(box);
      }
      if (!instances.length)
        $('approvalRows').append(text('p', '没有当前范围内的审批记录。', 'muted'));
    }
  },
};
document.addEventListener('workspace-reset', () => business.reset());
const controlsWorkspaceOriginal = controls;
controls = () => {
  controlsWorkspaceOriginal();
  business.controls();
};
const connectWorkspaceOriginal = $('connect').onclick;
$('connect').onclick = async () => {
  await connectWorkspaceOriginal();
  if (state.connected) await run(() => business.onConnected());
};
const refreshBasesOriginal = refreshBases;
refreshBases = async (...args) => {
  await refreshBasesOriginal(...args);
  if (state.connected) await business.loadSources();
};
$('localIdentity').onchange = async () => {
  let selected;
  await run(async () => {
    selected = await api(
      '/workspace/local-identity',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ identity: $('localIdentity').value }),
      },
      true,
    );
  });
  if (selected) {
    $('token').value = selected.token;
    reset();
    await $('connect').onclick();
    showView('approvals');
    await run(() => business.loadApprovals());
  }
};
document.querySelectorAll('[data-view]').forEach(
  (button) =>
    (button.onclick = () => {
      showView(button.dataset.view);
      if (!state.connected) return;
      if (button.dataset.view === 'contracts') run(() => business.refresh());
      if (button.dataset.view === 'approvals') run(() => business.loadApprovals());
    }),
);
$('ask').onclick = () => run(() => business.ask());
$('newConversation').onclick = () => {
  business.clearConversation();
  $('question').value = '';
};
$('qaBase').onchange = () =>
  run(async () => {
    business.clearConversation();
    await business.loadQADocuments();
  });
$('qaDocument').onchange = () => {
  business.clearConversation();
  business.scopeHint();
};
$('createContractForm').onsubmit = (event) => {
  event.preventDefault();
  run(() => business.create());
};
$('refreshContracts').onclick = () =>
  run(async () => {
    await business.loadSources();
    await business.refresh();
    if (business.current) await business.open(business.current.contract.id);
  });
$('contractsPrevious').onclick = () =>
  run(async () => {
    business.offset = Math.max(0, business.offset - 30);
    await business.refresh();
  });
$('contractsNext').onclick = () =>
  run(async () => {
    business.offset += 30;
    await business.refresh();
  });
$('openContractSource').onclick = () => recordRun(() => openRecord(business.current.document_id));
$('revisionDocument').onchange = () => business.buttons();
$('reviewBase').onchange = () => business.buttons();
$('bindContractRevision').onclick = () =>
  run(async () => {
    await business.write('/workspace/contracts/' + business.current.contract.id + '/documents', {
      document_id: $('revisionDocument').value,
      expected_version: business.current.contract.state_version,
    });
    await business.refresh();
    await business.open(business.current.contract.id);
    notify('已关联新合同版本，原件没有重复上传；旧报告不再代表当前版本。');
  });
$('confirmCandidates').onclick = () =>
  run(async () => {
    await business.write('/workspace/contracts/' + business.current.contract.id + ':confirm', {
      expected_version: business.current.contract.state_version,
      candidate_ids: [...document.querySelectorAll('.candidate-check:checked')].map(
        (input) => input.value,
      ),
    });
    await business.open(business.current.contract.id);
    notify('已保存人工核对标记；台账字段未自动改写。');
  });
$('generateReview').onclick = () =>
  run(async () => {
    await business.write('/workspace/contracts/' + business.current.contract.id + '/reviews', {
      policy_base_id: $('reviewBase').value,
    });
    await business.open(business.current.contract.id);
  });
$('submitContract').onclick = () =>
  run(async () => {
    await business.write('/workspace/contracts/' + business.current.contract.id + ':submit', {
      expected_version: business.current.contract.state_version,
      report_id: business.report.id,
    });
    await business.refresh();
    await business.open(business.current.contract.id);
    notify('已提交人工审批；请切换审批人查看待办。');
  });
$('compareContract').onclick = () =>
  run(async () => {
    const diff = await api('/workspace/contracts/' + business.current.contract.id + '/diff');
    $('contractDiff').replaceChildren(
      text('h2', '最近两个关联版本的原文变化'),
      text(
        'p',
        diff.message ||
          '变化 ' + diff.total_changes + ' 处' + (diff.truncated ? '（仅显示前100处）' : ''),
      ),
    );
    for (const entry of diff.entries) {
      const card = business.source(entry.source);
      card.prepend(text('strong', entry.change === 'ADDED' ? '新增内容' : '移除内容'));
      $('contractDiff').append(card);
    }
  });
$('loadTasks').onclick = () => run(() => business.loadApprovals());
$('approvalFilter').onchange = () => run(() => business.loadApprovals());
