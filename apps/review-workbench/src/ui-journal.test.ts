import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { webcrypto } from "node:crypto";
import { createContext, runInContext } from "node:vm";

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
