#!/usr/bin/env python3
"""从独立 QA 发布源读取固定检查器，对已归档的阶段原件作可复核重审。

CLI → load_archive（Git 锚定索引/原件/账本）→ fetch_bundle（实时对象 ACL）
→ build_checker → run_checker（无网络、无挂载容器）。不重跑产品或调用模型，
也不把旧候选证据升级成当前候选覆盖；输出与原执行报告分别保留。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"tools/source-platform"))
from client import SourceClient,SourceClientError
from host import git_reader,save,sha,command

CONTRACT="qa.commercial-phase-review.v1"
REGISTRATION=ROOT/"config/phase-acceptance-publications.v1.json"
INDEX="reports/platform-boundary/20261126-independent-phase-execution/evidence-index.json"
PREPARATION_INDEX="reports/platform-boundary/20261126-fresh-phase-preparation/evidence-index.json"
FILES={"release_oracle.py","phase_oracle.py","phase_entry.py","phase-contract.v1.json","Dockerfile.phase"}


def require(condition,code):
    if not condition:raise SourceClientError(code)


def private_bytes(path,expected_sha256,expected_bytes=None):
    """只读取实验私有目录的普通文件；索引中路径不能越界或经软链接逃逸。"""
    path=Path(path)
    if not path.is_absolute():path=ROOT/path
    require(path.is_file() and path.resolve()==path.absolute()
        and path.is_relative_to(ROOT/"artifacts/phase-experiments"),"QA_EVIDENCE_PATH_INVALID")
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,"rb") as stream:
        info=os.fstat(stream.fileno())
        require(not info.st_mode&0o077 and info.st_size<=16*1024*1024,"QA_PRIVATE_EVIDENCE_REQUIRED")
        raw=stream.read(16*1024*1024+1)
    require(hashlib.sha256(raw).hexdigest()==expected_sha256 and
        (expected_bytes is None or len(raw)==expected_bytes),"QA_EVIDENCE_DIGEST_MISMATCH")
    return raw


def load_archive(commit):
    """从已提交的索引读取旧实验，不接受自由指定观察包或调用方给出的 PASS。

