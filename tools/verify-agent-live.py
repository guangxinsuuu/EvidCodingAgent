#!/usr/bin/env python3
"""可复现的本地 Verify Agent 校准启动器，不导入或启动被测业务。

operator → 固定发布材料包 → 自有/借用 Ollama → TS PostgreSQL Runtime →
Python LangGraph → 本 run 的只读账本导出 → 精确回收本启动器拥有的进程。
本工具不 pull 模型、不启动云端推理、不生成 PASS、不删除数据库或历史证据。
数据库连接文件必须指向操作者为这次验证准备的专用 PostgreSQL 数据库。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
VERIFY_SERVICE = ROOT / "services/verify-agent"
RUNTIME_PACKAGE = ROOT / "packages/verify-runtime"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

# 固定程序而非模型生成脚本；凭据从 0600 config 读入，不放 argv/environment。
# 表名在代码中冻结，所有 SQL 值通过参数绑定；只导出准确 run，不扫整库。
EXPORT_LEDGER = r"""
import { readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
const [configPath, outputPath, packagePath] = process.argv.slice(1);
const config = JSON.parse(await readFile(configPath, 'utf8'));
const require = createRequire(packagePath);
const { Pool } = require('pg');
const pool = new Pool({connectionString:config.database_url, max:1, connectionTimeoutMillis:3000});
try {
  const client = await pool.connect();
  try {
    await client.query('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY');
    const runs = await client.query('SELECT id,bundle_digest,state,bundle,created_at FROM verify_runtime.runs WHERE id=$1', [config.run_id]);
    if (runs.rows.length !== 1) throw new Error('RUN_NOT_FOUND');
    const output = {schema_version:1, run_id:config.run_id, exported_at:new Date().toISOString(), scope:'LOCAL_SYNTHETIC_PLANNING', run:runs.rows[0]};
    for (const table of ['calls', 'operations', 'outbox', 'events']) {
      const order = table === 'events' ? 'seq' : 'created_at,id';
      output[table] = (await client.query(`SELECT * FROM verify_runtime.${table} WHERE run_id=$1 ORDER BY ${order}`, [config.run_id])).rows;
    }
    await client.query('COMMIT');
    await writeFile(outputPath, JSON.stringify(output, null, 2)+'\n', {flag:'wx', mode:0o600});
  } finally {client.release();}
} finally {await pool.end();}
"""


class DriverError(RuntimeError):
    """只使用固定错误码，避免连接串和模型正文出现在错误信息里。"""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def private_write(path: Path, value: object, *, text: bool = False) -> None:
    """0600 原子更新本轮账本；输出目录独占创建，因此不会覆盖上一轮证据。"""
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(str(value) if text else json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def clean_environment() -> dict[str, str]:
    """子进程只继承本地运行需要的非凭据项，不继承任意云端模型/trace出口。"""
    environment = {key: os.environ[key] for key in ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL") if key in os.environ}
    environment.update(PYTHONNOUSERSITE="1", LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false")
    return environment


def local_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise DriverError("LOCAL_MODEL_ENDPOINT_REQUIRED")
    return value.rstrip("/")


def read_json(endpoint: str, *, token: str | None = None) -> dict:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(endpoint, headers=headers)
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=3) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise DriverError("READINESS_RESPONSE_TOO_LARGE")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise DriverError("READINESS_RESPONSE_INVALID")
    return value


def wait_model(endpoint: str, process: subprocess.Popen | None, timeout: float = 25) -> dict:
    """只对无副作用的本地 readiness GET 做有界等待，不重试模型推理请求。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise DriverError("OWNED_MODEL_SERVER_EXITED")
        try:
            result = read_json(endpoint + "/api/tags")
            if process is not None and process.poll() is not None:
                raise DriverError("OWNED_MODEL_SERVER_EXITED")
            return result
        except (URLError, HTTPError, OSError):
            time.sleep(0.15)
    raise DriverError("MODEL_READINESS_TIMEOUT")


def wait_runtime(path: Path, process: subprocess.Popen, run_id: str, timeout: float = 25) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise DriverError("RUNTIME_START_FAILED")
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if value.get("run_id") != run_id or value.get("pid") != process.pid:
                    raise DriverError("RUNTIME_READY_IDENTITY_MISMATCH")
                local_endpoint(value["runtime"])
                return value
            except json.JSONDecodeError:
                # Runtime 当前直接写 ready 文件；只允许在写入窗口内重读。
                pass
        time.sleep(0.1)
    raise DriverError("RUNTIME_READINESS_TIMEOUT")


def spawn(command: list[str], log_path: Path, environment: dict[str, str]) -> subprocess.Popen:
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        # 自建会话把本轮 server 和其 model runner 子进程与用户的其他服务隔开。
        return subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.STDOUT, start_new_session=True)


