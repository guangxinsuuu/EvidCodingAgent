import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { Ajv2020 } from "ajv/dist/2020.js";
import { decisionProposalSchema, parseDecisionProposal } from "@egap/contracts";
import { groundedRequirements, modelBodySchema } from "./context.js";
import { sha, type Json, type ReleaseBundle, type RunState } from "./types.js";

const identity = {
  schema_version: 1,
  proposal_id: "b700a836-eb63-4b59-a9f8-bc37f3011275",
  based_on_decision_revision: 0,
  candidate_epoch: 1,
  context_view_ref: "context:sha256:conformance",
};
const identityKeys = new Set(Object.keys(identity));
const corpus = JSON.parse(readFileSync(new URL(
  "../../../protocols/verify-agent/v1/conformance.json", import.meta.url,
), "utf8")) as { cases: { name: string; valid: boolean; value: unknown }[] };
const ajv = new Ajv2020({ strict: true, allErrors: true, coerceTypes: false,
  useDefaults: false, removeAdditional: false });
const validate = ajv.compile(modelBodySchema());

/** 生产 ModelPort 注入身份前只允许模型输出动作体；不把任意原始身份当模型权限。 */
function bodyOf(value: unknown): unknown {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return value;
  return Object.fromEntries(Object.entries(structuredClone(value)).filter(([key]) => !identityKeys.has(key)));
}

function authorityAcceptsBody(value: unknown): boolean {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  try { parseDecisionProposal({ ...value, ...identity }); return true; }
  catch { return false; }
}

// 61份相同样本已由 Python jsonschema 和 TS 权威解析器消费。
// 身份缺失/格式错误在此归一成 Runtime 提供的合法身份，因此只比较模型动作体语义。
for (const sample of corpus.cases) {
  test(`生成Schema与共享权威协议等价：${sample.name}`, () => {
    const body = bodyOf(sample.value);
    assert.equal(validate(body), authorityAcceptsBody(body));
    if (sample.valid) assert.equal(validate(body), true);
  });
}

test("生成Schema覆盖7种动作并保留各自payload，不能互换READ与CHECK字段", () => {
  const validSamples = corpus.cases.filter((sample) => sample.valid && /^valid_(read|check_intent|novel_check|clarify|finding|request_final|stop)$/.test(sample.name));
  assert.equal(validSamples.length, 7);
  for (const sample of validSamples) assert.equal(validate(bodyOf(sample.value)), true);
  const read = bodyOf(validSamples.find((sample) => sample.name === "valid_read")!.value) as Json;
  const check = bodyOf(validSamples.find((sample) => sample.name === "valid_check_intent")!.value) as Json;
  assert.equal(validate({ ...read, payload: check["payload"] }), false);
  assert.equal(validate({ ...check, payload: read["payload"] }), false);
  assert.equal(validate({ ...read, action_kind: "CHECK", payload: check["payload"] }), false);
});

test("模型动作体不能携带authority身份字段，也不能把数组简写成字符串", () => {
  const base = bodyOf(corpus.cases[0]!.value) as Json;
  for (const [key, value] of Object.entries(identity)) {
    assert.equal(validate({ ...base, [key]: value }), false, `拒绝模型身份字段${key}`);
  }
  for (const key of ["requirement_refs", "hypothesis_refs", "evidence_refs", "hypothesis_updates"]) {
    assert.equal(validate({ ...base, [key]: "R1" }), false, `数组字段${key}不隐式转换`);
  }
  assert.equal(validate({ ...base, actor: "operator", result: "PASS" }), false);
  assert.equal(validate({ ...base, evidence_refs: ["https://host/?credential=private"] }), false);
});

test("生成Schema保留文本、引用与行号上界，非有限数字仍然拒绝", () => {
  const base = bodyOf(corpus.cases[0]!.value) as Json;
  const payload = base["payload"] as Json;
  const goodBoundary: Json = { ...base, rationale: "x".repeat(2000),
    evidence_refs: ["a".repeat(512)], payload: { ...payload, start_line: 1, end_line: 1_000_000 } };
  assert.equal(validate(goodBoundary), true);
  assert.equal(authorityAcceptsBody(goodBoundary), true);
  for (const bad of [
    { ...goodBoundary, rationale: "x".repeat(2001) },
    { ...goodBoundary, evidence_refs: ["a".repeat(513)] },
    ...[NaN, Infinity, -Infinity, 0.5, "1", 0, 1_000_001, Number.MAX_SAFE_INTEGER + 1]
      .map((start_line) => ({ ...base, payload: { ...payload, start_line } })),
  ]) {
    assert.equal(validate(bad), false);
    assert.equal(authorityAcceptsBody(bad), false);
  }
});

