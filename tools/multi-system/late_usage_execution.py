#!/usr/bin/env python3
"""固定人工计划的真实迟到用量执行切片，不开放 Agent 自动派发。

CLI prepare/deploy/run → PostgreSQL 操作账本 → 分步 SourceObjects/真实 SUT API
→ 独立 Observer/Oracle。每个步骤保存原回执；重启复用同一操作、事件和 Job。
这里仅接受与本机受信工作树逐字相符的已提交候选，不执行任意不可信补丁。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import psycopg

from seed import ROOT, Api, SeedError, load_runtime, save_private, sha
from check import access_token
from release_seed import NativeObjects, SourceObjects, fresh_usage_specs
from experiment_ledger import ExperimentLedger
from late_usage_observer import LateUsageObserver, assess_observation, assess_transition, summarize_execution
from sut import prepare as prepare_integration


SCOPE = "MANUAL_FIXED_PLAN_INTEGRATION_ONLY"
PRODUCT_PREFIXES = ("services/genai-support-sut/src/", "services/genai-support-sut/migrations/",
                    "datasets/genai-support/synthetic-v1/sut/")
PRODUCT_FILES = {"services/genai-support-sut/pyproject.toml", "services/genai-support-sut/uv.lock"}


def demand(condition: bool, code: str) -> None:
    if not condition:
        raise SeedError(code)


def git(*arguments: str) -> bytes:
    result = subprocess.run(["git", *arguments], cwd=ROOT, capture_output=True, timeout=15)
    demand(result.returncode == 0, "CANDIDATE_GIT_READ_FAILED")
    return result.stdout


def current_product_paths() -> set[str]:
    """核对文件集合，避免旧commit已有文件全匹配却额外加载新模块。"""
    found = set(PRODUCT_FILES)
    def visit(path: Path):
        demand(not path.is_symlink(), "CANDIDATE_SOURCE_SYMLINK")
        for child in path.iterdir():
            if child.name.startswith(".") or child.name == "__pycache__":
                continue
            demand(not child.is_symlink(), "CANDIDATE_SOURCE_SYMLINK")
            if child.is_dir():
                visit(child)
            elif child.suffix in {".py", ".json", ".sql"}:
                found.add(str(child.relative_to(ROOT)))
    for prefix in PRODUCT_PREFIXES:
        if (ROOT / prefix).exists():
            visit(ROOT / prefix)
    return found


def candidate_manifest(candidate: str) -> dict:
    """固定实际执行的产品字节；commit 字符串本身不足以证明正在运行哪个版本。"""
    demand(bool(re.fullmatch(r"[0-9a-f]{40}", candidate)), "FULL_CANDIDATE_COMMIT_REQUIRED")
    demand(git("cat-file", "-t", candidate).strip() == b"commit", "CANDIDATE_NOT_COMMIT")
    paths = git("ls-tree", "-rz", "--name-only", candidate).decode().split("\0")
    selected = sorted(path for path in paths if path in PRODUCT_FILES or
                      (path.startswith(PRODUCT_PREFIXES) and Path(path).suffix in {".py", ".json", ".sql"}))
    demand(bool(selected), "CANDIDATE_PRODUCT_MISSING")
    demand(current_product_paths() == set(selected), "CANDIDATE_PRODUCT_FILE_SET_MISMATCH")
    files = {}
    for name in selected:
        path = ROOT / name
        expected = hashlib.sha256(git("show", candidate + ":" + name)).hexdigest()
        demand(path.is_file() and not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest() == expected,
               "CANDIDATE_WORKTREE_MISMATCH")
        files[name] = expected
    extra = git("ls-files", "--others", "--exclude-standard", "--", *PRODUCT_PREFIXES, *sorted(PRODUCT_FILES))
    demand(not extra.strip(), "UNTRACKED_CANDIDATE_PRODUCT_FORBIDDEN")
    return {"candidate_sha": candidate, "files": files, "product_sha256": sha(files),
            "qualification": "REVIEWED_LOCAL_SOURCE_MATCH_NOT_UNTRUSTED_CODE_SANDBOX"}


def check_product(binding: dict) -> None:
    demand(current_product_paths() == set(binding["files"]), "CANDIDATE_SOURCE_DRIFT")
    for name, expected in binding["files"].items():
        path = ROOT / name
        demand(path.is_file() and not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest() == expected,
               "CANDIDATE_SOURCE_DRIFT")


def load_host():
    spec = importlib.util.spec_from_file_location("late_usage_support_host", ROOT / "tools/support-lab.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def checked_directory(path: Path) -> Path:
    path = path.absolute()
    demand(path.parent.resolve() == (ROOT / "artifacts/business-source").resolve()
           and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", path.name)), "EXPERIMENT_DIRECTORY_REQUIRED")
    demand(all(not p.is_symlink() for p in (path, *path.parents)), "EXPERIMENT_SYMLINK_FORBIDDEN")
    return path


class ExecutionSlice:
    """受信 CLI 持有执行权限；规划 Agent 没有本类入口或本账本凭据。

    PostgreSQL 是步骤完成身份；Seed/Commercial 的子操作账本继续保留每个原生
    HTTP 效果，不能用一个父步骤的 success 隐去中间写入或未知结果。
    """

    def __init__(self, directory: Path, dsn_file: Path):
        self.directory = checked_directory(directory)
        demand(dsn_file.is_file() and not dsn_file.is_symlink() and not dsn_file.stat().st_mode & 0o077,
               "PRIVATE_LEDGER_DSN_REQUIRED")
        self.ledger = ExperimentLedger(dsn_file.read_text().strip(), self.directory / "execution-evidence")
        self.manifest_path = self.directory / "execution-manifest.private.json"
        self.manifest = load_runtime(self.manifest_path) if self.manifest_path.exists() else None
        self.replays = []
        if self.manifest:
            self.ledger.register_experiment(self.manifest["experiment_id"], candidate=self.manifest["candidate"],
                configuration=self.manifest["configuration"], inputs=self.manifest["inputs"])

    @property
    def experiment_id(self) -> str:
        demand(self.manifest is not None, "PREPARE_REQUIRED")
        return self.manifest["experiment_id"]

    def step(self, key, kind, effect, request, callback):
        """每次派发前重验产品来源；COMPLETED 重放不再调用 callback。"""
        check_product(self.manifest["candidate"])
        demand(sha(load_runtime(ROOT / "artifacts/multi-system/runtime.json")) ==
               self.manifest["configuration"]["native_configuration_sha256"], "NATIVE_CONFIGURATION_DRIFT")
        if (self.directory / "sut/sut.json").exists():
            self.verify_deployment()
        def with_identity(dispatch):
            # 逐操作记实际工具版本；复用旧操作仍保留旧身份，不冒充用新版重新执行。
            names = ("late_usage_execution.py", "release_seed.py", "late_usage_observer.py",
                     "experiment_ledger.py", "release_oracle.py", "seed.py", "business_source.py", "check.py", "sut.py")
            dispatch.record_evidence("execution_identity", {"candidate_sha": self.manifest["candidate"]["candidate_sha"],
                "product_sha256": self.manifest["candidate"]["product_sha256"], "scope": SCOPE,
                "tools": {name: hashlib.sha256((ROOT / "tools/multi-system" / name).read_bytes()).hexdigest() for name in names}})
            return callback(dispatch)
        result = self.ledger.execute(self.experiment_id, key, kind=kind, effect=effect,
                                     request=request, callback=with_identity)
        if result.get("replayed"):
            self.replays.append(key)
        return result["result"]

    def source_step(self, dispatch, method: str, *args, **kwargs):
        """连接父操作与已有商业子账本；不把复用的子回执伪称为再次发送。"""
        pack = SourceObjects(self.directory)
        old_count = len(pack.state["receipts"])
        result = getattr(pack, method)(*args, **kwargs)
        for receipt in pack.state["receipts"][old_count:]:
            if receipt.get("method") != "GET" and isinstance(receipt.get("status"), int):
                dispatch.record_sent(receipt)
        if method == "dispatch_usage":
            for receipt in result["events"]:
                dispatch.record_sent(receipt)
        dispatch.record_evidence("source_suboperations", {"commands": pack.state["commands"],
            "receipts": pack.state["receipts"][old_count:]})
        return result if result is not None else {"status": pack.state["status"], "plans": pack.state["plans"]}

    def wait_job(self, observer, case, job_id, phase):
        """完整终态读回可复用；WAITING 或独立读回缺失时恢复须建立新观察。

        总计最多三轮，每轮有独立原始包。容量耗尽保留WAITING而不启动新Job或模型。
        未采集模型请求/输出是显式能力缺口，不作为不断重读的理由。
        """
        prefix = case["label"] + ".observe." + phase + "."
        existing = [row for row in self.ledger.list_operations(self.experiment_id)
                    if row["operation_key"].startswith(prefix)]
        for row in existing:
            if row["status"] == "COMPLETED" and self._job_observation_reusable(row.get("result") or {}):
                return self.step(row["operation_key"], "OBSERVE_ASSESSMENT", "READ", {"job_id": job_id},
                                 lambda dispatch: observer.wait(case, job_id))
        demand(len(existing) < 3, "JOB_OBSERVATION_BUDGET_EXHAUSTED")
        def observe(dispatch):
            return observer.wait(case, job_id, timeout=180, poll_interval=1,
                on_observation=lambda package: dispatch.record_evidence("business_observation", package))
        return self.step(prefix + str(len(existing) + 1), "OBSERVE_ASSESSMENT", "READ", {"job_id": job_id}, observe)

    @staticmethod
    def _job_observation_reusable(package: dict) -> bool:
        """Job 终态与证据完整分别判断；独立系统的一次读失败不能冻结后续调查。"""
        if package.get("wait_status") == "PROTOCOL_INVALID":
            return True
        if package.get("wait_status") != "TERMINAL" or package.get("protocol_status") != "VALID":
            return False
        if not all(isinstance(package.get(key), dict) for key in ("result", "context", "accepted_event")) \
                or not isinstance(package["result"].get("job"), dict):
            return False
        observed = package.get("observations") or {}
        if not isinstance(observed, dict) or not all(isinstance(observed.get(key), dict) for key in (
                "source_context", "invoice", "usage", "conversation", "original_message")) \
                or not isinstance(observed.get("messages"), list):
            return False
        reads = {row.get("name"): row.get("state") for row in package.get("reads", []) if isinstance(row, dict)}
        return all(reads.get(name) == "OBSERVED" for name in ("sut_job", "sut_snapshot", "sut_accepted_event",
            "chatwoot_messages", "chatwoot_conversation", "lago_invoice", "lago_usage", "commercial_source"))

    def prepare(self, candidate: str, run_id: str, period_start: str, model: str):
        """只创建新业务对象；原生期间不匹配就失败，不借旧对象或修改发票回执。"""
        binding = candidate_manifest(candidate)
        specs = fresh_usage_specs(period_start)
        ready = load_runtime(self.directory / "ready.json")
        demand(ready.get("status") == "READY", "COMMERCIAL_SOURCE_NOT_READY")
        runtime = load_runtime(ROOT / "artifacts/multi-system/runtime.json")
        configuration = {"scope": SCOPE, "model": model, "max_model_calls": 8,
                         "native_configuration_sha256": sha(runtime),
                         "commercial_instance": ready["instance_id"], "commercial_source_digest": ready["source_digest"]}
        manifest = {"schema_version": 1, "experiment_id": run_id, "candidate": binding,
                    "configuration": configuration, "inputs": {"scenario_specs": specs},
                    "agent_dispatch_authorized": False, "business_release_authorized": False}
        if self.manifest is not None:
            demand(self.manifest == manifest, "EXPERIMENT_IDENTITY_CHANGED")
        else:
            save_private(self.manifest_path, manifest)
            self.manifest = manifest
        self.ledger.register_experiment(run_id, candidate=binding, configuration=configuration, inputs=manifest["inputs"])
        def prepare_native(dispatch):
            writer = NativeObjects(runtime, self.directory / "native/seed.json", run_id=run_id, specs=specs)
            result = writer.prepare()
            for receipt in writer.state["receipts"]:
                if receipt.get("method") != "GET" and isinstance(receipt.get("status"), int):
                    dispatch.record_sent(receipt)
            dispatch.record_evidence("native_suboperations", writer.state)
            return result
        native = self.step("prepare.native", "PREPARE_NATIVE_OBJECTS", "WRITE", {"specs": specs}, prepare_native)
        self.step("prepare.source", "PREPARE_COMMERCIAL_OBJECTS", "WRITE", {"native_sha256": sha(native)},
                  lambda dispatch: self.source_step(dispatch, "prepare"))
        initial = {}
        for native_case in native["bindings"]["cases"]:
            case = {**native_case, "experiment_id": self.experiment_id,
                    "candidate_sha": self.manifest["candidate"]["candidate_sha"]}
            label = case["label"]
            initial[label] = self.step("initial." + label, "QUALIFY_INITIAL_STATE", "READ", {"case": case},
                lambda dispatch, label=label: SourceObjects(self.directory).observe_initial_state(label, observation_id="initial-" + label))
        demand(all(x["status"] == "READY" for x in initial.values()), "INITIAL_STATE_NOT_QUALIFIED")
        inventory = {"scope": SCOPE, "experiment_id": run_id, "candidate_sha": candidate,
                     "native_objects": native["bindings"]["cases"], "initial_qualifications": initial,
                     "business_objects": SourceObjects(self.directory).state["plans"]}
        save_private(self.directory / "object-inventory.private.json", inventory)
        return {"status": "PREPARED", "experiment_id": run_id, "cases": list(initial), "scope": SCOPE}

    def deploy(self):
        """复用受信宿主创建新 SUT/PG/Redis；显式环境ID，不覆盖旧 active 指针。"""
        self.ledger.register_experiment(self.experiment_id, candidate=self.manifest["candidate"],
            configuration=self.manifest["configuration"], inputs=self.manifest["inputs"])
        demand((self.directory / "object-inventory.private.json").exists(), "QUALIFIED_OBJECTS_REQUIRED")
        def start(dispatch):
            host = load_host()
            config = prepare_integration(bindings_path=self.directory / "native/bindings.json",
                output_directory=self.directory / "sut", commercial_config=self.directory / "commercial-reader.private.json")
            value = load_runtime(config)
            provision = {"mode": "LIVE_MODEL", "model": self.manifest["configuration"]["model"],
                         "business_source": "CHATWOOT_LAGO",
                         "integration_config_digest": hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
            saved = self.directory / "sut/host-environment.private.json"
            demand(not saved.exists(), "EXISTING_DEPLOYMENT_REQUIRES_RECONCILIATION")
            metadata = host.node("up", provision)
            save_private(saved, metadata)
            env_id = metadata["environment"]["environment_instance_id"]
            env = ROOT / metadata["directory"]
            source = load_runtime(env / "source-manifest.json")
            demand(source["files"] == self.manifest["candidate"]["files"], "DEPLOYED_CANDIDATE_SOURCE_MISMATCH")
            state = host.start_services(env, "LIVE_MODEL", provision["model"], max_calls=8, integration_config=config)
            sut = {"base_url": state["endpoints"]["support-gateway"], "environment_id": env_id,
                   "connections_ref": str(env / "connections.private.json"), "runtime_ref": str(env / "runtime.json"),
                   "source_manifest_ref": str(env / "source-manifest.json"), "integration_config_sha256": sha(value),
                   "candidate_sha": self.manifest["candidate"]["candidate_sha"],
                   "product_sha256": self.manifest["candidate"]["product_sha256"], "qualification": SCOPE}
            save_private(self.directory / "sut/sut.json", sut)
            return sut
        return self.step("deploy.sut", "DEPLOY_EXACT_LOCAL_CANDIDATE", "WRITE",
                         {"candidate_sha": self.manifest["candidate"]["candidate_sha"]}, start)

    def verify_deployment(self):
        """从已完成部署的不可变回执绑定实际端点、实例目录和各角色进程。

        sut.json 只是本地索引，不能单靠其中可改写的 URL/路径证明候选部署身份。
        直接回放 PG 的已完成操作会核对全部证据摘要；这里不能调用 step，否则
        step 的部署前置核验会递归。回放意外进入 callback 时只失败，绝不再部署。
        """
        sut = load_runtime(self.directory / "sut/sut.json")
        operation = self.ledger.get_operation(self.experiment_id, "deploy.sut")
        demand(operation is not None and operation["status"] == "COMPLETED", "DEPLOYMENT_RECEIPT_NOT_COMPLETE")
        def no_deploy(dispatch):
            raise SeedError("DEPLOYMENT_VERIFICATION_MUST_NOT_DISPATCH")
        recorded = self.ledger.execute(self.experiment_id, "deploy.sut", kind="DEPLOY_EXACT_LOCAL_CANDIDATE",
            effect="WRITE", request={"candidate_sha": self.manifest["candidate"]["candidate_sha"]}, callback=no_deploy)
        demand(recorded.get("replayed") is True and sut == recorded.get("result"), "DEPLOYMENT_INDEX_CHANGED")
        environment_id = sut.get("environment_id")
        demand(isinstance(environment_id, str) and bool(re.fullmatch(r"environment_[A-Za-z0-9_-]+", environment_id)),
               "DEPLOYED_ENVIRONMENT_ID_INVALID")
        environment = ROOT / "artifacts/lab/genai-support-runtime" / environment_id
        demand(all(not path.is_symlink() for path in (environment, *environment.parents)), "DEPLOYED_REFERENCE_SCOPE_MISMATCH")
        for key, filename in (("runtime_ref", "runtime.json"), ("source_manifest_ref", "source-manifest.json"),
                              ("connections_ref", "connections.private.json")):
            reference = Path(sut[key])
            demand(reference == environment / filename and not reference.is_symlink(), "DEPLOYED_REFERENCE_SCOPE_MISMATCH")
        source = load_runtime(Path(sut["source_manifest_ref"]))
        demand(source["files"] == self.manifest["candidate"]["files"] and sut["candidate_sha"] == self.manifest["candidate"]["candidate_sha"],
               "DEPLOYED_CANDIDATE_SOURCE_MISMATCH")
        check_product(self.manifest["candidate"])
        host = load_host()
        runtime = load_runtime(Path(sut["runtime_ref"]))
        demand(runtime.get("instance_id") == environment_id and
               runtime.get("endpoints", {}).get("support-gateway") == sut["base_url"], "DEPLOYED_ENDPOINT_INSTANCE_MISMATCH")
        for role in ("api", "worker", "relay", "gateway", "model"):
            process = runtime["processes"][role]
            signature = host.process_signature(process["pid"])
            demand(signature == process["signature"], "DEPLOYED_PROCESS_IDENTITY_CHANGED")
            # PID 签名相等只证明仍是同一进程；还要证明角色实际由本实例的配置启动。
            expected_tail = f" -m genai_support.runtime --role {role} --instance {environment_id} --config {environment / (role + '.private.json')}"
            demand(isinstance(signature, str) and signature.endswith(expected_tail)
                   and process.get("config_ref") == role + ".private.json", "DEPLOYED_PROCESS_SCOPE_MISMATCH")
        frozen_config = load_runtime(environment / "integration.private.json")
        demand(sha(frozen_config) == sut["integration_config_sha256"], "DEPLOYED_CONFIGURATION_CHANGED")
        return sut

    def run(self, *, interrupt_after: str | None = None):
        """恢复时重放已完成步骤的原回执；新观察使用新操作键，不重发原事件。"""
        sut = self.verify_deployment()
        pack = SourceObjects(self.directory)
        native = pack.native
        transport = []
        runtime = pack.runtime
        clients = {tenant: Api(sut["base_url"], {"Authorization": "Bearer " + access_token(runtime, tenant)},
                              service="sut", receipts=transport)
                   for tenant in {case["tenant_id"] for case in native["bindings"]["cases"]}}
        cw = Api(runtime["chatwoot"]["base_url"], {"api_access_token": runtime["chatwoot"]["api_token"]}, service="chatwoot", receipts=transport)
        lago = Api(runtime["lago"]["base_url"], {"Authorization": "Bearer " + runtime["lago"]["api_key"]}, service="lago", receipts=transport)
        commercial = Api(pack.ready["base_url"], {"Authorization": "Bearer " + pack.private["read_token"]}, service="commercial", receipts=transport)
        connections = load_runtime(Path(sut["connections_ref"]))
        def audit_scalar(query, parameters=()):
            with psycopg.connect(connections["database_url"], connect_timeout=3,
                    options="-c default_transaction_read_only=on -c statement_timeout=4000") as conn:
                row = conn.execute(query, parameters).fetchone()
                return row[0] if row else None
        cases = []
        for native_case in native["bindings"]["cases"]:
            case = {**native_case, "experiment_id": self.experiment_id,
                    "candidate_sha": self.manifest["candidate"]["candidate_sha"]}
            label = case["label"]
            client = clients[case["tenant_id"]]
            observer = LateUsageObserver(client, cw, lago, commercial, audit_scalar,
                                        cw_prefix=f"/api/v1/accounts/{case['account_id']}")
            body = {key: case[key] for key in ("tenant_id", "conversation_id", "invoice_id")}
            body["operation_key"] = self.experiment_id + ":" + label + ":submit"
            def http(dispatch, method, path, value):
                response = client.request(method, path, value, expected=(200, 202, 400, 403, 404, 409, 422, 500, 502, 503))
                receipt = transport[-1]
                dispatch.record_sent(receipt)
                # 错误受理也保留原HTTP与正文；没有Job并不证明服务端从未产生效果。
                return {"status": receipt["status"], "response": response, "transport": receipt}
            accepted = self.step(label + ".submit", "SUBMIT_ASSESSMENT", "WRITE", body,
                lambda dispatch: http(dispatch, "POST", "/integrations/disputes", body))
            submission = observer.observe_submission(accepted)
            if accepted["status"] != 202 or not isinstance(accepted["response"].get("job_id"), str):
                cases.append({"label": label, "status": "ADMISSION_UNRESOLVED", "submission": submission})
                continue
            j1 = accepted["response"]["job_id"]
            before = self.wait_job(observer, case, j1, "j1")
            initial_verdict = assess_observation(case, before, stage="initial")
            if before.get("job_state") != "DONE" or before.get("delivery_state") != "COMPLETE" \
                    or initial_verdict.get("status") == "INCOMPLETE":
                cases.append({"label": label, "status": "J1_INCOMPLETE", "observation": before, "verdict": initial_verdict})
                continue
            dispatched = self.step(label + ".dispatch", "SEND_REGISTERED_USAGE", "WRITE", {"label": label},
                lambda dispatch: self.source_step(dispatch, "dispatch_usage", label))
            if interrupt_after == label + ".dispatch":
                # 在账本已提交之后真正退出进程；下一次CLI必须复用COMPLETED，不依赖内存。
                save_private(self.directory / "interruption.private.json", {"experiment_id": self.experiment_id,
                    "operation_key": label + ".dispatch", "at": datetime.now(timezone.utc).isoformat(),
                    "pid": os.getpid(), "exit_code": 75, "boundary": "AFTER_COMMITTED_EVENT_SEND_BEFORE_AGGREGATE_OBSERVATION"})
                print(json.dumps({"status": "INTERRUPTED_AFTER_COMMIT", "operation_key": label + ".dispatch"}), flush=True)
                os._exit(75)
            aggregate_prefix = label + ".aggregate."
            aggregate_ops = [row for row in self.ledger.list_operations(self.experiment_id) if row["operation_key"].startswith(aggregate_prefix)]
            visible = next((row for row in aggregate_ops if row["status"] == "COMPLETED" and
                            (row.get("result") or {}).get("status") == "AGGREGATE_VISIBLE"), None)
            observed = self.step(visible["operation_key"], "OBSERVE_NATIVE_AGGREGATE", "READ", visible["request"],
                                 lambda dispatch: None) if visible else None
            start = len(aggregate_ops) + 1
            for attempt in range(start, min(start + 30, 91)) if observed is None else []:
                observation_id = label + "-aggregate-" + str(attempt)
                observed = self.step(label + ".aggregate." + str(attempt), "OBSERVE_NATIVE_AGGREGATE", "READ", {"observation_id": observation_id},
                    lambda dispatch, observation_id=observation_id: SourceObjects(self.directory).observe_usage(label, observation_id=observation_id))
                if observed["status"] == "AGGREGATE_VISIBLE":
                    break
                time.sleep(1)
            demand(observed is not None and observed["status"] == "AGGREGATE_VISIBLE", "AGGREGATE_OBSERVATION_BUDGET_EXHAUSTED")
            observation_id = observed["observation_id"]
            for action, method in (("receive", "record_usage_received"), ("reconcile", "reconcile_usage")):
                self.step(label + "." + action, action.upper() + "_REGISTERED_USAGE", "WRITE", {"observation_id": observation_id},
                    lambda dispatch, method=method: self.source_step(dispatch, method, label, observation_id=observation_id))
            sealed = self.step(label + ".seal", "SEAL_COVERAGE", "WRITE", {"label": label},
                               lambda dispatch: self.source_step(dispatch, "seal_usage", label))
            retry = self.step(label + ".retry", "RETRY_ORIGINAL_ASSESSMENT", "WRITE", body,
                lambda dispatch: http(dispatch, "POST", "/integrations/disputes", body))
            reevaluate_body = {"operation_key": self.experiment_id + ":" + label + ":reevaluate", "reason": "Registered usage arrived; explicitly reassess new facts."}
            reevaluated = self.step(label + ".reevaluate", "EXPLICIT_REEVALUATION", "WRITE", {"parent": j1, **reevaluate_body},
                lambda dispatch: http(dispatch, "POST", f"/integrations/disputes/{case['tenant_id']}/{j1}/reevaluate", reevaluate_body))
            if reevaluated["status"] != 202 or not isinstance(reevaluated["response"].get("job_id"), str):
                cases.append({"label": label, "status": "REEVALUATION_UNRESOLVED", "receipt": reevaluated,
                              "j1": j1, "before": before, "initial_verdict": initial_verdict})
                continue
            j2 = reevaluated["response"]["job_id"]
            after = self.wait_job(observer, case, j2, "j2")
            if after.get("job_state") != "DONE" or after.get("delivery_state") != "COMPLETE":
                cases.append({"label": label, "status": "J2_INCOMPLETE", "observation": after,
                              "verdict": assess_observation(case, after, stage="after_seal"), "initial_verdict": initial_verdict,
                              "j1": j1, "j2": j2, "before": before})
                continue
            old_after = self.step(label + ".observe.old", "READ_BACK_OLD_SNAPSHOT", "READ", {"job_id": j1},
                                  lambda dispatch: observer.observe(case, j1))
            final_verdict = assess_observation(case, after, stage="after_seal")
            transition = assess_transition(before, after, old_after, [retry], reevaluated)
            cases.append({"label": label, "status": "OBSERVED", "j1": j1, "j2": j2, "before": before,
                          "after": after, "old_after": old_after, "initial_verdict": initial_verdict,
                          "final_verdict": final_verdict, "transition": transition,
                          "dispatch": dispatched, "aggregate": observed, "seal": sealed})
        # 已观察的失败不能被另一阶段未完成掩盖；初态文案提示也属于原实验结果。
        summary = summarize_execution(cases)
        scoped = summary["scoped_result"]
        report = {"schema_version": 1, "scope": SCOPE, "experiment_id": self.experiment_id,
                  "scoped_result": scoped,
                  "candidate": self.manifest["candidate"], "deployment": sut, "cases": cases,
                  **summary, "replayed_operations_this_process": self.replays,
                  "ledger": self.ledger.resume_summary(self.experiment_id), "transport_receipts": transport,
                  "agent_autonomous_success": False, "agent_dispatch_authorized": False,
                  "protected_final": False, "release_authorized": False,
                  "model_semantic_quality": "NOT_FULLY_EVALUATED"}
        save_private(self.directory / "execution-result.private.json", report)
        return {"status": "OBSERVATIONS_RECORDED", "experiment_id": self.experiment_id,
                "scoped_result": scoped,
                "cases": [{"label": c["label"], "status": c["status"]} for c in cases],
                "agent_autonomous_success": False, "scope": SCOPE}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "deploy", "run", "status"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--ledger-dsn-file", type=Path, required=True)
    parser.add_argument("--candidate")
    parser.add_argument("--run-id")
    parser.add_argument("--period-start")
    parser.add_argument("--model", default="qwen2:7b")
    parser.add_argument("--interrupt-after", choices=("late_usage.dispatch",))
    args = parser.parse_args()
    try:
        directory = checked_directory(args.directory)
        demand(directory.is_dir(), "COMMERCIAL_SOURCE_PREPARATION_REQUIRED")
        with (directory / ".execution.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            execution = ExecutionSlice(directory, args.ledger_dsn_file)
            if args.command == "prepare":
                demand(bool(args.candidate and args.run_id and args.period_start), "EXPLICIT_PREPARATION_IDENTITY_REQUIRED")
                result = execution.prepare(args.candidate, args.run_id, args.period_start, args.model)
            elif args.command == "deploy":
                deployed = execution.deploy()
                result = {"status": "DEPLOYED", "environment_id": deployed["environment_id"], "candidate_sha": deployed["candidate_sha"]}
            elif args.command == "run":
                result = execution.run(interrupt_after=args.interrupt_after)
            else:
                result = execution.ledger.resume_summary(execution.experiment_id)
            print(json.dumps(result, ensure_ascii=False))
            return 0
    except Exception as error:
        # 完整HTTP回执留在私有账本；终端不回显数据库错误、凭据或模型正文。
        code = str(error) if isinstance(error, SeedError) else getattr(error, "code", type(error).__name__)
        print(json.dumps({"status": "INCOMPLETE", "error_code": code, "scope": SCOPE}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
