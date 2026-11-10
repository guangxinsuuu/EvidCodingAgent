"""ReleaseCheck → 权限/并发/依赖故障边界；不调用产品资格函数。

只有当前SUT冻结配置明确引用本directory来源实例时，才允许精确stop/start。
故障期间不重启SUT、不操作三套原生产品；finally始终恢复同一来源身份与端口。
全部请求使用新操作键或相同父Job，检查拒绝/复用没有新增Job和模型调用预留。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from threading import Barrier
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from seed import Api, ROOT, SeedError, load_runtime


class BoundaryError(SeedError):
    """仅稳定错误码离开驱动；不回显宿主stderr、数据库连接或鉴权头。"""


def _directory_identity(checker: Any, directory: Path) -> tuple[Path, dict, dict]:
    """绑定本次来源与SUT实际冻结配置，防止传错目录后停止另一次实验。

    commercial-source宿主还会核验容器标签、PID签名与源码摘要；这里增加调用方的
    业务归属约束。只读本机私有配置，不把口令或token存入检查结果。
    """
    directory = directory.absolute()
    if directory.parent.resolve() != (ROOT / "artifacts/business-source").resolve() \
            or any(item.is_symlink() for item in (directory, *directory.parents)):
        raise BoundaryError("BOUNDARY_SOURCE_DIRECTORY_INVALID")
    ready = load_runtime(directory / "ready.json")
    private = load_runtime(directory / "runtime.private.json")
    runtime_ref = Path(checker.sut["runtime_ref"])
    frozen = load_runtime(runtime_ref.parent / "integration.private.json").get("commercial_source", {})
    keys = ("instance_id", "base_url", "source_digest", "api_version")
    if ready.get("status") != "READY" or ready.get("authority_scope") != "LOCAL_LAB" \
            or any(frozen.get(key) != ready.get(key) for key in keys) \
            or private.get("instance_id") != ready.get("instance_id") \
            or private.get("read_token") != frozen.get("read_token"):
        raise BoundaryError("BOUNDARY_SOURCE_IDENTITY_MISMATCH")
    return directory, ready, private


def _host(action: str, directory: Path) -> dict:
    """只允许status/stop/start；固定可执行程序和目录参数，无shell/全局清理。"""
    if action not in {"status", "stop", "start"}:
        raise BoundaryError("BOUNDARY_HOST_ACTION_FORBIDDEN")
    try:
        process = subprocess.run([str(ROOT / "services/genai-support-sut/.venv/bin/python"),
            str(ROOT / "tools/commercial-source.py"), action, "--directory", str(directory)],
            cwd=ROOT, text=True, capture_output=True, timeout=60)
        value = json.loads(process.stdout) if process.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        raise BoundaryError("BOUNDARY_HOST_OPERATION_UNCONFIRMED") from None
    if not isinstance(value, dict):
        raise BoundaryError("BOUNDARY_HOST_OPERATION_UNCONFIRMED")
    return value


def _observed(api: Any, method: str, path: str, body: dict | None = None) -> dict:
    """保留实际HTTP状态作断言；非预期响应是FAIL，不在驱动中伪造某个结果。"""
    start = len(api.receipts)
    try:
        value = api.request(method, path, body, expected=tuple(range(200, 600)))
    except (SeedError, OSError, ValueError):
        return {"status": None, "body": {}, "error_code": "BOUNDARY_REQUEST_UNCONFIRMED"}
    receipts = api.receipts[start:]
    if len(receipts) != 1 or type(receipts[0].get("status")) is not int:
        return {"status": None, "body": {}, "error_code": "BOUNDARY_HTTP_RECEIPT_MISSING"}
    return {"status": receipts[0]["status"], "body": value}


def _isolated_client(client: Any) -> Api:
    """线程各有opener与收据列表；沿用刚取得的同租户token，不共享可变HTTP状态。"""
    return Api(client.base_url, dict(client.headers), service=client.service, receipts=[])


def _counts(checker: Any) -> dict:
    """只读固定SQL：API拒绝和同事实刷新都不得增加Job/模型调用或预算。"""
    import psycopg
    connections = load_runtime(Path(checker.sut["connections_ref"]))
    try:
        with psycopg.connect(connections["gateway_database_url"], connect_timeout=3,
                options="-c default_transaction_read_only=on -c statement_timeout=4000") as db:
            calls = db.execute("SELECT count(*) FROM egap_gateway.calls").fetchone()[0]
            reserved = db.execute("SELECT COALESCE(sum(reserved_calls),0) FROM egap_gateway.budgets").fetchone()[0]
        return {"jobs": checker.audit_scalar("SELECT count(*) FROM egap_support.support_jobs"),
                "gateway_calls": calls, "reserved_calls": reserved}
    except psycopg.Error:
        raise BoundaryError("BOUNDARY_BUDGET_OBSERVATION_UNAVAILABLE") from None


def _job_id(value: dict) -> str:
    job = value.get("job", value)
    identity = job.get("job_id") if isinstance(job, dict) else None
    if not isinstance(identity, str) or not identity:
        raise BoundaryError("BOUNDARY_EXISTING_JOB_REQUIRED")
    return identity


def _scope(case: dict, checker: Any) -> dict:
    # 角色拒绝在scope查库前执行；缺少非必要客户字段时用明确探测标识，不借其读对象。
    return {"tenant_id": case["tenant_id"], "customer_id": case.get("customer_id", "boundary-probe"),
        "subscription_id": case.get("subscription_id", "boundary-probe"), "invoice_id": case["invoice_id"],
        "account_id": checker.config["chatwoot"]["account_id"], "conversation_id": case["conversation_id"]}


def run_boundaries(checker: Any, directory: Path, cases: dict, jobs: dict) -> dict:
    """执行发布验证的边界切片，返回脱敏观察摘要；恢复失败必须显式阻断。

    前置：v1_history/v2_boundary已有已完成Job，当前没有未完成模型任务；cases至少
    包含tenant-a和tenant-b。主校准器负责串行调用此函数，不能与对象修改并行。
    checker.assertion记录失败但不抛异常；本模块仍对执行错误和恢复失败保留固定码。
    """
    directory, ready, private = _directory_identity(checker, directory)
    identifier = "boundary-" + uuid4().hex[:12]
    summary = {"boundary_id": identifier, "source_instance_id": ready["instance_id"],
        "started_at": datetime.now(timezone.utc).isoformat(), "status": "RUNNING", "checks": [],
        "source_recovery": "NOT_STARTED"}
    checker.result.setdefault("boundaries", []).append(summary)

    def record(name: str, condition: bool, **evidence):
        summary["checks"].append({"name": name, "passed": bool(condition), **evidence})
        checker.assertion("boundary_" + name, bool(condition), **evidence)
        checker.checkpoint()

    def client(tenant: str):
        return _isolated_client(checker.client(tenant))

    def consume(api: Any):
        checker.result["receipts"].extend(api.receipts)
        api.receipts.clear()
        checker.checkpoint()

    def submit(case: dict, suffix: str):
        return {"tenant_id": case["tenant_id"], "conversation_id": case["conversation_id"],
                "invoice_id": case["invoice_id"], "operation_key": identifier + ":" + suffix}

    try:
        history, boundary = cases["v1_history"], cases["v2_boundary"]
        history_job, boundary_job = _job_id(jobs["v1_history"]), _job_id(jobs["v2_boundary"])
        a_case = next(case for case in cases.values() if case["tenant_id"] == "tenant-a")
        b_case = next(case for case in cases.values() if case["tenant_id"] == "tenant-b")
        initial = _host("status", directory)
        if initial.get("instance_id") != ready["instance_id"] or initial.get("source_unchanged") is not True \
                or initial.get("ready") is not True:
            raise BoundaryError("BOUNDARY_SOURCE_NOT_READY")

        # HTTP角色边界使用无效空命令：即使权限实现错误，也不产生领域写入。
        source_reader = Api(ready["base_url"], {"Authorization": "Bearer " + private["read_token"]}, service="commercial", receipts=[])
        observed = _observed(source_reader, "POST", "/v1/commands", {})
        record("reader_cannot_write", observed["status"] == 401, observed_status=observed["status"])
        consume(source_reader)
        source_admin = Api(ready["base_url"], {"Authorization": "Bearer " + private["admin_token"]}, service="commercial", receipts=[])
        observed = _observed(source_admin, "GET", "/v1/context?" + urlencode(_scope(a_case, checker)))
        record("admin_not_reader", observed["status"] == 401, observed_status=observed["status"])
        consume(source_admin)

        # 发票属于B，但会话和身份属于A；检查详情读取次数，不能先泄露再拒绝。
        before = _counts(checker)
        reads_before = checker.invoice_reads(b_case["invoice_id"])
        a_client = client("tenant-a")
        request = {**submit(a_case, "foreign-invoice"), "invoice_id": b_case["invoice_id"]}
        observed = _observed(a_client, "POST", "/integrations/disputes", request)
        consume(a_client)
        record("cross_tenant_invoice_denied", observed["status"] == 403, observed_status=observed["status"])
        record("cross_tenant_no_detail_read", checker.invoice_reads(b_case["invoice_id"]) == reads_before)
        after = _counts(checker)
        record("cross_tenant_no_job_or_budget", after == before, before=before, after=after)

        # 两个新的operation key同时刷新相同父评估，实际HTTP并发，不只调用同一函数两遍。
        before = _counts(checker)
        concurrent_clients = [client(history["tenant_id"]), client(history["tenant_id"])]
        path = f"/integrations/disputes/{history['tenant_id']}/{history_job}/reevaluate"
        barrier = Barrier(2)
        def refresh(index: int, api: Any):
            barrier.wait(timeout=5)
            return _observed(api, "POST", path,
                {"operation_key": identifier + f":concurrent-{index}", "reason": "同一冻结商业事实的并发重复评估"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(refresh, index, api)
                for index, api in enumerate(concurrent_clients)]
            results = [future.result() for future in futures]
        for api in concurrent_clients:
            consume(api)
        record("concurrent_same_facts_reuse", all(row["status"] == 202 and row["body"].get("job_id") == history_job for row in results),
               statuses=[row["status"] for row in results], returned_job_ids=[row["body"].get("job_id") for row in results], expected_job_id=history_job)
        after = _counts(checker)
        record("concurrent_no_job_or_model_budget", after == before, before=before, after=after)

        # 使用新key确保SUT尝试重新收集上游，不能命中旧operation回放掩盖断源。
        before = _counts(checker)
        stop_attempted = False
        try:
            stop_attempted = True  # stop即使回执不明也可能已部分完成，finally必须尝试恢复。
            stopped = _host("stop", directory)
            summary["source_recovery"] = "STOPPED"
            if stopped.get("status") != "STOPPED" or stopped.get("instance_id") != ready["instance_id"]:
                raise BoundaryError("BOUNDARY_SOURCE_STOP_UNCONFIRMED")
            checker.checkpoint()
            down_client = client(boundary["tenant_id"])
            observed = _observed(down_client, "POST", "/integrations/disputes", submit(boundary, "source-down"))
            consume(down_client)
            record("source_down_is_503", observed["status"] == 503 and "job_id" not in observed["body"],
                   observed_status=observed["status"], observed_error=observed["body"].get("error_code"))
            after = _counts(checker)
            record("source_down_no_job_fixture_or_budget", after == before, before=before, after=after)
        finally:
            if stop_attempted:
                try:
                    restarted = _host("start", directory)
                    status = _host("status", directory)
                    health = Api(ready["base_url"], {}, service="commercial", receipts=[])
                    actual = _observed(health, "GET", "/health/ready")
                    consume(health)
                    restored = (restarted.get("status") == "READY" and all(restarted.get(key) == ready.get(key)
                        for key in ("instance_id", "base_url", "source_digest")) and status.get("ready") is True
                        and status.get("source_unchanged") is True and actual["status"] == 200
                        and actual["body"].get("instance_id") == ready["instance_id"])
                    record("source_restored_same_identity_port_digest", restored)
                    if not restored:
                        raise BoundaryError("BOUNDARY_SOURCE_RESTORE_FAILED")
                    summary["source_recovery"] = "RESTORED"
                    checker.checkpoint()
                except Exception:
                    summary.update(status="FAILED", source_recovery="FAILED", error_code="BOUNDARY_SOURCE_RESTORE_FAILED")
                    checker.checkpoint()
                    raise BoundaryError("BOUNDARY_SOURCE_RESTORE_FAILED") from None
        restored_client = client(boundary["tenant_id"])
        observed = _observed(restored_client, "POST", "/integrations/disputes", submit(boundary, "source-restored"))
        consume(restored_client)
        record("restored_same_facts_reuses_original_job", observed["status"] == 202 and observed["body"].get("job_id") == boundary_job,
               observed_status=observed["status"], expected_job_id=boundary_job, actual_job_id=observed["body"].get("job_id"))
        after = _counts(checker)
        record("restored_no_job_or_model_budget", before == after, before=before, after=after)
        summary["status"] = "PASS" if all(row["passed"] for row in summary["checks"]) else "FAIL"
        return summary
    except Exception as error:
        summary.update(status="FAILED", error_code=str(error) if isinstance(error, BoundaryError) else "BOUNDARY_EXECUTION_FAILED")
        raise BoundaryError(summary["error_code"]) from None
    finally:
        summary["completed_at"] = datetime.now(timezone.utc).isoformat()
        checker.checkpoint()
