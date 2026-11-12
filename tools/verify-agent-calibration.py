#!/usr/bin/env python3
"""为 Verify Agent 生成两份明确合成的 Git 发布版本对；不运行 SUT 或模型。

入口 → 固定 HEAD 的 Git blob → AST 提取实际函数/表达式 → 隔离 Git 基线和候选
→ release_assembler → 两份只读发布材料。人工变化说明只在独立 manifest 中保存，
不放进发布 goal、候选正文或模型上下文；模型必须从实际 diff 和规则形成自己的提案。
生成的源码仅为校准小程序，不能声称新真实 PR、真实业务缺陷或完整 SUT 回归。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.registry.reference_resolver import LocalGitReader, MaterialError  # noqa: E402
from tools.registry.release_assembler import assemble_release  # noqa: E402


TOOLS_PATH = "services/genai-support-sut/src/genai_support/business_tools.py"
COMMERCIAL_PATH = "services/genai-support-sut/src/genai_support/integrations/commercial.py"
GOAL = ("客服团队提交发布候选；请从准确版本差异出发，主动读取适用的续费退款争议规则和相关代码，"
        "提出最有价值的验证检查及未决信息。保持业务事实、政策时点和私有备注边界；阅读不代表业务 PASS。")
HEADER = ('"""CALIBRATION_SYNTHETIC：固定真实源码的最小摘录/合成调用者。\n'
          '这不是完整客服系统，也不是一份真实产品 PR。仅供版本分析 Agent 的隔离校准。\n"""\n\n')


def _private_json(path: Path, value: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _git(root: Path, *arguments: str) -> str:
    """仅对新建的临时仓库写 Git 对象；禁用用户 hooks、签名、外部协议和 fsmonitor。"""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1", GIT_NO_REPLACE_OBJECTS="1",
                       GIT_AUTHOR_NAME="EGAP synthetic calibration", GIT_AUTHOR_EMAIL="calibration@example.invalid",
                       GIT_COMMITTER_NAME="EGAP synthetic calibration", GIT_COMMITTER_EMAIL="calibration@example.invalid",
                       GIT_AUTHOR_DATE="2026-11-12T00:00:00Z", GIT_COMMITTER_DATE="2026-11-12T00:00:00Z")
    try:
        result = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull,
                                 "-c", "commit.gpgsign=false", "-c", "core.fsmonitor=false",
                                 "-c", "protocol.allow=never", *arguments], cwd=root, env=environment,
                                capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        raise MaterialError("CALIBRATION_GIT_FAILED") from None
    if result.returncode:
        raise MaterialError("CALIBRATION_GIT_FAILED")
    return result.stdout.decode("utf-8").strip()


def _function(source: str, symbol: str) -> str:
    """只解析 AST，绝不 import 或执行候选业务代码。"""
    matches = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == symbol]
    if len(matches) != 1:
        raise MaterialError("CALIBRATION_SOURCE_SYMBOL_CHANGED")
    return ast.get_source_segment(source, matches[0]) + "\n"


def _pair(directory: Path, path: str, baseline: str, candidate: str) -> tuple[str, str]:
    directory.mkdir(mode=0o700)
    _git(directory, "init", "--quiet", "--initial-branch=calibration")
    location = directory / path
    location.parent.mkdir(parents=True)
    commits = []
    for kind, content in (("baseline", baseline), ("candidate", candidate)):
        # 编译仅检查语法，不执行脚本；生成失败不会伪造成可用校准任务。
        ast.parse(content)
        location.write_text(content, encoding="utf-8")
        _git(directory, "add", "--", path)
        _git(directory, "commit", "--quiet", "-m", "Explicit synthetic " + kind)
        commits.append(_git(directory, "rev-parse", "HEAD"))
    return commits[0], commits[1]


