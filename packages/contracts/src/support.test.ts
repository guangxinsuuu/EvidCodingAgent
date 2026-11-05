import assert from "node:assert/strict";
import test from "node:test";
import { parseSupportJob, parseSupportRequest, parseSupportSubmission, supportJobSchema } from "./support.js";

const ticket = { schema_version: 1, ticket_id: "CS-1042", ticket_revision: 3 };
const accepted = { ...ticket, tenant_id: "tenant-a", job_id: "a".repeat(32), status: "QUEUED" };
const draft = { resolution_type: "REFUND_ELIGIBILITY_REVIEW", ticket_id: "CS-1042", ticket_revision: 3,
  applicable_policy_ref: "refund-policy-2026-10", billing_record_ref: "invoice-1042",
  eligibility: "NEEDS_MORE_EVIDENCE", proposed_action: "HUMAN_REVIEW", evidence_refs: ["policy-clause"],
  customer_reply_draft: "合成待审草稿，不是已经发送的客户消息。" };

test("DRAFT请求拒绝自行指定tenant、政策、日期或无效工单版本", () => {
  assert.deepEqual(parseSupportRequest(ticket), ticket);
  for (const body of [ { ...ticket, tenant_id: "tenant-b" }, { ...ticket, policy_ref: "answer" },
    { ...ticket, renewal_date: "2026-11-02" }, { ...ticket, ticket_revision: 0 },
    { ...ticket, ticket_revision: "3" }, { ...ticket, ticket_id: "CS-1042\n" } ]) assert.throws(() => parseSupportRequest(body));
});
test("202确认只能QUEUED；拒绝未知字段和错误schema版本", () => {
  assert.equal(parseSupportSubmission(accepted).status, "QUEUED");
  for (const body of [{ ...accepted, status: "DONE" }, { ...accepted, schema_version: 2 },
    { ...accepted, debug: "not allowed" }]) assert.throws(() => parseSupportSubmission(body));
});
test("终态与草稿必须一致且关联同一工单版本；输出深冻结", () => {
  const input = { ...accepted, status: "DONE", draft: { ...draft, evidence_refs: [...draft.evidence_refs] }, error_code: null };
  const parsed = parseSupportJob(input);
  input.draft.evidence_refs.push("later"); assert.deepEqual(parsed.draft?.evidence_refs, ["policy-clause"]);
  assert.equal(Object.isFrozen(parsed.draft), true); assert.equal(Object.isFrozen(parsed.draft?.evidence_refs), true);
  for (const bad of [ { ...input, draft: null }, { ...input, status: "PROCESSING" },
    { ...input, error_code: "PROCESSING_FAILED" }, { ...input, draft: { ...draft, ticket_revision: 2 } },
    { ...input, draft: { ...draft, proposed_action: "EXECUTE_REFUND" } },
    { ...input, draft: { ...draft, evidence_refs: ["same", "same"] } } ]) assert.throws(() => parseSupportJob(bad));
  assert.equal(parseSupportJob({ ...accepted, status: "FAILED", draft: null, error_code: "PROCESSING_FAILED" }).status, "FAILED");
});
test("协议形状校验不裁决policy/资格，Schema本身也不能被调用方改写", () => {
  const row = parseSupportJob({ ...accepted, status: "DONE", draft: { ...draft,
    applicable_policy_ref: "wrong-but-well-formed-policy", eligibility: "ELIGIBLE" }, error_code: null });
  assert.equal(row.draft?.eligibility, "ELIGIBLE");
  assert.equal(Object.isFrozen(supportJobSchema.oneOf[0]), true);
});
