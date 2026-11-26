#!/usr/bin/env python3
"""独立部署下的固定分阶段执行器；不是 Agent 自主实验或候选 Final。

CLI → PhaseExecution.step → PostgreSQL ExperimentLedger → 已绑定操作端口。
主实验/对照均走 OPEN/J1 → 发送/观察/接收/核对/封存 → 原请求重试 → J2。
每次操作前重验候选与来源；已完成步骤重放原回执，未知操作停止，不换键补发。
"""
from __future__ import annotations

from datetime import datetime,timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from phase_experiment import (ROOT,PhaseSource,bind_candidate,experiment_directory,
    parser as preparation_parser,private,product,request)
from phase_observe import PhaseObserver
from observe_native import login
from experiment_ledger import ExperimentLedger
from late_usage_observer import assess_observation,assess_transition,summarize_execution
from seed import SeedError,save_private


def require(condition,code):
    if not condition:raise SeedError(code)


def observation_complete(package):
    """完成状态与证据完整性分开；只有收到真正原件，观察才可以复用。"""
    if package.get("protocol_status")=="INVALID":return True
    if not package.get("terminal"):return False
    if package.get("job_state")=="FAILED" or package.get("delivery_state") in {"UNKNOWN","STALE","FAILED"}:
        return True
    return package.get("protocol_status")=="VALID" and not package.get("gaps") \
        and all(isinstance(package.get(k),dict) for k in ("context","result","accepted_event","decision","model_request","model_output"))


