#!/usr/bin/env python3
"""部署/seed完成后 → 私有集成配置 → 现有support-lab真实异步SUT。

命令prepare不启动服务；up创建新实验，旧实验必须先显式停止/整理。
端点及专用数据库配置引用仅落ignored artifacts，绝不打印口令或token。
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "artifacts/multi-system"
PYTHON = ROOT / "services/genai-support-sut/.venv/bin/python"


def private_save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def commercial_projection(path: Path) -> dict:
    """商业宿主→SUT只读投影；拒绝把管理员配置误当接入配置。

    来源实例/实现摘要/API版本与read token一起进入SUT冻结配置的digest；之后业务
    对象的revision由来源API返回，不通过覆盖这份私有配置制造“原来就存在”的事实。
    """
    if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
        raise RuntimeError("PRIVATE_COMMERCIAL_READER_CONFIG_REQUIRED")
    value = json.loads(path.read_text())
    required = {"base_url", "read_token", "tenant_scopes", "instance_id", "source_digest", "api_version", "authority_scope", "expires_at"}
    allowed = required | {"run_id", "qualification"}
    if not isinstance(value, dict) or not required.issubset(value) or set(value) - allowed:
        raise RuntimeError("COMMERCIAL_READER_PROJECTION_REQUIRED")
    endpoint = urlsplit(value["base_url"])
    if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or not endpoint.port \
            or endpoint.username or endpoint.password or endpoint.path not in {"", "/"} or endpoint.query or endpoint.fragment \
            or not isinstance(value["read_token"], str) or len(value["read_token"]) < 32 \
            or not isinstance(value["source_digest"], str) or re.fullmatch(r"[a-f0-9]{64}", value["source_digest"]) is None \
            or value["authority_scope"] != "LOCAL_LAB" or value["api_version"] != "commercial-source-v1" \
            or not isinstance(value["tenant_scopes"], list) or not value["tenant_scopes"] \
            or any(not isinstance(tenant, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", tenant) is None
                   for tenant in value["tenant_scopes"]):
        raise RuntimeError("COMMERCIAL_READER_CONFIG_INVALID")
    return {key: value[key] for key in required}


def prepare(*, runtime_path: Path | None = None, bindings_path: Path | None = None,
            output_directory: Path | None = None, commercial_config: Path | None = None) -> Path:
    """只接受已部署、已seed的原生绑定；显式目录允许新run保留旧证据。

    CLI默认值保持兼容；新Business Source实验应传三个路径，避免覆盖11月9日输入。
    这里只生成最小权限投影，不启动服务，不把缺失商业来源回落为fixture。
    """
    runtime = json.loads((runtime_path or ARTIFACTS / "runtime.json").read_text())
    bindings = json.loads((bindings_path or ARTIFACTS / "bindings.json").read_text())
    if not bindings.get("customers") or not bindings.get("cases"):
        raise RuntimeError("ENV_NOT_READY")
    config = {key: runtime[key] for key in ("chatwoot", "lago", "keycloak")}
    # 配置中删除仅部署器/seed才需要的管理员与测试用户密码。
    config["chatwoot"] = {key: config["chatwoot"][key] for key in ("base_url", "api_token", "account_id")}
    config["lago"] = {key: config["lago"][key] for key in ("base_url", "api_key")}
    config["keycloak"] = {key: config["keycloak"][key] for key in ("issuer", "client_id", "client_secret", "audience")}
    config.update(bindings=bindings, automatic_writeback=True, qualification="LOCAL_CROSS_SYSTEM_INTEGRATION")
    if commercial_config is not None:
        config["commercial_source"] = commercial_projection(commercial_config)
    path = (output_directory or ARTIFACTS) / "integration.private.json"
    private_save(path, config)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "up", "status"))
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--bindings", type=Path)
    parser.add_argument("--output-directory", type=Path)
    parser.add_argument("--commercial-config", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            config = prepare(runtime_path=args.runtime, bindings_path=args.bindings,
                             output_directory=args.output_directory, commercial_config=args.commercial_config)
            print(json.dumps({"status": "PREPARED", "config_ref": str(config)}))
            return 0
        if args.command == "up":
            config = prepare(runtime_path=args.runtime, bindings_path=args.bindings,
                             output_directory=args.output_directory, commercial_config=args.commercial_config)
            command = [str(PYTHON), "tools/support-lab.py", "up", "--mode", "live", "--model", "qwen2:7b",
                       "--max-calls", "24", "--integration-config", str(config)]
            result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
            if result.returncode:
                sys.stderr.write(result.stderr)
                return result.returncode
            state = json.loads(result.stdout)
            environment = state["environment_instance_id"]
            directory = ROOT / "artifacts/lab/genai-support-runtime" / environment
            private_save((args.output_directory or ARTIFACTS) / "sut.json", {"base_url": state["endpoints"]["support-gateway"],
                "environment_id": environment, "connections_ref": str(directory / "connections.private.json"),
                "runtime_ref": str(directory / "runtime.json"), "qualification": "LOCAL_CROSS_SYSTEM_INTEGRATION"})
            print(json.dumps({"status": "RUNNING", "base_url": state["endpoints"]["support-gateway"], "environment_id": environment}))
            return 0
        return subprocess.run([str(PYTHON), "tools/support-lab.py", "status"], cwd=ROOT).returncode
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        print(json.dumps({"status": "ENV_NOT_READY", "error_code": "INTEGRATION_PREPARATION_REQUIRED"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
