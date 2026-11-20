import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { webcrypto } from "node:crypto";
import { createContext, runInContext } from "node:vm";
import { ITEM_TEMPLATES } from "./workflow-store.js";

/**
 * 运行真实 public/app.js，只替换 DOM、会话存储与网络边界。
 * 这里验证可能重复提交的恢复语义；完整页面点击与排版仍由浏览器验收负责。
 */
class ElementStub {
  readonly tag: string;
  constructor(tag = "div") { this.tag = tag; }
  children: unknown[] = [];
  events: Record<string, (...args: unknown[]) => unknown> = {};
  attributes: Record<string, string> = {};
  disabled = false;
  checked = false;
  value: string | number = "";
  append(...children: unknown[]): void { this.children.push(...children); }
  replaceChildren(...children: unknown[]): void { this.children = children; }
  setAttribute(key: string, value: string): void { this.attributes[key] = value; }
  addEventListener(key: string, handler: (...args: unknown[]) => unknown): void { this.events[key] = handler; }
  querySelectorAll(): unknown[] { return []; }
}
class SessionStorageStub {
  readonly values = new Map<string, string>();
  rejectWrites = false;
  get length(): number { return this.values.size; }
  key(index: number): string | null { return [...this.values.keys()][index] ?? null; }
  getItem(key: string): string | null { return this.values.get(key) ?? null; }
  setItem(key: string, value: string): void { if (this.rejectWrites) throw new Error("STORAGE_UNAVAILABLE"); this.values.set(key, value); }
  removeItem(key: string): void { this.values.delete(key); }
}
function browserHarness() {
  const storage = new SessionStorageStub();
  const context = createContext({ Node: ElementStub, sessionStorage: storage, crypto: webcrypto, JSON,
    document: { querySelector: () => new ElementStub(), createElement: (tag: string) => new ElementStub(tag), createTextNode: (text: string) => ({ text }) },
    // 保持启动会话读取悬而未决，测试显式装配身份；不启动真实计时器或访问网络。
    fetch: () => new Promise(() => {}), setTimeout: () => 0, clearTimeout: () => {}, AbortController,
    location: { hash: "#/tasks" }, window: { addEventListener: () => {} },
  });
  runInContext(readFileSync(new URL("../public/app.js", import.meta.url), "utf8"), context);
  const run = (code: string): unknown => runInContext(code, context);
  run(`globalThis.actor={id:'qa',tenant_id:'tenant-b',role:'QA'};
    globalThis.task={id:'task-a',case_id:'case-a',task_revision:2,assignment_revision:1,case_scope_revision:1,bound_bundle_id:'bundle-a',bound_bundle_digest:'b'.repeat(64),candidate_sha:'c'.repeat(40)};
    globalThis.pending={action:'claim',endpoint:'/review-tasks/task-a/claim',body:{idempotency_key:'12345678-1234-1234-1234-123456789abc',...workflowBase(task)}};
    state.session={actor,csrf_token:'must-never-be-persisted'};
    globalThis.controls=[{disabled:false}]; globalThis.root={querySelectorAll:()=>controls};`);
  return { storage, run };
}
const settle = () => new Promise<void>(resolve => setImmediate(resolve));

test("DOM composition omits conditional null placeholders while preserving numeric zero and literal text", () => {
  const { run } = browserHarness();
  run("globalThis.probe=el('div',{},null,undefined,false,[0,'null','普通文本']);");
  assert.equal(run("JSON.stringify(probe.children.map(child=>child.text))"), JSON.stringify(["0", "null", "普通文本"]));
  run("replaceContent(probe,null,undefined,false,0); appendContent(probe,[null,'追加文本']);");
  assert.equal(run("JSON.stringify(probe.children.map(child=>child.text))"), JSON.stringify(["0", "追加文本"]));
});

test("workflow list and detail omit an absent flash instead of passing null to native DOM insertion", () => {
  const { run } = browserHarness();
  run(`globalThis.main=el('main'); globalThis.walk=node=>[node,...(node.children||[]).flatMap(walk)];
    renderWorkflowTasks(main,[]);`);
  assert.equal(run("walk(main).some(node=>node===null||node===undefined||node===false)"), false);
  assert.equal(run("walk(main).some(node=>node.text==='0')"), true, "zero task counts are still visible");
  run("state.flash='人员操作已记录'; renderWorkflowTasks(main,[]);");
  assert.equal(run("walk(main).some(node=>node.text==='人员操作已记录')"), true);
  run(`renderWorkflowTask(main,{task:{...task,assignee_id:'other',status:'ASSIGNED',title:'审查任务'},case:{owner_id:'owner'},bundle:{id:'bundle-a',manifest:[]},requests:[],allowed_targets:[]});`);
  assert.equal(run("walk(main).some(node=>node===null||node===undefined||node===false)"), false);
});