test("生成Schema转换不修改共享源；分支先输出action_kind，所有动作继续拒绝未知字段", () => {
  const before = JSON.stringify(decisionProposalSchema);
  const first = modelBodySchema();
  const branches = first["oneOf"] as Json[];
  assert.equal(branches.length, 7);
  for (const branch of branches) {
    assert.equal(Object.keys(branch["properties"] as Json)[0], "action_kind");
    assert.equal(branch["additionalProperties"], false);
    assert.ok((branch["required"] as string[]).includes("payload"));
  }
  // 不同调用不能共享可写树，避免一个Provider适配器污染后续调用或权威协议。
  (branches[0]!["properties"] as Json)["rationale"] = { type: "number" };
  assert.equal(JSON.stringify(decisionProposalSchema), before);
  assert.notDeepEqual(first, modelBodySchema());
});

/** 测试窗口/引用准入的合成材料，不包含业务案例答案或预定模型动作。 */
function scopedFixture(): { bundle: ReleaseBundle; state: RunState } {
  const content = {
    "rule:R1": "登记事实决定适用范围。\n缺少事实需要补证。\n计划不构成业务PASS。",
    "candidate:product.py": Array.from({ length: 245 }, (_, i) => `# source line ${i + 1}`).join("\n"),
    "diff:product.py": "--- a/product.py\n+++ b/product.py\n@@ -34 +34 @@\n-return None\n+return facts",
  };
  return {
    bundle: { schema_version: 1, task_mode: "RELEASE_VERIFICATION", scope: "LOCAL_SYNTHETIC_PLANNING",
      input_provenance: "CALIBRATION_SYNTHETIC", goal: "选择与合成版本变化相关的材料与检查。",
      baseline_sha: "a".repeat(40), candidate_sha: "b".repeat(40), catalog_commit: "c".repeat(40), catalog_basis_commit: "d".repeat(40),
      requirement_refs: ["R1"], changes: [{ path: "product.py", diff_material_ref: "diff:product.py" }],
      materials: Object.entries(content).map(([material_ref, text]) => ({ material_ref, content: text,
        title: material_ref, kind: material_ref.startsWith("diff:") ? "UNTRUSTED_DIFF" : "PINNED_MATERIAL",
        git_commit: "b".repeat(40), path: material_ref.startsWith("rule:") ? "rules.md" : "product.py", sha256: sha(text) })),
      catalog: { status: "DRAFT", dispatch_authorized: false, requirements: [{ id: "R1", material_ref: "rule:R1" }],
        capabilities: [{ id: "check:registered", dispatch_enabled: false }], scenarios: [{ id: "scenario:registered" }] },
      gaps: [], current_candidate_coverage: [], evaluation_status: "NOT_RUN" },
    state: { run_id: "b700a836-eb63-4b59-a9f8-bc37f3011275", status: "ACTIVE", candidate_epoch: 1,
      decision_basis_revision: 1, event_seq: 0, config_digest: "e".repeat(64),
      limits: { model_calls: 5, reads: 4, no_progress: 2 }, model_calls_used: 0, reads_used: 0,
      no_progress: 0, hypotheses: [], read_receipts: [], latest_feedback: null, stop_reason: null },
  };
}

function scopedRead(material_ref = "rule:R1", start_line = 1, end_line = 3): Json {
  return { ...(bodyOf(corpus.cases[0]!.value) as Json), requirement_refs: ["R1"], evidence_refs: ["diff:product.py"],
    hypothesis_refs: [], hypothesis_updates: [{ id: "H1", status: "OPEN",
      statement: "候选变化可能影响登记事实的保留，需要读取适用规则核对。", evidence_refs: ["diff:product.py"] }],
    payload: { material_ref, start_line, end_line } };
}

function scopedCheck(): Json {
  return { ...scopedRead(), action_kind: "CHECK_INTENT", payload: { capability_ref: "check:registered",
    scenario_ref: "scenario:registered", objective: "核对受本次变化影响的事实传递。", observation_requirements: ["读取固定候选的业务观察"] } };
}

function recordCompleteRule(state: RunState): void {
  state.read_receipts.push({ material_ref: "rule:R1", start_line: 1, end_line: 3, receipt_ref: "read:complete-rule-R1" });
}

