#!/usr/bin/env python3
"""真实依赖故障验收：精确停止单个Lago/Keycloak API → 请求拒绝 → finally恢复。

先完成check.py且确保没有其他业务请求再运行。入口只驱动现有测试客户，不提交
成功生成任务；通过真实PostgreSQL只读计数证明故障请求未受理、未增加模型调用。
所有停启按project/service标签双重核对，绝不删除容器/卷或操作其他实验环境。
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from check import access_token
from seed import Api, NoRedirect, ROOT, SeedError, load_runtime, save_private


STATE = ROOT / "artifacts/multi-system"
PROJECT = "egap-multisystem"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FaultChecks:
    def __init__(self) -> None:
        self.runtime = load_runtime(STATE / "runtime.json")
        self.sut = load_runtime(STATE / "sut.json")
        self.seed = load_runtime(STATE / "seed.json")
        self.connections = load_runtime(Path(self.sut["connections_ref"]))
        self.case = next(case for case in self.seed["bindings"]["cases"] if case["label"] == "a_zero")
        self.result = {"schema_version": 1, "status": "RUNNING", "started_at": now(), "checks": [],
                       "faults": [], "receipts": [], "environment_id": self.sut["environment_id"],
                       "qualification": "LOCAL_REAL_DEPENDENCY_FAULT_CHECK",
                       "limitations": ["NO_PROTECTED_OBSERVER_QUALIFICATION", "NO_COMPANY_TEAM_ADOPTION"]}
        self.checkpoint()

    def checkpoint(self) -> None:
        save_private(STATE / "fault-check.json", self.result)

    def assert_check(self, name: str, condition: bool, **evidence) -> None:
        self.result["checks"].append({"name": name, "status": "PASS" if condition else "FAIL", **evidence})
        self.checkpoint()
        if not condition:
            raise SeedError("FAULT_CHECK_FAILED_" + name.upper())

    def query(self, dsn_key: str, sql: str, parameters=()):
        """专用实验数据库只读事务；DSN只在内存传递，不放命令行/报告。"""
        with psycopg.connect(self.connections[dsn_key], connect_timeout=3, row_factory=dict_row,
                options="-c default_transaction_read_only=on -c statement_timeout=4000") as connection:
            return connection.execute(sql, parameters).fetchall()

    def snapshot(self) -> dict:
        support = self.query("database_url", """
          SELECT (SELECT count(*) FROM egap_support.support_jobs) AS jobs,
            (SELECT count(*) FROM egap_support.support_jobs WHERE status IN ('QUEUED','PROCESSING')) AS active_jobs,
            (SELECT count(*) FROM egap_support.external_operations WHERE delivery_state IN ('PENDING','DISPATCHING')) AS active_deliveries,
            (SELECT count(*) FROM egap_support.external_api_observations WHERE event->>'service' IN ('chatwoot','lago')) AS downstream_calls,
            (SELECT count(*) FROM egap_support.external_api_observations WHERE event->>'service'='lago' AND event->>'path'=%s) AS invoice_detail_reads
        """, ("/api/v1/invoices/" + self.case["invoice_id"],))[0]
        gateway = self.query("gateway_database_url", """
          SELECT (SELECT count(*) FROM egap_gateway.calls WHERE execution_mode='LIVE_MODEL') AS model_calls,
            (SELECT count(*) FROM egap_gateway.calls WHERE state='STARTED') AS active_model_calls,
            (SELECT coalesce(sum(reserved_calls),0) FROM egap_gateway.budgets) AS reserved_model_calls
        """)[0]
        return {**support, **gateway}

    def docker(self, *arguments: str) -> str:
        completed = subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=45)
        if completed.returncode:
            raise SeedError("DOCKER_OPERATION_FAILED")
        return completed.stdout.strip()

    def container(self, service: str, container_id: str | None = None) -> dict:
        """从真实标签定位唯一API容器；停启前再次核验同一ID，拒绝名称模糊匹配。"""
        if service not in {"lago", "keycloak"}:
            raise SeedError("FAULT_SERVICE_NOT_ALLOWED")
        if container_id is None:
            ids = self.docker("ps", "-a", "--filter", "label=com.docker.compose.project=" + PROJECT,
                              "--filter", "label=com.docker.compose.service=" + service,
                              "--format", "{{.ID}}").splitlines()
            if len(ids) != 1:
                raise SeedError("FAULT_CONTAINER_NOT_UNIQUE")
            container_id = ids[0]
        value = json.loads(self.docker("inspect", "--format",
            '{"id":"{{.Id}}","running":{{.State.Running}},"labels":{{json .Config.Labels}}}', container_id))
        if value["labels"].get("com.docker.compose.project") != PROJECT \
                or value["labels"].get("com.docker.compose.service") != service:
            raise SeedError("FAULT_CONTAINER_IDENTITY_MISMATCH")
        return value

    def wait_health(self, url: str, timeout: int = 60) -> None:
        opener = build_opener(ProxyHandler({}), NoRedirect())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with opener.open(Request(url), timeout=2) as response:
                    if response.status == 200:
                        return
            except (OSError, HTTPError, URLError):
                pass
            time.sleep(0.5)
        raise SeedError("FAULT_DEPENDENCY_NOT_RECOVERED")

    @contextmanager
    def stopped(self, service: str):
        """仅停止先前运行的本栈API，异常/SIGTERM也执行恢复并检查原生HTTP。

        不停止数据库、Worker或SUT；故障对象和恢复状态逐步写证据，恢复失败不得
        被业务断言掩盖。SIGKILL/宿主崩溃仍需按本记录中的精确容器ID人工恢复。
        """
        found = self.container(service)
        if not found["running"]:
            raise SeedError("FAULT_DEPENDENCY_NOT_RUNNING")
        url = self.runtime["lago"]["base_url"] + "/health" if service == "lago" else \
            self.runtime["keycloak"]["issuer"] + "/.well-known/openid-configuration"
        self.wait_health(url)
        record = {"service": service, "project": PROJECT, "container_id": found["id"], "started_at": now(),
                  "fault_kind": "DOCKER_STOP_API_ONLY", "restored": False}
        self.result["faults"].append(record)
        self.checkpoint()
        try:
            self.docker("stop", "--time", "10", found["id"])
            self.assert_check(service + "_actually_stopped", not self.container(service, found["id"])["running"])
            record["stopped_at"] = now()
            self.checkpoint()
            yield
        finally:
            self.container(service, found["id"])
            self.docker("start", found["id"])
            self.wait_health(url)
            record.update(restored=True, restored_at=now(), recovery_http_status=200)
            self.checkpoint()

    def client(self) -> Api:
        token = access_token(self.runtime, self.case["tenant_id"])
        return Api(self.sut["base_url"], {"Authorization": "Bearer " + token},
                   service="sut", receipts=self.result["receipts"])

    def command(self, name: str) -> dict:
        return {key: self.case[key] for key in ("tenant_id", "conversation_id", "invoice_id")} | {
            "operation_key": "fault-" + name + "-" + uuid4().hex}

    def run(self) -> None:
        before = self.snapshot()
        self.assert_check("environment_quiescent", all(before[key] == 0 for key in ("active_jobs", "active_deliveries", "active_model_calls")), snapshot=before)
        jobs = self.query("database_url", "SELECT job_id FROM egap_support.external_operations WHERE tenant_id=%s AND conversation_id=%s ORDER BY created_at DESC LIMIT 1",
                          (self.case["tenant_id"], self.case["conversation_id"]))
        if len(jobs) != 1:
            raise SeedError("SUCCESSFUL_E2E_JOB_REQUIRED")
        path = f"/integrations/disputes/{self.case['tenant_id']}/{jobs[0]['job_id']}"
        client = self.client()
        self.assert_check("valid_identity_before_lago_fault", client.request("GET", path)["job"]["job_id"] == jobs[0]["job_id"])
        with self.stopped("lago"):
            before = self.snapshot()
            result = client.request("POST", "/integrations/disputes", self.command("lago"), expected=(503,))
            after = self.snapshot()
            self.assert_check("lago_unavailable_returns_503", result.get("error_code") == "EXTERNAL_UNAVAILABLE", response=result)
            self.assert_check("lago_fault_no_job_or_model", all(after[key] == before[key] for key in ("jobs", "model_calls", "reserved_model_calls")), before=before, after=after)
            self.assert_check("lago_fault_no_invoice_detail_read", after["invoice_detail_reads"] == before["invoice_detail_reads"])
        # 重新签发并先成功访问同一受保护入口，证明故障发生时并非无效/过期令牌。
        client = self.client()
        self.assert_check("fresh_identity_authorized_before_keycloak_fault", client.request("GET", path)["job"]["job_id"] == jobs[0]["job_id"])
        with self.stopped("keycloak"):
            before = self.snapshot()
            result = client.request("POST", "/integrations/disputes", self.command("keycloak"), expected=(503,))
            after = self.snapshot()
            self.assert_check("keycloak_unavailable_fails_closed", result.get("error_code") == "IDENTITY_UNAVAILABLE", response=result)
            self.assert_check("keycloak_fault_no_downstream_read", after["downstream_calls"] == before["downstream_calls"], before=before, after=after)
            self.assert_check("keycloak_fault_no_job_or_model", all(after[key] == before[key] for key in ("jobs", "model_calls", "reserved_model_calls")))
        config = load_runtime(Path(self.sut["runtime_ref"]).parent / "api.private.json")
        legacy = Api(self.sut["base_url"], {"Authorization": "Bearer " + config["api_token_a"]}, service="sut-legacy", receipts=self.result["receipts"])
        denied = legacy.request("GET", "/support-resolution-jobs/" + jobs[0]["job_id"], expected=(404,))
        self.assert_check("legacy_static_token_cannot_read_integration_job", denied.get("error_code") == "LEGACY_API_DISABLED", response=denied)
        self.assert_check("recovered_identity_and_sut_access", self.client().request("GET", path)["job"]["job_id"] == jobs[0]["job_id"])
        self.result.update(status="PASS_REAL_DEPENDENCY_FAULTS", completed_at=now(), final_snapshot=self.snapshot())
        self.checkpoint()


def main() -> int:
    checker = None
    original_term = signal.getsignal(signal.SIGTERM)
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt("FAULT_CHECK_INTERRUPTED")
    signal.signal(signal.SIGTERM, interrupted)
    try:
        checker = FaultChecks()
        checker.run()
        print(json.dumps({"status": checker.result["status"], "checks": len(checker.result["checks"]),
                          "dependencies_restored": all(row["restored"] for row in checker.result["faults"]),
                          "report": "artifacts/multi-system/fault-check.json"}))
        return 0
    except (SeedError, OSError, ValueError, KeyError, psycopg.Error, subprocess.SubprocessError, KeyboardInterrupt) as error:
        if checker:
            checker.result.update(status="FAILED_OR_ENV_NOT_READY", error_code=str(error) if isinstance(error, SeedError) else type(error).__name__, completed_at=now())
            checker.checkpoint()
        print(json.dumps({"status": "FAILED_OR_ENV_NOT_READY", "error_kind": type(error).__name__,
                          "report": "artifacts/multi-system/fault-check.json"}))
        return 1
    finally:
        signal.signal(signal.SIGTERM, original_term)


if __name__ == "__main__":
    raise SystemExit(main())
