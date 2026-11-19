import assert from "node:assert/strict";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { randomUUID } from "node:crypto";
import { test, type TestContext } from "node:test";
import { Runtime } from "./runtime.js";
import type { EvidenceReadPort, Json } from "./types.js";
import { sha } from "./types.js";
import { evidenceFixture, evidenceProposal, evidenceState } from "./evidence-test-fixture.js";

const db=process.env["EGAP_VERIFY_TEST_DATABASE_URL"];
const pgTest=(name:string,fn:(t:TestContext)=>Promise<void>)=>test(name,{skip:!db,timeout:25_000},fn);
function deferred<T>() {let resolve!:(value:T)=>void;const promise=new Promise<T>(r=>{resolve=r;});return {promise,resolve};}
/** HTTP provider 是协议夹具；Runtime、预算、事务和回执均真实落在专用 PostgreSQL。 */
async function setup(t:TestContext, options:{read?:EvidenceReadPort["read"];authorize?:(id:string)=>Promise<void>}={}) {
  const bundle=evidenceFixture();let requests=0;
  const server=createServer(async(req,res)=>{
    if(req.url==="/api/tags") {res.end(JSON.stringify({models:[{name:"evidence-test",digest:"a".repeat(64)}]}));return;}
    let raw="";for await(const chunk of req) raw+=String(chunk);
    const request=JSON.parse(raw) as {messages:{content:string}[]};requests+=1;
    const context=JSON.parse(request.messages[1]!.content).context_view as {read_receipts:Json[]};
    let body:unknown;
    if(!context.read_receipts.length) body={action_kind:"READ",unresolved_questions:["需要原件。"],expected_information:"读取已保存草稿。",rationale:"先核对来源。",
      payload:{material_ref:bundle.materials[0]!.material_ref,start_line:1,end_line:bundle.materials[0]!.content.split("\n").length}};
    else {
      const state=evidenceState();state.read_receipts=context.read_receipts;
      const {schema_version,proposal_id,based_on_decision_revision,candidate_epoch,context_view_ref,...proposal}=evidenceProposal(bundle,state);body=proposal;
    }
    res.setHeader("content-type","application/json");res.end(JSON.stringify({model:"evidence-test",done:true,message:{content:JSON.stringify(body)},prompt_eval_count:100,eval_count:80}));
  });
  await new Promise<void>(resolve=>server.listen(0,"127.0.0.1",resolve));
  const port=(server.address() as AddressInfo).port;
  const runtime=new Runtime({databaseUrl:db!,plannerPrompt:"旧发布prompt不应影响归档profile",model:{endpoint:`http://127.0.0.1:${port}`,name:"evidence-test",digest:"a".repeat(64),timeoutMs:3000,maxOutputTokens:2000},
    ...(options.read?{evidenceReadPort:{read:options.read}}:{}),...(options.authorize?{assertEvidenceDispatchAllowed:options.authorize}:{})});
  const id=randomUUID();let accepted=false;
  t.after(async()=>{if(accepted) {
    for(const table of ["events","outbox","operations","calls"]) await runtime.store.pool.query(`DELETE FROM verify_runtime.${table} WHERE run_id=$1`,[id]);
    await runtime.store.pool.query("DELETE FROM verify_runtime.runs WHERE id=$1",[id]);
  } await runtime.close();server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));});
  await runtime.store.migrate();
  await runtime.acceptRun(bundle,"test-token-".repeat(4),{model_calls:4,reads:3,no_progress:2},id);accepted=true;
  return {runtime,id,bundle,requests:()=>requests};
}
async function next(runtime:Runtime,id:string) {
  const p=await runtime.projection(id);const call=await runtime.decide(id,{command_id:String(p.next_command_id),based_on_decision_revision:Number(p.decision_basis_revision)});
  assert.equal(call.status,"COMPLETED");return call.proposal;
}
pgTest("evidence profile:真实PG两轮READ→响应、预算、幂等及无QA批准",async t=>{
  const {runtime,id}=await setup(t);const p=await next(runtime,id);const first=await runtime.submit(id,p);
  assert.equal(first.status,"COMPLETED");assert.deepEqual(await runtime.submit(id,p),first);
  const responseOp=await runtime.submit(id,await next(runtime,id));const response=(responseOp.result as Json).evidence_response as Json;
  assert.equal(response.status,"PROPOSED");assert.equal(response.qa_acceptance,"NOT_EVALUATED");
  const projection=await runtime.projection(id);assert.equal(projection.status,"PLANNED");assert.equal(projection.stop_reason,"EVIDENCE_RESPONSE_PROPOSED");
  assert.equal(projection.model_calls_used,2);assert.equal(projection.reads_used,1);assert.deepEqual(projection.current_candidate_coverage,[]);
});
pgTest("MCP等待不持有Run行锁，取消可以立即完成且迟到内容不能进入回执",async t=>{
  const entered=deferred<void>(),release=deferred<void>();let runtime!:Runtime,id!:string;
  const configured=await setup(t,{read:async req=>{
    // NOWAIT 在独立连接成功，直接证明 MCP 外部等待没有占住 Runtime 行锁。
    const c=await runtime.store.pool.connect();try {await c.query("BEGIN");await c.query("SELECT id FROM verify_runtime.runs WHERE id=$1 FOR UPDATE NOWAIT",[id]);await c.query("ROLLBACK");}finally{c.release();}
    entered.resolve();await release.promise;
    const m=configured.bundle.materials[0]!;return {...req,content:m.content,segment_sha256:sha(m.content)};
  }});runtime=configured.runtime;id=configured.id;
  const pending=runtime.submit(id,await next(runtime,id));await entered.promise;
  const cancelled=await runtime.cancel(id);assert.equal(cancelled.status,"CANCELLED");release.resolve();
  assert.equal((await pending).status,"CANCELLED");const after=await runtime.projection(id);assert.deepEqual(after.read_receipts,[]);assert.equal(after.reads_used,1);
});
pgTest("错误MCP字节不能成为证据；不确定读取持有预算并阻塞后续动作",async t=>{
  const first=await setup(t,{read:async req=>({...req,content:"wrong bytes",segment_sha256:sha("wrong bytes")})});
  const op=await first.runtime.submit(first.id,await next(first.runtime,first.id));assert.equal(op.status,"REJECTED");assert.equal((op.result as Json).error_code,"MCP_READ_BINDING_INVALID");
  assert.deepEqual((await first.runtime.projection(first.id)).read_receipts,[]);
  const second=await setup(t,{read:async()=>{throw new Error("transport unavailable");}});
  const uncertain=await second.runtime.submit(second.id,await next(second.runtime,second.id));assert.equal(uncertain.status,"UNKNOWN");
  const projection=await second.runtime.projection(second.id);assert.equal(projection.status,"BLOCKED");assert.equal(projection.reads_used,1);
});
pgTest("工作台撤回fence在模型及READ派发前阻断，已预留额度不退回",async t=>{
  let allowed=false;const {runtime,id,requests}=await setup(t,{authorize:async()=>{if(!allowed)throw new Error("withdrawn");}});
  const projection=await runtime.projection(id);const call=await runtime.decide(id,{command_id:String(projection.next_command_id),based_on_decision_revision:Number(projection.decision_basis_revision)});
  assert.equal(call.status,"CANCELLED");assert.equal(requests(),0);assert.equal((await runtime.projection(id)).model_calls_used,1);
  allowed=true;
});
pgTest("DB-only对账不派发QUEUED READ，未知deadline不重发且迟到结果不激活",async t=>{
  let reads=0;const {runtime,id}=await setup(t,{read:async req=>{reads+=1;return {...req,content:"unused",segment_sha256:sha("unused")};}});
  const proposal=await next(runtime,id) as Json;const operationId=randomUUID();
  // 精确构造“登记已提交、进程尚未drain”的崩溃窗口，不由Graph伪造操作。
  await runtime.store.transaction(async c=>{
    const {state}=await runtime.store.lock(c,id);state.reads_used+=1;
    await c.query("INSERT INTO verify_runtime.operations(id,run_id,proposal_id,operation_key,proposal,status) VALUES($1,$2,$3,$4,$5,'QUEUED')",[operationId,id,proposal.proposal_id,"queued-crash",proposal]);
    await c.query("INSERT INTO verify_runtime.outbox(id,run_id,kind,status) VALUES($1,$2,'READ','PENDING')",[operationId,id]);
    await c.query("UPDATE verify_runtime.calls SET applied=true WHERE run_id=$1",[id]);await runtime.store.save(c,state);
  });
  const projection=await runtime.reconcileProjection(id);assert.equal(projection.status,"ACTIVE");assert.equal(reads,0);
  assert.equal((await runtime.operation(id,operationId)).status,"QUEUED");
  await runtime.store.pool.query("UPDATE verify_runtime.operations SET status='DISPATCHED',deadline=now()-interval '1 second' WHERE id=$1",[operationId]);
  const expired=await runtime.reconcileProjection(id);assert.equal(expired.status,"BLOCKED");assert.equal(reads,0);
  assert.equal((await runtime.operation(id,operationId)).status,"UNKNOWN");assert.equal((await runtime.projection(id)).reads_used,1);assert.equal(reads,0);
});
