import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { parseEvidenceDecisionProposal } from "./evidence-review.js";
import { DecisionProposalError } from "./verify.js";
const corpus=JSON.parse(readFileSync(new URL("../../../protocols/verify-agent/v2/conformance.json",import.meta.url),"utf8")) as {cases:{name:string;valid:boolean;value:unknown}[]};
for(const c of corpus.cases) test(`Evidence v2跨语言协议 ${c.name}`,()=>{
  if(c.valid) assert.deepEqual(parseEvidenceDecisionProposal(c.value),c.value);
  else assert.throws(()=>parseEvidenceDecisionProposal(c.value),DecisionProposalError);
});
test("证据提案独立复制且冻结，不能解析后再改变引用",()=>{
  const input=structuredClone(corpus.cases[0]!.value) as Record<string,unknown>;
  const proposal=parseEvidenceDecisionProposal(input);assert.equal(Object.isFrozen(proposal.payload),true);
  (input.payload as Record<string,unknown>).material_ref="changed";
  if(proposal.action_kind==="READ") assert.equal(proposal.payload.material_ref,"archive:test");
});