def generate(output_directory: Path, source_commit: str | None = None) -> dict:
    """创建新的校准目录，不覆盖旧证据，不修改原 SUT、docs、README 或原仓库历史。

    同一固定源提交会产生相同合成 commit。output_directory 必须位于项目 artifacts/tmp
    或系统临时目录；本函数没有数据库或网络调用，也不能把生成包变成执行许可。
    """
    output = Path(output_directory).resolve()
    allowed = [ROOT / "artifacts", ROOT / "tmp", Path(tempfile.gettempdir()).resolve(), Path("/private/tmp")]
    if not any(output.is_relative_to(parent.resolve()) and output != parent.resolve() for parent in allowed):
        raise MaterialError("CALIBRATION_OUTPUT_OUTSIDE_TEMP_SCOPE")
    if output.exists():
        raise MaterialError("CALIBRATION_OUTPUT_ALREADY_EXISTS")
    git = LocalGitReader(ROOT)
    commit = source_commit or git._git("rev-parse", "HEAD").decode("ascii").strip()
    tools_raw = git.read(commit, TOOLS_PATH)
    commercial_raw = git.read(commit, COMMERCIAL_PATH)
    source = tools_raw.decode("utf-8")
    baseline_projection = (HEADER + "from collections.abc import Mapping\nfrom typing import Any\n\n"
                           + _function(source, "plain") + "\n" + _function(source, "model_reply_context")
                           + '\n\ndef compose_reply_input(context: dict, decision: dict) -> dict:\n'
                           + '    """合成调用者 → model_reply_context；结果只作为文案模型输入。"""\n'
                           + '    return model_reply_context(context, decision)\n')
    anchor = '    result["decision"] = plain(decision)'
    if baseline_projection.count(anchor) != 1:
        raise MaterialError("CALIBRATION_PROJECTION_ANCHOR_CHANGED")
    candidate_projection = baseline_projection.replace(anchor,
        '    result["decision"] = {key: value for key, value in plain(decision).items()\n'
        '                          if key not in {"reason_codes", "missing_evidence"}}')
    commercial = commercial_raw.decode("utf-8")
    comparisons = [node.value for node in ast.walk(ast.parse(commercial))
                   if isinstance(node, ast.keyword) and node.arg == "within_window" and isinstance(node.value, ast.Compare)]
    if len(comparisons) != 1:
        raise MaterialError("CALIBRATION_WINDOW_ANCHOR_CHANGED")
    expression = ast.get_source_segment(commercial, comparisons[0])
    if expression != '0 <= elapsed <= terms["window_seconds"]':
        raise MaterialError("CALIBRATION_WINDOW_ANCHOR_CHANGED")
    baseline_window = (HEADER + 'from datetime import datetime\n\n\n'
        + 'def within_refund_window(renewed_at: str, claimed_at: str, window_seconds: int) -> bool:\n'
        + '    """合成入口：保留固定版本商业判断中的经过秒数和窗口比较表达式。\n\n'
        + '    claimed_at 是冻结的原申请时点；补充消息不在此入口重置它。\n'
        + '    仅测试比较表达式，不代表合同、付款、用量或整条业务已验收。\n'
        + '    """\n'
        + '    renewed = datetime.fromisoformat(renewed_at)\n'
        + '    claimed = datetime.fromisoformat(claimed_at)\n'
        + '    elapsed = (claimed - renewed).total_seconds()\n'
        + '    terms = {"window_seconds": window_seconds}\n'
        + '    return ' + expression + '\n')
    candidate_window = baseline_window.replace('0 <= elapsed <= terms["window_seconds"]', '0 <= elapsed < terms["window_seconds"]')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(mode=0o700)
    manifest = {"schema_version": 1, "input_provenance": "CALIBRATION_SYNTHETIC",
                "source_commit": commit, "not_a_real_product_pr": True,
                "source_blobs": [{"path": path, "sha256": hashlib.sha256(raw).hexdigest()}
                                 for path, raw in ((TOOLS_PATH, tools_raw), (COMMERCIAL_PATH, commercial_raw))],
                "cases": []}
    for name, path, old, new, intervention in (
        ("projection", TOOLS_PATH, baseline_projection, candidate_projection,
         "人工删除实际决定对象的 reason_codes 与 missing_evidence 投影；此说明不进入模型任务。"),
        ("window", COMMERCIAL_PATH, baseline_window, candidate_window,
         "人工将窗口上界 <= 改为 <；此说明不进入模型任务。"),
    ):
        candidate_root = output / (name + "-repository")
        baseline, candidate = _pair(candidate_root, path, old, new)
        bundle = assemble_release(ROOT, baseline, candidate, GOAL, candidate_root=candidate_root,
                                  input_provenance="CALIBRATION_SYNTHETIC")
        bundle_path = output / (name + "-bundle.json")
        _private_json(bundle_path, bundle)
        manifest["cases"].append({"name": name, "baseline_sha": baseline, "candidate_sha": candidate,
                                  "bundle_path": str(bundle_path), "synthetic_intervention": intervention,
                                  "expected_analysis_domain": "决定到回复的事实传递" if name == "projection" else "政策窗口和原申请时间边界"})
    _private_json(output / "calibration-manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="创建明确合成的 Verify Agent 校准版本对，不运行实验")
    parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument("--source-commit")
    args = parser.parse_args()
    try:
        manifest = generate(args.output_directory, args.source_commit)
    except (MaterialError, OSError, ValueError) as error:
        print(json.dumps({"status": "REJECTED", "error_code": getattr(error, "code", "CALIBRATION_GENERATION_FAILED")}))
        return 1
    print(json.dumps({"status": "CALIBRATION_SYNTHETIC_CREATED", "source_commit": manifest["source_commit"],
                      "bundle_paths": [case["bundle_path"] for case in manifest["cases"]]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
