#!/usr/bin/env python3
"""虚拟来源平台操作员：固定来源代码 → 独立部署 → 分身份发布 → 实际权限验收。

此入口持 bootstrap/发布账号，不能注册为 Agent 工具。Agent 后续只消费自己的
planner-reader 投影；Oracle 与产品各持独立配置，原件和凭据只落 ignored artifacts。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
from uuid import uuid4

from client import SourceClient, SourceClientError

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.registry.reference_resolver import LocalGitReader, MaterialError

STATE=ROOT/"artifacts/source-platform"
REGISTRATION=ROOT/"config/source-publications.v1.json"


def command(args,*,timeout=120):
    result=subprocess.run(args,cwd=ROOT,capture_output=True,text=True,timeout=timeout)
    if result.returncode:raise SourceClientError("SOURCE_HOST_COMMAND_FAILED")
    return result.stdout.strip()


def save(path,value,mode=0o600):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(".tmp")
    with tmp.open("w") as stream:
        os.chmod(tmp,mode);json.dump(value,stream,ensure_ascii=False,indent=2);stream.write("\n");stream.flush();os.fsync(stream.fileno())
    os.replace(tmp,path)


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def git_reader(repo:Path,name:str):
    git=LocalGitReader(repo)
    if Path(git._git("rev-parse","--show-toplevel").decode().strip()).resolve()!=repo.resolve() \
            or git._git("config","--get","remote.origin.url").decode().strip()!=f"https://github.com/guangxinsuuu/{name}.git":
        raise SourceClientError("SOURCE_REPOSITORY_MISMATCH")
    return git


def registration_path(value):
    """发布目录是操作员维护的 config 原件；禁止越界或软链接偷换目录。"""
    path=Path(value)
    if not path.is_absolute():path=ROOT/path
    if not path.is_file() or path.resolve()!=path.absolute() or not path.resolve().is_relative_to(ROOT/"config"):
        raise SourceClientError("SOURCE_REGISTRATION_PATH_INVALID")
    return path


def instance_registration(directory):
    """每个来源实例固定自己的发布目录；新 QA 目录不能破坏已有规则实例。"""
    manifest=json.loads((directory/"manifest.json").read_text())
    path=registration_path(manifest.get("registration_ref",str(REGISTRATION)))
    if sha(path)!=manifest["registration_sha256"]:raise SourceClientError("SOURCE_DEPLOYMENT_CONFIG_CHANGED")
    return path


def up(repo:Path,commit:str,registration:Path=REGISTRATION):
    """来源服务器独立镜像与数据卷；API 容器不挂载任何客户端或发布者的凭据文件。"""
    registration=registration_path(registration)
    git=git_reader(repo,"EvidEnterpriseSources")
    if not re.fullmatch(r"[a-f0-9]{40}",commit):raise SourceClientError("SOURCE_COMMIT_REQUIRED")
    identity="evid-sources-"+uuid4().hex[:12]
    directory=STATE/identity;directory.mkdir(parents=True,mode=0o700);os.chmod(directory,0o700)
    with tempfile.TemporaryDirectory(prefix="evid-source-context-") as temp:
        context=Path(temp)
        inputs={}
        for path in ("Dockerfile","source_platform.py"):
            raw=git.read(commit,path);(context/path).write_bytes(raw);inputs[path]=hashlib.sha256(raw).hexdigest()
        tag="evid-enterprise-sources:"+identity
        save(directory/"build-state.json",{"status":"BUILDING","tag":tag,"source_commit":commit,"inputs":inputs})
        try:
            result=subprocess.run(["docker","build","--build-arg","SOURCE_REVISION="+commit,"-t",tag,str(context)],cwd=ROOT,capture_output=True,text=True,timeout=180)
        except subprocess.TimeoutExpired:
            # Docker daemon 可能继续完成构建；保留原 tag，不能把无回执当成未执行。
            save(directory/"build-state.json",{"status":"UNKNOWN","tag":tag,"source_commit":commit,"inputs":inputs})
            raise SourceClientError("SOURCE_IMAGE_BUILD_UNKNOWN") from None
        with (directory/"build.private.log").open("w") as stream:
            os.chmod(stream.fileno(),0o600);stream.write(result.stdout+result.stderr)
        if result.returncode:
            save(directory/"build-state.json",{"status":"FAILED","tag":tag,"source_commit":commit,"inputs":inputs})
            raise SourceClientError("SOURCE_IMAGE_BUILD_FAILED")
    image=json.loads(command(["docker","image","inspect",tag]))[0]
    if image["Config"]["Labels"].get("org.opencontainers.image.revision")!=commit:raise SourceClientError("SOURCE_IMAGE_REVISION_MISMATCH")
    save(directory/"build-state.json",{"status":"CONFIRMED","tag":tag,"source_commit":commit,"inputs":inputs,"image_id":image["Id"]})
    objects=json.loads(registration.read_text())["objects"]
    roles={"business-publisher":"BUSINESS_PUBLISHER","qa-publisher":"QA_PUBLISHER","requirements-publisher":"REQUIREMENTS_PUBLISHER",
           "planner-reader":"PLANNER_READER","oracle-reader":"ORACLE_READER","product-reader":"PRODUCT_READER","operator":"SOURCE_OPERATOR","outsider-reader":"PLANNER_READER"}
    principals=[]
    for name,role in roles.items():
        grants=[]
        for obj in objects:
            if name==obj["publisher"]:permissions=("READ","PUBLISH")
            elif name in obj["readers"] or (name=="outsider-reader" and obj["namespace"]=="rules"):permissions=("READ",)
            else:permissions=()
            grants.extend({"namespace":obj["namespace"],"object_id":obj["object_id"],"permission":p} for p in permissions)
        principal={"id":"lab:"+name,"tenant":"company-b" if name=="outsider-reader" else "company-a","role":role,"token":secrets.token_urlsafe(40),"grants":grants}
        principals.append(principal)
    save(directory/"bootstrap.private.json",{"instance_id":identity,"principals":principals},mode=0o444)
    common={"image":image["Id"],"read_only":True,"cap_drop":["ALL"],"security_opt":["no-new-privileges:true"],"tmpfs":["/tmp"],"labels":{"evid.source_instance":identity}}
    compose={"name":identity,"services":{
        "bootstrap":{**common,"command":["bootstrap","--config","/run/secrets/bootstrap.json"],"volumes":["source-data:/data",f"{directory}/bootstrap.private.json:/run/secrets/bootstrap.json:ro"]},
        "api":{**common,"command":["serve"],"depends_on":{"bootstrap":{"condition":"service_completed_successfully"}},"volumes":["source-data:/data"],"ports":["127.0.0.1::8080"]}},"volumes":{"source-data":{}}}
    save(directory/"compose.json",compose)
    save(directory/"manifest.json",{"schema_version":1,"instance_id":identity,"image_id":image["Id"],"source_commit":commit,"source_inputs":inputs,
        "compose_sha256":sha(directory/"compose.json"),"registration_sha256":sha(registration),
        "registration_ref":str(registration.relative_to(ROOT)),"egap_host_commit":command(["git","rev-parse","HEAD"]),"qualification":"SIMULATED_ENTERPRISE_SOURCE"})
    command(["docker","compose","-f",str(directory/"compose.json"),"up","-d"])
    cid=command(["docker","compose","-f",str(directory/"compose.json"),"ps","-q","api"])
    container=json.loads(command(["docker","inspect",cid]))[0]
    ports=container["NetworkSettings"]["Ports"]["8080/tcp"]
    if container["Image"]!=image["Id"] or len(ports)!=1 or ports[0]["HostIp"]!="127.0.0.1":raise SourceClientError("SOURCE_DEPLOYMENT_IDENTITY_MISMATCH")
    endpoint="http://127.0.0.1:"+ports[0]["HostPort"]
    for principal in principals:
        name=principal["id"].removeprefix("lab:")
        save(directory/(name+".private.json"),{"base_url":endpoint,"token":principal["token"],"principal_id":principal["id"],"tenant_id":principal["tenant"],"instance_id":identity,"source_kind":"SIMULATED_ENTERPRISE_SOURCE"})
    receipt={"instance_id":identity,"image_id":image["Id"],"container_id":cid,"source_commit":commit,"endpoint":endpoint,
        "api_mounts":[{"destination":m["Destination"],"type":m["Type"]} for m in container["Mounts"]],"qualification":"SIMULATED_ENTERPRISE_SOURCE"}
    if any(m["type"]=="bind" for m in receipt["api_mounts"]):raise SourceClientError("SOURCE_API_SECRET_MOUNT_FORBIDDEN")
    save(directory/"deployment-receipt.json",receipt)
    # 专用 QA 目录必须显式传 instance，不抢占 Jira/规则的既有默认入口。
    if registration==REGISTRATION:save(STATE/"active.json",{"instance_id":identity})
    return receipt


def registered(identity:str|None):
    if identity is None:identity=json.loads((STATE/"active.json").read_text())["instance_id"]
    if not re.fullmatch(r"evid-sources-[a-f0-9]{12}",identity):raise SourceClientError("SOURCE_INSTANCE_INVALID")
    directory=STATE/identity;manifest=json.loads((directory/"manifest.json").read_text())
    instance_registration(directory)
    if manifest["compose_sha256"]!=sha(directory/"compose.json"):raise SourceClientError("SOURCE_DEPLOYMENT_CONFIG_CHANGED")
    receipt=json.loads((directory/"deployment-receipt.json").read_text());container=json.loads(command(["docker","inspect",receipt["container_id"]]))[0]
    if container["Image"]!=manifest["image_id"] or container["State"]["Status"]!="running" or container["Config"]["Labels"].get("evid.source_instance")!=identity:raise SourceClientError("SOURCE_INSTANCE_UNAVAILABLE")
    ports=container["NetworkSettings"]["Ports"]["8080/tcp"]
    if len(ports)!=1 or ports[0]["HostIp"]!="127.0.0.1" or receipt["endpoint"]!="http://127.0.0.1:"+ports[0]["HostPort"]:raise SourceClientError("SOURCE_ENDPOINT_DRIFT")
    return directory


def publish(directory:Path,repositories:dict):
    """按固定登记读取 Git 原件，用各自发布账号逐项发布；已有未知操作只读对账。"""
    objects=json.loads(instance_registration(directory).read_text())["objects"]
    state_path=directory/"publications.json"
    state=json.loads(state_path.read_text()) if state_path.exists() else {"schema_version":1,"objects":[]}
    for obj in objects:
        repo,commit=repositories[obj["repository"]];git=git_reader(repo,obj["repository"])
        raw=git.read(commit,obj["path"]);digest=hashlib.sha256(raw).hexdigest()
        source={"repository_url":f"https://github.com/guangxinsuuu/{obj['repository']}.git","commit":commit,"path":obj["path"],"blob":git._git("rev-parse",commit+":"+obj["path"]).decode().strip()}
        if obj["repository"]!="EvidEnterpriseSources":
            lineage=json.loads(git.read(commit,obj.get("lineage_manifest","publication-manifest.v1.json")))
            rows=[row for row in lineage["files"] if row["path"]==obj["path"]]
            if len(rows)!=1 or rows[0]["sha256"]!=digest:raise SourceClientError("EXTRACTED_ASSET_LINEAGE_MISMATCH")
        client=SourceClient.from_file(directory/(obj["publisher"]+".private.json"))
        existing=next((row for row in state["objects"] if row["namespace"]==obj["namespace"]
            and row["object_id"]==obj["object_id"] and row["version"]==obj["version"]),None)
        if existing:
            read=client.read(obj["namespace"],obj["object_id"],obj["version"],expected_sha256=digest)
            if read["publication_id"]!=existing["publication_id"] or read["metadata"]["source"]!=source:
                raise SourceClientError("PUBLICATION_BINDING_MISMATCH")
            existing["status"]="CONFIRMED";save(state_path,state);continue
        row={"namespace":obj["namespace"],"object_id":obj["object_id"],"version":obj["version"],"publication_id":uuid4().hex,"status":"DISPATCHING","content_sha256":digest,"publisher":"lab:"+obj["publisher"]}
        body={"publication_id":row["publication_id"],"content_base64":base64.b64encode(raw).decode(),"content_sha256":digest,
            "metadata":{"authority_scope":"SYNTHETIC_LOCAL_CALIBRATION","approval_state":"PENDING_USER_REVIEW" if obj["namespace"]=="rules" else "DRAFT",
            "content_type":obj["content_type"],"profile":obj["profile"],"source":source}}
        state["objects"].append(row);save(state_path,state)
        try:receipt=client.request("POST",client.path(obj["namespace"],obj["object_id"],obj["version"]),body)
        except SourceClientError as error:
            row.update(status="UNKNOWN" if error.status is None or error.status>=500 else "REJECTED",error_code=error.code);save(state_path,state);raise
        if receipt.get("publication_id")!=row["publication_id"] or receipt.get("content_sha256")!=digest or receipt.get("publisher_id")!=row["publisher"]:raise SourceClientError("PUBLICATION_RECEIPT_MISMATCH")
        row.update(status="CONFIRMED",receipt=receipt);save(state_path,state)
    return {"status":"PUBLISHED_SIMULATED_ASSETS","count":len(state["objects"]),"instance_id":directory.name}


def verify(directory:Path):
    """真实来源 ACL 验收；异常时也恢复暂时故障开关和目标对象授权。"""
    clients={name:SourceClient.from_file(directory/(name+".private.json")) for name in ("planner-reader","oracle-reader","product-reader","business-publisher","qa-publisher","operator","outsider-reader")}
    rule=("rules","refund-rules","lab-v1-extracted");answer=("acceptance","support-synthetic-v1","lab-v1-extracted")
    checks=[]
    def denied(name,client,args,expected):
        try:client.read(*args)
        except SourceClientError as error:
            if error.code!=expected:raise
            checks.append({"name":name,"status":"REJECTED_AS_EXPECTED","code":error.code});return
        raise SourceClientError("EXPECTED_ACCESS_DENIAL_MISSING")
    first=clients["planner-reader"].read(*rule)
    jira=clients["planner-reader"].read("jira","EGAPLAB-100","1")
    expected=clients["oracle-reader"].read(*answer)
    checks.append({"name":"separate_reads","status":"CONFIRMED","rule_sha256":first["content_sha256"],"jira_sha256":jira["content_sha256"],"answer_sha256":expected["content_sha256"]})
    denied("product_cannot_read_expected_answers",clients["product-reader"],answer,"OBJECT_NOT_ACCESSIBLE")
    denied("planner_cannot_read_expected_answers",clients["planner-reader"],answer,"OBJECT_NOT_ACCESSIBLE")
    denied("cross_tenant_read",clients["outsider-reader"],rule,"OBJECT_NOT_ACCESSIBLE")
    # 真实写入口的负向检查：原版本不可覆盖，读者/错误发布者不能发规则。
    body={"publication_id":uuid4().hex,"content_base64":first["content_base64"],
          "content_sha256":first["content_sha256"],"metadata":first["metadata"]}
    for name,code in (("product-reader","OBJECT_NOT_ACCESSIBLE"),("qa-publisher","OBJECT_NOT_ACCESSIBLE"),("business-publisher","VERSION_ALREADY_EXISTS")):
        try:clients[name].request("POST",SourceClient.path(*rule),body)
        except SourceClientError as error:
            if error.code!=code:raise
            checks.append({"name":name+"_cannot_overwrite_rule","status":"REJECTED_AS_EXPECTED","code":code})
        else:raise SourceClientError("EXPECTED_PUBLISH_DENIAL_MISSING")
    operator=clients["operator"]
    try:
        operator.request("POST","/v1/control",{"acl_available":False})
        denied("fresh_prior_read_with_acl_unavailable",clients["planner-reader"],rule,"ACL_UNAVAILABLE")
    finally:operator.request("POST","/v1/control",{"acl_available":True})
    grant={"principal_id":"lab:planner-reader","namespace":rule[0],"object_id":rule[1],"permission":"READ"}
    try:
        operator.request("POST","/v1/control",{**grant,"enabled":False})
        denied("revoked_exact_object",clients["planner-reader"],rule,"OBJECT_NOT_ACCESSIBLE")
        clients["planner-reader"].read("rules","refund-lab-v1","lab-v1-extracted")
        checks.append({"name":"other_object_still_readable","status":"CONFIRMED"})
    finally:operator.request("POST","/v1/control",{**grant,"enabled":True})
    assert clients["planner-reader"].read(*rule)["content_sha256"]==first["content_sha256"]
    result={"schema_version":1,"instance_id":directory.name,"status":"SOURCE_BOUNDARIES_CONFIRMED","checks":checks,"authority":"SIMULATED_NOT_COMPANY_APPROVED"}
    save(directory/"boundary-verification.json",result)
    return result


def verify_phase(directory:Path):
    """独立 QA 目录的真实权限检查；只临时切换既有读授权，finally 恢复。

