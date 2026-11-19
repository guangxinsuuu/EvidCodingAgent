import { randomUUID } from "node:crypto";
import type { EvidenceReviewInput, EvidenceDecisionProposal } from "@egap/contracts";
import { sha, type RunState } from "./types.js";

/** 仅协议与竞态测试的合成档案；不属于真实业务回执。 */
export function evidenceFixture(): EvidenceReviewInput {
  const target = {tenant_id:"tenant-test",historical_run_id:"historical-test",job_id:"job-test",ticket_id:"ticket-test",ticket_revision:2,phase:"after"};
  const content = JSON.stringify({kind:"SAVED_PRODUCT_DRAFT",...target,customer_reply_draft:"这是合成草稿，不是模型原始响应。"},null,2);
  return {schema_version:2,task_mode:"EVIDENCE_REVIEW",scope:"LOCAL_ARCHIVED_EVIDENCE",tenant_id:target.tenant_id,
    candidate_sha:"a".repeat(40),historical_run_id:target.historical_run_id,request_id:"request-test",request_spec_revision:1,
    request_spec_sha256:"b".repeat(64),case_id:"case-test",case_scope_revision:1,task_id:"task-test",obligation_id:"obligation-test",
    source_bundle_id:"bundle-test",source_bundle_sha256:"c".repeat(64),goal:"补取已归档草稿，并保留未采集的模型原始响应缺口。",
    reviewed_policy:{policy_id:"test-policy",policy_version:"1",sha256:"d".repeat(64)},
    authorized_manifest:{manifest_id:"test-manifest",manifest_version:"1",sha256:"e".repeat(64)},
    continuation:null,
    request_items:[{item_id:"E1",kind:"SAVED_PRODUCT_DRAFT",target,required:true,acceptance_criteria:["同 Job、ticket、revision、阶段的已保存原文。"]},
      {item_id:"E4",kind:"MODEL_RAW_RESPONSE",target,required:true,acceptance_criteria:["供应商原始响应。"]}],
    materials:[{material_ref:"archive:draft",kind:"SAVED_PRODUCT_DRAFT",title:"合成已保存草稿",sha256:sha(content),content,
      provenance:{...target,candidate_sha:"a".repeat(40),source_ref:"archive:test",source_sha256:"f".repeat(64)}}]};
}
export function evidenceState(): RunState {
  return {run_id:randomUUID(),status:"ACTIVE",candidate_epoch:1,decision_basis_revision:1,event_seq:0,config_digest:"x",limits:{model_calls:4,reads:3,no_progress:2},
    model_calls_used:0,reads_used:0,no_progress:0,hypotheses:[],read_receipts:[],latest_feedback:null,stop_reason:null};
}
export function evidenceProposal(bundle: EvidenceReviewInput, state: RunState): Extract<EvidenceDecisionProposal,{action_kind:"PROPOSE_EVIDENCE_RESPONSE"}> {
  return {schema_version:2,proposal_id:randomUUID(),based_on_decision_revision:state.decision_basis_revision,candidate_epoch:state.candidate_epoch,
    context_view_ref:"context:test",action_kind:"PROPOSE_EVIDENCE_RESPONSE",unresolved_questions:["模型原始响应未归档。"],
    expected_information:"逐项区分原件与缺口。",rationale:"已读真实登记材料，无法用产品草稿反推供应商响应。",payload:{summary:"草稿已引用，模型原始响应缺失。",
      items:bundle.request_items.map(i => {
        const m=bundle.materials.find(m=>m.kind===i.kind);
        const r=m?state.read_receipts.find(r=>r.material_ref===m.material_ref):undefined;
        return {item_id:i.item_id,proposed_disposition:m?"PROVIDED":"UNAVAILABLE",evidence_refs:m?[m.material_ref]:[],read_receipt_refs:r?[String(r.receipt_ref)]:[],
          supporting_locations:m&&r?[{material_ref:m.material_ref,receipt_ref:String(r.receipt_ref),start_line:1,end_line:m.content.split("\n").length}]:[],
          remaining_gaps:m?[]:["固定档案未采集此原件。"],unavailable_reason:m?null:"NOT_CAPTURED_IN_PINNED_ARCHIVE",explanation:m?"已读取固定原件。":"缺失不等于未发生。"};
      })}};
}
