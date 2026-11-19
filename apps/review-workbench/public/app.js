/**
 * 页面入口 → session 认证 → hash 路由 → 只读 API → 有版本约束的审查命令。
 * 浏览器仅持 HttpOnly 会话及内存中的 CSRF token，不持 Run token、数据库或模型密钥。
 * 所有导入材料均以 textContent 插入；证据中的 HTML/脚本/指令没有页面执行权限。
 */
const app = document.querySelector('#app');
const announcer = document.querySelector('#announcer');
const state = { session: null, routeVersion: 0, currentCase: null, currentTask: null, drafts: new Map(), flash: null, journalBlocked: null };
const roles = { BUSINESS: '业务负责人', SOURCE: '数据源负责人', QA: 'QA 审查员' };
// 这里只配置已知材料种类的展示与初始选择；可提交种类、目标和验收标准仍来自服务端模板。
// provider 原始响应不是默认必取原件，不能为了勾满列表把未归档材料混进核心三项。
const evidenceMaterialKinds = {
  SAVED_PRODUCT_DRAFT: { label: '已保存客服草稿', defaultSelected: true },
  DELIVERED_PRIVATE_NOTE: { label: '实际最终私有备注', defaultSelected: true },
  DELIVERY_RECEIPT: { label: '草稿与最终交付关联', defaultSelected: true },
  RAW_PROVIDER_RESPONSE: { label: '模型供应商原始响应', defaultSelected: false },
};
const statusLabels = {
  REQUIRED: '待审查', WAITING_EVIDENCE: '等待补充证据', HISTORICAL_IMPORT: '历史材料已导入',
  PASS_SCOPED_DETERMINISTIC_CHECKS: '限定范围内的确定性检查通过',
  OPEN: '待审查', PENDING: '待审查', ACKNOWLEDGED: '已核对材料', REQUEST_CHANGES: '要求补充 / 修改',
  CHANGES_REQUESTED: '要求补充 / 修改', WAITING_CAPABILITY: '等待执行能力', ALL_ACKNOWLEDGED: '各方已核对材料',
  REVIEW_REQUIRED: '需要人员审查', IN_REVIEW: '审查中', REVIEWED: '材料审查完成',
  MANUAL_FIXED_PLAN_INTEGRATION_ONLY: '历史固定计划实验', MANUAL_FIXED_PLAN: '历史固定计划实验',
  NEEDS_MORE_EVIDENCE: '需要更多证据', INELIGIBLE: '不具备退款资格', ELIGIBLE: '进入人工资格复核',
  HUMAN_REVIEW: '人工复核', NO_ACTION: '不执行操作', REQUEST_INFORMATION: '请求补充信息', SEALED: '窗口已封存',
  DECISION: '审查决定已登记', EVIDENCE_REQUEST: '补证请求已登记', CASE_IMPORTED: '历史材料已导入',
  ACKNOWLEDGE: '确认已核对材料', COMPLETED: '已完成', NOT_EVALUATED: '未评价', NOT_FULLY_EVALUATED: '尚未完整评价',
  ASSIGNED: '已分配 · 待接手', IN_PROGRESS: '已接手', QUEUED: '排队中', RUNNING: '正在调查', BLOCKED: '等待澄清',
  UNKNOWN: '执行结果待对账', FINISHED: '调查已结束', CANCELLED: '已取消', WITHDRAWN: '已撤回',
  PENDING_REVIEW: '响应待审查', RESPONSE_READY: '响应已返回', ACCEPTED: '响应已接受', SUPERSEDED: '已有后继版本',
  UNASSESSED: '尚未评定', COMPLETE: '要求已完整满足', PARTIAL: '部分满足', UNAVAILABLE: '材料无法取得',
  CONFLICTING: '材料存在冲突', PROVIDED: '已提供', SATISFIED: '义务已满足', UNSATISFIED: '义务仍有缺口',
  EVIDENCE_LINKAGE: '证据关联核对', BUSINESS_SEMANTICS: '业务语义判断',
  SEMANTIC_REVIEW: '业务语义判断',
  ...Object.fromEntries(Object.entries(evidenceMaterialKinds).map(([kind, settings]) => [kind, settings.label])),
  TASK_CLAIMED: '任务已接手', TASK_REASSIGNED: '任务已转派', EVIDENCE_REQUESTED: '逐项补证已请求',
  EVIDENCE_RESPONSE_ACCEPTED: '响应已限定接受', EVIDENCE_REQUEST_WITHDRAWN: '补证已撤回', EVIDENCE_CLARIFIED: '澄清已生成后继规格',
};
const limitationLabels = {
  MANUAL_FIXED_PLAN_NO_AUTONOMOUS_AGENT_DISPATCH: '实验由固定脚本执行，尚不能证明 Agent 自主规划与派发。',
  SYNTHETIC_CONTRACT_PAYMENT_CLAIM_CLOCK: '合同、付款与申诉时钟为本地合成场景。',
  LOCAL_CONTROLLED_EVENT_SET_ONLY: '事件完整性仅覆盖本地受控事件集合。',
  DETERMINISTIC_DECISION_NOT_CAPTURED: '尚未完整采集确定性判定记录。',
  ACTUAL_MODEL_REQUEST_NOT_CAPTURED: '尚未采集实际模型请求。', RAW_MODEL_OUTPUT_NOT_CAPTURED: '尚未采集完整模型原始输出。',
  WORDING_NOT_FULLY_EVALUATED: '文案语义尚未完成独立评价。', REVIEWED_LOCAL_PRODUCT_NOT_UNTRUSTED_CODE_SANDBOX: '本地已审查的产品运行，不代表不可信候选沙盒已完成。',
  ONE_PRODUCT_CANDIDATE_NO_BASELINE_CANDIDATE_COMPARISON: '仅有一个产品候选，尚无基线与新版本的对照验证。',
  NO_CODEX_REPAIR_NO_PROTECTED_FINAL_NO_RELEASE: '未执行 Codex 修复、受保护 Final 或发布。',
};

const PENDING_PREFIX = 'egap.review.pending.v1:';
const WORKFLOW_PENDING_PREFIX = 'egap.review.pending.v2:';
function pendingOwner(actor) { return `${PENDING_PREFIX}${encodeURIComponent(actor.tenant_id)}:${encodeURIComponent(actor.id)}:`; }
function pendingKey(actor, caseId) { return pendingOwner(actor) + encodeURIComponent(caseId); }
function journalFailure(message) { return Object.assign(new Error(message), { journal: true }); }

/**
 * 只持久化已准备提交的命令，不保存访问码、cookie 或 CSRF。sessionStorage 限于当前标签，
 * key 包含租户、人员、case；刷新后只能恢复当前身份的原始 payload 与幂等键。
 * 日志不是授权来源；恢复后仍由服务器校验会话、权限、候选、版本和幂等冲突。
 */
function restorePending(actor, caseId) {
  let raw;
  try { raw = sessionStorage.getItem(pendingKey(actor, caseId)); }
  catch { throw journalFailure('无法读取浏览器提交恢复记录；已暂停提交，请恢复会话存储后刷新。'); }
  if (raw === null) return null;
  try {
    const record = JSON.parse(raw);
    const pending = record.pending;
    const body = pending.body;
    const request = pending.endpoint === `/review-cases/${encodeURIComponent(caseId)}/evidence-requests`;
    const decision = pending.endpoint === `/review-cases/${encodeURIComponent(caseId)}/decisions`;
    const keys = ['idempotency_key', 'expected_revision', 'candidate_sha', 'bundle_digest', 'reason', 'evidence_refs', request ? 'question' : 'decision'];
    const text = (value, limit) => typeof value === 'string' && value.trim().length > 0 && value.length <= limit;
    if (record.version !== 1 || record.actor_id !== actor.id || record.tenant_id !== actor.tenant_id || record.role !== actor.role || record.case_id !== caseId
      || Object.keys(record).sort().join(',') !== ['version', 'actor_id', 'tenant_id', 'role', 'case_id', 'pending'].sort().join(',')
      || Object.keys(pending).sort().join(',') !== 'body,endpoint' || (!request && !decision)
      || !body || Array.isArray(body) || Object.keys(body).sort().join(',') !== keys.sort().join(',')
      || !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(body.idempotency_key) || !Number.isInteger(body.expected_revision) || body.expected_revision < 1
      || !/^[a-f0-9]{40}$/i.test(body.candidate_sha) || !/^[a-f0-9]{64}$/i.test(body.bundle_digest)
      || !text(body.reason, 3000) || !Array.isArray(body.evidence_refs) || body.evidence_refs.length > 100
      || !body.evidence_refs.every(ref => text(ref, 160)) || new Set(body.evidence_refs).size !== body.evidence_refs.length
      || (request ? !text(body.question, 2000) : !['ACKNOWLEDGE', 'REQUEST_CHANGES'].includes(body.decision))) throw new Error('INVALID_JOURNAL');
    return pending;
  } catch { throw journalFailure('浏览器中的原提交记录不完整或身份不符；为避免重复决定，已暂停提交。请先核对服务器审查记录。'); }
}
function savePending(actor, caseId, pending) {
  const key = pendingKey(actor, caseId);
  const record = JSON.stringify({ version: 1, actor_id: actor.id, tenant_id: actor.tenant_id, role: actor.role, case_id: caseId, pending });
  try {
    const old = sessionStorage.getItem(key);
    if (old !== null && old !== record) throw new Error('PENDING_CONFLICT');
    sessionStorage.setItem(key, record);
    // 写后回读确认。没有可恢复的幂等键就不发命令，防止刷新后以新键重复提交。
    if (sessionStorage.getItem(key) !== record) throw new Error('PENDING_NOT_DURABLE');
  } catch { throw journalFailure('无法可靠保存原提交和幂等键；本次没有再次发送。请恢复浏览器会话存储后重试原提交。'); }
}
function clearPending(actor, caseId) {
  try {
    const key = pendingKey(actor, caseId); sessionStorage.removeItem(key);
    if (sessionStorage.getItem(key) !== null) throw new Error('REMOVE_FAILED');
  } catch { throw journalFailure('服务器已返回明确结果，但浏览器恢复记录尚未清理；请恢复会话存储，再重试原提交核对结果。'); }
}
/** 登录仅保留同一身份的未决命令；明确退出清除本标签的全部命令日志，不清其他应用存储。 */
function clearOtherPending(actor = null) {
  try {
    const keep = actor ? [pendingOwner(actor), workflowPendingOwner(actor)] : [];
    const remove = [];
    for (let i = 0; i < sessionStorage.length; i += 1) {
      const key = sessionStorage.key(i);
      if ((key?.startsWith(PENDING_PREFIX) || key?.startsWith(WORKFLOW_PENDING_PREFIX)) && !keep.some(prefix => key.startsWith(prefix))) remove.push(key);
    }
    for (const key of remove) { sessionStorage.removeItem(key); if (sessionStorage.getItem(key) !== null) throw new Error('REMOVE_FAILED'); }
  } catch { throw journalFailure('浏览器会话存储暂不可用；只读查看仍可继续，提交前必须恢复存储。'); }
}
function expireSession() {
  state.session = null; state.drafts.clear(); state.currentCase = null; state.currentTask = null; state.flash = null;
  // 401 只说明本次无权执行，不能否定更早一次未知结果；原日志按身份保留，重新登录后核对。
  showLogin('登录已失效，请重新验证身份。未决提交仍按原身份保存，不会自动重发。');
}

