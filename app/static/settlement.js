import { localInstant } from './axis.js';

const $ = (id) => document.getElementById(id);
const currency = new Intl.NumberFormat('zh-CN', {style: 'currency', currency: 'CNY'});
const money = (cents) => currency.format((cents ?? 0) / 100);
const instant = (stamp) => stamp ? localInstant(stamp).replace('T', ' ') : '—';
const zone = document.body.dataset.zone;

const state = {arSourceId: null, term: null, receipt: null, snapshot: null, runs: []};

function node(tag, text, cls) {
  const el = document.createElement(tag);
  if (text !== undefined && text !== null) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}

function table(host, columns, rows, empty) {
  if (rows.length === 0) {
    $(host).replaceChildren(node('p', empty, 'empty'));
    return;
  }
  const ledger = node('table', null, 'ledger');
  const head = ledger.createTHead().insertRow();
  columns.forEach(([title, cls]) => {
    const th = node('th', title, cls);
    th.scope = 'col';
    head.append(th);
  });
  const body = ledger.createTBody();
  rows.forEach((cells) => {
    const tr = body.insertRow();
    cells.forEach((item) => {
      const td = tr.insertCell();
      if (item instanceof Node) {
        td.append(item);
      } else {
        const {value, cls} = item && typeof item === 'object' ? item : {value: item};
        td.textContent = value ?? '—';
        td.title = td.textContent;
        if (cls) td.className = cls;
      }
    });
  });
  $(host).replaceChildren(ledger);
}

function definitions(items) {
  const list = node('dl', null, 'info-list');
  for (const [label, value] of items) {
    const row = node('div');
    row.append(node('dt', label), node('dd', value ?? '—', 'mono'));
    list.append(row);
  }
  return list;
}

async function request(url, options) {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.message || `请求失败 · HTTP ${response.status}`);
  return body;
}

function evidenceButton(label, render) {
  const button = node('button', label, 'evidence-btn');
  button.type = 'button';
  button.addEventListener('click', () => {
    $('details-title').textContent = label;
    $('details-body').replaceChildren(...render());
    $('details').showModal();
  });
  return button;
}

function kvSection(title, entries) {
  const section = node('section');
  section.append(node('h3', title), definitions(entries));
  return section;
}

function receiptEvidence(row) {
  const r = row.receipt;
  const sections = [kvSection('收款回单', [
    ['回单编号', r.receipt_id], ['发票 / 客户', `${r.invoice_id} / ${r.customer_id}`],
    ['付款日', r.pay_date], ['知悉时间', instant(r.known)], ['实收', money(r.paid_cents)],
    ['引用原票', r.ref], ['备注', r.note || '—'],
  ])];
  if (row.target) sections.push(kvSection('原票版本', [
    ['事件', row.target.event_id], ['面额', money(row.target.amount_cents)],
    ['有效始日', row.target.effective], ['知悉时间', instant(row.target.known)], ['来源行', row.target.line],
  ]));
  if (row.term) sections.push(kvSection('采用条款版本', [
    ['条款', row.term.term_id], ['费率', `${row.term.discount_rate_bp / 100}%`],
    ['折扣窗口', `${row.window_start} → ${row.window_end}`],
    ['条款知悉时间', instant(row.term.known)], ['票面', money(row.face_cents)],
    ['应折扣', money(row.expected_discount_cents)], ['净应收', money(row.expected_net_cents)],
  ]));
  if (row.message) sections.push(kvSection('裁决', [['原因码', row.code], ['说明', row.message]]));
  return sections;
}

function renderSources() {
  const items = [
    ['应收来源', state.arSourceId ? state.arSourceId.slice(0, 12) : '—'],
    ['条款来源', state.term ? state.term.source_id.slice(0, 12) : '未导入'],
    ['回单来源', state.receipt ? state.receipt.source_id.slice(0, 12) : '未导入'],
  ];
  if (state.term) items.push(['条款采信 / 隔离', `${state.term.counts.accepted} / ${state.term.counts.quarantine}`]);
  if (state.receipt) items.push(['回单采信 / 隔离', `${state.receipt.counts.accepted} / ${state.receipt.counts.quarantine}`]);
  $('settle-sources').replaceChildren(definitions(items));
}

