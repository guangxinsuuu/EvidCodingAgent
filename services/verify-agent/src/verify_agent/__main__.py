"""启动入口：python -m verify_agent；仅凭 scoped token 驱动既有 Run。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys

from .graph import run_agent
from .protocol import ROOT
from .runtime_port import HttpRuntimePort


def _read_private_token(path: Path) -> str:
    """只读 0600 普通文件；不跟随 token 符号链接，也不阻塞在伪装成文件的 FIFO。"""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("PRIVATE_REGULAR_TOKEN_FILE_REQUIRED")
        if metadata.st_size > 4098:
            raise ValueError("TOKEN_FILE_TOO_LARGE")
        content = stream.read(4099)
        if len(content) > 4098:
            raise ValueError("TOKEN_FILE_TOO_LARGE")
        return content.strip()


def _open_new_report(path: Path) -> tuple[int, Path]:
    """只在固定仓库 artifacts/tmp 中独占创建 JSON；不获得冻结 docs/README 写权限。

按目录 fd 逐层 openat + O_NOFOLLOW，而非先 resolve 后普通 open，防止父目录
符号链接或检查后替换把报告写到许可范围之外。不存在的目录创建为 0700。
"""
    destination = Path(os.path.abspath(path))
    try:
        relative = destination.relative_to(ROOT)
    except ValueError:
        raise ValueError("PRIVATE_REPORT_PATH_REQUIRED") from None
    if (len(relative.parts) < 2 or relative.parts[0] not in {"artifacts", "tmp"}
            or destination.suffix != ".json"):
        raise ValueError("PRIVATE_JSON_REPORT_PATH_REQUIRED")
    parent = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in relative.parts[:-1]:
            try:
                os.mkdir(component, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        descriptor = os.open(relative.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             mode=0o600, dir_fd=parent)
        return descriptor, destination
    finally:
        os.close(parent)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the bounded EGAP V0.1 verification planner")
    parser.add_argument("--runtime", required=True, help="固定本地 Runtime URL")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--token-file", type=Path, required=True, help="0600 普通文件内的 scoped token，不是模型输入")
    parser.add_argument("--output", type=Path, required=True, help="仓库 artifacts/ 或 tmp/ 下新的 JSON 报告；不覆盖旧证据")
    parser.add_argument("--max-iterations", type=int, default=32)
    args = parser.parse_args(argv)
    try:
        token = _read_private_token(args.token_file)
        port = HttpRuntimePort(args.runtime, token)
        # 先独占创建输出，避免做完模型工作才发现报告将覆盖现有文件。
        descriptor, destination = _open_new_report(args.output)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            report = run_agent(port, args.run_id, max_iterations=args.max_iterations)
            json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
        print(json.dumps({"run_id": report["run_id"], "outcome": report["outcome"],
                          "business_verdict": report["business_verdict"], "report": str(destination)}, ensure_ascii=False))
        return 0 if report["outcome"] in {"PLANNED", "STOPPED"} else 2
    except (OSError, ValueError) as error:
        # 只暴露类别，避免路径/请求/凭据混入 traceback；详细业务失败已在 report。
        print(json.dumps({"status": "AGENT_START_FAILED", "error_type": type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
