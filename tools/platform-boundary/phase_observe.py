"""独立分阶段实验的观察端口；只有普通客服身份和只读凭据，没有准备者入口。

PhaseExecution → PhaseObserver → 产品 GET/只读 SQL、原生 OBSERVER、来源 OBSERVER、
模型出口 OBSERVER。这里不启动候选、不重新调用模型，不导入产品仓库代码。
本机受信进程是信任根；Python 对象分离不等于跨主机的安全隔离。
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

# 观察模块可单独导入，不通过准备入口加载 PhaseSource 或其管理员配置装配。
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/"tools/product-boundary"),str(ROOT/"tools/multi-system")]
from bind_platform import private, request
from observe_native import connect, login, model_observation_checks, native_messages
from late_usage_observer import LateUsageObserver, SNAPSHOT_SQL, ACCEPTED_SQL
from seed import SeedError


class ReadPort:
    """仅接受 GET 的固定角色端点；HTTP 失败保留回执，不能被解释为业务空值。"""
    def __init__(self, base, token, receipts, service):
        self.base, self.token, self.receipts, self.service = base, token, receipts, service

    def request(self, method, path):
        if method != "GET":
            raise SeedError("OBSERVER_WRITE_FORBIDDEN")
        response = request(self.base, path, token=self.token() if callable(self.token) else self.token)
        self.receipts.append({"service":self.service,"method":"GET","path":path,
                              "http_status":response[0],**response[2]})
        if response[0] != 200:
            raise SeedError("OBSERVER_HTTP_UNAVAILABLE")
        return response[1]


class ChatwootReadPort(ReadPort):
    """读完整有界消息集合；首屏不是全部历史，分页未完成就不给出完整消息观察。"""
    def request(self, method, path):
        match = re.fullmatch(r"/api/v1/accounts/(\d+)/conversations/(\d+)/messages", path)
        if method == "GET" and match:
            messages, receipts = native_messages({"base_url":self.base,"token":self.token},
                int(match[1]),int(match[2]))
            self.receipts.extend({"service":"chatwoot","method":"GET",**r} for r in receipts)
            return {"payload":messages}
        return super().request(method,path)


class PhaseObserver:
    """通过固定候选绑定装配只读客户端；调用方在每个账本步骤前复核 Docker 身份。"""
    def __init__(self, directory, deployed, platform_repo, basis):
        self.directory,self.deployed,self.basis=directory,deployed,basis
        self.receipt=private(deployed/"deployment-receipt.json")
        self.database=private(deployed/"observer.private.json")
        self.caller=private(platform_repo/".local"/basis["platform"]["project"]/"caller.private.json")
        access=basis["experiment_access"];commercial=basis["commercial_source"]
        self.native=private(platform_repo/".local"/access["project"]/"observer-access.private.json")
        self.commercial=private(platform_repo/".local"/commercial["project"]/"observer-client.private.json")
        self.model=private(deployed/"model-observer.private.json")
        self.base="http://127.0.0.1:"+str(self.receipt["endpoints"]["api"]["port"])
        if self.database["deployment_id"]!=basis["candidate"]["deployment_id"] \
                or self.caller.get("role")!="BUSINESS_CALLER" or self.caller["project"]!=basis["platform"]["project"] \
                or self.caller["keycloak"]["base_url"]!=basis["platform"]["deployment"]["endpoints"]["keycloak"] \
                or self.native["role"]!="OBSERVER" or self.native["instance_id"]!=access["project"] \
                or self.native["base_url"]!=access["access_endpoint"] \
                or self.commercial["role"]!="OBSERVER" or self.commercial["instance_id"]!=commercial["project"] \
                or self.commercial["base_url"]!="http://127.0.0.1:"+str(commercial["endpoints"]["observer"]["port"]) \
                or self.model["role"]!="OBSERVER" or self.model["deployment_id"]!=self.database["deployment_id"]:
            raise SeedError("PHASE_OBSERVER_BINDING_MISMATCH")
        self.receipts=[];self.model_records=[]

    def audit_scalar(self, query, parameters):
        """只暴露观察器实际需要的两条 SELECT；数据库角色本身也没有写权。"""
        if query not in {SNAPSHOT_SQL,ACCEPTED_SQL}:
            raise SeedError("UNREGISTERED_OBSERVER_QUERY")
        with connect(self.receipt,self.database,"support") as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            row=conn.execute(query,parameters).fetchone()
            return next(iter(row.values())) if row else None

    def model_trace(self, case, job_id):
        """独立取得 wire 原件，再与产品存储逐项核对；不从最终草稿反推模型输出。"""
        with connect(self.receipt,self.database,"support") as conn:
            native=conn.execute("SELECT * FROM egap_support.support_jobs WHERE tenant_id=%s AND job_id=%s",
                (case["tenant_id"],job_id)).fetchone()
        with connect(self.receipt,self.database,"gateway") as conn:
            gateway=conn.execute("SELECT * FROM egap_gateway.calls WHERE job_id=%s",(job_id,)).fetchone()
        base="http://127.0.0.1:"+str(self.receipt["endpoints"]["model-access"]["port"])
        response=request(base,"/v1/operations/"+job_id,token=self.model["token"])
        record={"http_status":response[0],"response":response[1],**response[2]}
        self.model_records.append(record)
        checks=model_observation_checks(response[1],self.basis["model"],self.database["deployment_id"],native or {},gateway or {})
        record["binding_checks"]=checks
        if response[0]!=200 or not all(checks.values()):
            raise SeedError("PHASE_MODEL_ORIGINAL_NOT_BOUND")
        wire=response[1]["operation"]
        model_request=json.loads(wire["request_text"]);model_response=json.loads(wire["response_text"])
        context=json.loads(model_request["messages"][1]["content"])
        return {"job_id":job_id,"decision":context["decision"],"model_request":model_request,
            "model_output":model_response,"authority":"INDEPENDENT_MODEL_OUTLET_MATCHED_PRODUCT_RECORD"}

    def observe(self, case, job_id):
        """单次观察保留所有缺项；只有原件完整绑定后才提供可供检查的 decision。"""
        self.receipts=[];self.model_records=[]
        native=ChatwootReadPort(self.native["base_url"],self.native["token"],self.receipts,"native")
        observer=LateUsageObserver(
            ReadPort(self.base,lambda:login(self.caller,case["tenant_id"]),self.receipts,"sut"),
            native,ReadPort(self.native["base_url"],self.native["token"],self.receipts,"lago"),
            ReadPort(self.commercial["base_url"],self.commercial["token"],self.receipts,"commercial"),
            self.audit_scalar,cw_prefix=f"/api/v1/accounts/{case['account_id']}",model_trace_reader=self.model_trace)
        package=observer.observe(case,job_id)
        package.update(deployment_binding=self.basis,transport_receipts=self.receipts,model_originals=self.model_records,
            candidate_binding_status="DOCKER_AND_FROZEN_EXPERIMENT_BINDING_REVALIDATED_BY_RUNTIME")
        return package

    def counts(self):
        """恢复前后核对实际效果数量；不把操作账本的 COMPLETED 数当模型调用数。"""
        with connect(self.receipt,self.database,"support") as conn:
            jobs=conn.execute("SELECT job_id,status FROM egap_support.support_jobs ORDER BY job_id").fetchall()
            deliveries=conn.execute("SELECT job_id,delivery_state FROM egap_support.external_operations ORDER BY job_id").fetchall()
        with connect(self.receipt,self.database,"gateway") as conn:
            calls=conn.execute("SELECT job_id,state FROM egap_gateway.calls ORDER BY job_id").fetchall()
        return {"jobs":jobs,"deliveries":deliveries,"model_calls":calls}