/** 用真正的服务端模板绘制表单，防止测试与前端一起沿用已经失效的旧枚举。 */
function requestFormHarness(templates = ITEM_TEMPLATES) {
  const harness = browserHarness();
  harness.run(`globalThis.formData={task,allowed_targets:[{tenant_id:'tenant-b',historical_run_id:'archive-a',job_id:'job-a',ticket_id:'ticket-a',ticket_revision:2,phase:'after'}],item_templates:${JSON.stringify(templates)}};
    globalThis.submissions=[]; globalThis.formCommand={submit:(...args)=>submissions.push(args)};
    globalThis.walk=node=>[node,...(node.children||[]).flatMap(walk)];
    globalThis.redraw=()=>{globalThis.formPanel=workflowRequestForm(formData,formCommand);globalThis.form=walk(formPanel).find(node=>node.tag==='form');globalThis.boxes=walk(form).filter(node=>node.tag==='input');};
    redraw();`);
  return harness;
}

test("request form defaults to the three real core kinds, labels them, and leaves provider raw output opt-in", () => {
  const { run } = requestFormHarness();
  assert.equal(run("JSON.stringify(boxes.filter(box=>box.checked).map(box=>box.value))"),
    JSON.stringify(["SAVED_PRODUCT_DRAFT", "DELIVERED_PRIVATE_NOTE", "DELIVERY_RECEIPT"]));
  assert.equal(run("boxes.find(box=>box.value==='RAW_PROVIDER_RESPONSE').checked"), false);
  for (const template of ITEM_TEMPLATES) assert.equal(run(`label(${JSON.stringify(template.kind)})`), template.title);
});

test("request form only defaults registered kinds even when templates are reordered or a core kind is absent", () => {
  const { run } = requestFormHarness([ITEM_TEMPLATES[3]!, ITEM_TEMPLATES[2]!, ITEM_TEMPLATES[0]!]);
  assert.equal(run("JSON.stringify(boxes.filter(box=>box.checked).map(box=>box.value))"),
    JSON.stringify(["DELIVERY_RECEIPT", "SAVED_PRODUCT_DRAFT"]));
});

test("request form preserves manual deselection and provider opt-in through redraw, including an empty selection", () => {
  const { run } = requestFormHarness();
  run("for(const box of boxes)box.events.change({target:{checked:false}}); redraw();");
  assert.equal(run("boxes.filter(box=>box.checked).length"), 0);
  run("boxes.find(box=>box.value==='RAW_PROVIDER_RESPONSE').events.change({target:{checked:true}}); redraw();");
  assert.equal(run("JSON.stringify(boxes.filter(box=>box.checked).map(box=>box.value))"), JSON.stringify(["RAW_PROVIDER_RESPONSE"]));
});

test("request form submits exact server kinds, criteria and target, and does not dispatch an empty selection", () => {
  const { run } = requestFormHarness();
  run("walk(form).find(node=>node.tag==='textarea').value='核对指定阶段的原件'; form.events.submit({preventDefault(){}});");
  assert.equal(run("submissions.length"), 1);
  assert.equal(run("submissions[0][0]"), "request-evidence");
  assert.equal(run("submissions[0][1]"), "/review-tasks/task-a/evidence-requests");
  const submitted = JSON.parse(String(run("JSON.stringify(submissions[0][2])"))) as {
    items: { item_id: string; kind: string; target: object; required: boolean; acceptance_criteria: string[] }[];
  };
  assert.deepEqual(submitted.items.map(item => ({ kind: item.kind, acceptance_criteria: item.acceptance_criteria })),
    ITEM_TEMPLATES.slice(0, 3).map(({ kind, acceptance_criteria }) => ({ kind, acceptance_criteria })));
  assert.deepEqual(submitted.items.map(item => item.item_id), ["E1", "E2", "E3"]);
  assert.ok(submitted.items.every(item => item.required && JSON.stringify(item.target) === String(run("JSON.stringify(formData.allowed_targets[0])"))));
  run("for(const box of boxes)box.events.change({target:{checked:false}}); form.events.submit({preventDefault(){}});");
  assert.equal(run("submissions.length"), 1);
});

test("UI journal recovers exact command, isolates actor and action, and excludes session credentials", () => {
  const { storage, run } = browserHarness();
  run("saveWorkflowPending(actor,task.id,pending)");
  assert.equal(run("JSON.stringify(restoreWorkflowPending(actor,task.id))"), run("JSON.stringify(pending)"));
  assert.equal(run("restoreWorkflowPending({...actor,id:'other'},task.id)"), null);
  assert.ok(![...storage.values.values()].join().includes("must-never-be-persisted"));
  assert.throws(() => run("saveWorkflowPending(actor,task.id,{...pending,action:'reassign'})"));
  assert.throws(() => run("saveWorkflowPending(actor,task.id,{...pending,body:{...pending.body,expected_task_revision:3}})"));
  run("clearOtherPending(actor)"); assert.equal(storage.length, 1);
  run("clearOtherPending({...actor,id:'next'})"); assert.equal(storage.length, 0);
});