def stop_owned(process: subprocess.Popen, name: str) -> dict:
    """只回收本启动器保存的 Popen 身份和其自建会话，不按名称/端口扫描杀进程。"""
    result = {"name": name, "pid": process.pid, "owned": True, "returncode_before_cleanup": process.poll()}
    if process.poll() is None:
        try:
            # 同时通知 server 与其 runner，防止 server 退出后模型子进程残留。
            if os.getpgid(process.pid) != process.pid:
                raise DriverError("OWNED_PROCESS_GROUP_CHANGED")
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                if process.poll() is None and os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            result["status"] = "STOPPED_OWNED_PROCESS"
        except ProcessLookupError:
            process.wait(timeout=5)
            result["status"] = "ALREADY_EXITED"
    else:
        result["status"] = "ALREADY_EXITED"
    result["returncode"] = process.returncode
    return result


def export_ledger(node: str, config: Path, directory: Path) -> None:
    """读取真实 PostgreSQL 的本 run 记录；导出失败必须在 summary 标为缺口。"""
    result = subprocess.run([node, "--input-type=module", "-e", EXPORT_LEDGER, str(config),
                             str(directory / "full-evidence.json"), str(RUNTIME_PACKAGE / "package.json")],
                            cwd=ROOT, env=clean_environment(), capture_output=True, timeout=20, check=False)
    if result.returncode != 0:
        # pg 错误可能带连接信息，绝不写入公开 summary；证据状态明确标记未导出。
        raise DriverError("LEDGER_EXPORT_FAILED")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a bounded local Verify Agent calibration and preserve real evidence")
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True, help="artifacts/ 或 tmp/ 下新的独占目录")
    parser.add_argument("--database-url-file", type=Path, required=True, help="0600 专用数据库连接文件")
    parser.add_argument("--model", default="qwen2:7b")
    parser.add_argument("--model-endpoint", help="借用既有回环 Ollama；本工具不会停止它")
    parser.add_argument("--max-model-calls", type=int, default=6)
    parser.add_argument("--max-reads", type=int, default=4)
    parser.add_argument("--model-timeout-seconds", type=int, default=180)
    parser.add_argument("--max-output-tokens", type=int, default=2000)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    directory = args.output_directory.resolve()
    summary: dict = {"schema_version": 1, "run_id": str(uuid4()), "started_at": now(), "status": "PREPARING",
                     "scope": "LOCAL_SYNTHETIC_PLANNING", "business_verdict": "NOT_EVALUATED",
                     "ledger_export": "NOT_ATTEMPTED", "cleanup": [], "processes": {}}
    owned: list[tuple[str, subprocess.Popen]] = []
    directory_created = False
    runtime_ready = False
    config_path = directory / "runtime-config.json"
    node = shutil.which("node")
    return_code = 2
    try:
        if not any(directory.is_relative_to(ROOT / parent) for parent in ("artifacts", "tmp")):
            raise DriverError("PRIVATE_ARTIFACT_DIRECTORY_REQUIRED")
        if not args.bundle.is_file() or args.bundle.is_symlink():
            raise DriverError("REGULAR_RELEASE_BUNDLE_REQUIRED")
        if (not args.database_url_file.is_file() or args.database_url_file.is_symlink()
                or args.database_url_file.stat().st_mode & 0o077):
            raise DriverError("PRIVATE_DATABASE_URL_FILE_REQUIRED")
        if (not 1 <= args.max_model_calls <= 12 or not 1 <= args.max_reads <= 8
                or not 1 <= args.model_timeout_seconds <= 180 or not 128 <= args.max_output_tokens <= 3000):
            raise DriverError("INVALID_LOCAL_BUDGET")
        if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,160}", args.model):
            raise DriverError("INVALID_MODEL_NAME")
        python = VERIFY_SERVICE / ".venv/bin/python"
        if not node or not python.is_file() or not (RUNTIME_PACKAGE / "dist/main.js").is_file():
            raise DriverError("BUILD_AND_VERIFY_ENVIRONMENT_REQUIRED")
        # 仅检查连接字符串类型；Runtime/pg 决定实际连通性，日志不输出 URL。
        database_url = args.database_url_file.read_text(encoding="utf-8").strip()
        if urlsplit(database_url).scheme not in {"postgres", "postgresql"}:
            raise DriverError("POSTGRESQL_URL_REQUIRED")
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        directory_created = True
        private_write(directory / "summary.json", summary)
        bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
        private_write(directory / "release-bundle.json", bundle)
        environment = clean_environment()

        model_process = None
        if args.model_endpoint:
            endpoint = local_endpoint(args.model_endpoint)
        else:
            binary = shutil.which("ollama")
            models = Path.home() / ".ollama/models"
            if not binary or not models.is_dir():
                raise DriverError("INSTALLED_LOCAL_OLLAMA_REQUIRED")
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            endpoint = f"http://127.0.0.1:{port}"
            model_environment = {**environment, "OLLAMA_HOST": f"127.0.0.1:{port}",
                                 "OLLAMA_MODELS": str(models), "OLLAMA_NO_CLOUD": "1"}
            model_process = spawn([binary, "serve"], directory / "ollama.log", model_environment)
            owned.append(("ollama", model_process))
            summary["processes"]["ollama"] = {"pid": model_process.pid, "owned": True}
        tags = wait_model(endpoint, model_process)
        selected = next((row for row in tags.get("models", []) if row.get("name") == args.model), None)
        if not selected or not re.fullmatch(r"[0-9a-f]{64}", selected.get("digest", "")):
            raise DriverError("MODEL_NOT_INSTALLED_NO_AUTOMATIC_DOWNLOAD")
        summary["model"] = {"name": args.model, "digest": selected["digest"], "endpoint": endpoint,
                            "owned_server": model_process is not None}
        private_write(directory / "model-observation.json", summary["model"])
        token = secrets.token_urlsafe(32)
        private_write(directory / "planner-token", token + "\n", text=True)
        config = {"database_url": database_url,
                  "model": {"endpoint": endpoint, "name": args.model, "digest": selected["digest"],
                            "timeoutMs": args.model_timeout_seconds * 1000, "maxOutputTokens": args.max_output_tokens},
                  "prompt_path": str(VERIFY_SERVICE / "planner-prompt.txt"),
                  "bundle_path": str(directory / "release-bundle.json"), "run_id": summary["run_id"],
                  "planner_token": token, "limits": {"model_calls": args.max_model_calls, "reads": args.max_reads, "no_progress": 2},
                  "port": 0, "ready_path": str(directory / "runtime-ready.json")}
        private_write(config_path, config)
        runtime = spawn([node, str(RUNTIME_PACKAGE / "dist/main.js"), "--config", str(config_path)],
                        directory / "runtime.log", environment)
        owned.append(("runtime", runtime))
        summary["processes"]["runtime"] = {"pid": runtime.pid, "owned": True}
        ready = wait_runtime(directory / "runtime-ready.json", runtime, summary["run_id"])
        runtime_ready = True
        read_json(ready["runtime"] + "/runs/" + summary["run_id"], token=token)
        summary.update(status="RUNNING", runtime=ready["runtime"])
        private_write(directory / "summary.json", summary)
        graph = spawn([str(python), "-m", "verify_agent", "--runtime", ready["runtime"], "--run-id", summary["run_id"],
                       "--token-file", str(directory / "planner-token"), "--output", str(directory / "agent-report.json")],
                      directory / "graph.log", {**environment, "PYTHONPATH": str(VERIFY_SERVICE / "src")})
        owned.append(("graph", graph))
        summary["processes"]["graph"] = {"pid": graph.pid, "owned": True}
        private_write(directory / "summary.json", summary)
        # 总执行上限包含每次模型调用及固定端口开销；超时不是“没有发生”。
        graph.wait(timeout=args.max_model_calls * (args.model_timeout_seconds + 15) + 60)
        report_path = directory / "agent-report.json"
        if not report_path.is_file():
            raise DriverError("AGENT_REPORT_MISSING")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        projection = read_json(ready["runtime"] + "/runs/" + summary["run_id"], token=token)
        private_write(directory / "final-projection.json", projection)
        complete = graph.returncode == 0 and report.get("outcome") == "PLANNED"
        summary.update(status="PLANNED" if complete else "INCOMPLETE", agent_outcome=report.get("outcome"), agent_exit_code=graph.returncode,
                       runtime_status=projection.get("status"), task_identity=projection.get("task_identity"),
                       budgets=projection.get("budgets"), model_call_count=len(projection.get("model_calls", [])),
                       operation_count=len(projection.get("operations", [])), final_eligibility=projection.get("final_eligibility"))
        return_code = 0 if complete else 2
    except KeyboardInterrupt:
        summary.update(status="INTERRUPTED", error_code="OPERATOR_INTERRUPTED")
    except subprocess.TimeoutExpired:
        summary.update(status="FAILED", error_code="PROCESS_TIMEOUT_OUTCOME_UNCONFIRMED")
    except (DriverError, OSError, ValueError, URLError) as error:
        summary.update(status="FAILED", error_code=str(error) if isinstance(error, DriverError) else type(error).__name__)
    finally:
        if directory_created:
            # graph 已经结束或失败；先回收，避免导出时它仍产生新操作。
            for name, process in reversed(owned):
                try:
                    summary["cleanup"].append(stop_owned(process, name))
                except (OSError, DriverError, subprocess.TimeoutExpired) as error:
                    summary["cleanup"].append({"name": name, "pid": process.pid, "status": "CLEANUP_UNCONFIRMED",
                                               "error_type": type(error).__name__})
                    return_code = 2
            if runtime_ready and node:
                try:
                    export_ledger(node, config_path, directory)
                    summary["ledger_export"] = "EXPORTED_FULL_LOCAL_RUN"
                except (DriverError, OSError, subprocess.TimeoutExpired):
                    summary["ledger_export"] = "FAILED_EVIDENCE_GAP"
                    return_code = 2
            summary["finished_at"] = now()
            summary["exit_code"] = return_code
            private_write(directory / "summary.json", summary)
        print(json.dumps({"run_id": summary["run_id"], "status": summary["status"],
                          "agent_outcome": summary.get("agent_outcome"), "business_verdict": "NOT_EVALUATED",
                          "error_code": summary.get("error_code"), "output_directory": str(directory),
                          "ledger_export": summary["ledger_export"]}, ensure_ascii=False))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