Planner 只读可执行能力契约，Oracle 读取固定代码，Product/跨租户身份拒绝。
来源 ACL 不可用时，即使刚成功读过也拒绝；已有版本不能重新发布覆盖。
"""
    objects=json.loads(instance_registration(directory).read_text())["objects"]
    require_set={(o["namespace"],o["object_id"],o["version"]) for o in objects}
    contract=("acceptance","phase-review-contract","phase-v1")
    code=("acceptance","phase-json-entry","phase-v1")
    if contract not in require_set or code not in require_set:raise SourceClientError("PHASE_REGISTRATION_REQUIRED")
    clients={name:SourceClient.from_file(directory/(name+".private.json")) for name in
        ("planner-reader","oracle-reader","product-reader","qa-publisher","operator","outsider-reader")}
    checks=[]
    def denied(label,callback,expected):
        try:callback()
        except SourceClientError as error:
            if error.code!=expected:raise
            checks.append({"name":label,"status":"REJECTED_AS_EXPECTED","code":expected});return
        raise SourceClientError("EXPECTED_ACCESS_DENIAL_MISSING")
    for obj in objects:
        args=(obj["namespace"],obj["object_id"],obj["version"])
        receipt=clients["oracle-reader"].read(*args)
        checks.append({"name":"oracle_reads:"+obj["object_id"],"status":"CONFIRMED","sha256":receipt["content_sha256"]})
        denied("product_cannot_read:"+obj["object_id"],lambda args=args:clients["product-reader"].read(*args),"OBJECT_NOT_ACCESSIBLE")
        if args!=contract:
            denied("planner_cannot_read:"+obj["object_id"],lambda args=args:clients["planner-reader"].read(*args),"OBJECT_NOT_ACCESSIBLE")
    clients["planner-reader"].read(*contract)
    checks.append({"name":"planner_reads_only_contract","status":"CONFIRMED"})
    denied("cross_tenant_cannot_read_contract",lambda:clients["outsider-reader"].read(*contract),"OBJECT_NOT_ACCESSIBLE")
    first=clients["oracle-reader"].read(*code)
    body={"publication_id":uuid4().hex,"content_base64":first["content_base64"],
        "content_sha256":first["content_sha256"],"metadata":first["metadata"]}
    denied("reader_cannot_publish",lambda:clients["oracle-reader"].request("POST",SourceClient.path(*code),body),"OBJECT_NOT_ACCESSIBLE")
    denied("qa_cannot_overwrite_version",lambda:clients["qa-publisher"].request("POST",SourceClient.path(*code),body),"VERSION_ALREADY_EXISTS")
    operator=clients["operator"]
    try:
        operator.request("POST","/v1/control",{"acl_available":False})
        denied("fresh_read_still_denied_when_acl_unavailable",lambda:clients["oracle-reader"].read(*code),"ACL_UNAVAILABLE")
    finally:operator.request("POST","/v1/control",{"acl_available":True})
    grant={"principal_id":"lab:oracle-reader","namespace":code[0],"object_id":code[1],"permission":"READ"}
    try:
        operator.request("POST","/v1/control",{**grant,"enabled":False})
        denied("revoked_checker_not_readable",lambda:clients["oracle-reader"].read(*code),"OBJECT_NOT_ACCESSIBLE")
        clients["oracle-reader"].read(*contract)
        checks.append({"name":"other_object_unaffected","status":"CONFIRMED"})
    finally:operator.request("POST","/v1/control",{**grant,"enabled":True})
    if clients["oracle-reader"].read(*code)["content_sha256"]!=first["content_sha256"]:
        raise SourceClientError("QA_CONTENT_CHANGED")
    result={"schema_version":1,"instance_id":directory.name,"status":"PHASE_QA_SOURCE_BOUNDARIES_CONFIRMED", "checks":checks,
        "authority":"SIMULATED_QA_NOT_COMPANY_APPROVAL"}
    save(directory/"phase-boundary-verification.json",result)
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("action",choices=("up","publish","verify","verify-phase"));parser.add_argument("--instance")
    parser.add_argument("--registration",type=Path,default=REGISTRATION)
    for name in ("source","rules","acceptance"):
        parser.add_argument("--"+name+"-repo",type=Path);parser.add_argument("--"+name+"-commit")
    args=parser.parse_args()
    if args.action=="up":result=up(args.source_repo,args.source_commit,args.registration)
    elif args.action=="publish":result=publish(registered(args.instance),{
        "EvidEnterpriseSources":(args.source_repo,args.source_commit),"EvidBusinessRules":(args.rules_repo,args.rules_commit),"EvidAcceptanceAssets":(args.acceptance_repo,args.acceptance_commit)})
    elif args.action=="verify-phase":result=verify_phase(registered(args.instance))
    else:result=verify(registered(args.instance))
    print(json.dumps(result,ensure_ascii=False))


if __name__=="__main__":
    try:main()
    except Exception as error:
        print(json.dumps({"status":"INCOMPLETE","error_code":getattr(error,"code","SOURCE_HOST_FAILED")}));raise SystemExit(2)