test("Scoped：合法动作仍满足共享权威协议，新增enum只缩小引用范围", () => {
  const { bundle, state } = scopedFixture();
  const scoped = ajv.compile(modelBodySchema(bundle, state));
  const bodies = [scopedRead(), scopedRead("candidate:product.py", 1, 200), scopedRead("candidate:product.py", 201, 245),
    ...corpus.cases.filter((sample) => sample.valid && ["valid_novel_check", "valid_clarify", "valid_finding", "valid_request_final", "valid_stop"].includes(sample.name))
      .map((sample) => ({ ...(bodyOf(sample.value) as Json), requirement_refs: ["R1"], evidence_refs: ["diff:product.py"], hypothesis_refs: [], hypothesis_updates: [] }))];
  for (const body of bodies) {
    assert.equal(scoped(body), true, JSON.stringify(scoped.errors));
    assert.equal(authorityAcceptsBody(body), true);
  }
  recordCompleteRule(state);
  assert.equal(ajv.compile(modelBodySchema(bundle, state))(scopedCheck()), true);
  assert.equal(authorityAcceptsBody(scopedCheck()), true);
});

test("Scoped：不存在的引用、尚未读取的规则证据和不真实的行窗口都不可生成", () => {
  const { bundle, state } = scopedFixture();
  const scoped = ajv.compile(modelBodySchema(bundle, state));
  for (const body of [
    { ...scopedRead(), requirement_refs: ["R999"] },
    { ...scopedRead(), evidence_refs: ["rule:R1"] },
    { ...scopedRead(), hypothesis_updates: [{ id: "H1", statement: "尚未观察的规则不能作为依据。", status: "OPEN", evidence_refs: ["rule:R1"] }] },
    scopedRead("unregistered:secret", 1, 3), scopedRead("rule:R1", 34, 34),
    scopedRead("candidate:product.py", 34, 34), scopedRead("candidate:product.py", 201, 246),
    { ...scopedCheck(), payload: { ...(scopedCheck()["payload"] as Json), capability_ref: "check:invented" } },
    { ...scopedCheck(), payload: { ...(scopedCheck()["payload"] as Json), scenario_ref: "scenario:invented" } },
  ]) assert.equal(scoped(body), false);
});

test("Scoped：已完成窗口不再供新调用生成；下一页仍可读，原协议保留传输重放资格", () => {
  const { bundle, state } = scopedFixture();
  const read = scopedRead("candidate:product.py", 1, 200);
  assert.equal(ajv.compile(modelBodySchema(bundle, state))(read), true);
  state.read_receipts.push({ material_ref: "candidate:product.py", start_line: 1, end_line: 200,
    receipt_ref: "read:finished-page-1" });
  const scoped = ajv.compile(modelBodySchema(bundle, state));
  assert.equal(scoped(read), false, "新调用不把重复阅读当进展");
  assert.equal(scoped(scopedRead("candidate:product.py", 201, 245)), true);
  assert.equal(scoped({ ...scopedRead(), evidence_refs: ["read:finished-page-1", "candidate:product.py"] }), true);
  // 已登记proposal的重发使用共享解析器+operation映射，不能被当前生成选项拒绝。
  // 对应真实PG缓存/HTTP重放由runtime.integration.test.ts验证，非在此伪造执行成功。
  assert.equal(authorityAcceptsBody(read), true);
});

test("Scoped：完整展示的diff不重复读取，截断diff仍保留有新内容的窗口", () => {
  const { bundle, state } = scopedFixture();
  const shortDiff = scopedRead("diff:product.py", 1, 5);
  assert.equal(ajv.compile(modelBodySchema(bundle, state))(shortDiff), false);
  const diff = bundle.materials.find((m) => m.material_ref === "diff:product.py")!;
  diff.content = Array.from({ length: 100 }, (_, i) => `+ ${i}: ${"x".repeat(80)}`).join("\n");
  diff.sha256 = sha(diff.content);
  assert.equal(ajv.compile(modelBodySchema(bundle, state))(scopedRead("diff:product.py", 1, 100)), true);
});