class PhaseExecution:
    """操作员驱动固定计划；候选、输入与配置由 PG 冻结，工具版本逐操作记录。

    .preparation.lock 与准备入口共用，防止两个本机进程竞争修改 seed 子账本。
    PG 操作锁和 DISPATCHED 写前记录提供第二层恢复约束；仅取得文件锁不能
    授权重发未知远端效果。角色分离仍基于本机受信控制器，不声称进程级机密隔离。
    """
    def __init__(self,args):
        self.args=args;self.directory=experiment_directory(args.directory)
        bind_candidate(args)
        self.basis=private(self.directory/"candidate-binding.json")["basis"]
        self.experiment=self.directory.name
        self.seed=private(self.directory/"native/seed.json")
        require(args.ledger_dsn_file.is_file() and not args.ledger_dsn_file.is_symlink()
            and not args.ledger_dsn_file.stat().st_mode & 0o077,"PRIVATE_LEDGER_DSN_REQUIRED")
        # 凭据不进入实验配置、操作输入、报告或候选容器。
        self.ledger=ExperimentLedger(args.ledger_dsn_file.read_text().strip(),self.directory/"execution-evidence",
            schema="independent_phase_ledger")
        self.ledger.register_experiment(self.experiment,candidate=self.basis["candidate"],configuration=self.basis,
            inputs={"scenario_specs":self.seed["scenario_specs"],"plan_version":"KNOWN_PHASE_SEQUENCE_V1"})
        self.deployed=product.registered_directory(args.deployment)
        self.observer=PhaseObserver(self.directory,self.deployed,args.platform_repo,self.basis)
        self.replayed=[]
        self.process_id=uuid4().hex

    def step(self,key,kind,effect,body,callback):
        """一次唯一操作：身份先核验，原始输出存入不可覆盖证据，再登记结果。"""
        bind_candidate(self.args)
        def invoke(dispatch):
            paths=[Path(__file__),Path(__file__).with_name("phase_observe.py"),Path(__file__).with_name("phase_experiment.py")]
            paths += [ROOT/"tools/multi-system"/p for p in ("release_seed.py","business_source.py","seed.py",
                "late_usage_observer.py","release_oracle.py","experiment_ledger.py")]
            dispatch.record_evidence("execution_identity",{"egap_commit":product.deployment.command(["git","rev-parse","HEAD"]),
                "candidate_binding":self.basis,"tools":{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}})
            return callback(dispatch)
        result=self.ledger.execute(self.experiment,key,kind=kind,effect=effect,request=body,callback=invoke)
        if result.get("replayed"):self.replayed.append(key)
        return result["result"]

    def source_step(self,dispatch,method,label,**kwargs):
        """父账本链接已有原生/商业子账本；只有本轮产生的真实 POST 回执记为发送。

        子操作先保存请求，父步骤未知时不能再次调用本方法。已有商业幂等命令和
        原生 write_once 回执保留，后续专门对账可读取，不因父步骤异常重新造事件。
        """
        source=PhaseSource(self.args)
        before=len(source.state["receipts"])
        native_before=len(private(self.directory/"native/seed-state.json")["receipts"])
        try:return getattr(source,method)(label,**kwargs)
        finally:
            receipts=source.state["receipts"][before:]
            native=private(self.directory/"native/seed-state.json")
            receipts += native["receipts"][native_before:]
            for receipt in receipts:
                if receipt.get("method")!="GET" and isinstance(receipt.get("status"),int):
                    dispatch.record_sent(receipt)
            dispatch.record_evidence("source_suboperations",{"method":method,"label":label,
                "commands":source.state["commands"],"native_operations":native["operations"],"receipts":receipts})

    def submit(self,case,key,path,body):
        """先取得普通客服 token，后进入写账本；HTTP 超时保持 UNKNOWN，不自动重投。"""
        token=login(self.observer.caller,case["tenant_id"])
        def invoke(dispatch):
            response=request(self.observer.base,path,token=token,method="POST",body=body)
            receipt={"status":response[0],"response":response[1],"path":path,**response[2]}
            dispatch.record_sent(receipt)
            return receipt
        return self.step(key,"PRODUCT_REQUEST","WRITE",{"path":path,"body":body},invoke)

    def wait_job(self,case,job_id,phase):
        """最多三轮观察同一 Job；每轮 120 秒，完整包逐次落账，不重提任务。"""
        prefix=case["label"]+".observe."+phase+"."
        existing=[r for r in self.ledger.list_operations(self.experiment) if r["operation_key"].startswith(prefix)]
        for row in existing:
            if row["status"]=="COMPLETED" and observation_complete(row.get("result") or {}):
                return self.step(row["operation_key"],"OBSERVE_JOB","READ",{"job_id":job_id},lambda _:None)
        require(len(existing)<3,"PHASE_OBSERVATION_BUDGET_EXHAUSTED")
        def observe(dispatch):
            deadline=time.monotonic()+120
            while True:
                package=self.observer.observe(case,job_id)
                dispatch.record_evidence("business_observation",package)
                if observation_complete(package) or time.monotonic()>=deadline:
                    package["wait_status"]="TERMINAL" if observation_complete(package) else "WAITING"
                    return package
                time.sleep(1)
        return self.step(prefix+str(len(existing)+1),"OBSERVE_JOB","READ",{"job_id":job_id},observe)

    def aggregate(self,case):
        """按新观察 ID 有界读取聚合；0 是 PENDING，不能据此封存或补造回执。"""
        label=case["label"];prefix=label+".aggregate."
        old=[r for r in self.ledger.list_operations(self.experiment) if r["operation_key"].startswith(prefix)]
        for row in old:
            if row["status"]=="COMPLETED" and (row.get("result") or {}).get("status")=="AGGREGATE_VISIBLE":
                return self.step(row["operation_key"],"OBSERVE_AGGREGATE","READ",row["request"],lambda _:None)
        for attempt in range(len(old)+1,min(len(old)+31,91)):
            identity=label+"-aggregate-"+str(attempt)
            observed=self.step(prefix+str(attempt),"OBSERVE_AGGREGATE","READ",{"label":label,"observation_id":identity},
                lambda d:self.source_step(d,"observe_usage",label,observation_id=identity))
            if observed["status"]=="AGGREGATE_VISIBLE":return observed
            time.sleep(1)
        raise SeedError("PHASE_AGGREGATE_OBSERVATION_BUDGET_EXHAUSTED")

    def run_case(self,native_case):
        """固定实验次序。任何必需观察未完成就停止本组，不向后推进商业事实。"""
        case={**native_case,"experiment_id":self.experiment,"candidate_sha":self.basis["candidate"]["sut_commit"]}
        label=case["label"]
        initial=self.step(label+".initial","OBSERVE_INITIAL","READ",{"label":label},
            lambda d:self.source_step(d,"observe_initial_state",label,observation_id="execution-initial"))
        require(initial["status"]=="READY","PHASE_INITIAL_STATE_CHANGED")
        body={k:case[k] for k in ("tenant_id","conversation_id","invoice_id")}
        body["operation_key"]=self.experiment+":"+label+":submit"
        accepted=self.submit(case,label+".submit","/integrations/disputes",body)
        if accepted["status"]!=202 or not isinstance(accepted["response"].get("job_id"),str):
            return {"label":label,"status":"ADMISSION_UNRESOLVED","submission":accepted}
        j1=accepted["response"]["job_id"]
        before=self.wait_job(case,j1,"j1")
        initial_verdict=assess_observation(case,before,stage="initial")
        if not observation_complete(before) or before.get("job_state")!="DONE" or before.get("delivery_state")!="COMPLETE":
            return {"label":label,"status":"J1_INCOMPLETE","observation":before,"verdict":initial_verdict}
        dispatched=self.step(label+".dispatch","SEND_USAGE","WRITE",{"label":label},lambda d:self.source_step(d,"dispatch_usage",label))
        if self.args.interrupt_after==label+".dispatch":
            # 真正退出进程，不能用内存中的重放冒充恢复。中断点在 PG COMMITTED 之后。
            save_private(self.directory/"interruption.private.json",{"experiment_id":self.experiment,
                "operation_key":label+".dispatch","exit_code":75,"pid":os.getpid(),
                "at":datetime.now(timezone.utc).isoformat(),"boundary":"AFTER_COMMITTED_NATIVE_SEND"})
            print(json.dumps({"status":"INTERRUPTED_AFTER_COMMIT","operation_key":label+".dispatch"}),flush=True)
            os._exit(75)
        aggregate=self.aggregate(case);observation_id=aggregate["observation_id"]
        for action,method in (("receive","record_usage_received"),("reconcile","reconcile_usage")):
            self.step(label+"."+action,action.upper()+"_USAGE","WRITE",{"label":label,"observation_id":observation_id},
                lambda d,method=method:self.source_step(d,method,label,observation_id=observation_id))
        sealed=self.step(label+".seal","SEAL_COVERAGE","WRITE",{"label":label},lambda d:self.source_step(d,"seal_usage",label))
        retry=self.submit(case,label+".retry","/integrations/disputes",body)
        refreshed=self.submit(case,label+".reevaluate",f"/integrations/disputes/{case['tenant_id']}/{j1}/reevaluate",
            {"operation_key":self.experiment+":"+label+":reevaluate","reason":"Registered usage arrived; reassess the original claim against new facts."})
        if refreshed["status"]!=202 or not isinstance(refreshed["response"].get("job_id"),str):
            return {"label":label,"status":"REEVALUATION_UNRESOLVED","receipt":refreshed,"initial_verdict":initial_verdict}
        j2=refreshed["response"]["job_id"];after=self.wait_job(case,j2,"j2")
        old=self.step(label+".observe.old","READ_OLD_SNAPSHOT","READ",{"job_id":j1},lambda _:self.observer.observe(case,j1))
        return {"label":label,"status":"OBSERVED" if observation_complete(after) and observation_complete(old) else "J2_INCOMPLETE",
            "j1":j1,"j2":j2,"before":before,"after":after,"old_after":old,"initial_verdict":initial_verdict,
            "final_verdict":assess_observation(case,after,stage="after_seal"),"transition":assess_transition(before,after,old,[retry],refreshed),
            "dispatch":dispatched,"aggregate":aggregate,"seal":sealed,"retry":retry,"reevaluation":refreshed}

    def run(self):
        cases=[]
        for case in self.seed["bindings"]["cases"]:
            cases.append(self.run_case(case))
            save_private(self.directory/"execution-progress.private.json",{"cases":cases,"process_id":self.process_id})
        summary=summarize_execution(cases)
        report={"experiment_id":self.experiment,"process_id":self.process_id,"basis":self.basis,"cases":cases,**summary,
            "ledger":self.ledger.resume_summary(self.experiment),"actual_counts":self.observer.counts(),
            "replayed_operations":self.replayed,"agent_autonomous_success":False,"protected_final":False,
            "release_authorized":False,"scope":"FRESH_KNOWN_PHASE_EXPERIMENT_LOCAL_CHECKS"}
        save_private(self.directory/"execution-runs"/(self.process_id+".json"),report)
        save_private(self.directory/"execution-result.private.json",report)
        return {"status":"PHASE_OBSERVATIONS_RECORDED","experiment_id":self.experiment,"scoped_result":summary["scoped_result"],
            "cases":[{"label":c["label"],"status":c["status"],"j1":c.get("j1"),"j2":c.get("j2")} for c in cases],
            "replayed_operations":len(self.replayed),"protected_final":False}


def main():
    parser=preparation_parser(actions=("run","status"))
    # 复用明确的来源/候选参数，而非允许 Agent 指定 URL、任意命令或凭据。
    parser.add_argument("--ledger-dsn-file",type=Path,required=True)
    parser.add_argument("--interrupt-after",choices=("late_usage.dispatch","post_claim_usage.dispatch"))
    args=parser.parse_args();directory=experiment_directory(args.directory)
    with (directory/".preparation.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        execution=PhaseExecution(args)
        result=execution.run() if args.action=="run" else {"status":"OBSERVED","ledger":execution.ledger.resume_summary(execution.experiment),
            "actual_counts":execution.observer.counts()}
    print(json.dumps(result))


if __name__=="__main__":
    try:main()
    except Exception as error:
        # 异常原文可能含 SQL/DSN；只输出项目稳定码或异常类型。
        print(json.dumps({"status":"INCOMPLETE","error_code":str(error) if isinstance(error,SeedError) else
            getattr(error,"code",type(error).__name__)}))
        raise SystemExit(2)