112 份账本证据重新验摘要，并逐项比较用于重审的观察与 COMPLETED 操作原结果。
这证明本机归档与已提交指纹一致，不声称 Git 索引具有外部公证或生产签名。
"""
    git=git_reader(ROOT,"EvidCodingAgent")
    indexes=[json.loads(git.read(commit,path)) for path in (INDEX,PREPARATION_INDEX)]
    def archived(index,suffix,collection="private_snapshots"):
        rows=[r for r in index[collection] if r["source_path"].endswith(suffix)]
        require(len(rows)==1,"QA_ARCHIVE_ENTRY_AMBIGUOUS")
        row=rows[0]
        return json.loads(private_bytes(row["path"],row["sha256"],row["bytes"])),row
    execution,execution_ref=archived(indexes[0],"/execution-result.private.json")
    binding,_=archived(indexes[0],"/candidate-binding.json")
    ledger,_=archived(indexes[0],"/execution-ledger-snapshot.private.json")
    seed,seed_ref=archived(indexes[1],"/native/seed.json","evidence")
    basis=execution["basis"]
    require(basis==binding["basis"]==ledger["experiment"]["configuration"]
        and basis["candidate"]==ledger["experiment"]["candidate"]
        and basis["native_seed_sha256"]==seed_ref["sha256"]
        and basis["experiment_id"]==execution["experiment_id"]==ledger["experiment"]["experiment_id"],"QA_ARCHIVED_BINDING_MISMATCH")
    references=0
    for op in ledger["operations"]:
        for ref in op["evidence_refs"]:
            private_bytes(ref["path"],ref["sha256"],ref["bytes"]);references+=1
    def operation(prefix,value):
        rows=[r for r in ledger["operations"] if r["operation_key"].startswith(prefix)
              and r["status"]=="COMPLETED" and r["result"]==value]
        require(len(rows)==1,"QA_OBSERVATION_NOT_LINKED_TO_OPERATION")
    cases=[]
    for row in execution["cases"]:
        matches=[c for c in seed["bindings"]["cases"] if c["label"]==row["label"]]
        require(len(matches)==1,"QA_CASE_BINDING_AMBIGUOUS")
        case={**matches[0],"experiment_id":execution["experiment_id"],"candidate_sha":basis["candidate"]["sut_commit"]}
        for field,suffix in (("before",".observe.j1."),("after",".observe.j2."),("old_after",".observe.old"),
                             ("retry",".retry"),("reevaluation",".reevaluate")):
            operation(row["label"]+suffix,row[field])
        cases.append({"case":case,**{k:row[k] for k in ("before","after","old_after","retry","reevaluation")}})
    return {"schema_version":1,"contract_id":CONTRACT,"basis":basis,"cases":cases},execution,{
        "execution_index_commit":commit,"execution_ref":execution_ref,"seed_ref":seed_ref,
        "verified_ledger_references":references,"experiment_id":execution["experiment_id"]}


def verify_publication(receipt,obj,source):
    """发布者、语义 profile、源 Git 对象与草稿权限都必须匹配，不能只验正文 hash。"""
    metadata=receipt.get("metadata",{})
    require(receipt.get("publisher_id")=="lab:qa-publisher" and receipt.get("publication_id")
        and metadata.get("source")==source and metadata.get("profile")==obj["profile"]
        and metadata.get("content_type")==obj["content_type"]
        and metadata.get("approval_state")=="DRAFT"
        and metadata.get("authority_scope")=="SYNTHETIC_LOCAL_CALIBRATION","QA_PUBLICATION_PROVENANCE_MISMATCH")


def fetch_bundle(repo,commit,reader_config,directory):
    """每次重审重新读取五个准确版本；ACL 故障或撤权时不能用磁盘代码继续。"""
    git=git_reader(repo,"EvidAcceptanceAssets")
    manifest=json.loads(git.read(commit,"phase-publication.v1.json"))
    require(manifest["contract_id"]==CONTRACT and manifest["version"]=="phase-v1"
        and manifest["publisher_principal"]=="lab:qa-publisher"
        and manifest["authority_scope"]=="SYNTHETIC_LOCAL_CALIBRATION" and manifest["approval_state"]=="DRAFT",
        "QA_MANIFEST_MISMATCH")
    client=SourceClient.from_file(reader_config)
    require(client.config["principal_id"]=="lab:oracle-reader","QA_ORACLE_READER_REQUIRED")
    objects=json.loads(REGISTRATION.read_text())["objects"]
    require({Path(o["path"]).name for o in objects}==FILES and len(objects)==len(FILES),"QA_BUNDLE_FILE_SET_INVALID")
    context=directory/"context";context.mkdir(mode=0o700)
    publications=[]
    for obj in objects:
        raw=git.read(commit,obj["path"]);digest=hashlib.sha256(raw).hexdigest()
        rows=[r for r in manifest["files"] if r["path"]==obj["path"]]
        require(len(rows)==1 and rows[0]["sha256"]==digest,"QA_FILE_NOT_IN_MANIFEST")
        receipt=client.read(obj["namespace"],obj["object_id"],obj["version"],expected_sha256=digest)
        source={"repository_url":"https://github.com/guangxinsuuu/EvidAcceptanceAssets.git","commit":commit,
            "path":obj["path"],"blob":git._git("rev-parse",commit+":"+obj["path"]).decode().strip()}
        verify_publication(receipt,obj,source)
        require(receipt["content"].encode()==raw,"QA_GIT_PUBLICATION_MISMATCH")
        # 这里只放 QA 公开代码，父目录仍为 0700。COPY 保留模式，必须让容器
        # 的非 root 用户可读；观察原件与 reader 配置不进入构建上下文。
        path=context/Path(obj["path"]).name;path.write_bytes(raw);path.chmod(0o644)
        publications.append(receipt)
    save(directory/"publications.private.json",publications)
    return {"qa_commit":commit,"manifest_sha256":hashlib.sha256(git.read(commit,"phase-publication.v1.json")).hexdigest(),
        "source_instance":client.config["instance_id"],"registration_sha256":sha(REGISTRATION),
        "publications":[{k:r[k] for k in ("object_id","version","content_sha256","publication_id","publisher_id")} for r in publications]}


def build_checker(directory,commit):
    """仅以已核验 QA 原件构建镜像；不挂载候选仓库，不发送宿主机构建凭据。"""
    tag="evid-phase-qa:"+directory.name
    save(directory/"build-state.json",{"status":"BUILDING","tag":tag,"qa_commit":commit})
    try:
        result=subprocess.run(["docker","build","--network=none","--pull=false","--build-arg","QA_REVISION="+commit,
            "-f",str(directory/"context/Dockerfile.phase"),"-t",tag,str(directory/"context")],capture_output=True,timeout=120)
    except subprocess.TimeoutExpired:
        save(directory/"build-state.json",{"status":"UNKNOWN","tag":tag,"qa_commit":commit})
        raise SourceClientError("QA_BUILD_UNKNOWN") from None
    log=directory/"build.private.log";log.write_bytes(result.stdout+result.stderr);log.chmod(0o600)
    require(result.returncode==0,"QA_BUILD_FAILED")
    image=json.loads(command(["docker","image","inspect",tag]))[0]
    require(image["Config"]["Labels"].get("org.opencontainers.image.revision")==commit
        and image["Config"]["User"]=="10001:10001","QA_IMAGE_IDENTITY_MISMATCH")
    receipt={"status":"CONFIRMED","image_id":image["Id"],"tag":tag,"qa_commit":commit,
        "inputs":{p.name:{"sha256":sha(p),"mode":oct(p.stat().st_mode&0o777)} for p in (directory/"context").iterdir()}}
    save(directory/"build-state.json",receipt)
    return receipt


def run_checker(directory,image_id,request):
    """固定容器只接收 stdin。无网络、宿主机挂载、发布凭据和 Docker socket。