test("Scoped：没有新增材料窗口时关闭READ生成，保留有范围的STOP等动作", () => {
  const { bundle, state } = scopedFixture();
  state.read_receipts = [
    { material_ref: "rule:R1", start_line: 1, end_line: 3, receipt_ref: "read:rule" },
    { material_ref: "candidate:product.py", start_line: 1, end_line: 200, receipt_ref: "read:page1" },
    { material_ref: "candidate:product.py", start_line: 201, end_line: 245, receipt_ref: "read:page2" },
  ];
  const scoped = ajv.compile(modelBodySchema(bundle, state));
  assert.equal(scoped(scopedRead()), false);
  assert.equal(scoped({ ...scopedRead(), action_kind: "STOP", payload: { reason: "当前可读范围已全部观察，需要执行能力补足。" } }), true);
});

test("Scoped：目录缺少可引用检查能力时不能构造非法空enum，也不能生成假检查", () => {
  const { bundle, state } = scopedFixture();
  recordCompleteRule(state);
  bundle.catalog["capabilities"] = [];
  bundle.catalog["scenarios"] = [];
  const scoped = ajv.compile(modelBodySchema(bundle, state));
  assert.equal(scoped(scopedCheck()), false);
  assert.equal(scoped({ ...scopedRead(), action_kind: "NOVEL_CHECK", payload: { objective: "核对新增变化", capability_gap: "当前目录没有检查能力" } }), true);
});

test("Scoped：enum不能绕过共享引用格式和长度，材料登记值也不放宽传输协议", () => {
  const { bundle, state } = scopedFixture();
  recordCompleteRule(state);
  const invalidReference = "not a reference";
  const oversizedReference = "r".repeat(513);
  bundle.requirement_refs.push(invalidReference, oversizedReference);
  (bundle.catalog["capabilities"] as Json[]).push({ id: invalidReference });
  const scoped = ajv.compile(modelBodySchema(bundle, state));
  assert.equal(scoped(scopedCheck()), true);
  for (const body of [
    { ...scopedRead(), requirement_refs: [invalidReference] },
    { ...scopedRead(), requirement_refs: [oversizedReference] },
    { ...scopedCheck(), payload: { ...(scopedCheck()["payload"] as Json), capability_ref: invalidReference } },
  ]) {
    assert.equal(scoped(body), false);
    assert.equal(authorityAcceptsBody(body), false);
  }
});

test("Scoped：读取额度耗尽时不再生成READ，检查建议和停止仍由模型选择", () => {
  const { bundle, state } = scopedFixture();
  recordCompleteRule(state);
  state.reads_used = state.limits.reads;
  const scoped = ajv.compile(modelBodySchema(bundle, state));
  assert.equal(scoped(scopedRead()), false);
  assert.equal(scoped(scopedCheck()), true);
  assert.equal(scoped({ ...scopedRead(), action_kind: "STOP", hypothesis_updates: [],
    payload: { reason: "读取额度已耗尽；仍无业务实验结果。" } }), true);
  assert.equal(authorityAcceptsBody(scopedRead()), true, "生成可选动作收窄不修改传输重放协议");
});

test("Scoped：首次读取或检查必须提出可审查假设，已有假设后允许保持而不虚构更新", () => {
  const { bundle, state } = scopedFixture();
  const initial = ajv.compile(modelBodySchema(bundle, state));
  assert.equal(initial(scopedRead()), true);
  assert.equal(initial({ ...scopedRead(), hypothesis_updates: [] }), false);
  assert.equal(initial(scopedCheck()), false, "只有规则标题时不能生成CHECK");
  assert.equal(initial({ ...scopedRead(), action_kind: "CLARIFY", hypothesis_updates: [],
    payload: { question: "发布任务缺少的业务范围由谁确认？" } }), true,
  "初始假设要求不应误传播到其他动作分支");
  recordCompleteRule(state);
  const afterReadingRule = ajv.compile(modelBodySchema(bundle, state));
  assert.equal(afterReadingRule(scopedCheck()), true);
  assert.equal(afterReadingRule({ ...scopedCheck(), hypothesis_updates: [] }), false);
  state.hypotheses = [{ id: "H1", statement: "已登记的待验证影响。", status: "OPEN", evidence_refs: ["diff:product.py"] }];
  const next = ajv.compile(modelBodySchema(bundle, state));
  assert.equal(next({ ...scopedRead("candidate:product.py", 1, 200), hypothesis_refs: ["H1"], hypothesis_updates: [] }), true);
  assert.equal(next({ ...scopedCheck(), hypothesis_refs: ["H1"], hypothesis_updates: [] }), true);
});