test("UI journal rejects a recovered external endpoint instead of dispatching it", () => {
  const { storage, run } = browserHarness();
  run("saveWorkflowPending(actor,task.id,pending)");
  const [key, value] = [...storage.values.entries()][0]!;
  storage.values.set(key, value.replace("/review-tasks/task-a/claim", "https://external.invalid/claim"));
  assert.throws(() => run("restoreWorkflowPending(actor,task.id)"));
});

test("UI uncertain mutation survives render and only explicitly retries its original version and idempotency key", async () => {
  const { storage, run } = browserHarness();
  run(`globalThis.sent=[]; api=async(path,options)=>{sent.push({path,options});throw Object.assign(new Error('NETWORK'),{network:true});};
    globalThis.command=workflowCommander({task},root); command.submit('claim','/review-tasks/task-a/claim');`);
  await settle();
  assert.equal(run("sent.length"), 1); assert.equal(storage.length, 1); assert.equal(run("controls[0].disabled"), true);
  const original = run("JSON.stringify(sent[0].options.body)");
  run("globalThis.recovered=workflowCommander({task},root); recovered.paint();");
  assert.equal(run("sent.length"), 1, "render cannot autonomously replay a mutation");
  run("recovered.recovery.children[1].children[0].events.click()"); await settle();
  assert.equal(run("sent.length"), 2); assert.equal(run("JSON.stringify(sent[1].options.body)"), original);
});

test("UI does not dispatch a command until its recovery journal is durable", async () => {
  const { storage, run } = browserHarness(); storage.rejectWrites = true;
  run(`globalThis.sent=0; api=async()=>{sent+=1;};
    globalThis.command=workflowCommander({task},root); command.submit('claim','/review-tasks/task-a/claim');`);
  await settle(); assert.equal(run("sent"), 0); assert.equal(run("controls[0].disabled"), true);
});

test("UI stale version rejection clears unresolved journal and keeps commands locked until explicit reload", async () => {
  const { storage, run } = browserHarness();
  run(`api=async()=>{throw Object.assign(new Error('STALE'),{status:409,code:'STALE_TASK_REVISION'});};
    globalThis.command=workflowCommander({task},root); command.submit('claim','/review-tasks/task-a/claim');`);
  await settle(); assert.equal(storage.length, 0); assert.equal(run("controls[0].disabled"), true);
});

test("UI expired session keeps the unresolved task command for same-identity relogin without auto replay", async () => {
  const { storage, run } = browserHarness();
  run(`globalThis.sent=0; api=async()=>{sent+=1;throw Object.assign(new Error('EXPIRED'),{status:401});};
    globalThis.command=workflowCommander({task},root); command.submit('claim','/review-tasks/task-a/claim');`);
  await settle();
  assert.equal(run("state.session"), null); assert.equal(storage.length, 1);
  run("state.session={actor,csrf_token:'new-not-persisted'}; clearOtherPending(actor); globalThis.recovered=workflowCommander({task},root); recovered.paint();");
  assert.equal(run("sent"), 1); assert.equal(run("restoreWorkflowPending(actor,task.id).body.expected_task_revision"), 2);
});

test("UI acceptance submits the displayed request, response and policy rather than a moving response pointer", () => {
  const { run } = browserHarness();
  run(`globalThis.selected=null;
    globalThis.request={id:'request-a',request_spec_revision:3,review_state:'PENDING_REVIEW',execution_state:'FINISHED',fulfillment_state:'PARTIAL',withdrawn:false,input:{request_items:[],materials:[],source_bundle_id:'old-bundle',source_bundle_sha256:'a'.repeat(64)},current_response:{id:'response-a',digest:'d'.repeat(64),bundle_id:'bundle-a',bundle_digest:'b'.repeat(64),body:{items:[]}}};
    globalThis.panelNode=workflowRequestPanel({task,case:{review_policy_version:'policy-display-v2'}},request,{submit:(...args)=>{selected=args;}},true);
    globalThis.walk=node=>[node,...(node.children||[]).flatMap(walk)];
    globalThis.acceptForm=walk(panelNode).find(node=>node.tag==='form'&&node.className.includes('acceptance-form'));
    globalThis.checkbox=walk(acceptForm).find(node=>node.tag==='input');
    acceptForm.events.submit({preventDefault(){}});`);
  assert.equal(run("selected"), null, "an unchecked acceptance cannot dispatch");
  run("checkbox.checked=true; acceptForm.events.submit({preventDefault(){}});");
  assert.equal(run("selected[0]"), "accept:request-a");
  assert.equal(run("selected[1]"), "/evidence-requests/request-a/accept");
  assert.equal(run("JSON.stringify(selected[2])"), JSON.stringify({ request_spec_revision: 3, response_id: "response-a", response_digest: "d".repeat(64), review_policy_version: "policy-display-v2" }));
});

