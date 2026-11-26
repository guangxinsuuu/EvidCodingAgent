#!/usr/bin/env python3
"""新分阶段实验的受信准备入口；不从旧产品工作树启动代码或复用旧业务对象。

prepare-native → 独立原生平台 API → 固定主实验/对照 seed；平台操作员另行创建
实验访问入口及商业来源；prepare-source → 分角色来源客户端 → OPEN 初态读回。
prepare-native 仅登记计划候选；bind-candidate 核对实际部署，不提交业务 Job。
后续执行器仍须逐操作复核身份，不能把本次预检回执作为永久执行授权。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/"tools/product-boundary"),str(ROOT/"tools/multi-system"),str(ROOT/"tools/source-platform")]
from bind_platform import binding, access_binding, private, digest, request
from commercial_attachment import validate as validate_source
from prepare_commercial import PublishedRules
from release_seed import NativeObjects, SourceObjects, fresh_usage_specs
from seed import SeedError, save_private, sha
import product
from observe_product import bind_deployment
from observe_native import connect
from model_attachment import validate as validate_model
from platform_attachment import validate_attachment


def experiment_directory(path):
    path=path.absolute()
    if path.parent!=(ROOT/"artifacts/phase-experiments") or not re.fullmatch(r"[a-z0-9_-]{3,40}",path.name) \
            or any(p.is_symlink() for p in (path,*path.parents)):
        raise SeedError("PRIVATE_PHASE_EXPERIMENT_DIRECTORY_REQUIRED")
    path.mkdir(parents=True,exist_ok=True,mode=0o700);path.chmod(0o700)
    return path


def planned_candidate(repo,image):
    """准备绑定受信构建回执；只声明计划，不把 image label 单独当成部署证明。"""
    if not re.fullmatch(r"sha256:[a-f0-9]{64}",image or ""):
        raise SeedError("FIXED_CANDIDATE_IMAGE_REQUIRED")
    source=product.checkout(repo)
    record=private(product.BUILDS/(image.split(":")[1]+".image.json"))
    actual=json.loads(product.deployment.command(["docker","image","inspect",image]))[0]
    if record["status"]!="BUILT" or record["image_id"]!=image or record["source"]["repository_url"]!=source.profile["repository_url"] \
            or actual["Id"]!=image or actual["Config"]["Labels"].get("org.opencontainers.image.revision")!=record["source"]["commit"] \
            or source._git("rev-parse",record["source"]["commit"]+"^{tree}").decode().strip()!=record["source"]["tree"]:
        raise SeedError("PLANNED_CANDIDATE_IDENTITY_MISMATCH")
    return {"repository_url":record["source"]["repository_url"],"commit":record["source"]["commit"],"tree":record["source"]["tree"],
        "image_id":image,"build_operation_id":record["operation_id"],"qualification":"PLANNED_CANDIDATE_NOT_DEPLOYMENT_OBSERVATION"}


def prepare_native(args):
    directory=experiment_directory(args.directory)
    platform_dir,platform=binding(args.platform_repo,args.platform_project)
    runtime=private(platform_dir/"preparer.private.json")
    if runtime["project"]!=args.platform_project or any(runtime[k]["base_url"]!=platform["deployment"]["endpoints"][k] for k in ("chatwoot","lago","keycloak")):
        raise SeedError("EXACT_NATIVE_PREPARER_REQUIRED")
    inputs={"experiment_id":directory.name,"platform":{k:v for k,v in platform.items() if k!="egap_commit"},
        "preparer_config_sha256":digest((platform_dir/"preparer.private.json").read_bytes()),
        "planned_candidate":planned_candidate(args.product_repo,args.image),"scenario_specs":fresh_usage_specs(args.period_start)}
    path=directory/"preparation-manifest.json"
    if path.exists():
        if private(path)["inputs"]!=inputs:raise SeedError("PHASE_PREPARATION_IDENTITY_CHANGED")
    else:
        save_private(path,{"schema_version":1,"inputs":inputs,"created_at":datetime.now(timezone.utc).isoformat(),
            "preparer_code_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),"agent_dispatch_authorized":False})
    writer=NativeObjects(runtime,directory/"native/seed.json",run_id=directory.name,specs=inputs["scenario_specs"])
    seed=writer.prepare()
    if seed.get("history_reuse") is not None or len(seed["bindings"]["cases"])!=2:
        raise SeedError("FRESH_MAIN_AND_CONTROL_OBJECTS_REQUIRED")
    save_private(directory/"native-preparation-result.json",{"experiment_id":directory.name,"seed_sha256":digest((directory/"native/seed.json").read_bytes()),
        "scenario_specs_sha256":sha(seed["scenario_specs"]),"qualification":"FRESH_OBJECTS_KNOWN_PHASE_SCENARIO","planned_candidate":inputs["planned_candidate"]})
    return {"status":"FRESH_NATIVE_OBJECTS_PREPARED","experiment_id":directory.name,"cases":len(seed["bindings"]["cases"]),"history_reused":False}


class PhaseSource(SourceObjects):
    """受信阶段控制器组合准备者和读者；独立观察进程不获得本对象或准备者凭据。

    原生写仍走 NativeObjects 的未知写账本。self.cw/self.lago 只用实验 OBSERVER；
    商业命令用 PREPARER，商业初态查询用独立 OBSERVER 服务，绝不回退管理员 GET。
    """
    def __init__(self,args):
        directory=experiment_directory(args.directory)
        platform_dir,platform=binding(args.platform_repo,args.platform_project)
        access_dir,access=access_binding(args.platform_repo,platform,args.access_id)
        seed=private(directory/"native/seed.json");enrollment=private(directory/"enrollment.json")
        prepared=private(directory/"preparation-manifest.json")["inputs"]
        if enrollment.get("experiment_access")!=access or access["seed_sha256"]!=digest((directory/"native/seed.json").read_bytes()) \
                or seed["run_id"]!=directory.name or seed["scenario_specs"]!=prepared["scenario_specs"] \
                or prepared["platform"]!={k:v for k,v in platform.items() if k!="egap_commit"}:
            raise SeedError("PHASE_NATIVE_ACCESS_MISMATCH")
        runtime=private(platform_dir/"preparer.private.json")
        if prepared["preparer_config_sha256"]!=digest((platform_dir/"preparer.private.json").read_bytes()):
            raise SeedError("PHASE_PREPARER_CHANGED")
        source_dir=args.platform_repo/".local"/args.commercial_project
        receipt=private(source_dir/"deployment-receipt.json");validate_source(receipt)
        scope_set=private(source_dir/"scope-set.json")
        if scope_set["native_seed_sha256"]!=access["seed_sha256"] \
                or scope_set["native_enrollment_sha256"]!=digest((directory/"enrollment.json").read_bytes()) \
                or scope_set.get("experiment_access")!={"project":access["project"],"manifest_sha256":access["manifest_sha256"]}:
            raise SeedError("PHASE_COMMERCIAL_SOURCE_MISMATCH")
        native_reader=private(access_dir/"observer-access.private.json")
        preparer=private(source_dir/"preparer-client.private.json")
        reader=private(source_dir/"observer-client.private.json")
        if native_reader["role"]!="OBSERVER" or native_reader["instance_id"]!=access["project"] or native_reader["base_url"]!=access["access_endpoint"]:
            raise SeedError("PHASE_NATIVE_READER_MISMATCH")
        for config,role in ((preparer,"preparer"),(reader,"observer")):
            if config["instance_id"]!=receipt["project"] or config["role"]!=role.upper() \
                    or config["base_url"]!="http://127.0.0.1:"+str(receipt["endpoints"][role]["port"]):
                raise SeedError("PHASE_SOURCE_ROLE_MISMATCH")
        loader=PublishedRules(args.rules_reader_config,args.rules_repo,args.rules_commit,directory)
        read_runtime={"chatwoot":{"base_url":native_reader["base_url"],"api_token":native_reader["token"],"account_id":seed["chatwoot_account_id"]},
            "lago":{"base_url":native_reader["base_url"],"api_key":native_reader["token"]}}
        ready={"base_url":preparer["base_url"],"instance_id":receipt["project"],"source_digest":receipt["image_id"],"api_version":"commercial-source-v1"}
        super().__init__(directory,native=seed,runtime=runtime,ready=ready,private={"admin_token":preparer["token"]},
            read_runtime=read_runtime,commercial_reader=reader,asset_loader=loader)
        self.deployment_binding={"platform":prepared["platform"],"experiment_access":access,"commercial_source":receipt,
            "rules_commit":args.rules_commit,"native_seed_sha256":access["seed_sha256"],"planned_candidate":prepared["planned_candidate"]}
        if self.state.get("deployment_binding",self.deployment_binding)!=self.deployment_binding:
            raise SeedError("PHASE_SOURCE_DEPLOYMENT_CHANGED")
        self.state["deployment_binding"]=self.deployment_binding;self.save()

    def prepare_initial(self):
        """恢复可重用写命令，但初态必须重新 GET；旧 READY 不证明当前仍是 OPEN。

        原始资格文件保留第一次结果，后续观察分别存档。已经派发的实验不允许
        再走初态入口；未知原生派发则由 observe_initial_state 的原生账本检查拒绝。
        """
        if self.state.get("usage_dispatches") or self.state.get("usage_completions"):
            raise SeedError("INITIAL_PHASE_ALREADY_ADVANCED")
        self.prepare()
        attempt="initial-"+uuid4().hex
        observations={label:self.observe_initial_state(label,observation_id=attempt) for label in self.specs}
        result={"experiment_id":self.native["run_id"],"deployment_binding":self.deployment_binding,
            "observation_id":attempt,"observations":observations,"qualification":"TECHNICAL_INITIAL_STATE_NOT_BUSINESS_PASS"}
        save_private(self.directory/"initial-observations"/(attempt+".json"),result)
        save_private(self.directory/"latest-initial-qualification.json",result)
        if any(o["status"]!="READY" for o in observations.values()):raise SeedError("PHASE_INITIAL_STATE_NOT_READY")
        if not (self.directory/"initial-qualification.json").exists():
            save_private(self.directory/"initial-qualification.json",result)
        return {"status":"OPEN_INITIAL_STATE_QUALIFIED","experiment_id":self.native["run_id"],"cases":len(observations),
            "native_usage_sent":False,"coverage_sealed":False,"business_final_qualification":False}


def match_planned_candidate(planned,actual,manifest):
    """计划版本、受信构建与 Docker 实物三方相等；相同 commit 不允许偷偷换镜像。"""
    if any(actual.get(observed)!=planned.get(expected) for observed,expected in (
        ("sut_commit","commit"),("sut_tree","tree"),("sut_image_id","image_id"))) \
            or manifest.get("build_operation_id")!=planned["build_operation_id"] \
            or manifest.get("product_repository_url")!=planned["repository_url"]:
        raise SeedError("PHASE_ACTUAL_CANDIDATE_MISMATCH")


def bind_candidate(args):
    """部署后只读预检 → 冻结实际身份。既不触发模型，也不允许沿用其他实验的结果。

    主机核验 Docker、配置与来源身份；只读 DB 角色取得任务计数并实测写权限
    被拒。将来 Runtime 派发须消费此绑定并再次检查实物，不信任产品自报版本。
    """
    directory=experiment_directory(args.directory)
    prepared=private(directory/"preparation-manifest.json")["inputs"]
    initial=private(directory/"latest-initial-qualification.json")
    enrollment=private(directory/"enrollment.json")
    deployed=product.registered_directory(args.deployment)
    manifest=private(deployed/"manifest.json");receipt=private(deployed/"deployment-receipt.json")
    if any(receipt.get(key)!=value for key,value in manifest.items()):
        raise SeedError("PHASE_DEPLOYMENT_RECEIPT_CHANGED")
    validate_attachment(deployed);validate_model(deployed)
    actual=bind_deployment(receipt,args.product_repo)
    match_planned_candidate(prepared["planned_candidate"],actual,manifest)
    _,platform=binding(args.platform_repo,args.platform_project)
    access_id=enrollment["basis"]["access_scope_id"]
    _,access=access_binding(args.platform_repo,platform,access_id)
    commercial=receipt["commercial_source_binding"];validate_source(commercial)
    stable=lambda value:{k:v for k,v in value.items() if k!="egap_commit"}
    seed_sha=digest((directory/"native/seed.json").read_bytes())
    if stable(receipt["platform_binding"])!=prepared["platform"] or stable(platform)!=prepared["platform"] \
            or receipt.get("experiment_access_binding")!=access or enrollment["experiment_access"]!=access \
            or access["seed_sha256"]!=seed_sha or enrollment["basis"]["seed_sha256"]!=seed_sha \
            or initial["deployment_binding"]["native_seed_sha256"]!=seed_sha \
            or receipt["platform_enrollment_sha256"]!=digest((directory/"enrollment.json").read_bytes()) \
            or initial["experiment_id"]!=directory.name or initial["deployment_binding"]["commercial_source"]!=commercial \
            or initial["deployment_binding"]["experiment_access"]!=access \
            or initial["deployment_binding"]["planned_candidate"]!=prepared["planned_candidate"] \
            or set(initial["observations"])!={"late_usage","post_claim_usage"} \
            or any(row["status"]!="READY" for row in initial["observations"].values()):
        raise SeedError("PHASE_DEPLOYED_EXPERIMENT_MISMATCH")
    base="http://127.0.0.1:"+str(receipt["endpoints"]["api"]["port"])
    ready=request(base,"/health/ready")
    if receipt["mode"]!="LIVE_MODEL" or ready[0]!=200 or ready[1].get("checks",{}).get("execution_mode")!="LIVE_MODEL":
        raise SeedError("PHASE_LIVE_PRODUCT_NOT_READY")
    basis={"experiment_id":directory.name,"candidate":stable(actual),"deployment_manifest_sha256":digest((deployed/"manifest.json").read_bytes()),
        "preparation_manifest_sha256":digest((directory/"preparation-manifest.json").read_bytes()),
        "native_seed_sha256":digest((directory/"native/seed.json").read_bytes()),"enrollment_sha256":digest((directory/"enrollment.json").read_bytes()),
        "platform":prepared["platform"],"experiment_access":access,"commercial_source":commercial,"model":receipt["model_binding"]}
    frozen=directory/"candidate-binding.json"
    if frozen.exists():
        if private(frozen)["basis"]!=basis:raise SeedError("PHASE_CANDIDATE_BINDING_CHANGED")
        return {"status":"ORIGINAL_CANDIDATE_BINDING_REVALIDATED","experiment_id":directory.name,"new_execution":False}
    observer=private(deployed/"observer.private.json")
    if observer.get("deployment_id")!=args.deployment:raise SeedError("PHASE_OBSERVER_SCOPE_MISMATCH")
    import psycopg
    with connect(receipt,observer,"support") as conn:
        counts={name:conn.execute(query).fetchone()["n"] for name,query in (
            ("jobs","SELECT count(*) AS n FROM egap_support.support_jobs"),
            ("deliveries","SELECT count(*) AS n FROM egap_support.external_operations"))}
        conn.execute("SET default_transaction_read_only=off");conn.commit()
        try:conn.execute("UPDATE egap_support.external_request_keys SET job_id=job_id WHERE false")
        except psycopg.errors.InsufficientPrivilege:conn.rollback()
        else:conn.rollback();raise SeedError("PHASE_OBSERVER_CAN_WRITE")
    with connect(receipt,observer,"gateway") as conn:
        counts["model_calls"]=conn.execute("SELECT count(*) AS n FROM egap_gateway.calls").fetchone()["n"]
    if any(counts.values()):raise SeedError("PHASE_REQUIRES_UNUSED_PRODUCT_DEPLOYMENT")
    save_private(frozen,{"basis":basis,"observed_at":datetime.now(timezone.utc).isoformat(),"initial_counts":counts,
        "observer_write_denied":True,"health_receipt":{"http_status":ready[0],**ready[2]},
        "qualification":"DEPLOYMENT_PREFLIGHT_NOT_BUSINESS_EXECUTION","agent_dispatch_authorized":False,"business_final_qualification":False})
    return {"status":"FRESH_LIVE_CANDIDATE_BOUND","experiment_id":directory.name,"deployment_id":args.deployment,
        "initial_counts":counts,"observer_write_denied":True,"business_final_qualification":False}


def parser(*,actions=("prepare-native","prepare-source","bind-candidate")):
    result=argparse.ArgumentParser(description=__doc__)
    result.add_argument("action",choices=actions)
    result.add_argument("--directory",type=Path,required=True)
    result.add_argument("--platform-repo",type=Path,required=True)
    result.add_argument("--platform-project",required=True)
    result.add_argument("--product-repo",type=Path);result.add_argument("--image");result.add_argument("--period-start")
    result.add_argument("--access-id");result.add_argument("--commercial-project")
    result.add_argument("--rules-reader-config",type=Path);result.add_argument("--rules-repo",type=Path);result.add_argument("--rules-commit")
    result.add_argument("--deployment")
    return result


if __name__=="__main__":
    try:
        args=parser().parse_args();directory=experiment_directory(args.directory)
        with (directory/".preparation.lock").open("a") as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            if args.action=="prepare-native":value=prepare_native(args)
            elif args.action=="prepare-source":value=PhaseSource(args).prepare_initial()
            else:value=bind_candidate(args)
        print(json.dumps(value))
    except Exception as exc:
        print(json.dumps({"status":"INCOMPLETE","error_code":str(exc) if isinstance(exc,SeedError) else type(exc).__name__}))
        raise SystemExit(2)