test("Scoped：源码与局部规则不能代替完整合同，拼合无缺口的规则区间后才可选CHECK", () => {
  const { bundle, state } = scopedFixture();
  state.read_receipts.push({ material_ref: "candidate:product.py", start_line: 1, end_line: 245, receipt_ref: "read:source" });
  assert.deepEqual(groundedRequirements(bundle, state), []);
  assert.equal(ajv.compile(modelBodySchema(bundle, state))(scopedCheck()), false);
  state.read_receipts.push({ material_ref: "rule:R1", start_line: 1, end_line: 1, receipt_ref: "read:first-rule-line" });
  state.read_receipts.push({ material_ref: "rule:R1", start_line: 3, end_line: 3, receipt_ref: "read:last-rule-line" });
  assert.deepEqual(groundedRequirements(bundle, state), [], "规则中间存在缺口，不能用首尾行冒充完整阅读");
  assert.equal(ajv.compile(modelBodySchema(bundle, state))(scopedCheck()), false);
  state.read_receipts.push({ material_ref: "rule:R1", start_line: 2, end_line: 2, receipt_ref: "read:missing-rule-line" });
  assert.deepEqual(groundedRequirements(bundle, state), ["R1"]);
  assert.equal(ajv.compile(modelBodySchema(bundle, state))(scopedCheck()), true);
});

test("Scoped：CHECK只能引用已完整读取的规则，不能夹带另一份只知道标题的规则", () => {
  const { bundle, state } = scopedFixture();
  const extra = "R2: 另一条尚未读取的要求。";
  bundle.requirement_refs.push("R2");
  (bundle.catalog["requirements"] as Json[]).push({ id: "R2", material_ref: "rule:R2" });
  bundle.materials.push({ material_ref: "rule:R2", title: "R2", kind: "REGISTERED_RULE_EXCERPT",
    git_commit: "d".repeat(40), path: "extra-rules.md", sha256: sha(extra), content: extra });
  recordCompleteRule(state);
  const scoped = ajv.compile(modelBodySchema(bundle, state));
  assert.equal(scoped(scopedCheck()), true);
  assert.equal(scoped({ ...scopedCheck(), requirement_refs: ["R2"] }), false);
  assert.equal(scoped({ ...scopedCheck(), requirement_refs: ["R1", "R2"] }), false);
  assert.equal(scoped({ ...scopedCheck(), requirement_refs: [] }), false);
});

test("Scoped：CHECK至少引用一条已观察证据，有限枚举数组不能超过去重后的可选ID数", () => {
  const { bundle, state } = scopedFixture();
  bundle.requirement_refs.push("R1");
  recordCompleteRule(state);
  recordCompleteRule(state);
  const schema = modelBodySchema(bundle, state);
  const scoped = ajv.compile(schema);
  assert.equal(scoped(scopedCheck()), true);
  assert.equal(scoped({ ...scopedCheck(), evidence_refs: [] }), false);
  let finiteArrays = 0;
  function inspect(value: unknown): void {
    if (!value || typeof value !== "object") return;
    if (Array.isArray(value)) { value.forEach(inspect); return; }
    const node = value as Json;
    if (node["type"] === "array" && node["items"] && typeof node["items"] === "object") {
      const options = (node["items"] as Json)["enum"];
      if (Array.isArray(options)) {
        finiteArrays += 1;
        assert.equal(node["maxItems"], Math.min(32, new Set(options).size));
        assert.equal(node["uniqueItems"], true);
      }
    }
    Object.values(node).forEach(inspect);
  }
  inspect(schema);
  assert.ok(finiteArrays >= 7, "覆盖各动作分支与假设内部引用数组");
  const first = ((schema["oneOf"] as Json[])[0]!["properties"] as Json)["requirement_refs"] as Json;
  assert.equal(first["maxItems"], 1, "唯一R1不能被模型重复填满一个宽泛的32项数组");
});

test("生成字段顺序先选择动作，再表达假设与目的，最后才选择业务引用", () => {
  const { bundle, state } = scopedFixture();
  recordCompleteRule(state);
  for (const schema of [modelBodySchema(), modelBodySchema(bundle, state)]) {
    for (const branch of schema["oneOf"] as Json[]) {
      const keys = Object.keys(branch["properties"] as Json);
      assert.equal(keys[0], "action_kind");
      for (const earlier of ["hypothesis_updates", "expected_information", "rationale"]) {
        assert.ok(keys.indexOf(earlier) > 0 && keys.indexOf(earlier) < keys.indexOf("requirement_refs"));
      }
      assert.ok(keys.indexOf("payload") > keys.indexOf("requirement_refs"));
    }
  }
});