/** 展示层只允许人员显式撤回；覆盖回退和历史不可覆盖由 WorkflowStore 的事务负责。 */
function acceptedRequestHarness() {
  const harness = browserHarness();
  harness.run(`globalThis.submissions=[]; globalThis.canWork=true;
    globalThis.request={id:'request-a',request_spec_revision:3,review_state:'ACCEPTED',execution_state:'FINISHED',fulfillment_state:'PARTIAL',withdrawn:false,input:{request_items:[{item_id:'E4',kind:'RAW_PROVIDER_RESPONSE'}],materials:[],source_bundle_id:'old-bundle',source_bundle_sha256:'a'.repeat(64)},current_response:{id:'response-a',digest:'d'.repeat(64),bundle_id:'bundle-a',bundle_digest:'b'.repeat(64),body:{items:[{item_id:'E4',validated_disposition:'UNAVAILABLE',remaining_gaps:['未归档供应商原始响应']}]}}};
    globalThis.walk=node=>[node,...(node.children||[]).flatMap(walk)];
    globalThis.textOf=node=>walk(node).filter(entry=>typeof entry.text==='string').map(entry=>entry.text).join(' ');
    globalThis.redraw=()=>{globalThis.panelNode=workflowRequestPanel({task,case:{review_policy_version:'policy-display-v2'}},request,{submit:(...args)=>submissions.push(args)},canWork);globalThis.withdrawForm=walk(panelNode).find(node=>node.tag==='form'&&textOf(node).includes('撤回原因'));};
    redraw();`);
  return harness;
}

test("UI can explicitly withdraw an accepted unavailable response without rewriting historical acceptance", () => {
  const { run } = acceptedRequestHarness();
  assert.equal(run("Boolean(withdrawForm)"), true);
  assert.equal(run("textOf(panelNode).includes('相关义务仍需补证。')"), true);
  assert.equal(run("textOf(withdrawForm).includes('保留原响应、审查与成本记录；从当前有效请求范围撤回会重新计算任务覆盖。')"), true);
  run("withdrawForm.events.submit({preventDefault(){}});");
  assert.equal(run("submissions.length"), 0, "a reason is required for explicit withdrawal");
  run("walk(withdrawForm).find(node=>node.tag==='textarea').value='  原始模型响应未归档，退出本次有效补证范围  '; withdrawForm.events.submit({preventDefault(){}});");
  assert.equal(run("JSON.stringify(submissions[0])"), JSON.stringify(["withdraw:request-a", "/evidence-requests/request-a/withdraw", { reason: "原始模型响应未归档，退出本次有效补证范围" }]));
  assert.equal(run("request.review_state"), "ACCEPTED");
  assert.equal(run("request.withdrawn"), false, "render and submit must wait for persisted server state");
});

test("UI withdrawn accepted unavailable response retains its gap but does not claim the completed task still needs it", () => {
  const { run } = acceptedRequestHarness();
  run("request.withdrawn=true; task.status='COMPLETED'; redraw();");
  assert.equal(run("textOf(panelNode).includes('缺口及原接受记录保留；此请求已退出当前有效请求范围，不计入本任务完成条件。')"), true);
  assert.equal(run("textOf(panelNode).includes('未归档供应商原始响应')"), true);
  assert.equal(run("textOf(panelNode).includes('相关义务仍需补证。')"), false);
  assert.equal(run("textOf(panelNode).includes('撤回此请求')"), false);
  assert.equal(run("withdrawForm"), undefined);
  assert.equal(run("request.review_state"), "ACCEPTED");
});

test("UI accepted complete requests remain withdrawable, while superseded, withdrawn or read-only requests cannot withdraw", () => {
  const { run } = acceptedRequestHarness();
  run("request.fulfillment_state='COMPLETE'; redraw();");
  assert.equal(run("Boolean(withdrawForm)"), true, "withdrawing accepted core materials must be possible and server coverage will be recomputed");
  run("request.review_state='SUPERSEDED'; redraw();");
  assert.equal(run("withdrawForm"), undefined);
  run("request.review_state='ACCEPTED'; request.withdrawn=true; redraw();");
  assert.equal(run("withdrawForm"), undefined);
  assert.equal(run("textOf(panelNode).includes('整体任务尚未完成。')"), false);
  run("request.withdrawn=false; canWork=false; redraw();");
  assert.equal(run("withdrawForm"), undefined);
});
