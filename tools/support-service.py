#!/usr/bin/env python3
"""pnpm setup/test/dev:support → 此入口 → 独立Python环境 → 服务或unittest。

只有setup允许下载安装依赖；test/start绝不隐式安装或改锁。依赖安装只继承
必要系统环境，不把模型/发布凭据交给安装进程。src放到PYTHONPATH而非全局安装。
"""

import argparse
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "services/genai-support-sut"
TOOLING = ROOT / "tmp/genai-tooling"
PYTHON = SERVICE / ".venv/bin/python"
UV_VERSION = "0.13.7"
SYSTEM_ENVIRONMENT = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT"}


def runtime_environment(action: str, source: dict[str, str]) -> dict[str, str]:
    """按执行用途传能力，避免SUT继承父进程模型key/Git token或Python注入配置。

    本机白名单不是容器隔离：后续候选执行仍需无凭据挂载及受控网络。
    test生成自己的临时会话；仅start接收显式客服会话配置。
    """
    allowed = set(SYSTEM_ENVIRONMENT)
    if action == "start":
        allowed.update({"EGAP_SUPPORT_API_TOKEN", "EGAP_SUPPORT_TENANT_ID", "EGAP_SUPPORT_HOST", "EGAP_SUPPORT_PORT"})
    elif action == "test":
        allowed.add("EGAP_SUPPORT_TEST_EVIDENCE")
    environment = {key: value for key, value in source.items() if key in allowed}
    environment["PYTHONPATH"] = str(SERVICE / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def run(command: list[str], *, environment: dict[str, str], cwd: Path = ROOT) -> None:
    """保留子命令退出码；不shell拼接，不打印环境或命令中不存在的凭据。"""
    subprocess.run(command, cwd=cwd, env=environment, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "test", "start", "fixtures"))
    args = parser.parse_args()
    try:
        if args.action == "setup":
            expected = (SERVICE / ".python-version").read_text().strip()
            if ".".join(map(str, sys.version_info[:3])) != expected:
                raise ValueError("PYTHON_VERSION_MISMATCH")
            clean = {key: value for key, value in os.environ.items() if key in SYSTEM_ENVIRONMENT}
            if not (TOOLING / "bin/python").exists():
                run([sys.executable, "-m", "venv", str(TOOLING)], environment=clean)
            run([str(TOOLING / "bin/python"), "-m", "pip", "--isolated", "install",
                 "--disable-pip-version-check", "--no-cache-dir", "--index-url", "https://pypi.org/simple",
                 "--only-binary=:all:", "uv==" + UV_VERSION], environment=clean)
            run([str(TOOLING / "bin/uv"), "sync", "--locked", "--project", str(SERVICE),
                 "--python", sys.executable, "--no-managed-python", "--cache-dir", str(ROOT / "tmp/uv-cache")],
                environment=clean)
            return 0
        if not PYTHON.exists():
            raise ValueError("SUPPORT_ENV_MISSING_RUN_SETUP_SUPPORT")
        environment = runtime_environment(args.action, dict(os.environ))
        if args.action == "test":
            run([str(PYTHON), "-m", "unittest", "discover", "-s", str(SERVICE / "tests"), "-v"],
                environment=environment)
        elif args.action == "start":
            # exec直接交接进程身份，Ctrl-C/SIGTERM不会只停包装进程而遗留服务器。
            os.execve(PYTHON, [str(PYTHON), "-m", "genai_support.main"], environment)
        else:
            code = (
                "import json; from genai_support.config import DEFAULT_FIXTURE_PATH; "
                "from genai_support.fixtures import load_fixture_bundle; "
                "b=load_fixture_bundle(DEFAULT_FIXTURE_PATH); "
                "print(json.dumps({'bundle_id':b.bundle_id,'revision':b.revision,"
                "'sha256':b.content_sha256,'clock':b.clock,"
                "'scope':'synthetic-draft-calibration','business_verification':'not_performed'},indent=2))"
            )
            run([str(PYTHON), "-c", code], environment=environment)
        return 0
    except subprocess.CalledProcessError as error:
        return error.returncode if error.returncode > 0 else 1
    except (OSError, ValueError):
        print("SUPPORT_COMMAND_FAILED: check pinned Python, setup and command prerequisites", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
