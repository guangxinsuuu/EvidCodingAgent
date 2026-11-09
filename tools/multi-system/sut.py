#!/usr/bin/env python3
"""部署/seed完成后 → 私有集成配置 → 现有support-lab真实异步SUT。

命令prepare不启动服务；up创建新实验，旧实验必须先显式停止/整理。
端点及专用数据库配置引用仅落ignored artifacts，绝不打印口令或token。
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "artifacts/multi-system"
PYTHON = ROOT / "services/genai-support-sut/.venv/bin/python"


def private_save(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def prepare() -> Path:
    """只接受已部署、已seed的原生对象绑定；禁止依赖不存在时退回fixture。"""
    runtime = json.loads((ARTIFACTS / "runtime.json").read_text())
    bindings = json.loads((ARTIFACTS / "bindings.json").read_text())
    if not bindings.get("customers") or not bindings.get("cases"):
        raise RuntimeError("ENV_NOT_READY")
    config = {key: runtime[key] for key in ("chatwoot", "lago", "keycloak")}
    # 配置中删除仅部署器/seed才需要的管理员与测试用户密码。
    config["chatwoot"] = {key: config["chatwoot"][key] for key in ("base_url", "api_token", "account_id")}
    config["lago"] = {key: config["lago"][key] for key in ("base_url", "api_key")}
    config["keycloak"] = {key: config["keycloak"][key] for key in ("issuer", "client_id", "client_secret", "audience")}
    config.update(bindings=bindings, automatic_writeback=True, qualification="LOCAL_CROSS_SYSTEM_INTEGRATION")
    path = ARTIFACTS / "integration.private.json"
    private_save(path, config)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "up", "status"))
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare()
            print(json.dumps({"status": "PREPARED", "config_ref": "artifacts/multi-system/integration.private.json"}))
            return 0
        if args.command == "up":
            config = prepare()
            command = [str(PYTHON), "tools/support-lab.py", "up", "--mode", "live", "--model", "qwen2:7b",
                       "--max-calls", "24", "--integration-config", str(config)]
            result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
            if result.returncode:
                sys.stderr.write(result.stderr)
                return result.returncode
            state = json.loads(result.stdout)
            environment = state["environment_instance_id"]
            directory = ROOT / "artifacts/lab/genai-support-runtime" / environment
            private_save(ARTIFACTS / "sut.json", {"base_url": state["endpoints"]["support-gateway"],
                "environment_id": environment, "connections_ref": str(directory / "connections.private.json"),
                "runtime_ref": str(directory / "runtime.json"), "qualification": "LOCAL_CROSS_SYSTEM_INTEGRATION"})
            print(json.dumps({"status": "RUNNING", "base_url": state["endpoints"]["support-gateway"], "environment_id": environment}))
            return 0
        return subprocess.run([str(PYTHON), "tools/support-lab.py", "status"], cwd=ROOT).returncode
    except (OSError, ValueError, KeyError, RuntimeError):
        print(json.dumps({"status": "ENV_NOT_READY", "error_code": "INTEGRATION_PREPARATION_REQUIRED"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