/** 小型 DOM 构造器：字符串始终成为文本节点，不提供 innerHTML 通道。 */
function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === 'value') node.value = value;
    else if (key === 'checked') node.checked = value;
    else node.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child !== null && child !== undefined && child !== false) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}
function label(value) { return statusLabels[value] || String(value ?? '未提供'); }
function short(value) { return typeof value === 'string' ? value.slice(0, 12) : '未提供'; }
function date(value) { const d = new Date(value); return Number.isNaN(d.getTime()) ? '时间未提供' : d.toLocaleString('zh-CN', { hour12: false }); }
function pretty(value) { return typeof value === 'string' ? value : JSON.stringify(value, null, 2); }
function announce(message) { announcer.textContent = message; }
function badge(text, tone = '') { return el('span', { class: `badge ${tone}` }, text); }
function statusBadge(value) {
  return badge(label(value), /ACKNOWLEDGED|REVIEWED/.test(value || '') ? 'teal' : /CHANGES|WAITING|PENDING|OPEN|REQUIRED/.test(value || '') ? 'amber' : '');
}
function notice(message, tone = '') { return el('div', { class: `notice ${tone}`, role: tone === 'error' ? 'alert' : 'note' }, el('span', { class: 'notice-symbol', 'aria-hidden': 'true' }, tone === 'error' ? '!' : 'i'), el('p', {}, message)); }
function button(text, onClick, kind = '') { return el('button', { class: `button ${kind}`, type: 'button', onClick }, text); }
function panel(title, subtitle, body, extra = null) { return el('section', { class: 'panel' }, el('div', { class: 'panel-head' }, el('div', {}, el('h2', {}, title), subtitle && el('p', {}, subtitle)), extra), el('div', { class: 'panel-body' }, body)); }
function factList(items) { return el('dl', { class: 'facts' }, items.map(([key, value]) => el('div', { class: 'fact' }, el('dt', {}, key), el('dd', {}, value)))); }
function apiErrorText(error) {
  const workflowMessages = {
    NOT_TASK_ASSIGNEE: '任务已不归当前身份处理，请核对接手人。', TASK_NOT_CLAIMED: '请先接手这项任务。',
    TASK_ALREADY_CLAIMED: '任务已经接手，请读取当前状态。', STALE_TASK_REVISION: '任务版本已变化，请重新核对。',
    STALE_ASSIGNMENT_REVISION: '任务归属已变化，原页面不能继续提交。', STALE_CASE_SCOPE: '审查范围或候选已经变化。',
    STALE_REVIEWED_BUNDLE: '当前审查材料包已经变化，请重新打开材料。', STALE_REQUEST_SPEC: '请求规格已变化或已撤回。',
    STALE_RESPONSE_POINTER: '当前响应已经更新，请审查新响应后决定。', STALE_REVIEW_POLICY: '审查策略版本已经变化。',
    RESPONSE_INTEGRITY_ERROR: '响应身份或完整性核对失败，不能接受。', EVIDENCE_INTEGRITY_ERROR: '原件内容与登记摘要不符，已停止展示。',
    INVALID_REQUEST_ITEMS: '请求项不符合登记的证据要求。', UNAUTHORIZED_EVIDENCE_TARGET: '该 Job 或阶段不在授权目录内。',
    CASE_BUDGET_EXHAUSTED: '本案累计预算不足，请核对已消费和待对账额度。', REQUEST_NOT_BLOCKED: '请求当前不处于待澄清状态。',
  };
  if (workflowMessages[error.code]) return workflowMessages[error.code];
  const messages = { LOGIN_REQUIRED: '登录已失效，请重新登录。', LOGIN_REJECTED: '访问码不正确或暂时受到登录限速。', CSRF_REJECTED: '会话校验失败，请重新登录。', INVALID_LOGIN: '访问码格式不正确。', ORIGIN_REJECTED: '请求来源未通过工作台校验。', UNAUTHORIZED: '登录已失效，请重新登录。', AUTH_REQUIRED: '请先登录工作台。', FORBIDDEN: '当前身份没有操作权限。', NOT_FOUND: '对象不存在，或当前身份无权查看。', CASE_NOT_FOUND: '审查对象不存在，或当前身份无权查看。', EVIDENCE_NOT_FOUND: '证据不存在，或当前身份无权查看。', STALE_REVISION: '材料状态已发生变化。请刷新版本并重新核对后再提交。', STALE_BASIS: '候选或材料版本已变化。请刷新版本并重新核对。', IDEMPOTENCY_CONFLICT: '此提交标识已用于另一份内容，请刷新后重新提交。', INVALID_REQUEST: '提交内容不符合接口要求，请检查必填项。', INVALID_COMMAND: '提交内容不符合接口要求，请检查必填项。', INVALID_CODE: '访问码不正确或已失效。', CSRF_INVALID: '会话校验失败，请重新登录。', CSRF_REQUIRED: '会话校验失败，请重新登录。', INVALID_CSRF: '会话校验失败，请重新登录。' };
  if (error.network) return error.readOnly ? '服务连接中断或超时，请重新读取页面。' : '连接中断，尚不能确认服务器是否已保存。请重试同一提交以查询结果。';
  return messages[error.code] || (error.status === 409 ? '材料状态或提交标识发生冲突。请刷新版本后重新核对。' : error.status === 403 ? '当前身份没有操作权限，或会话校验失败。' : error.status === 401 ? '访问码无效或登录已失效。' : `请求未完成（${error.code || error.status || '连接异常'}）。`);
}
/** GET 仅用于展示；POST 带 CSRF，命令的 actor 一律由服务器会话决定。 */
async function api(path, { method = 'GET', body, version = 'v1' } = {}) {
  const controller = new AbortController();
  // 超时只停止浏览器等待，不假定服务器操作已经取消；写请求必须保留原幂等键。
  const timeout = setTimeout(() => controller.abort(), method === 'GET' ? 15000 : 20000);
  try {
    let response;
    try {
      response = await fetch(`/api/${version}${path}`, { method, credentials: 'same-origin', signal: controller.signal, headers: { ...(body ? { 'content-type': 'application/json' } : {}), ...(method !== 'GET' && state.session ? { 'x-csrf-token': state.session.csrf_token } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}), cache: 'no-store' });
    } catch { throw Object.assign(new Error('NETWORK_UNCERTAIN'), { network: true, readOnly: method === 'GET' }); }
    let result;
    try { result = await response.json(); }
    catch { throw Object.assign(new Error('INVALID_SERVER_RESPONSE'), { status: response.status, network: method !== 'GET' && response.ok, readOnly: method === 'GET' }); }
    if (!result || typeof result !== 'object' || Array.isArray(result)) throw Object.assign(new Error('INVALID_SERVER_RESPONSE'), { status: response.status, network: method !== 'GET', readOnly: method === 'GET' });
    if (!response.ok) throw Object.assign(new Error(result.error_code || 'REQUEST_FAILED'), { status: response.status, code: result.error_code });
    return result;
  } finally { clearTimeout(timeout); }
}
function brand() { return el('a', { class: 'brand', href: '#/tasks' }, el('span', { class: 'brand-mark', 'aria-hidden': 'true' }, 'E'), el('div', {}, el('div', { class: 'brand-name' }, 'EGAP'), el('div', { class: 'brand-sub' }, 'REVIEW WORKBENCH'))); }
function pageHeading(eyebrow, title, description, action) { return el('div', { class: 'page-heading' }, el('div', {}, el('div', { class: 'eyebrow' }, eyebrow), el('h1', {}, title), el('p', {}, description)), action); }
function shell(active, pageTitle) {
  const actor = state.session.actor;
  const navItems = [['01', '我的任务', '#/tasks', 'tasks'], ['02', '任务处理', state.currentTask ? `#/tasks/${encodeURIComponent(state.currentTask.id)}` : null, 'task'], ['03', '历史材料', '#/history', 'history'], ['04', '证据详情', null, 'evidence']];
  const sidebar = el('aside', { class: 'sidebar' }, brand(), el('div', { class: 'sidebar-caption' }, '审查工作流'), el('nav', { 'aria-label': '主导航' }, navItems.map(([number, text, href, key]) => href ? el('a', { class: `nav-link ${active === key ? 'active' : ''}`, href, 'aria-current': active === key ? 'page' : undefined }, el('span', { class: 'nav-index' }, number), text) : el('span', { class: `nav-link ${active === key ? 'active' : 'disabled'}`, 'aria-current': active === key ? 'page' : undefined }, el('span', { class: 'nav-index' }, number), text))), el('div', { class: 'sidebar-footer' }, el('strong', {}, el('span', { class: 'lab-dot' }), '本地实验 · 首版工作台'), el('p', {}, '候选、证据与决定逐项关联。'), el('p', {}, '材料确认不授予修复或发布权限。')));
  const main = el('main', { class: 'main', id: 'main-content', tabIndex: '-1' });
  const top = el('header', { class: 'topbar' }, el('div', { class: 'crumbs' }, el('span', {}, '工程审查'), el('span', { 'aria-hidden': 'true' }, '/'), el('b', {}, pageTitle)), el('div', { class: 'identity' }, el('span', { class: 'avatar', 'aria-hidden': 'true' }, actor.role === 'QA' ? 'QA' : actor.role === 'SOURCE' ? '源' : '业'), el('div', { class: 'identity-text' }, actor.display_name, el('small', {}, `${roles[actor.role] || actor.role} · 实验身份`)), button('退出', logout, 'subtle')));
  app.replaceChildren(el('div', { class: 'shell' }, sidebar, el('div', { class: 'workspace' }, top, main)));
  document.title = `${pageTitle} · EGAP 审查工作台`;
  return main;
}
function footer() { return el('footer', { class: 'page-footer' }, el('span', {}, 'EGAP · 真实材料 / 范围明确 / 决定可追溯'), el('span', {}, '机器验证与人员审查分别记录')); }
async function logout() {
  try {
    await api('/logout', { method: 'POST', body: {} });
    let warning = ''; try { clearOtherPending(); } catch (error) { warning = error.message; }
    state.session = null; state.currentCase = null; state.currentTask = null; state.drafts.clear(); state.flash = null; showLogin(warning);
  }
  catch (error) { const main = document.querySelector('main'); main?.prepend(notice(apiErrorText(error), 'error')); }
}
function showLogin(message = '') {
  state.routeVersion += 1;
  const status = el('div', { class: 'form-status', role: 'alert' }, message);
  const codeInput = el('input', { id: 'access-code', type: 'password', required: true, autocomplete: 'off', spellcheck: 'false', placeholder: '输入本机配置中的身份访问码', maxLength: 256 });
  const submit = el('button', { class: 'button primary', type: 'submit' }, '进入审查工作台 →');
  const form = el('form', { class: 'login-form', onSubmit: async event => {
    event.preventDefault(); submit.disabled = true; status.className = 'form-status'; status.textContent = '正在验证身份…';
    try {
      const session = await api('/session', { method: 'POST', body: { code: codeInput.value.trim() } });
      // 登录边界清除上一身份的草稿与对象，防止会话过期后跨角色继承审查意图。
      state.drafts.clear(); state.currentCase = null; state.currentTask = null; state.flash = null; state.session = session;
      try { clearOtherPending(session.actor); state.journalBlocked = null; } catch (error) { state.journalBlocked = error.message; }
      codeInput.value = ''; announce('登录成功'); if (!location.hash || !location.hash.startsWith('#/')) location.hash = '#/tasks'; else void renderRoute();
    } catch (error) { status.className = 'form-status error'; status.textContent = apiErrorText(error); codeInput.focus(); }
    finally { submit.disabled = false; }
  } }, el('div', { class: 'form-field' }, el('label', { for: 'access-code' }, '实验身份访问码'), codeInput, el('p', { class: 'form-hint' }, '访问码决定身份和审查范围，页面不能自行切换权限。')), submit, status);
  app.replaceChildren(el('div', { class: 'login' }, el('section', { class: 'login-story' }, brand(), el('div', { class: 'eyebrow' }, 'EVIDENCE · REVIEW · ACCOUNTABILITY'), el('h1', {}, '每一个工程决定，', el('br'), el('span', {}, '都有证据可循。')), el('p', {}, '从真实实验材料出发，让业务、数据源与 QA 在同一个候选版本上核对事实、补充证据并留下决定。'), el('ol', { class: 'story-steps' }, [['01', '找到与你有关的问题', '按身份查看分配的审查义务。'], ['02', '沿着证据核对事实', '区分原始草稿、业务结果与实际交付。'], ['03', '提交有范围的决定', '每次决定绑定候选、材料版本和依据。']].map(([number, title, body]) => el('li', { class: 'story-step' }, el('span', { class: 'step-number' }, number), el('div', {}, el('strong', {}, title), el('p', {}, body))))), el('footer', {}, 'LOCAL LAB / 本地实验身份 · 非企业 SSO')), el('main', { class: 'login-form-wrap', id: 'main-content' }, el('div', { class: 'login-form-inner' }, el('div', { class: 'eyebrow' }, 'WELCOME TO EGAP'), el('h2', {}, '登录审查工作台'), el('p', {}, '查看已有实验，完成一次可追溯的材料审查。'), el('div', { class: 'role-preview', 'aria-label': '可配置的实验身份' }, el('div', {}, '业务负责人', el('small', {}, '规则与结论')), el('div', {}, '数据源负责人', el('small', {}, '来源与完整性')), el('div', {}, 'QA 审查员', el('small', {}, '证据与交付'))), form, el('p', { class: 'login-note' }, '访问码由启动配置在本机生成，请从私有登录信息文件读取对应身份的 code。访问码不会保存到浏览器本地存储。'), notice('本期审查历史固定计划实验。人员确认不等于 Agent 验证通过，也不授予代码发布权限。')))));
  document.title = '登录 · EGAP 审查工作台';
}
function loading(main) { main.append(el('div', { class: 'loading', role: 'status' }, el('span', { class: 'spinner', 'aria-hidden': 'true' }), '正在读取审查材料…')); }
function showError(main, error) {
  if (error.status === 401) { expireSession(); return; }
  main.replaceChildren(el('section', { class: 'panel error-box' }, el('div', { class: 'eyebrow' }, 'REQUEST NOT COMPLETED'), el('h2', {}, '暂时无法读取此页面'), el('p', { class: 'muted' }, apiErrorText(error)), button('重新读取', () => void renderRoute(), 'primary'), el('p', { class: 'form-hint' }, '重试页面查询不会推进实验、生成模型请求或提交决定。')));
}

