#!/usr/bin/env python3
"""EGAP 操作员入口：核对独立平台 → 原生对象回读 → 授权范围登记 → 三角色验收。

这里只建立实验基础设施/业务对象的身份，不给 Agent 原生管理员凭据，不签发业务
PASS。原生 seed 仍用已有先落账后请求实现；平台维护者决定后端对象访问范围。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[2]


class BoundaryError(ValueError):pass


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        os.chmod(stream.fileno(),0o600);json.dump(value,stream,ensure_ascii=False,indent=2);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def digest(raw):return hashlib.sha256(raw).hexdigest()


def private(path):
    """用同一已打开文件描述符核对权限再读取，拒绝软链接和超过上限的配置。"""
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(descriptor,"rb") as stream:
        mode=os.fstat(stream.fileno())
        if not stat.S_ISREG(mode.st_mode) or mode.st_mode & 0o077:raise BoundaryError("PRIVATE_CONFIG_REQUIRED")
        raw=stream.read(4*1024*1024+1)
        if len(raw)>4*1024*1024:raise BoundaryError("PRIVATE_CONFIG_TOO_LARGE")
        return json.loads(raw)


def command(args):
    result=subprocess.run(args,capture_output=True,text=True,timeout=15)
    if result.returncode:raise BoundaryError("PLATFORM_IDENTITY_LOOKUP_FAILED")
    return result.stdout.strip()


def binding(repo,project):
    """端点由原生 Docker 发布记录证明；传入文件中的地址不能自行成为网络权限。"""
    registration=json.loads((ROOT/"config/external-platforms.v1.json").read_text())
    if not re.fullmatch(r"evid-business-[a-f0-9]{12}",project) \
            or command(["git","-C",str(repo),"config","--get","remote.origin.url"])!=registration["repository_url"] \
            or Path(command(["git","-C",str(repo),"rev-parse","--show-toplevel"])).resolve()!=repo.resolve():
        raise BoundaryError("REGISTERED_PLATFORM_REQUIRED")
    directory=repo/".local"/project;receipt=private(directory/"deployment-receipt.json");manifest=private(directory/"manifest.json")
    commit=manifest["platform_commit"]
    if command(["git","-C",str(repo),"rev-parse",commit+"^{commit}"])!=commit:raise BoundaryError("PLATFORM_COMMIT_UNAVAILABLE")
    live=json.loads(command(["docker","inspect",*[r["container_id"] for r in receipt["containers"]]]))
    for container in live:
        role=container["Config"]["Labels"].get("com.docker.compose.service")
        expected=next((r for r in receipt["containers"] if r["role"]==role),None)
        labels=container["Config"]["Labels"]
        state=container["State"]
        if not expected or container["Image"]!=expected["image_id"] or labels.get("evid.platform_instance")!=project \
                or labels.get("com.docker.compose.project")!=project \
                or (state["Status"]!="running" and not (role.endswith("-migrate") and state["Status"]=="exited" and state["ExitCode"]==0)):
            raise BoundaryError("PLATFORM_DEPLOYMENT_DRIFT")
        if role in receipt["endpoints"]:
            ports=[p for value in container["NetworkSettings"]["Ports"].values() for p in (value or [])]
            if len(ports)!=1 or ports[0]["HostIp"]!="127.0.0.1" or receipt["endpoints"][role]!="http://127.0.0.1:"+ports[0]["HostPort"]:
                raise BoundaryError("PLATFORM_ENDPOINT_DRIFT")
    return directory,{"platform_id":"evid-business","project":project,"repository_url":registration["repository_url"],
        "platform_commit":commit,"deployment":receipt,"manifest_sha256":digest((directory/"manifest.json").read_bytes()),
        "egap_commit":command(["git","-C",str(ROOT),"rev-parse","HEAD"]),"binding_authority":"LOCAL_DOCKER_OPERATOR"}


def access_binding(repo,platform,scope_id):
    """独立实验入口的固定来源核验；产品/Agent 不能用 URL 或自报租户替代该绑定。

    原生平台身份继续独立保存。这个新入口只增加自己的权限空间，不修改父入口的
    scope 或凭据；镜像、配置、容器、网络与宿主端口分别从 Docker/文件核对。
    """
    if not re.fullmatch(r"evid-access-[a-f0-9]{12}",scope_id or ""):
        raise BoundaryError("REGISTERED_ACCESS_SCOPE_REQUIRED")
    directory=repo/".local"/scope_id
    if any(p.is_symlink() for p in (directory,*directory.parents)):raise BoundaryError("ACCESS_SCOPE_SYMLINK")
    manifest=private(directory/"manifest.json");receipt=private(directory/"deployment-receipt.json")
    if manifest["project"]!=scope_id or manifest["native_project"]!=platform["project"] \
            or manifest["native_manifest_sha256"]!=platform["manifest_sha256"] \
            or any(receipt.get(k)!=v for k,v in manifest.items()) or receipt["manifest_sha256"]!=digest((directory/"manifest.json").read_bytes()) \
            or manifest["compose_sha256"]!=digest((directory/"compose.private.json").read_bytes()):
        raise BoundaryError("ACCESS_SCOPE_PROVENANCE_MISMATCH")
    config=directory/"access.private.json"
    if config.is_symlink() or config.stat().st_size>262144 or digest(config.read_bytes())!=manifest["access_config_sha256"]:
        raise BoundaryError("ACCESS_SCOPE_CONFIG_CHANGED")
    validate_access(receipt)
    return directory,receipt


def host_mount_file(source):
    """Docker Desktop 的 /host_mnt 是虚拟机映射；只在 macOS 还原该固定前缀。

    不搜索同名文件、不在读取失败时换路径。后续仍核对完整配置摘要与软链接，
    因而路径兼容不会把另一个配置或一个旧副本当成容器当前挂载。
    """
    if sys.platform=="darwin" and source.startswith("/host_mnt/"):
        source=source[len("/host_mnt"):]
    path=Path(source)
    if not path.is_absolute() or any(p.is_symlink() for p in (path,*path.parents)):
        raise BoundaryError("ACCESS_SCOPE_MOUNT_PATH_INVALID")
    return path


def validate_access(identity):
    """每次派发前/产品恢复前检查原入口；新建同名网络或修改对象入口必须新身份。"""
    if datetime.now(timezone.utc)>=datetime.fromisoformat(identity["expires_at"]):
        raise BoundaryError("ACCESS_SCOPE_EXPIRED")
    row=json.loads(command(["docker","inspect",identity["container_id"]]))[0]
    labels=row["Config"]["Labels"]
    if row["Id"]!=identity["container_id"] or row["Image"]!=identity["image_id"] or row["State"]["Status"]!="running" \
            or labels.get("com.docker.compose.project")!=identity["project"] or labels.get("evid.access_instance")!=identity["project"] \
            or labels.get("evid.native_project")!=identity["native_project"] or row["Config"]["User"]!="10001:10001" \
            or not row["HostConfig"]["ReadonlyRootfs"]:
        raise BoundaryError("ACCESS_SCOPE_DEPLOYMENT_CHANGED")
    mounts=[m for m in row["Mounts"] if m["Destination"]=="/run/secrets/access.json"]
    if len(mounts)!=1 or mounts[0]["RW"] or Path(mounts[0]["Source"]).name!="access.private.json":
        raise BoundaryError("ACCESS_SCOPE_MOUNT_CHANGED")
    mounted=host_mount_file(mounts[0]["Source"])
    if mounted.is_symlink() or digest(mounted.read_bytes())!=identity["access_config_sha256"]:
        raise BoundaryError("ACCESS_SCOPE_CONFIG_CHANGED")
    ports=row["NetworkSettings"]["Ports"].get("8080/tcp") or []
    if len(ports)!=1 or ports[0]["HostIp"]!="127.0.0.1" or identity["access_endpoint"]!="http://127.0.0.1:"+ports[0]["HostPort"]:
        raise BoundaryError("ACCESS_SCOPE_ENDPOINT_CHANGED")
    network=json.loads(command(["docker","network","inspect",identity["product_network"]]))[0]
    if network["Id"]!=identity["product_network_id"] or network["Internal"] is not True \
            or network["Labels"].get("com.docker.compose.project")!=identity["project"]:
        raise BoundaryError("ACCESS_SCOPE_NETWORK_CHANGED")
    for reference in identity["native_networks"].values():
        if row["NetworkSettings"]["Networks"].get(reference["name"],{}).get("NetworkID")!=reference["id"]:
            raise BoundaryError("ACCESS_NATIVE_NETWORK_CHANGED")
    expected={r["name"] for r in identity["native_networks"].values()}|{identity["product_network"]}
    if set(row["NetworkSettings"]["Networks"])!=expected \
            or row["NetworkSettings"]["Networks"][identity["product_network"]]["NetworkID"]!=identity["product_network_id"]:
        raise BoundaryError("ACCESS_NETWORK_ATTACHMENTS_CHANGED")


def request(base,path,*,token=None,chatwoot=False,method="GET",body=None,form=None):
    endpoint=urlsplit(base)
    if endpoint.scheme!="http" or endpoint.hostname!="127.0.0.1" or not endpoint.port or endpoint.path not in {"","/"} \
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:raise BoundaryError("LOCAL_PLATFORM_ENDPOINT_REQUIRED")
    headers={"Accept":"application/json"}
    if token:headers["api_access_token" if chatwoot else "Authorization"]=token if chatwoot else "Bearer "+token
    data=None
    if body is not None:data=json.dumps(body).encode();headers["Content-Type"]="application/json"
    if form is not None:data=urlencode(form).encode();headers["Content-Type"]="application/x-www-form-urlencoded"
    conn=http.client.HTTPConnection("127.0.0.1",endpoint.port,timeout=10)
    try:
        conn.request(method,path,data,headers);response=conn.getresponse();raw=response.read(2097153)
        if len(raw)>2097152:raise BoundaryError("PLATFORM_RESPONSE_TOO_LARGE")
        return response.status,json.loads(raw),{"response_sha256":digest(raw),"operation_id":response.getheader("X-Evid-Operation")}
    finally:conn.close()


def require_success(result):
    if result[0] not in (200,201):raise BoundaryError("NATIVE_OPERATION_UNCONFIRMED")
    return result[1]


def member_ids(response):
    """Chatwoot 的 inbox_members 返回 payload 包装；拒绝不明结构，避免把字典键当成员。"""
    rows=response.get("payload") if isinstance(response,dict) else None
    if not isinstance(rows,list) or any(not isinstance(row,dict) or type(row.get("id")) is not int for row in rows):
        raise BoundaryError("NATIVE_MEMBERSHIP_RESPONSE_INVALID")
    return {row["id"] for row in rows}


def enroll(repo,project,seed_path,output,access_id=None):
    """原生 GET 再核对所有目标归属，随后才登记不可变范围。输出路径只能新建或恢复同一输入。"""
    directory,identity=binding(repo,project);seed=private(seed_path);preparer=private(directory/"preparer.private.json")
    access_dir,scoped=(access_binding(repo,identity,access_id) if access_id else (directory,None))
    access=private(access_dir/"operator-access.private.json")
    endpoint=scoped["access_endpoint"] if scoped else identity["deployment"]["endpoints"]["access"]
    if preparer["project"]!=project or any(preparer[k]["base_url"]!=identity["deployment"]["endpoints"][k] for k in ("chatwoot","lago")) \
            or access["base_url"]!=endpoint or access["instance_id"]!=(access_id or project):
        raise BoundaryError("PREPARER_SCOPE_MISMATCH")
    basis={"project":project,"seed_sha256":digest(seed_path.read_bytes()),"seed_run_id":seed["run_id"]}
    if scoped:
        if scoped["seed_sha256"]!=basis["seed_sha256"] or scoped["experiment_id"]!=seed["run_id"]:
            raise BoundaryError("ACCESS_SCOPE_SEED_MISMATCH")
        basis["access_scope_id"]=access_id
    journal=private(output) if output.exists() else {"basis":basis,"state":"PREPARING","grants":[],"native_reads":[]}
    if journal["basis"]!=basis:raise BoundaryError("ENROLLMENT_IDENTITY_CONFLICT")
    save(output,journal)
    cw=preparer["chatwoot"];lago=preparer["lago"];prefix=f"/api/v1/accounts/{cw['account_id']}"
    def native(service,path):
        c=cw if service=="chatwoot" else lago
        result=request(c["base_url"],path,token=c["api_token" if service=="chatwoot" else "api_key"],chatwoot=service=="chatwoot")
        journal["native_reads"].append({"service":service,"path":path,"http_status":result[0],**result[2]});save(output,journal)
        return require_success(result)
    scopes=[]
    for customer in seed["bindings"]["customers"]:
        cases=[c for c in seed["bindings"]["cases"] if c["tenant_id"]==customer["tenant_id"]]
        for case in cases:
            conversation=native("chatwoot",prefix+f"/conversations/{case['conversation_id']}")
            invoice=native("lago","/api/v1/invoices/"+case["invoice_id"])["invoice"]
            if conversation.get("meta",{}).get("sender",{}).get("id")!=customer["chatwoot_contact_id"] \
                    or invoice.get("customer",{}).get("external_id")!=customer["lago_customer_id"] \
                    or not any(s.get("external_id")==case["subscription_id"] for s in invoice.get("subscriptions",[])):
                raise BoundaryError("NATIVE_SEED_OBJECT_MISMATCH")
        scopes.append({"tenant_id":customer["tenant_id"],"account_id":cw["account_id"],"contact_id":customer["chatwoot_contact_id"],
            "customer_id":customer["lago_customer_id"],"conversation_ids":sorted({c["conversation_id"] for c in cases}),
            "invoice_ids":sorted({c["invoice_id"] for c in cases}),"subscription_ids":sorted({c["subscription_id"] for c in cases})})
    agents=native("chatwoot",prefix+"/agents")
    ids=[r["id"] for r in agents if r.get("email") in {"product@example.invalid","observer@example.invalid"}]
    if len(ids)!=2:raise BoundaryError("NATIVE_AGENT_IDENTITIES_MISSING")
    inbox=seed["chatwoot_inbox_id"];membership_path=prefix+"/inbox_members/"+str(inbox)
    members=native("chatwoot",membership_path)
    # 原生 GET 是成员变更的对账入口。已经添加不重复 POST；未知写后下次先查询。
    if not set(ids)<=member_ids(members):
        journal["membership"]={"state":"DISPATCHING","inbox_id":inbox,"user_ids":ids};save(output,journal)
        try:
            require_success(request(cw["base_url"],prefix+"/inbox_members",token=cw["api_token"],chatwoot=True,method="POST",body={"inbox_id":inbox,"user_ids":ids}))
        except Exception:
            journal["membership"]["state"]="UNKNOWN";save(output,journal);raise
        members=native("chatwoot",membership_path)
        if not set(ids)<=member_ids(members):raise BoundaryError("NATIVE_MEMBERSHIP_NOT_OBSERVED")
    journal["membership"]={"state":"OBSERVED","inbox_id":inbox,"user_ids":ids};save(output,journal)
    for scope in scopes:
        # 来源平台 grant API 明确是同 digest 的幂等登记，冲突会 409；不复用原生 POST 的重试语义。
        journal["pending_grant"]=scope;save(output,journal)
        result=require_success(request(access["base_url"],"/control/grants",token=access["token"],method="POST",body=scope))
        if not any(g["scope"]["tenant_id"]==scope["tenant_id"] for g in journal["grants"]):journal["grants"].append({"scope":scope,"receipt":result})
        journal.pop("pending_grant",None);save(output,journal)
    journal.update(state="ENROLLED",platform_binding=identity)
    if scoped:journal["experiment_access"]=scoped
    save(output,journal)
    return {"status":"NATIVE_SCOPES_ENROLLED","project":project,"tenants":len(scopes),"cases":sum(len(s["conversation_ids"]) for s in scopes)}


def verify(repo,project,enrollment,output):
    """三角色真实 HTTP 验收；写入限一个已登记合成会话的私有备注，先存意图再派发。"""
    directory,identity=binding(repo,project);journal=private(enrollment)
    if journal["state"]!="ENROLLED" or journal["basis"]["project"]!=project:raise BoundaryError("ENROLLED_PLATFORM_REQUIRED")
    if output.exists():raise BoundaryError("NEW_VERIFICATION_OUTPUT_REQUIRED")
    scoped=journal.get("experiment_access")
    access_dir,current=access_binding(repo,identity,scoped["project"]) if scoped else (directory,None)
    if scoped!=current:raise BoundaryError("ACCESS_SCOPE_IDENTITY_CHANGED")
    readers={role:private(access_dir/(role+"-access.private.json")) for role in ("product","observer")}
    checks=[];report={"state":"STARTED","platform_binding":identity,"enrollment_sha256":digest(enrollment.read_bytes()),"checks":checks}
    save(output,report)
    scope=journal["grants"][0]["scope"];conversation=scope["conversation_ids"][0]
    path=f"/api/v1/accounts/{scope['account_id']}/conversations/{conversation}"
    def invoke(role,name,target,expected,*,method="GET",body=None):
        c=readers[role]
        result=request(c["base_url"],target,token=c["token"],method=method,body=body)
        checks.append({"name":name,"role":role,"path":target,"http_status":result[0],**result[2]});save(output,report)
        if result[0]!=expected:raise BoundaryError("ROLE_BOUNDARY_CHECK_FAILED_"+name.upper())
        return result[1]
    for role in readers:
        invoke(role,role+"_conversation",path,200)
        invoke(role,role+"_invoice","/api/v1/invoices/"+scope["invoice_ids"][0],200)
        invoke(role,role+"_unregistered_customer","/api/v1/customers/not-registered",404)
    if scoped:
        # 验证两条边：旧 token 不能穿过新入口；旧入口的旧对象范围没有被扩张。
        for role in readers:
            old=private(directory/(role+"-access.private.json"))
            for name,base,token,expected in ((role+"_old_token_new_entry",readers[role]["base_url"],old["token"],401),
                    (role+"_old_scope_new_object",old["base_url"],old["token"],404),
                    (role+"_new_token_old_entry",old["base_url"],readers[role]["token"],401)):
                result=request(base,path,token=token)
                checks.append({"name":name,"http_status":result[0],**result[2]});save(output,report)
                if result[0]!=expected:raise BoundaryError("EXPERIMENT_SCOPE_ISOLATION_FAILED")
    note={"content":"Evid 独立平台边界验收，仅内部实验。 "+uuid4().hex,"private":True,"message_type":"outgoing","content_type":"text"}
    invoke("observer","observer_write_denied",path+"/messages",403,method="POST",body=note)
    invoke("product","public_message_denied",path+"/messages",403,method="POST",body={**note,"private":False})
    invoke("product","billing_write_denied","/api/v1/events",403,method="POST",body={})
    report["private_note_operation"]={"state":"DISPATCHING","conversation_id":conversation,"content_sha256":digest(note["content"].encode())};save(output,report)
    result=invoke("product","private_note_allowed",path+"/messages",200,method="POST",body=note)
    report["private_note_operation"].update(state="RECEIVED",message_id=result["id"]);save(output,report)
    observed=invoke("observer","private_note_readback",path+"/messages",200)
    matching=[m for m in observed["payload"] if m["id"]==result["id"]]
    if len(matching)!=1 or matching[0].get("private") is not True or matching[0].get("content")!=note["content"]:
        raise BoundaryError("PRIVATE_NOTE_NOT_OBSERVED")
    # OIDC 令牌的 iss/aud/sub 从实际 introspection 核验，不能靠本地 JWT 解码来授权。
    prep=private(directory/"preparer.private.json");kc=prep["keycloak"]
    for user in kc["operators"]:
        result=require_success(request(kc["base_url"],f"/realms/{kc['realm']}/protocol/openid-connect/token",method="POST",form={"grant_type":"password","client_id":kc["client_id"],"client_secret":kc["client_secret"],"username":user["username"],"password":user["password"]}))
        claims=require_success(request(kc["base_url"],f"/realms/{kc['realm']}/protocol/openid-connect/token/introspect",method="POST",form={"client_id":kc["client_id"],"client_secret":kc["client_secret"],"token":result["access_token"]}))
        audience=claims.get("aud",[]);audience=[audience] if isinstance(audience,str) else audience
        if claims.get("active") is not True or claims.get("sub")!=user["subject"] or claims.get("iss")!=kc["issuer"] or kc["audience"] not in audience:
            raise BoundaryError("NATIVE_IDENTITY_NOT_CONFIRMED")
        checks.append({"name":"keycloak_identity","tenant_id":user["tenant_id"],"subject":claims["sub"],"active":True})
    report.update(state="ROLE_BOUNDARIES_CONFIRMED",business_final_qualification=False);save(output,report)
    return {"status":report["state"],"checks":len(checks),"project":project,"business_final_qualification":False}


def commercial_scopes(repo,project,enrollment,seed_path,output):
    """来源服务部署前的授权目录投影；只携带已回读原生对象，不带实验答案或凭据。"""
    _,identity=binding(repo,project);journal=private(enrollment);seed=private(seed_path)
    expected_basis={"project":project,"seed_sha256":digest(seed_path.read_bytes()),"seed_run_id":seed["run_id"]}
    scoped=journal.get("experiment_access")
    if scoped:
        _,current=access_binding(repo,identity,scoped["project"])
        if current!=scoped:raise BoundaryError("ACCESS_SCOPE_IDENTITY_CHANGED")
        expected_basis["access_scope_id"]=scoped["project"]
    if journal.get("state")!="ENROLLED" or journal["basis"]!=expected_basis \
            or journal["platform_binding"]["manifest_sha256"]!=identity["manifest_sha256"]:
        raise BoundaryError("EXACT_NATIVE_ENROLLMENT_REQUIRED")
    scopes=[]
    for case in seed["bindings"]["cases"]:
        grant=next(g["scope"] for g in journal["grants"] if g["scope"]["tenant_id"]==case["tenant_id"])
        if case["conversation_id"] not in grant["conversation_ids"] or case["invoice_id"] not in grant["invoice_ids"] or case["subscription_id"] not in grant["subscription_ids"]:
            raise BoundaryError("COMMERCIAL_NATIVE_SCOPE_MISMATCH")
        scopes.append({"tenant_id":case["tenant_id"],"customer_id":grant["customer_id"],"subscription_id":case["subscription_id"],
            "invoice_id":case["invoice_id"],"account_id":grant["account_id"],"conversation_id":case["conversation_id"]})
    value={"native_platform_project":project,"native_platform_manifest_sha256":identity["manifest_sha256"],
        "native_enrollment_sha256":digest(enrollment.read_bytes()),"native_seed_sha256":digest(seed_path.read_bytes()),"object_scopes":scopes}
    if scoped:value["experiment_access"]={"project":scoped["project"],"manifest_sha256":scoped["manifest_sha256"]}
    if output.exists() and private(output)!=value:raise BoundaryError("SCOPE_EXPORT_IDENTITY_CONFLICT")
    save(output,value)
    return {"status":"COMMERCIAL_OBJECT_SCOPES_EXPORTED","project":project,"object_scopes":len(scopes)}


def main():
    parser=argparse.ArgumentParser();parser.add_argument("action",choices=("enroll","verify","commercial-scopes"));parser.add_argument("--platform-repo",type=Path,required=True)
    parser.add_argument("--project",required=True);parser.add_argument("--seed",type=Path);parser.add_argument("--enrollment",type=Path);parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--access-id")
    args=parser.parse_args()
    if "docs" in args.output.resolve().parts or args.output.name.lower()=="readme.md":raise BoundaryError("OUTPUT_FORBIDDEN")
    if args.action=="commercial-scopes":result=commercial_scopes(args.platform_repo,args.project,args.enrollment,args.seed,args.output)
    else:result=enroll(args.platform_repo,args.project,args.seed,args.output,args.access_id) if args.action=="enroll" else verify(args.platform_repo,args.project,args.enrollment,args.output)
    print(json.dumps(result))


if __name__=="__main__":
    try:main()
    except Exception as error:print(json.dumps({"status":"INCOMPLETE","error_code":str(error) if isinstance(error,BoundaryError) else "PLATFORM_BOUNDARY_FAILED"}));raise SystemExit(2)
