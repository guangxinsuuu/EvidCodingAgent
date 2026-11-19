import { test } from "node:test";
import assert from "node:assert/strict";
import { Ajv2020 } from "ajv/dist/2020.js";
import { evidenceReviewDigest, parseEvidenceReviewInput, parseEvidenceDecisionProposal } from "@egap/contracts";
import { buildEvidenceResponse, evidenceProposalError, evidenceModelSchema } from "./evidence-profile.js";
import { contextView } from "./context.js";
import { sha } from "./types.js";
import { evidenceFixture, evidenceProposal, evidenceState } from "./evidence-test-fixture.js";

function setup() {
  const b=evidenceFixture(),s=evidenceState(),m=b.materials[0]!;
  s.read_receipts=[{receipt_ref:"read:one",material_ref:m.material_ref,source_sha256:m.sha256,fragment_sha256:sha(m.content),
    candidate_epoch:1,start_line:1,end_line:m.content.split("\n").length,content:m.content}];
  return {b,s,p:evidenceProposal(b,s)};
}
test("EVIDENCE_REVIEW strict input has no fake baseline/diff and validates every provenance identity/hash",()=>{
  const b=evidenceFixture(); assert.deepEqual(parseEvidenceReviewInput(b),b);
  for (const mutate of [ (x:typeof b)=>{(x as unknown as Record<string,unknown>).baseline_sha="a".repeat(40);},
    (x:typeof b)=>{x.materials[0]!.content+="tamper";},(x:typeof b)=>{x.materials[0]!.provenance.tenant_id="wrong";},
    (x:typeof b)=>{x.request_items[0]!.target.historical_run_id="wrong";},(x:typeof b)=>{x.request_items.push(x.request_items[0]!);}]) {
    const changed=structuredClone(b);mutate(changed);assert.throws(()=>parseEvidenceReviewInput(changed));
  }
  const view=contextView(b,evidenceState());assert.equal(JSON.stringify(view).includes("customer_reply_draft"),false);
});
test("逐项响应保留缺原件且只成为PROPOSED，关联通过不代替QA语义接受",()=>{
  const {b,s,p}=setup(); assert.equal(evidenceProposalError(b,s,p),null);
  assert.deepEqual(parseEvidenceDecisionProposal(p),p);
  const r=buildEvidenceResponse(b,s,p,"op-test"); const {response_sha256,...rest}=r;
  assert.equal(response_sha256,evidenceReviewDigest(rest)); assert.equal(r.status,"PROPOSED");assert.equal(r.qa_acceptance,"NOT_EVALUATED");
  assert.deepEqual(r.items.map(i=>i.validated_disposition),["PROVIDED","UNAVAILABLE"]);
});
test("错误类型、邻近Job、阶段、revision、租户、run及receipt不得冒充所需原件",()=>{
  for (const field of ["job_id","ticket_id","phase","tenant_id","historical_run_id","ticket_revision"] as const) {
    const {b,s,p}=setup(); const provenance=b.materials[0]!.provenance;
    if (field==="ticket_revision") provenance[field]+=1; else provenance[field]+="wrong";
    assert.equal(evidenceProposalError(b,s,p),"EVIDENCE_TARGET_OR_TYPE_MISMATCH",field);
  }
  const {b,s,p}=setup();b.materials[0]!.kind="MODEL_RAW_RESPONSE";assert.equal(evidenceProposalError(b,s,p),"EVIDENCE_TARGET_OR_TYPE_MISMATCH");
  const fresh=setup();fresh.s.read_receipts[0]!.source_sha256="wrong";assert.equal(evidenceProposalError(fresh.b,fresh.s,fresh.p),"EVIDENCE_RECEIPT_LOCATION_INVALID");
});
test("只读标题、跨receipt引用、重复/遗漏item、用未读代替未采集都拒绝",()=>{
  let x=setup();x.p.payload.items[0]!.supporting_locations[0]!.end_line=1;assert.equal(evidenceProposalError(x.b,x.s,x.p),"PROVIDED_REQUIRES_FULL_CAPTURE");
  x=setup();x.p.payload.items[0]!.read_receipt_refs=["read:foreign"];assert.equal(evidenceProposalError(x.b,x.s,x.p),"EVIDENCE_REFERENCES_NOT_EXACT");
  x=setup();x.p.payload.items.pop();assert.equal(evidenceProposalError(x.b,x.s,x.p),"EVIDENCE_ITEMS_NOT_EXACT");
  x=setup();x.p.payload.items[1]=x.p.payload.items[0]!;assert.equal(evidenceProposalError(x.b,x.s,x.p),"EVIDENCE_ITEMS_NOT_EXACT");
  x=setup();Object.assign(x.p.payload.items[0]!,{proposed_disposition:"UNAVAILABLE",unavailable_reason:"NOT_CAPTURED_IN_PINNED_ARCHIVE",evidence_refs:[],read_receipt_refs:[],supporting_locations:[],remaining_gaps:["未读"]});
  assert.equal(evidenceProposalError(x.b,x.s,x.p),"REGISTERED_EVIDENCE_NOT_READ");
});
test("v2生成Schema兼容严格协议，禁止把CHECK/PASS放进证据profile",()=>{
  const {b,s,p}=setup();const validate=new Ajv2020({strict:true}).compile(evidenceModelSchema(b,s));
  const {schema_version,proposal_id,based_on_decision_revision,candidate_epoch,context_view_ref,...body}=p;
  assert.equal(validate(body),true);assert.equal(validate({...body,action_kind:"CHECK_INTENT"}),false);
  assert.throws(()=>parseEvidenceDecisionProposal({...p,action_kind:"PASS"}));
  assert.throws(()=>parseEvidenceDecisionProposal({...p,hypothesis_updates:[]}));
});
test("CONFLICTING须有同对象不同摘要的原件，重复副本或一句冲突主张不能成立",()=>{
  const {b,s,p}=setup();const first=p.payload.items[0]!;
  first.proposed_disposition="CONFLICTING";first.remaining_gaps=["存在互异原件待审。"];
  assert.equal(evidenceProposalError(b,s,p),"CONFLICTING_REQUIRES_DISTINCT_CAPTURES");
  const duplicate=structuredClone(b.materials[0]!);duplicate.material_ref="archive:duplicate";b.materials.push(duplicate);
  s.read_receipts.push({...s.read_receipts[0]!,material_ref:duplicate.material_ref,receipt_ref:"read:two"});
  first.evidence_refs.push(duplicate.material_ref);first.read_receipt_refs.push("read:two");first.supporting_locations.push({...first.supporting_locations[0]!,material_ref:duplicate.material_ref,receipt_ref:"read:two"});
  assert.equal(evidenceProposalError(b,s,p),"CONFLICTING_REQUIRES_DISTINCT_CAPTURES");
  duplicate.content=duplicate.content.replace("合成草稿","不同草稿");duplicate.sha256=sha(duplicate.content);s.read_receipts[1]!.source_sha256=duplicate.sha256;
  assert.equal(evidenceProposalError(b,s,p),null);
  first.proposed_disposition="PROVIDED";first.remaining_gaps=[];
  assert.equal(evidenceProposalError(b,s,p),"CONFLICTING_REGISTERED_CAPTURES");
});
test("交付消息保留原生正整数或字符串ID，零值、错误类型及隐式转换关联均拒绝",()=>{
  function delivered(messageId:unknown) {
    const {b,s,p}=setup(),m=b.materials[0]!;
    m.kind="DELIVERED_PRIVATE_NOTE";b.request_items[0]!.kind=m.kind;
    m.content=JSON.stringify({kind:m.kind,...b.request_items[0]!.target,external_message_id:messageId,private:true,content:"已交付的合成私有备注。"},null,2);
    m.sha256=sha(m.content);Object.assign(s.read_receipts[0]!,{source_sha256:m.sha256,fragment_sha256:sha(m.content),content:m.content,end_line:m.content.split("\n").length});
    p.payload.items[0]!.supporting_locations[0]!.end_line=m.content.split("\n").length;
    return {b,s,p};
  }
  for(const messageId of [52,Number.MAX_SAFE_INTEGER,"message-52"]) {
    const x=delivered(messageId);assert.equal(evidenceProposalError(x.b,x.s,x.p),null,String(messageId));
  }
  for(const messageId of [0,-1,1.5,Number.MAX_SAFE_INTEGER+1,"", " ",true,null,{},[]]) {
    const x=delivered(messageId);assert.equal(evidenceProposalError(x.b,x.s,x.p),"DELIVERED_PRIVATE_NOTE_INVALID",JSON.stringify(messageId));
  }
  const {b,s,p}=delivered(52),note=b.materials[0]!;
  const receipt={...structuredClone(note),material_ref:"archive:delivery",kind:"DELIVERY_RECEIPT"};
  const body={kind:receipt.kind,...b.request_items[0]!.target,external_message_id:52 as string|number,delivery_state:"COMPLETE",delivered_note_ref:note.material_ref,delivered_note_sha256:note.sha256};
  const update=()=>{
    receipt.content=JSON.stringify(body,null,2);receipt.sha256=sha(receipt.content);
    s.read_receipts[0]={receipt_ref:"read:delivery",material_ref:receipt.material_ref,source_sha256:receipt.sha256,candidate_epoch:1,start_line:1,end_line:receipt.content.split("\n").length};
    Object.assign(p.payload.items[0]!,{evidence_refs:[receipt.material_ref],read_receipt_refs:["read:delivery"],supporting_locations:[{material_ref:receipt.material_ref,receipt_ref:"read:delivery",start_line:1,end_line:receipt.content.split("\n").length}]});
  };
  b.materials.push(receipt);b.request_items[0]!.kind=receipt.kind;update();
  assert.equal(evidenceProposalError(b,s,p),null);
  body.external_message_id="52";update();assert.equal(evidenceProposalError(b,s,p),"DELIVERY_LINK_MISMATCH");
  body.external_message_id=0;update();assert.equal(evidenceProposalError(b,s,p),"DELIVERY_LINK_MISMATCH");
});
test("动作可行性：未读匹配原件不生成PROPOSE，READ只提供请求匹配材料",()=>{
  const b=evidenceFixture(),s=evidenceState();
  const unrelated=structuredClone(b.materials[0]!);unrelated.material_ref="archive:other-job";unrelated.provenance.job_id="other-job";b.materials.push(unrelated);
  const view=contextView(b,s),schema=evidenceModelSchema(b,s);
  assert.deepEqual(view.allowed_actions,["READ","CLARIFY","STOP"]);
  assert.deepEqual((view.material_index as {material_ref:string}[]).map(m=>m.material_ref),["archive:draft"]);
  assert.equal((view.authorized_archive as {registered_material_count:number}).registered_material_count,2);
  const branches=schema.oneOf as {properties:{action_kind:{const:string};payload:unknown}}[];
  assert.equal(branches.some(b=>b.properties.action_kind.const==="PROPOSE_EVIDENCE_RESPONSE"),false);
  assert.equal(JSON.stringify(branches.find(b=>b.properties.action_kind.const==="READ")).includes("other-job"),false);
  const read={schema_version:2 as const,proposal_id:"c5adfd90-895a-4b74-a84d-000000000001",based_on_decision_revision:1,candidate_epoch:1,context_view_ref:"context:1",
    action_kind:"READ" as const,unresolved_questions:[],rationale:"说明",expected_information:"原件",payload:{material_ref:unrelated.material_ref,start_line:1,end_line:1}};
  assert.equal(evidenceProposalError(b,s,read),"MATERIAL_NOT_RELEVANT_TO_REQUEST");
});
test("固定档案没有请求的原始模型响应：允许直接说明缺口，不提供无关READ",()=>{
  const b=evidenceFixture(),s=evidenceState();b.request_items=b.request_items.filter(i=>i.kind==="MODEL_RAW_RESPONSE");
  const view=contextView(b,s);assert.deepEqual(view.allowed_actions,["CLARIFY","PROPOSE_EVIDENCE_RESPONSE","STOP"]);
  assert.deepEqual(view.material_index,[]);assert.equal(b.materials.length,1);
  const status=(view.request_item_status as {registered_count:number;allowed_dispositions:string[]}[])[0]!;
  assert.equal(status.registered_count,0);assert.deepEqual(status.allowed_dispositions,["UNAVAILABLE"]);
  const p=evidenceProposal(b,s);const {schema_version,proposal_id,based_on_decision_revision,candidate_epoch,context_view_ref,...body}=p;
  const validate=new Ajv2020({strict:true}).compile(evidenceModelSchema(b,s));assert.equal(validate(body),true);
  assert.equal(evidenceProposalError(b,s,p),null);
});
test("已读后逐项Schema绑定当前item、material、receipt及区间，伪造引用不能生成",()=>{
  const {b,s,p}=setup();const {schema_version,proposal_id,based_on_decision_revision,candidate_epoch,context_view_ref,...body}=p;
  const validate=new Ajv2020({strict:true}).compile(evidenceModelSchema(b,s));assert.equal(validate(body),true);
  for(const field of ["evidence_refs","read_receipt_refs"] as const) {
    const changed=structuredClone(body);changed.payload.items[0]![field]=["invented:reference"];assert.equal(validate(changed),false,field);
  }
  const changed=structuredClone(body);changed.payload.items[0]!.supporting_locations[0]!.end_line=1;assert.equal(validate(changed),false);
  const status=(contextView(b,s).request_item_status as {allowed_dispositions:string[]}[])[0]!;
  assert.deepEqual(status.allowed_dispositions,["PROVIDED","PARTIAL"]);
});
test("冲突目录需读完所有匹配原件；人答与旧receipt仅历史导航，不能解锁当前PROPOSE",()=>{
  const {b,s}=setup(),second=structuredClone(b.materials[0]!);second.material_ref="archive:second";second.content+="\n";second.sha256=sha(second.content);b.materials.push(second);
  assert.equal((contextView(b,s).allowed_actions as string[]).includes("PROPOSE_EVIDENCE_RESPONSE"),false);
  s.read_receipts.push({...s.read_receipts[0]!,material_ref:second.material_ref,source_sha256:second.sha256,receipt_ref:"read:second",end_line:second.content.split("\n").length});
  const view=contextView(b,s);assert.equal((view.allowed_actions as string[]).includes("PROPOSE_EVIDENCE_RESPONSE"),true);
  assert.deepEqual((view.request_item_status as {allowed_dispositions:string[]}[])[0]!.allowed_dispositions,["CONFLICTING","PARTIAL"]);
  const old=evidenceFixture(),fresh=evidenceState();old.continuation={predecessor_run_id:s.run_id,predecessor_request_spec_revision:1,
    read_receipt_refs:["read:old-run"],remaining_gaps:["请确认阶段"],human_answer:"请按after阶段核对"};
  const parsed=parseEvidenceReviewInput(old);assert.deepEqual(parsed.continuation,old.continuation);
  assert.equal((contextView(parsed,fresh).allowed_actions as string[]).includes("PROPOSE_EVIDENCE_RESPONSE"),false);
  assert.equal(JSON.stringify(evidenceModelSchema(parsed,fresh)).includes("read:old-run"),false);
  delete old.continuation;const originalDigest=evidenceReviewDigest(old);assert.equal(evidenceReviewDigest(parseEvidenceReviewInput(old)),originalDigest);
});
