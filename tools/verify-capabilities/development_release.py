#!/usr/bin/env python3
"""公开窗口比较开发样本：完整受信源码 → 合成Git版本对 → 只读发布包。

只复用已经公开的 <= 改 < 干预，供接口开发；不是新的留出集。复制源码从不导入或
执行候选，所有生成文件放在新实验目录。模型任务不携带干预说明或预期输出。
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
from pathlib import Path
import sys
import tempfile

from commercial_fixture import ROOT, SOURCE_PATH, load_contract

sys.path.insert(0, str(ROOT))
from tools.registry.reference_resolver import LocalGitReader, MaterialError
from tools.registry.release_assembler import assemble_release

# 复用既有校准器的受限Git/排他写入工具；该文件只有main才生成实验，不会在import执行候选。
_spec = importlib.util.spec_from_file_location("public_calibration_helpers", ROOT / "tools/verify-agent-calibration.py")
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)


def generate(directory: Path) -> dict:
    """创建完整commercial入口的公开开发版本；返回导航，不生成任何业务执行证据。"""
    output = directory.resolve()
    roots = [ROOT / "artifacts", ROOT / "tmp", Path(tempfile.gettempdir()).resolve(), Path("/private/tmp")]
    if not any(output.is_relative_to(base.resolve()) and output != base.resolve() for base in roots):
        raise MaterialError("DEVELOPMENT_OUTPUT_OUTSIDE_TEMP_SCOPE")
    if output.exists():
        raise MaterialError("DEVELOPMENT_OUTPUT_ALREADY_EXISTS")
    contract = load_contract()
    commit = contract["fixture_provenance"]["source_commit"]
    reader = LocalGitReader(ROOT)
    source = reader.read(commit, SOURCE_PATH).decode("utf-8")
    anchor = 'within_window=0 <= elapsed <= terms["window_seconds"]'
    if source.count(anchor) != 1:
        raise MaterialError("DEVELOPMENT_WINDOW_ANCHOR_CHANGED")
    changed = source.replace(anchor, 'within_window=0 <= elapsed < terms["window_seconds"]')
    ast.parse(source); ast.parse(changed)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(mode=0o700)
    repository = output / "repository"
    repository.mkdir(mode=0o700)
    _helpers._git(repository, "init", "--quiet", "--initial-branch=development")
    # 这些真实依赖也固定到原始commit；本阶段只写Git对象，不启动Python/SUT。
    files = [SOURCE_PATH, "services/genai-support-sut/src/genai_support/__init__.py",
             "services/genai-support-sut/src/genai_support/integrations/__init__.py",
             "services/genai-support-sut/src/genai_support/integrations/http.py"]
    for path in files:
        target = repository / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(reader.read(commit, path))
    _helpers._git(repository, "add", "--", "services")
    _helpers._git(repository, "commit", "--quiet", "-m", "Public development full baseline")
    baseline = _helpers._git(repository, "rev-parse", "HEAD")
    (repository / SOURCE_PATH).write_text(changed, encoding="utf-8")
    _helpers._git(repository, "add", "--", SOURCE_PATH)
    _helpers._git(repository, "commit", "--quiet", "-m", "Public development candidate")
    candidate = _helpers._git(repository, "rev-parse", "HEAD")
    bundle = assemble_release(ROOT, baseline, candidate,
        "审核客服商业判断新版本：结合准确差异、适用规则和已登记能力，提出可绑定的高价值检查。没有执行记录不能声称业务通过。",
        candidate_root=repository, input_provenance="CALIBRATION_SYNTHETIC")
    bundle_path = output / "bundle.json"
    _helpers._private_json(bundle_path, bundle)
    manifest = {"schema_version": 1, "purpose": "PUBLIC_DEVELOPMENT_NOT_HOLDOUT", "source_commit": commit,
        "baseline_sha": baseline, "candidate_sha": candidate, "bundle_path": str(bundle_path),
        "intervention": "Reuse the public inclusive/exclusive window-boundary intervention on the full module.",
        "evaluation_status": "NOT_RUN", "candidate_executed": False}
    _helpers._private_json(output / "development-manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(generate(args.output_directory), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