function renderSnapshot(snap) {
  state.snapshot = snap;
  const held = snap.status === 'HELD';
  const status = $('settle-status');
  status.replaceChildren(node('p', held
    ? `推迟发布：${snap.totals.held_receipt_count} 笔回单的折扣与回单解释不通`
    : '可发布：本批回单折扣全部可解释', held ? 'subtle warn' : 'subtle good'));
  const t = snap.totals;
  $('settle-summary').replaceChildren(definitions([
    ['票面合计', money(t.face_cents)], ['折扣合计', money(t.discount_cents)],
    ['净额合计', money(t.net_settled_cents)], ['实收合计', money(t.paid_cents)],
    ['未收余额', money(t.outstanding_cents)],
    ['已结算 / 推迟回单', `${t.settled_receipt_count} / ${t.held_receipt_count}`],
  ]));
  table('settle-invoices',
    [['发票 / 客户', null], ['票面', 'num'], ['折扣', 'num'], ['净额', 'num'], ['未收', 'num'], ['回单', 'num'], ['证据', null]],
    snap.invoices.map((inv) => [
      cell(`${inv.invoice_id} / ${inv.customer_id}`, 'mono'),
      cell(money(inv.face_cents), 'num'), cell(money(inv.discount_cents), 'num'),
      cell(money(inv.net_settled_cents), 'num'), cell(money(inv.outstanding_cents), 'num'),
      cell(`${inv.settled_receipts.length} / ${inv.held_receipts.length}`, 'num'),
      evidenceButton(inv.invoice_id, () => [
        ...inv.settled_receipts.flatMap(receiptEvidence),
        ...inv.held_receipts.flatMap(receiptEvidence),
      ]),
    ]), '请先预览结算快照');
  table('settle-held',
    [['回单', null], ['发票', null], ['原因码', 'mono warn'], ['说明', null], ['证据', null]],
    snap.held.map((row) => [
      cell(row.receipt.receipt_id, 'mono'), cell(row.receipt.invoice_id, 'mono'),
      cell(row.code, 'mono warn'), row.message,
      evidenceButton(row.receipt.receipt_id, () => receiptEvidence(row)),
    ]), '没有推迟发布的回单');
  const issues = [
    ...snap.term_quarantine.map((row) => [cell(row.term.term_id, 'mono'), cell('条款规则', null),
      cell(row.code, 'mono warn'), row.message, number(row.term.line)]),
    ...snap.intake_quarantine.term.map((row) => [cell(row.event_id ?? '—', 'mono'), cell('条款收件', null),
      cell(row.code, 'mono warn'), row.message, number(row.line)]),
    ...snap.intake_quarantine.receipt.map((row) => [cell(row.event_id ?? '—', 'mono'), cell('回单收件', null),
      cell(row.code, 'mono warn'), row.message, number(row.line)]),
    ...snap.intake_duplicates.term.map((row) => [cell(row.event_id ?? '—', 'mono'), cell('条款重复', null),
      cell(row.code, 'mono'), row.message, number(row.line)]),
    ...snap.intake_duplicates.receipt.map((row) => [cell(row.event_id ?? '—', 'mono'), cell('回单重复', null),
      cell(row.code, 'mono'), row.message, number(row.line)]),
  ];
  table('settle-term-issues',
    [['事件', null], ['来源', null], ['原因码', null], ['说明', null], ['行', 'num']],
    issues, '没有条款冲突或收件隔离');
}

const cell = (value, cls) => ({value, cls});
const number = (value) => ({value, cls: 'num'});

async function loadRuns() {
  const data = await request('/api/settlement');
  state.runs = data.runs;
  table('settle-runs',
    [['批次', 'mono'], ['生成时间', null], ['规则版本', 'mono'], ['操作', null]],
    state.runs.map((run) => {
      const exportLink = node('a', '导出证据', 'btn');
      exportLink.href = `/api/settlement/${run.run_id}/export`;
      const rerunBtn = node('button', '重跑', 'btn');
      rerunBtn.type = 'button';
      rerunBtn.addEventListener('click', async () => {
        const reply = await request(`/api/settlement/${run.run_id}/rerun`, {method: 'POST'});
        await loadRuns();
        window.alert(reply.created ? '已生成新批次' : '重跑一致，复用原批次');
      });
      const actions = node('div', null, 'row-actions');
      actions.append(rerunBtn, exportLink);
      return [cell(run.run_id.slice(0, 12), 'mono'), instant(run.generated_at),
              cell(run.rule_version, 'mono'), actions];
    }), '尚未发布结算批次');
}

function cutKnown() {
  const value = $('settle-run').elements.known.value;
  return value.includes('Z') || /[+-]\d\d:?\d\d$/.test(value) ? value : `${value}${zone}`;
}

export function setArSource(source) {
  state.arSourceId = source?.source_id ?? null;
  renderSources();
}

export function show(source) {
  setArSource(source);
  loadRuns().catch((error) => window.alert(error.message));
}

function attach() {
  $('settle-upload').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      for (const stream of ['term', 'receipt']) {
        const file = form.elements[stream].files[0];
        if (!file) continue;
        const body = new FormData();
        body.append('file', file);
        const summary = await request(`/api/settlement/sources/${stream}`, {method: 'POST', body});
        state[stream] = summary;
      }
      renderSources();
    } catch (error) {
      window.alert(error.message);
    }
  });
  $('settle-run').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!state.arSourceId || !state.term || !state.receipt) {
      window.alert('请先导入条款与回单来源');
      return;
    }
    const action = event.submitter?.value || 'preview';
    const known = cutKnown();
    const base = {ar_source_id: state.arSourceId, term_source_id: state.term.source_id,
                  receipt_source_id: state.receipt.source_id, known};
    try {
      if (action === 'publish') {
        const reply = await request('/api/settlement/publish', {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(base)});
        window.alert(reply.created ? '结算快照已发布' : '同一批回单已存在，复用原快照');
        await loadRuns();
      }
      const params = new URLSearchParams(base);
      const snap = await request(`/api/settlement/preview?${params}`);
      renderSnapshot(snap);
    } catch (error) {
      window.alert(error.message);
    }
  });
  $('settle-refresh').addEventListener('click', () => loadRuns().catch((error) => window.alert(error.message)));
}

attach();