/** 路由序号隔离迟到的 fetch：快速切换页面时，旧响应不能覆盖新页面。 */
async function renderRoute() {
  if (!state.session) { showLogin(); return; }
  const version = ++state.routeVersion;
  const parts = location.hash.replace(/^#\/?/, '').split('/');
  const kind = parts[0] || 'tasks';
  const active = kind === 'cases' || kind === 'history' ? 'history' : kind === 'tasks' && parts[1] ? 'task' : kind === 'evidence' ? 'evidence' : 'tasks';
  const main = shell(active, kind === 'cases' ? '历史审查详情' : kind === 'history' ? '历史材料' : kind === 'evidence' ? '证据详情' : parts[1] ? '任务处理' : '我的任务');
  loading(main);
  try {
    if (kind === 'tasks' && parts[1]) {
      const data = await api(`/review-tasks/${encodeURIComponent(decodeURIComponent(parts[1]))}`, { version: 'v2' });
      if (version !== state.routeVersion) return; state.currentTask = data.task; renderWorkflowTask(main, data);
    } else if (kind === 'tasks') {
      const data = await api('/me/review-tasks', { version: 'v2' }); if (version !== state.routeVersion) return; renderWorkflowTasks(main, data.tasks);
    } else if (kind === 'history') {
      const data = await api('/me/review-tasks'); if (version !== state.routeVersion) return; renderTasks(main, data.tasks);
    } else if (kind === 'cases' && parts[1]) {
      const data = await api(`/review-cases/${encodeURIComponent(decodeURIComponent(parts[1]))}`); if (version !== state.routeVersion) return; state.currentCase = data; renderCase(main, data);
    } else if (kind === 'evidence' && parts[1]) {
      const data = await api(`/evidence/${encodeURIComponent(decodeURIComponent(parts[1]))}`); if (version !== state.routeVersion) return; renderEvidence(main, data);
    } else { main.replaceChildren(notice('页面不存在，请从“我的任务”选择审查对象。', 'warn')); }
  } catch (error) { if (version === state.routeVersion) showError(main, error); }
}
function renderTasks(main, tasks) {
  const pending = tasks.filter(t => !/ACKNOWLEDGED|REVIEWED/.test(t.obligation_status));
  const reviewed = tasks.length - pending.length;
  const stat = (title, value, caption) => el('div', { class: 'stat' }, el('div', { class: 'stat-top' }, title), el('div', { class: 'stat-value' }, value), el('div', { class: 'stat-caption' }, caption));
  main.replaceChildren(pageHeading('HISTORICAL MATERIAL / V1', '历史材料审查', `以${roles[state.session.actor.role]}身份，核对当前候选的证据与业务关系。每项确认都有明确的材料范围。`, button('刷新任务', () => void renderRoute())), notice('这里保留旧版材料确认记录；它们不是新任务的接手、响应接受或义务完成记录。历史实验与新证据调查分别记录。'), el('div', { class: 'stats' }, stat('待处理审查义务', pending.length, '包含待确认与要求补充的事项'), stat('已核对材料', reviewed, '仅代表本身份已提交确认'), stat('可见审查对象', tasks.length, '按服务端身份与租户权限筛选')), el('section', { class: 'panel' }, el('div', { class: 'panel-head' }, el('div', {}, el('h2', {}, '分配给我的审查'), el('p', {}, '从一个问题开始，沿证据完成一次决定。')), badge(`${tasks.length} 个对象`)), tasks.length ? el('ul', { class: 'task-list' }, tasks.map(t => el('li', { class: 'task' }, el('div', {}, el('div', { class: 'badges' }, statusBadge(t.obligation_status), badge('固定计划 · 历史实验')), el('a', { class: 'task-title', href: `#/cases/${encodeURIComponent(t.case_id)}` }, t.title), el('p', { class: 'task-description' }, t.obligation_title), el('div', { class: 'task-meta' }, el('span', {}, '候选 ', el('code', {}, short(t.candidate_sha))), el('span', {}, `审查状态 v${t.revision}`), el('span', {}, `材料 ${short(t.bundle_digest)}`))), el('a', { class: 'button task-action', href: `#/cases/${encodeURIComponent(t.case_id)}` }, '进入审查 →')))) : el('div', { class: 'empty' }, el('strong', {}, '当前没有分配给你的任务'), '可见范围由服务端身份与已导入的审查义务决定。')), footer());
  announce(`已读取 ${tasks.length} 个审查任务`);
}
function limitations(items) { return el('ul', { class: 'limitation-list' }, (items || []).map(item => el('li', {}, limitationLabels[item] || item))); }
function evidenceList(items) { return items.length ? items.map(e => el('article', { class: 'evidence-item' }, el('div', { class: 'evidence-top' }, el('a', { class: 'evidence-title', href: `#/evidence/${encodeURIComponent(e.id)}` }, e.title), badge(e.kind)), el('p', {}, e.source_ref), el('code', {}, `SHA-256 ${short(e.sha256)}…`))) : el('p', { class: 'muted small' }, '当前材料包没有可展示的证据，不能据此认定业务通过。'); }
function machineFacts(result) {
  const entries = Object.entries(result || {});
  if (!entries.length) return el('p', { class: 'muted small' }, '未导入机器结果。');
  const names = { interpretation: '结果解释', business_assertions_passed: '通过的业务断言', operations_completed: '已完成操作', historical_verified_evidence_refs_reported: '原报告核验的证据引用数', evidence_reference_note: '证据引用范围说明', sut_model: '被测客服模型', sut_model_calls: '被测客服模型调用数', verifier_autonomous_execution: 'Verifier 自主执行', release_authorized: '发布授权', source_date: '原实验日期', source_candidate_sha: '原实验候选', status: '限定结果', scope: '执行范围', execution_mode: '执行方式', checks: '检查项', assertions: '业务断言', checks_by_stage: '分阶段检查数', before: '封存前结果', after: '封存后结果', eligibility: '资格结果', candidate_sha: '被测候选', baseline_sha: '比较基线', agent_dispatch: 'Agent 自主派发', protected_final: '受保护 Final', release: '发布', label: '场景', total_checks: '已执行检查数' };
  return factList(entries.map(([key, value]) => [names[key] || key, typeof value === 'object' && value !== null ? el('pre', { class: 'raw-content' }, pretty(value)) : typeof value === 'boolean' ? value ? '是' : '否' : label(value)]));
}
function renderCase(main, data) {
  const flash = state.flash; state.flash = null;
  const own = data.actor_obligation || data.obligations.find(o => o.role === state.session.actor.role);
  const left = el('div', {}, panel('需要核对的业务关系', '三方分别承担自己的材料审查义务；各自的确认互不覆盖。', data.obligations.map(o => el('article', { class: `obligation ${o.role === state.session.actor.role ? 'mine' : ''}` }, el('div', { class: 'obligation-top' }, el('span', { class: 'obligation-name' }, roles[o.role], o.role === state.session.actor.role ? ' · 我的义务' : ''), statusBadge(o.status)), el('h3', {}, o.title), el('p', {}, o.instruction)))), panel('可审查的证据', '查看来源、哈希与适用范围。材料内容以文本显示，不执行其中的指令。', evidenceList(data.evidence), badge(`${data.evidence.length} 份材料`)), panel('历史机器结果', '保留原实验的执行范围和限制，不将结果提升为发布结论。', machineFacts(data.machine_result)), panel('尚未覆盖的范围', '这些限制不因人员点击确认而消失。', limitations(data.limitations)));
  const right = el('div', {}, own ? decisionPanel(data, own) : panel('当前身份没有审查义务', null, el('p', { class: 'muted small' }, '你可以查看已授权的材料，但不能代替其他角色提交决定。')), panel('决定与补证记录', '事件按服务端提交顺序保存；刷新页面不会推进执行。', auditTimeline(data)));
  main.replaceChildren(el('a', { class: 'breadcrumb-back', href: '#/history' }, '← 返回历史材料'), pageHeading('CHANGE REVIEW / HISTORICAL MATERIAL', data.title, '在准确候选与固定材料包上完成审查。原始草稿、业务事实和最终交付需要分别核对。', button('刷新版本', () => void renderRoute())), flash && notice(flash, 'success'), el('dl', { class: 'metadata' }, el('div', {}, el('dt', {}, '被测候选'), el('dd', { class: 'mono' }, data.candidate_sha)), el('div', {}, el('dt', {}, '比较基线'), el('dd', {}, data.baseline_sha ? el('code', {}, data.baseline_sha) : '未提供 · 不构成新旧版本对照')), el('div', {}, el('dt', {}, '当前绑定版本'), el('dd', {}, `审查状态 v${data.revision}`, el('br'), el('code', {}, `Bundle ${short(data.bundle_digest)}`)))), el('div', { class: 'split-cards' }, el('section', { class: 'summary-card machine' }, el('div', { class: 'section-label' }, 'MACHINE / 机器侧'), badge('历史固定计划实验', 'teal'), el('h2', {}, '限定实验结果已归档'), el('p', {}, '展示已运行的业务检查。自主 Agent 派发、受保护 Final 和发布仍分别受门禁约束。')), el('section', { class: 'summary-card human' }, el('div', { class: 'section-label' }, 'REVIEW / 人员侧'), statusBadge(data.review_status), el('h2', {}, own?.title || '材料审查'), el('p', {}, '确认仅适用于当前材料与自己的审查义务；不能替代机器验证或其他人员的决定。'))), el('div', { class: 'two-column' }, left, right), footer());
  announce('审查详情已读取');
}
function auditTimeline(data) {
  const events = [...(data.events || [])].reverse();
  return events.length ? el('ol', { class: 'timeline' }, events.map(event => {
    const payload = event.payload || {};
    const matching = [...(data.decisions || []), ...(data.evidence_requests || [])].find(d => d.id === payload.command_id || d.id === payload.id || d.id === payload.decision_id || d.id === payload.request_id);
    const content = matching || payload;
    return el('li', {}, el('strong', {}, label(event.kind)), el('time', { datetime: event.created_at }, date(event.created_at)), el('p', {}, [content.role ? roles[content.role] || content.role : null, content.decision ? label(content.decision) : null, content.status ? label(content.status) : null].filter(Boolean).join(' · ')), content.question && el('p', {}, `补证问题：${content.question}`), content.reason && el('p', {}, content.reason), el('p', { class: 'small' }, `事件 #${event.seq}`));
  })) : el('p', { class: 'muted small' }, '暂无审查提交记录。');
}

/**
 * 一次提交冻结 payload + 幂等键。网络回执不明确时只允许重试同一份 payload，
 * 不能换 key 猜测重发；409 后显式刷新，保留理由草稿但重新绑定服务器版本。
 */
function decisionPanel(data, obligation) {
  let restored = null; let journalError = state.journalBlocked ? journalFailure(state.journalBlocked) : null;
  try { restored = restorePending(state.session.actor, data.id); } catch (error) { journalError = error; }
  const draft = state.drafts.get(data.id) || { mode: 'decision', decision: 'ACKNOWLEDGE', reason: '', question: '', refs: [], pending: null };
  if (restored) {
    draft.pending = restored; draft.mode = restored.endpoint.endsWith('/evidence-requests') ? 'request' : 'decision';
    draft.reason = restored.body.reason; draft.question = restored.body.question || '';
    draft.decision = restored.body.decision || 'ACKNOWLEDGE'; draft.refs = [...restored.body.evidence_refs];
  }
  state.drafts.set(data.id, draft);
  const status = el('div', { class: 'form-status', role: 'status', 'aria-live': 'polite' });
  const body = el('div');
  const panelNode = panel('提交我的审查', `${roles[state.session.actor.role]} · ${obligation.title}`, body);
  const tabs = el('div', { class: 'tab-toggle', role: 'tablist', 'aria-label': '审查操作' });
  const decisionTab = el('button', { id: 'decision-tab', type: 'button', role: 'tab', 'aria-controls': 'review-action-panel', onClick: () => switchMode('decision') }, '审查决定');
  const requestTab = el('button', { id: 'request-tab', type: 'button', role: 'tab', 'aria-controls': 'review-action-panel', onClick: () => switchMode('request') }, '请求补充证据');
  tabs.append(decisionTab, requestTab);
  const decisionSelect = el('select', { id: 'review-decision', onChange: event => { draft.decision = event.target.value; } }, el('option', { value: 'ACKNOWLEDGE' }, '确认已核对当前材料'), el('option', { value: 'REQUEST_CHANGES' }, '要求补充或修改'));
  decisionSelect.value = draft.decision;
  const questionInput = el('textarea', { id: 'review-question', rows: '3', maxLength: 2000, placeholder: '例如：请补充最终私有备注，判断交付文案是否明确注明实验范围。', onInput: event => { draft.question = event.target.value; } }, draft.question);
  const reasonInput = el('textarea', { id: 'review-reason', rows: '4', required: true, maxLength: 3000, placeholder: '写清已核对的事实、仍未闭环的问题，以及此次决定的适用范围。', onInput: event => { draft.reason = event.target.value; } }, draft.reason);
  const refs = el('div', { class: 'check-list' }, data.evidence.map(e => el('label', { class: 'check-item' }, el('input', { type: 'checkbox', name: 'evidence_refs', value: e.id, checked: draft.refs.includes(e.id), onChange: event => { if (event.target.checked) draft.refs.push(e.id); else draft.refs = draft.refs.filter(id => id !== e.id); } }), el('span', {}, e.title))));
  const choiceField = el('div', { class: 'form-field' }, el('label', { for: 'review-decision' }, '决定范围'), decisionSelect);
  const questionField = el('div', { class: 'form-field' }, el('label', { for: 'review-question' }, '希望补充什么证据'), questionInput);
  const requestNote = notice('补证请求会持久化为 WAITING_CAPABILITY。当前尚未接通自动实验派发，提交不会伪造执行或完成。', 'warn');
  const submit = el('button', { type: 'submit', class: 'button primary' }, '提交审查决定');
  const retry = button('重试同一提交', () => void sendPending(), 'primary'); retry.hidden = true;
  const refresh = button('刷新版本并保留草稿', () => { void renderRoute(); }); refresh.hidden = true;
  const form = el('form', { id: 'review-action-panel', role: 'tabpanel', onSubmit: event => {
    event.preventDefault();
    if (draft.pending) return;
    if (!reasonInput.value.trim()) { reasonInput.focus(); status.textContent = '请填写决定或请求的理由。'; status.className = 'form-status error'; return; }
    if (draft.mode === 'request' && !questionInput.value.trim()) { questionInput.focus(); status.textContent = '请说明希望补充的证据。'; status.className = 'form-status error'; return; }
    if (draft.mode === 'decision' && draft.decision === 'ACKNOWLEDGE' && !draft.refs.length) { status.textContent = '确认材料至少需要选择一份作为依据的证据。'; status.className = 'form-status error'; return; }
    draft.pending = { endpoint: `/review-cases/${encodeURIComponent(data.id)}/${draft.mode === 'request' ? 'evidence-requests' : 'decisions'}`, body: { idempotency_key: crypto.randomUUID(), expected_revision: data.revision, candidate_sha: data.candidate_sha, bundle_digest: data.bundle_digest, reason: reasonInput.value.trim(), evidence_refs: [...draft.refs], ...(draft.mode === 'request' ? { question: questionInput.value.trim() } : { decision: draft.decision }) } };
    void sendPending();
  } }, choiceField, questionField, requestNote, el('div', { class: 'form-field' }, el('label', { for: 'review-reason' }, '理由与核对范围'), reasonInput), el('div', { class: 'field-label' }, '引用证据'), el('p', { class: 'form-hint' }, '确认材料时至少选择一份依据；补证请求可以说明当前缺少何种证据。'), refs, el('p', { class: 'form-hint' }, `本次提交绑定审查状态 v${draft.pending?.body.expected_revision ?? data.revision} 与 Bundle ${short(draft.pending?.body.bundle_digest ?? data.bundle_digest)}。身份由服务端会话确定。`), el('div', { class: 'form-actions' }, submit, retry, refresh), status);
  function setDisabled(disabled) { for (const control of form.querySelectorAll('input,textarea,select')) control.disabled = disabled; decisionTab.disabled = disabled; requestTab.disabled = disabled; submit.disabled = disabled; }
  function switchMode(mode) {
    if (draft.pending && mode !== draft.mode) return;
    draft.mode = mode; decisionTab.setAttribute('aria-selected', String(mode === 'decision')); requestTab.setAttribute('aria-selected', String(mode === 'request'));
    decisionTab.tabIndex = mode === 'decision' ? 0 : -1; requestTab.tabIndex = mode === 'request' ? 0 : -1; form.setAttribute('aria-labelledby', mode === 'decision' ? 'decision-tab' : 'request-tab');
    choiceField.hidden = mode !== 'decision'; questionField.hidden = mode !== 'request'; requestNote.hidden = mode !== 'request'; questionInput.required = mode === 'request'; submit.textContent = mode === 'request' ? '保存补证请求' : '提交审查决定';
  }
  async function sendPending() {
    if (!draft.pending) return;
    const submittingSession = state.session;
    const pending = draft.pending;
    setDisabled(true); retry.disabled = true; refresh.hidden = true; status.className = 'form-status'; status.textContent = '正在保存有版本约束的审查记录…';
    try {
      // 首次与每次重试均先持久化；sessionStorage 拒绝写入时不会发出任何业务命令。
      savePending(submittingSession.actor, data.id, pending);
      const result = await api(pending.endpoint, { method: 'POST', body: pending.body });
      // 已退出或换身份时，迟到回执仍由服务器保存，但不污染新身份的页面。
      if (state.session !== submittingSession) return;
      if (typeof result.id !== 'string' || !Number.isInteger(result.revision) || result.revision <= pending.body.expected_revision || result.case_id !== data.id || result.candidate_sha !== pending.body.candidate_sha || result.bundle_digest !== pending.body.bundle_digest || result.actor_id !== submittingSession.actor.id || result.role !== submittingSession.actor.role || result.kind !== (pending.endpoint.endsWith('/evidence-requests') ? 'EVIDENCE_REQUEST' : 'DECISION')) throw Object.assign(new Error('INVALID_COMMAND_RECEIPT'), { network: true });
      clearPending(submittingSession.actor, data.id);
      draft.pending = null; state.drafts.delete(data.id);
      state.flash = result.kind === 'EVIDENCE_REQUEST' ? `补证请求已保存（审查状态 v${result.revision}），当前等待执行能力接通。尚未派发实验。` : `审查决定已保存（审查状态 v${result.revision}）。它仅适用于当前材料，不构成发布批准。`;
      announce(state.flash); void renderRoute();
    } catch (error) {
      if (state.session !== submittingSession) return;
      if (error.status === 401) { expireSession(); return; }
      status.className = 'form-status error'; status.textContent = error.journal ? error.message : apiErrorText(error);
      if (error.journal || error.network || error.status >= 500) { retry.hidden = false; retry.disabled = false; submit.hidden = true; status.textContent += error.journal ? '\n原提交仍保留在本页；存储恢复前不会再次发送。' : '\n原始提交和幂等键已保存在本标签会话中，刷新后仍可重试同一提交。'; }
      else {
        try { clearPending(submittingSession.actor, data.id); }
        catch (journalError) { status.textContent = journalError.message; retry.hidden = false; retry.disabled = false; submit.hidden = true; return; }
        draft.pending = null; retry.hidden = true; submit.hidden = false; setDisabled(false);
        if (error.status === 409) { submit.disabled = true; refresh.hidden = false; }
      }
    }
  }
  tabs.addEventListener('keydown', event => {
    if (draft.pending || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault(); const mode = event.key === 'Home' ? 'decision' : event.key === 'End' ? 'request' : draft.mode === 'decision' ? 'request' : 'decision';
    switchMode(mode); (mode === 'decision' ? decisionTab : requestTab).focus();
  });
  body.append(tabs, form); switchMode(draft.mode);
  if (draft.pending) {
    setDisabled(true); submit.hidden = true; retry.hidden = false; status.className = 'form-status warn';
    status.textContent = `已恢复原提交：审查状态 v${draft.pending.body.expected_revision}，当前页面 v${data.revision}。仅重试原 payload 与幂等键，不会改绑为新版本。`;
    body.prepend(el('div', { class: 'notice warn' }, el('div', {}, el('strong', {}, '有一笔待核对的原提交'), el('p', { class: 'small' }, '原幂等键：', el('code', {}, draft.pending.body.idempotency_key)), el('p', { class: 'small' }, '原候选：', el('code', {}, draft.pending.body.candidate_sha)), el('p', { class: 'small' }, '原 Bundle：', el('code', {}, draft.pending.body.bundle_digest)), el('p', { class: 'small' }, `原证据引用：${draft.pending.body.evidence_refs.join('、') || '无'}`))));
  }
  if (journalError) { setDisabled(true); submit.hidden = true; retry.hidden = true; status.className = 'form-status error'; status.textContent = journalError.message; }
  return panelNode;
}
function renderEvidence(main, data) {
  main.replaceChildren(el('a', { class: 'breadcrumb-back', href: `#/cases/${encodeURIComponent(data.case_id)}` }, '← 返回关联审查'), pageHeading('EVIDENCE DETAIL / READ ONLY', data.title, '核对这份材料能支持什么、不能支持什么。展示内容来自服务端保存的材料快照。', button('重新读取', () => void renderRoute())), notice('此处的材料是证据文本。原始草稿、最终交付与确定性事实属于不同观察层，不能互相替代。'), el('div', { class: 'two-column' }, el('div', {}, panel('材料内容', '按原有文本结构显示；不会执行链接、HTML、脚本或嵌入指令。', el('pre', { class: 'raw-content', tabIndex: '0', 'aria-label': '证据内容' }, data.content), badge(data.kind))), el('div', {}, panel('来源与绑定身份', null, factList([['证据标识', el('code', {}, data.id)], ['来源', data.source_ref], ['内容 SHA-256', el('code', {}, data.sha256)], ['源文件 SHA-256', el('code', {}, data.source_sha256)], ['候选提交', el('code', {}, data.candidate_sha)], ['材料包摘要', el('code', {}, data.bundle_digest)]])), panel('完整性与适用限制', '哈希用于核对材料身份，不自行证明业务内容正确。', data.limitations?.length ? limitations(data.limitations) : el('p', { class: 'muted small' }, '该条材料未附加单独限制；仍受审查对象整体限制约束。')))), footer());
  announce('证据详情已读取');
}
/**
 * v2 任务页面 → 有版本的工作流命令 → Runtime 证据调查 → 逐项响应 → 人员限定接受。
 * 页面只提交人的意图，不自行推进执行，也不从模型提案推断义务已完成。
 */
function workflowPendingOwner(actor) { return `${WORKFLOW_PENDING_PREFIX}${encodeURIComponent(actor.tenant_id)}:${encodeURIComponent(actor.id)}:`; }
function workflowPendingScope(actor, taskId) { return `${workflowPendingOwner(actor)}${encodeURIComponent(taskId)}:`; }
function workflowPendingKey(actor, taskId, action) { return workflowPendingScope(actor, taskId) + encodeURIComponent(action); }
function workflowBase(task) {
  return { expected_task_revision: task.task_revision, expected_assignment_revision: task.assignment_revision,
    expected_case_scope_revision: task.case_scope_revision, reviewed_bundle_id: task.bound_bundle_id,
    reviewed_bundle_digest: task.bound_bundle_digest, candidate_sha: task.candidate_sha };
}
/** 恢复记录按租户、人员、Task、动作隔离；旧 v1 Case 记录不能用来接受 v2 响应。 */
function restoreWorkflowPending(actor, taskId) {
  let records;
  try {
    records = [];
    for (let i = 0; i < sessionStorage.length; i += 1) {
      const key = sessionStorage.key(i);
      if (key?.startsWith(workflowPendingScope(actor, taskId))) records.push([key, sessionStorage.getItem(key)]);
    }
  } catch { throw journalFailure('无法读取任务提交恢复记录；已暂停写入，可继续查看材料。'); }
  if (!records.length) return null;
  try {
    if (records.length !== 1) throw new Error('MULTIPLE_PENDING');
    const [key, raw] = records[0];
    const record = JSON.parse(raw); const pending = record.pending; const body = pending.body;
    const taskPath = `/review-tasks/${encodeURIComponent(taskId)}/`;
    const taskAction = pending.endpoint.startsWith(taskPath) && ['claim', 'reassign', 'evidence-requests'].includes(pending.endpoint.slice(taskPath.length));
    const requestAction = /^\/evidence-requests\/[a-zA-Z0-9%_.-]+\/(accept|withdraw|clarify)$/.test(pending.endpoint);
    const suffix = pending.endpoint.split('/').at(-1);
    const extraKeys = { claim: [], reassign: ['assignee_id'], 'evidence-requests': ['goal', 'items'], accept: ['request_spec_revision', 'response_id', 'response_digest', 'review_policy_version'], withdraw: ['reason'], clarify: ['answer'] }[suffix];
    const keys = ['idempotency_key', 'expected_task_revision', 'expected_assignment_revision', 'expected_case_scope_revision', 'reviewed_bundle_id', 'reviewed_bundle_digest', 'candidate_sha', ...(extraKeys || [])];
    if (record.version !== 2 || record.actor_id !== actor.id || record.tenant_id !== actor.tenant_id || record.role !== actor.role || record.task_id !== taskId
      || Object.keys(record).sort().join() !== ['version','actor_id','tenant_id','role','task_id','action','pending'].sort().join()
      || key !== workflowPendingKey(actor, taskId, record.action) || typeof record.action !== 'string' || record.action.length > 240
      || Object.keys(pending).sort().join() !== 'body,endpoint' || (!taskAction && !requestAction) || !body || typeof body !== 'object' || Array.isArray(body)
      || Object.keys(body).sort().join() !== keys.sort().join()
      || !/^[a-f0-9-]{36}$/i.test(body.idempotency_key) || !/^[a-f0-9]{40}$/i.test(body.candidate_sha) || !/^[a-f0-9]{64}$/i.test(body.reviewed_bundle_digest)
      || !['expected_task_revision','expected_assignment_revision','expected_case_scope_revision'].every(name => Number.isInteger(body[name]) && body[name] > 0)
      || typeof body.reviewed_bundle_id !== 'string' || !body.reviewed_bundle_id || raw.length > 200000) throw new Error('INVALID_PENDING');
    return { action: record.action, ...pending };
  } catch { throw journalFailure('任务原提交记录不完整或绑定身份不符；已暂停写入，请先核对服务端记录。'); }
}
function saveWorkflowPending(actor, taskId, pending) {
  const key = workflowPendingKey(actor, taskId, pending.action);
  const record = JSON.stringify({ version: 2, actor_id: actor.id, tenant_id: actor.tenant_id, role: actor.role, task_id: taskId, action: pending.action, pending: { endpoint: pending.endpoint, body: pending.body } });
  try {
    const previous = restoreWorkflowPending(actor, taskId);
    if (previous && JSON.stringify(previous) !== JSON.stringify(pending)) throw new Error('PENDING_CONFLICT');
    sessionStorage.setItem(key, record);
    if (sessionStorage.getItem(key) !== record) throw new Error('NOT_DURABLE');
  } catch { throw journalFailure('无法可靠保存原任务提交和幂等键，本次没有发出请求。恢复会话存储后可重试同一提交。'); }
}
function clearWorkflowPending(actor, taskId, action) {
  try {
    const key = workflowPendingKey(actor, taskId, action); sessionStorage.removeItem(key);
    if (sessionStorage.getItem(key) !== null) throw new Error('NOT_REMOVED');
  } catch { throw journalFailure('服务端已有明确回执，但本地恢复记录尚未清除；请重试同一提交核对，不要新建命令。'); }
}
function technicalDetails(title, value) { return el('details', { class: 'technical-details' }, el('summary', {}, title), el('pre', { class: 'raw-content', tabIndex: '0' }, pretty(value))); }
function targetSummary(target) { return target ? `Job ${target.job_id} · ${target.phase} · 工单 ${target.ticket_id || ''} v${target.ticket_revision}` : '未提供目标'; }
function listText(values, empty = '无') { return Array.isArray(values) && values.length ? el('ul', { class: 'compact-list' }, values.map(value => el('li', {}, typeof value === 'string' ? value : pretty(value)))) : el('span', { class: 'muted small' }, empty); }
function workflowBadge(value) { return badge(label(value), ['COMPLETE','COMPLETED','ACCEPTED','PROVIDED'].includes(value) ? 'teal' : ['PARTIAL','UNAVAILABLE','CONFLICTING','UNKNOWN','BLOCKED','WAITING_EVIDENCE','UNASSESSED'].includes(value) ? 'amber' : ''); }
function renderWorkflowTasks(main, tasks) {
  const owned = tasks.filter(task => task.assignee_id === state.session.actor.id);
  const open = owned.filter(task => task.status !== 'COMPLETED');
  const stat = (title, value, caption) => el('div', { class: 'stat' }, el('div', { class: 'stat-top' }, title), el('div', { class: 'stat-value' }, value), el('div', { class: 'stat-caption' }, caption));
  const flash = state.flash; state.flash = null;
  main.replaceChildren(pageHeading('MY REVIEW QUEUE / V2', '我的审查任务', '先接手具体义务，再补齐证据，最后对明确版本的响应作出限定决定。', button('刷新任务', () => void renderRoute())), flash && notice(flash, 'success'),
    notice('证据关联核对与业务语义判断是两项独立义务。接手任务不代表批准，接受响应也不自动代表证据齐全。'),
    el('div', { class: 'stats' }, stat('我的未结任务', open.length, '按具体接手人分配'), stat('我的已完成义务', owned.length - open.length, '以服务端闭合条件为准'), stat('可见任务', tasks.length, '包含本身份管理范围')),
    el('section', { class: 'panel' }, el('div', { class: 'panel-head' }, el('div', {}, el('h2', {}, '责任明确的任务'), el('p', {}, '任务版本、人员归属与材料版本分别记录。')), badge(`${tasks.length} 项`)),
      tasks.length ? el('ul', { class: 'task-list' }, tasks.map(task => el('li', { class: 'task' }, el('div', {}, el('div', { class: 'badges' }, workflowBadge(task.status), badge(task.assignee_id === state.session.actor.id ? '分配给我' : `接手人 ${task.assignee_id}`)), el('a', { class: 'task-title', href: `#/tasks/${encodeURIComponent(task.id)}` }, task.title), el('p', { class: 'task-description' }, `义务 ${task.obligation_id}`), el('div', { class: 'task-meta' }, el('span', {}, `任务 v${task.task_revision} · 分配 v${task.assignment_revision}`), el('span', {}, `Bundle ${short(task.bound_bundle_digest)}`), el('span', {}, `候选 ${short(task.candidate_sha)}`))), el('a', { class: 'button task-action', href: `#/tasks/${encodeURIComponent(task.id)}` }, '处理任务 →')))) : el('div', { class: 'empty' }, el('strong', {}, '当前没有可见任务'), '任务由服务端授权范围与接手人决定。')),
    el('p', { class: 'form-hint' }, '旧版材料确认记录仍保存在 ', el('a', { href: '#/history' }, '历史材料'), '，不计作新任务的接受记录。'), footer());
  announce(`已读取 ${tasks.length} 项任务`);
}
/**
 * 命令控制器是所有 v2 人员写入的唯一入口。先冻结并持久化 payload，再发送；
 * 未知结果仅重放原命令。人、任务、归属、范围及 Bundle 均由服务器再次校验。
 */
function workflowCommander(data, root) {
  const task = data.task; let pending = null; let blocked = state.journalBlocked; let sending = false;
  try { pending = restoreWorkflowPending(state.session.actor, task.id); } catch (error) { blocked = error.message; }
  const message = el('div', { class: 'form-status', role: 'status', 'aria-live': 'polite' });
  const retry = button('核对 / 重试同一提交', () => void send(), 'primary');
  const refresh = button('重新读取任务版本', () => void renderRoute());
  const recovery = el('section', { class: 'workflow-recovery', 'aria-label': '命令提交状态' }, message, el('div', { class: 'form-actions' }, retry, refresh));
  function lock() { for (const control of root.querySelectorAll('.workflow-command input,.workflow-command textarea,.workflow-command select,.workflow-command button')) control.disabled = Boolean(pending || blocked || sending); }
  function paint() {
    recovery.hidden = !pending && !blocked;
    retry.hidden = !pending || Boolean(blocked); retry.disabled = sending;
    if (blocked) { message.className = 'form-status error'; message.textContent = blocked; }
    else if (pending && !sending) { message.className = 'form-status warn'; message.textContent = `待核对原提交：${pending.action}。任务 v${pending.body.expected_task_revision}，分配 v${pending.body.expected_assignment_revision}，Bundle ${short(pending.body.reviewed_bundle_digest)}。刷新或重新登录不会自动重发。`; }
    lock();
  }
  async function send() {
    if (!pending || sending || blocked) return;
    const session = state.session; const original = pending;
    sending = true; paint(); message.textContent = '正在保存绑定具体版本的操作…';
    try {
      saveWorkflowPending(session.actor, task.id, original);
      const result = await api(original.endpoint, { version: 'v2', method: 'POST', body: original.body });
      if (state.session !== session) return;
      if (typeof result.id !== 'string' || typeof result.kind !== 'string' || result.task_id !== task.id || result.case_id !== task.case_id || !Number.isInteger(result.task_revision) || result.task_revision < original.body.expected_task_revision || !Number.isInteger(result.assignment_revision)
        || (original.body.response_id && result.response_id !== original.body.response_id)) throw Object.assign(new Error('INVALID_WORKFLOW_RECEIPT'), { network: true });
      clearWorkflowPending(session.actor, task.id, original.action); pending = null;
      state.drafts.delete(`task:${task.id}`); state.flash = '操作已保存。以下状态来自服务端当前记录；接手和响应接受均不授予 Codex 修复或发布权限。';
      announce('操作已保存'); void renderRoute();
    } catch (error) {
      if (state.session !== session) return;
      if (error.status === 401) { expireSession(); return; }
      const uncertain = error.journal || error.network || error.status >= 500;
      if (!uncertain) {
        try { clearWorkflowPending(session.actor, task.id, original.action); pending = null; }
        catch (clearError) { blocked = clearError.message; }
        // 明确拒绝后也不自动改绑新版本，要求人重新读取并核对。
        blocked ||= `${apiErrorText(error)} 请重新读取任务后核对版本。`;
      }
      sending = false; paint(); message.className = 'form-status error'; message.textContent = error.journal ? error.message : apiErrorText(error);
      if (uncertain) message.textContent += ' 原始操作与幂等键保留，只能重试同一提交。';
    } finally { sending = false; lock(); retry.disabled = false; }
  }
  return { recovery, paint, submit(action, endpoint, extra = {}) {
    if (pending || blocked || sending) return;
    pending = { action, endpoint, body: { idempotency_key: crypto.randomUUID(), ...workflowBase(task), ...extra } };
    paint(); void send();
  } };
}
function workflowRequestForm(data, command) {
  const task = data.task; const draft = state.drafts.get(`task:${task.id}`) || { target: 0, goal: '', kinds: null };
  state.drafts.set(`task:${task.id}`, draft);
  const targets = data.allowed_targets || []; const templates = data.item_templates || [];
  // 只在首次创建草稿时设默认值；用户全部取消后留下的 [] 也必须在重绘时保留。
  if (draft.kinds === null) draft.kinds = templates.filter(item => evidenceMaterialKinds[item.kind]?.defaultSelected === true).map(item => item.kind);
  const target = el('select', { id: 'request-target', required: true, onChange: event => { draft.target = Number(event.target.value); } }, targets.map((item, index) => el('option', { value: index }, targetSummary(item))));
  target.value = String(draft.target < targets.length ? draft.target : 0);
  const goal = el('textarea', { id: 'request-goal', rows: '3', maxLength: 2000, required: true, placeholder: '例如：核对该 Job 的草稿、最终私有备注与交付关联是否属于同一对象。', onInput: event => { draft.goal = event.target.value; } }, draft.goal);
  const feedback = el('div', { class: 'form-status', role: 'status' });
  const form = el('form', { class: 'workflow-command', onSubmit: event => {
    event.preventDefault(); const chosen = targets[Number(target.value)];
    const selected = templates.filter(item => draft.kinds.includes(item.kind));
    if (!chosen || !selected.length || !goal.value.trim()) { feedback.textContent = '请选择登记的目标、至少一项材料，并填写具体核对问题。'; return; }
    command.submit('request-evidence', `/review-tasks/${encodeURIComponent(task.id)}/evidence-requests`, { goal: goal.value.trim(), items: selected.map((item) => ({ item_id: `E${templates.indexOf(item) + 1}`, kind: item.kind, target: chosen, required: true, acceptance_criteria: item.acceptance_criteria })) });
  } }, el('div', { class: 'form-field' }, el('label', { for: 'request-target' }, '登记的 Job 与阶段'), target), el('div', { class: 'field-label' }, '逐项要求'), el('div', { class: 'check-list' }, templates.map((item, index) => el('label', { class: 'check-item' }, el('input', { type: 'checkbox', value: item.kind, checked: draft.kinds.includes(item.kind), onChange: event => { draft.kinds = event.target.checked ? [...draft.kinds, item.kind] : draft.kinds.filter(kind => kind !== item.kind); } }), el('span', {}, `E${index + 1} · ${item.title}`, el('small', { class: 'item-criteria' }, Array.isArray(item.acceptance_criteria) ? item.acceptance_criteria.join('；') : String(item.acceptance_criteria)))))), el('div', { class: 'form-field' }, el('label', { for: 'request-goal' }, '本次需要回答的问题'), goal), el('p', { class: 'form-hint' }, '目标来自服务端授权目录。已保存草稿不等于供应商原始响应；无法取得的原件应明确说明。'), el('button', { class: 'button primary', type: 'submit', disabled: !targets.length || !templates.length }, '提交逐项补证请求'), feedback);
  return panel('创建逐项补证请求', '问题引导调查，固定请求项决定材料如何验收。', targets.length ? form : notice('当前范围没有登记的可取证目标，不能自由编造 Job 或运行身份。', 'warn'));
}
function renderWorkflowTask(main, data) {
  const task = data.task; const flash = state.flash; state.flash = null;
  const content = el('div', { class: 'workflow-page' }); const command = workflowCommander(data, content);
  const actions = el('div', { class: 'form-actions workflow-command' });
  if (task.can_claim) actions.append(button('接手这项任务', () => command.submit('claim', `/review-tasks/${encodeURIComponent(task.id)}/claim`), 'primary'));
  const actors = data.eligible_assignees || task.eligible_assignees || [];
  const currentActor = data.actor || state.session.actor;
  if (task.can_reassign || task.assignee_id === currentActor.id || data.case.owner_id === currentActor.id) {
    const people = el('select', { id: 'task-assignee', 'aria-label': '转派给谁' }, actors.filter(actor => actor.id !== task.assignee_id).map(actor => el('option', { value: actor.id }, `${actor.display_name} · ${roles[actor.role] || actor.role}`)));
    if (people.options.length) actions.append(el('div', { class: 'inline-field form-field' }, el('label', { for: 'task-assignee' }, '转派给'), people), button('确认转派', () => command.submit('reassign', `/review-tasks/${encodeURIComponent(task.id)}/reassign`, { assignee_id: people.value })));
  }
  const canWork = task.can_act && !task.can_claim;
  const requestPanels = (data.requests || []).map(request => workflowRequestPanel(data, request, command, canWork));
  content.append(pageHeading('TASK / SCOPED RESPONSIBILITY', task.title, '接手责任、调查材料、审查响应分别留痕。证据齐全也不自动完成业务语义判断。', button('刷新当前状态', () => void renderRoute())), flash && notice(flash, 'success'), command.recovery,
    el('div', { class: 'workflow-task-summary' }, el('div', { class: 'badges' }, workflowBadge(task.status), badge(task.obligation_id)), factList([['当前接手人', actors.find(actor => actor.id === task.assignee_id)?.display_name || task.assignee_id], ['任务 / 分配版本', `v${task.task_revision} / v${task.assignment_revision}`], ['材料绑定', `${task.bound_bundle_id} · ${short(task.bound_bundle_digest)}`], ['审查范围版本', `v${task.case_scope_revision}`]]), actions, el('p', { class: 'form-hint' }, task.can_claim ? '接手仅记录处理责任，接手后才能提交此任务的补证请求。' : '转派不自动取消有效调查；新的接手人审查结果，旧接手人不能继续接受。')),
    notice('证据关联任务核对对象、来源与交付关系；业务语义任务判断内容是否符合公司规则。接受一份缺口说明后，未取得的证据仍然缺失。'),
    data.required_obligation_coverage?.length && panel('这项义务的完整覆盖范围', '请求可以逐次处理，义务完成取决于全部登记目标与材料类型。', [
      notice(`本案需要覆盖 ${data.allowed_targets.length} 个 Job / 阶段目标、${data.required_obligation_coverage.length} 项核心材料关系。接受单个请求不会自动关闭整体义务。`),
      el('details', { class: 'technical-details' }, el('summary', {}, '查看必需覆盖项'), el('ul', { class: 'compact-list' }, data.required_obligation_coverage.map(item => el('li', {}, `${targetSummary(item.target)} · ${label(item.kind)}`)))),
    ]),
    canWork ? workflowRequestForm(data, command) : notice(task.can_claim ? '请先接手任务，再发起逐项补证。' : '当前身份可以查看材料，写入权限由任务归属与服务端校验决定。'),
    panel('补证执行与逐项响应', '执行是否结束、人员是否接受、要求是否满足是三个独立状态。', requestPanels.length ? requestPanels : el('p', { class: 'muted small' }, '尚无逐项补证请求。接手任务不会自动调用模型。')),
    panel('当前材料包与过程记录', '服务端合并经过核验的材料变更，响应不会覆盖其他任务已加入的材料。', [
      factList([['当前 Bundle', el('code', {}, data.bundle.id)], ['父 Bundle', data.bundle.parent_id || '初始材料包'], ['登记材料数', String(data.bundle.manifest?.length || 0)]]),
      technicalDetails('查看 Bundle manifest 与精确绑定', { bundle: data.bundle, case: data.case, task_id: task.id, task_revision: task.task_revision, assignment_revision: task.assignment_revision }),
      (data.events || []).length ? el('ol', { class: 'timeline' }, [...data.events].reverse().slice(0, 12).map(event => el('li', {}, el('strong', {}, label(event.kind)), el('time', { datetime: event.created_at }, date(event.created_at)), el('p', { class: 'small' }, `事件 #${event.seq} · ${event.payload?.actor_id || '执行系统'}`)))) : null,
    ]),
    el('p', { class: 'form-hint' }, el('a', { href: `#/cases/${encodeURIComponent(task.case_id)}` }, '查看原历史材料与旧版确认记录'), '。旧记录不替代本任务的响应接受。'), footer());
  main.replaceChildren(content); command.paint(); announce('已读取任务、执行状态与逐项响应');
}
function workflowRequestPanel(data, request, command, canWork) {
  const response = request.current_response; const inputItems = request.input?.request_items || [];
  const responseItems = response?.body?.items || [];
  const body = el('article', { class: 'workflow-request' }, el('div', { class: 'evidence-top' }, el('h3', {}, `请求 ${short(request.id)} · 规格 v${request.request_spec_revision}`), request.withdrawn && workflowBadge('WITHDRAWN')),
    el('div', { class: 'workflow-state-grid' }, [['执行状态', request.execution_state], ['响应审查', request.review_state], ['满足程度', request.fulfillment_state]].map(([name, value]) => el('div', {}, el('span', { class: 'section-label' }, name), workflowBadge(value)))),
    request.input?.goal && el('p', { class: 'request-goal' }, request.input.goal),
    response?.body?.summary && el('p', { class: 'small' }, response.body.summary),
    response && factList([['提出请求的材料包', `${request.input.source_bundle_id} · ${short(request.input.source_bundle_sha256)}`], ['响应生成后的材料包', `${response.bundle_id} · ${short(response.bundle_digest)}`], ['此次人员审查材料包', `${data.task.bound_bundle_id} · ${short(data.task.bound_bundle_digest)}`]]),
    request.clarification && notice(`Agent 需要澄清：${request.clarification}`, 'warn'),
    request.review_state === 'ACCEPTED' && request.fulfillment_state !== 'COMPLETE' && notice(request.withdrawn ? '缺口及原接受记录保留；此请求已退出当前有效请求范围，不计入本任务完成条件。' : '响应已接受，但请求仍未完整满足。缺失、不可取得或冲突的材料不会因点击接受而变成已取得，相关义务仍需补证。', 'warn'),
    request.review_state === 'ACCEPTED' && !request.withdrawn && request.fulfillment_state === 'COMPLETE' && data.task.status !== 'COMPLETED' && notice('这项请求已完整响应并被接受；整体任务尚未完成。其他 Job / 阶段的覆盖与业务语义审查仍按各自条件处理。', 'warn'),
    response ? el('div', { class: 'response-items' }, responseItems.map(item => {
      const asked = inputItems.find(entry => entry.item_id === item.item_id);
      return el('section', { class: 'response-item' }, el('div', { class: 'evidence-top' }, el('h4', {}, `${item.item_id} · ${label(asked?.kind || '请求项')}`), workflowBadge(item.validated_disposition || item.proposed_disposition)), asked?.target && el('p', { class: 'small muted' }, targetSummary(asked.target)),
        el('p', { class: 'small muted' }, item.validated_disposition ? '材料关联与引用范围已经服务端核验；业务语义仍需独立判断。' : '此状态是 Agent 提案；整体满足程度以服务端核验记录为准。'),
        item.explanation && el('p', { class: 'small' }, item.explanation),
        factList([['业务语义判断', label(item.semantic_acceptance || 'NOT_EVALUATED')], ['剩余缺口', listText(item.remaining_gaps, '该项未列出剩余缺口')]]),
        (item.evidence_refs || []).map(ref => workflowMaterial(data, request, ref)),
        workflowReceipts(response, item), item.supporting_locations?.length ? technicalDetails('查看证据定位与读取范围', item.supporting_locations) : null);
    })) : notice(request.execution_state === 'UNKNOWN' ? '执行结果尚待对账。不要把超时当作未执行，也不要重复创建相同请求。' : '尚无可审查的响应。刷新只读取服务端状态，不会派发执行。'),
    response && technicalDetails('查看精确响应与 Bundle 绑定', { response_id: response.id, response_digest: response.digest, response_bundle_id: response.bundle_id, response_bundle_digest: response.bundle_digest, body: response.body }),
    request.responses?.length > 1 && technicalDetails('查看历史响应与归档版本', request.responses.map(entry => ({ id: entry.id, digest: entry.digest, review_state: entry.review_state, archived: entry.archived, bundle_id: entry.bundle_id, created_at: entry.created_at }))),
    technicalDetails('查看请求规格与执行身份', { request_id: request.id, request_spec_revision: request.request_spec_revision, spec_digest: request.spec_digest, input: request.input }));
  if (response && canWork && !request.withdrawn && request.review_state === 'PENDING_REVIEW') {
    const checkId = `accept-${request.id}`;
    const agree = el('input', { type: 'checkbox', id: checkId, required: true });
    body.append(el('form', { class: 'workflow-command acceptance-form', onSubmit: event => {
      event.preventDefault(); if (!agree.checked) return;
      command.submit(`accept:${request.id}`, `/evidence-requests/${encodeURIComponent(request.id)}/accept`, { request_spec_revision: request.request_spec_revision, response_id: response.id, response_digest: response.digest, review_policy_version: data.case.review_policy_version });
    } }, el('p', { class: 'form-hint' }, `接受对象：响应 ${short(response.digest)} · 请求规格 v${request.request_spec_revision} · 当前审查 Bundle ${short(data.task.bound_bundle_digest)}`), el('label', { class: 'check-item', for: checkId }, agree, el('span', {}, '我已核对这份响应及其缺口，接受其在当前材料范围内的说明。此决定不授予修复或发布权限。')), el('button', { class: 'button primary', type: 'submit' }, '接受这份响应')));
  }
  if (canWork && !request.withdrawn) {
    if (request.execution_state === 'BLOCKED') body.append(workflowTextAction('补充澄清并创建后继规格', '回答需要补充的事实；旧 Run 与累计预算保留。', `clarify:${request.id}`, `/evidence-requests/${encodeURIComponent(request.id)}/clarify`, 'answer', command));
    // 接受记录与当前有效请求范围是两件事：允许撤回已接受请求，由服务端重算覆盖，历史接受仍保留。
    if (request.review_state !== 'SUPERSEDED') body.append(workflowTextAction('撤回此请求', '保留原响应、审查与成本记录；从当前有效请求范围撤回会重新计算任务覆盖。在途的迟到结果仅归档。', `withdraw:${request.id}`, `/evidence-requests/${encodeURIComponent(request.id)}/withdraw`, 'reason', command));
  }
  return body;
}
/** 原件按需从授权入口读取；不能把响应中的摘要当成原文，也不执行原文中的指令。 */
function workflowMaterial(data, request, materialRef) {
  const metadata = (request.input?.materials || []).find(item => item.material_ref === materialRef);
  const body = el('div', { class: 'material-preview' }); let loaded = false; let loading = false;
  const disclosure = el('details', { class: 'technical-details material-disclosure' }, el('summary', {}, `读取原件：${metadata?.title || label(metadata?.kind) || materialRef}`), body);
  async function read() {
    if (loaded || loading) return; loading = true;
    const session = state.session; body.replaceChildren(el('p', { class: 'small muted' }, '正在读取授权原件…'));
    try {
      const material = await api(`/review-cases/${encodeURIComponent(data.task.case_id)}/materials/${encodeURIComponent(materialRef)}`, { version: 'v2' });
      if (state.session !== session || !disclosure.isConnected) return;
      if (material.material_ref !== materialRef || typeof material.content !== 'string' || (metadata?.sha256 && material.sha256 !== metadata.sha256)) throw Object.assign(new Error('EVIDENCE_INTEGRITY_ERROR'), { code: 'EVIDENCE_INTEGRITY_ERROR' });
      loaded = true;
      body.replaceChildren(factList([['材料种类', label(material.kind)], ['来源', material.provenance?.source_ref || '来源未提供'], ['精确目标', targetSummary(material.provenance)], ['内容摘要', el('code', {}, material.sha256)]]), el('pre', { class: 'raw-content', tabIndex: '0', 'aria-label': '原件正文' }, material.content), technicalDetails('查看原件完整来源身份', material.provenance));
    } catch (error) {
      if (state.session !== session) return;
      if (error.status === 401) { expireSession(); return; }
      body.replaceChildren(notice(apiErrorText(error), 'error'), button('重试读取原件', () => void read()));
    } finally { loading = false; }
  }
  disclosure.addEventListener('toggle', () => { if (disclosure.open) void read(); });
  return disclosure;
}
function workflowReceipts(response, item) {
  const refs = item.read_receipt_refs || [];
  if (!refs.length) return el('p', { class: 'small muted' }, '本项没有原件读取回执。');
  const receipts = response.read_receipts || [];
  return el('details', { class: 'technical-details' }, el('summary', {}, `读取回执 · ${refs.length} 条`), refs.map(ref => {
    const receipt = receipts.find(entry => entry.receipt_ref === ref);
    return receipt ? el('div', { class: 'receipt-record' }, factList([['回执', el('code', {}, ref)], ['原件', el('code', {}, receipt.material_ref)], ['读取范围', `第 ${receipt.start_line}–${receipt.end_line} 行`]]), typeof receipt.content === 'string' && el('pre', { class: 'raw-content', tabIndex: '0' }, receipt.content), technicalDetails('回执完整字段', receipt)) : el('p', { class: 'small' }, el('code', {}, ref), ' · 此页面尚无回执正文，不能将引用标识当作已展示的原文。');
  }));
}
function workflowTextAction(title, hint, action, endpoint, field, command) {
  const inputId = `command-${action}`; const input = el('textarea', { id: inputId, required: true, maxLength: 2000, rows: '2' });
  return el('details', { class: 'technical-details' }, el('summary', {}, title), el('form', { class: 'workflow-command', onSubmit: event => { event.preventDefault(); if (input.value.trim()) command.submit(action, endpoint, { [field]: input.value.trim() }); } }, el('div', { class: 'form-field' }, el('label', { for: inputId }, field === 'answer' ? '澄清回答' : '撤回原因'), input), el('p', { class: 'form-hint' }, hint), el('button', { class: 'button', type: 'submit' }, title)));
}

// 跳转正文是辅助导航，不应改变 hash 路由或发起另一页查询。
document.querySelector('.skip-link').addEventListener('click', event => {
  event.preventDefault(); const main = document.querySelector('#main-content');
  if (main) { main.tabIndex = -1; main.focus(); main.scrollIntoView({ block: 'start' }); }
});
window.addEventListener('hashchange', () => { if (state.session) void renderRoute(); });
(async () => {
  app.replaceChildren(el('div', { class: 'loading', role: 'status' }, el('span', { class: 'spinner', 'aria-hidden': 'true' }), '正在恢复工作台会话…'));
  try { state.session = await api('/session'); try { clearOtherPending(state.session.actor); state.journalBlocked = null; } catch (error) { state.journalBlocked = error.message; } if (!location.hash) location.hash = '#/tasks'; else await renderRoute(); }
  catch (error) { showLogin(error.status === 401 ? '' : '暂时无法连接工作台服务，请确认本机服务正在运行。'); }
})();