Docker 仍由本机受信操作员管理；该隔离不是独立企业 QA 管理员或远端证明。
超时停止准确容器，保留 UNKNOWN 与原件，不自动重试或伪造完成结果。
"""
    raw=json.dumps(request,ensure_ascii=False,allow_nan=False).encode()
    require(len(raw)<=16*1024*1024,"QA_INPUT_TOO_LARGE")
    save(directory/"input.private.json",request)
    name="evid-phase-qa-"+directory.name
    cid=command(["docker","create","-i","--name",name,"--network","none","--read-only","--user","10001:10001",
        "--cap-drop","ALL","--security-opt","no-new-privileges:true","--pids-limit","32","--memory","256m","--cpus","1",
        "--tmpfs","/tmp:rw,noexec,nosuid,size=16m","--log-driver","none","--label","evid.qa_run="+directory.name,image_id])
    inspected=json.loads(command(["docker","inspect",cid]))[0]
    host=inspected["HostConfig"]
    require(inspected["Image"]==image_id and host["NetworkMode"]=="none" and host["ReadonlyRootfs"]
        and not inspected["Mounts"] and not host.get("Binds") and inspected["Config"]["User"]=="10001:10001",
        "QA_EXECUTION_BOUNDARY_INVALID")
    policy={"container_id":cid,"image_id":image_id,"network":host["NetworkMode"],"read_only":host["ReadonlyRootfs"],
        "mounts":inspected["Mounts"],"user":inspected["Config"]["User"],"cap_drop":host["CapDrop"],
        "security_options":host["SecurityOpt"],"memory":host["Memory"],"pids_limit":host["PidsLimit"],
        "cpu_nanos":host["NanoCpus"],"input_sha256":sha(directory/"input.private.json"),
        "host_source_sha256":sha(Path(__file__)),"status":"DISPATCHING"}
    save(directory/"execution.json",policy)
    out=directory/"stdout.private.json";err=directory/"stderr.private.log"
    with out.open("wb") as stdout,err.open("wb") as stderr:
        os.chmod(out,0o600);os.chmod(err,0o600)
        try:
            result=subprocess.run(["docker","start","-ai",cid],input=raw,stdout=stdout,stderr=stderr,timeout=30)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker","kill",cid],capture_output=True,timeout=10)
            save(directory/"execution.json",{**policy,"status":"UNKNOWN","error_code":"QA_EXECUTION_TIMEOUT"})
            raise SourceClientError("QA_EXECUTION_TIMEOUT") from None
    state=json.loads(command(["docker","inspect",cid]))[0]["State"]
    if result.returncode!=0 or state["ExitCode"]!=0 or state["OOMKilled"] or out.stat().st_size>=2*1024*1024:
        save(directory/"execution.json",{**policy,"status":"FAILED","exit_code":state["ExitCode"],
            "oom_killed":state["OOMKilled"],"output_sha256":sha(out),"stderr_sha256":sha(err)})
        command(["docker","rm",cid])
        raise SourceClientError("QA_EXECUTION_FAILED")
    try:
        verdict=json.loads(out.read_bytes())
        require(isinstance(verdict,dict) and verdict.get("contract_id")==CONTRACT and verdict.get("release_authorized") is False
            and verdict.get("protected_final") is False and verdict.get("semantic_quality_status")=="NOT_FULLY_EVALUATED"
            and verdict.get("scoped_result") in {"PASS_SCOPED_DETERMINISTIC_CHECKS","FAIL","INCOMPLETE"},"QA_RESULT_CONTRACT_INVALID")
    except (ValueError,UnicodeError):
        # 进程 exit=0 只表示结束，不能将错误输出协议当检查成功，也不能遗留派发中状态。
        save(directory/"execution.json",{**policy,"status":"FAILED","exit_code":state["ExitCode"],
            "error_code":"QA_RESULT_CONTRACT_INVALID","output_sha256":sha(out)})
        command(["docker","rm",cid])
        raise SourceClientError("QA_RESULT_CONTRACT_INVALID") from None
    save(directory/"execution.json",{**policy,"status":"COMPLETED","exit_code":state["ExitCode"],"output_sha256":sha(out)})
    command(["docker","rm",cid])
    return verdict


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--qa-repo",type=Path,required=True);parser.add_argument("--qa-commit",required=True)
    parser.add_argument("--reader-config",type=Path,required=True);parser.add_argument("--execution-commit",required=True)
    args=parser.parse_args()
    require(re.fullmatch(r"[a-f0-9]{40}",args.qa_commit),"QA_FIXED_COMMIT_REQUIRED")
    directory=ROOT/"artifacts/phase-qa"/uuid4().hex;directory.mkdir(parents=True,mode=0o700)
    save(directory/"run-identity.json",{"egap_parent_commit":command(["git","rev-parse","HEAD"]),
        "host_sources":{str(p.relative_to(ROOT)):sha(p) for p in
            (Path(__file__),ROOT/"tools/source-platform/host.py",ROOT/"tools/source-platform/client.py")},
        "qa_commit":args.qa_commit,"execution_index_commit":args.execution_commit})
    request,prior,archive=load_archive(args.execution_commit)
    publication=fetch_bundle(args.qa_repo,args.qa_commit,args.reader_config,directory)
    build=build_checker(directory,args.qa_commit)
    verdict=run_checker(directory,build["image_id"],request)
    old=[{k:row[k] for k in ("label","status","initial_verdict","final_verdict","transition")} for row in prior["cases"]]
    summary={"run_id":directory.name,"scope":"ARCHIVED_KNOWN_CANDIDATE_QA_REREVIEW","archive":archive,
        "publication":publication,"image_id":build["image_id"],"scoped_result":verdict["scoped_result"],
        "original_phase_verdicts_identical":verdict["cases"]==old,
        "structured_checks":sum(len(c[k]["checks"]) for c in verdict["cases"] for k in ("initial_verdict","final_verdict","transition")),
        "review_required_findings":len(verdict.get("findings",[])),"gaps":verdict.get("gaps",[]),
        "model_calls":0,"business_writes":0,"protected_final":False,"release_authorized":False}
    save(directory/"result.json",summary)
    print(json.dumps({k:summary[k] for k in ("run_id","scoped_result","structured_checks","review_required_findings",
        "original_phase_verdicts_identical","gaps","model_calls","business_writes","protected_final")}))


if __name__=="__main__":
    try:main()
    except Exception as error:
        print(json.dumps({"status":"INCOMPLETE","error_code":getattr(error,"code",type(error).__name__)}))
        raise SystemExit(2)
