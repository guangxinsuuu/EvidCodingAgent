#!/usr/bin/env python3
"""生成公开开发用 primary/control 合成输入；不运行产品、不提供期望或留出答案。

调用：development_fixture → commercial_fixture.prepare_probe → 新文件中的输入/摘要。
正式发布包仍通过 release_assembler 装配已有公开版本对；本文件不是版本差异生成器。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from commercial_fixture import CAPABILITY, SCENARIO, STAGE, prepare_probe


def development_inputs() -> dict:
    """公开的全额/不足额付款对照只提供事实；模型必须从已读取规则独立提出期望。"""
    base = {"capability_ref": CAPABILITY, "scenario_ref": SCENARIO, "stage_ref": STAGE}
    return {"schema_version": 1, "purpose": "PUBLIC_DEVELOPMENT_FIXTURE_ONLY_NOT_HOLDOUT",
        "primary": prepare_probe({**base, "inputs": [{"name": "commercial_source.payment.amount_minor", "value": 100}]}),
        "control": prepare_probe({**base, "inputs": [{"name": "commercial_source.payment.amount_minor", "value": 99}]}),
        "evaluation_status": "NOT_RUN", "expected_results_provided": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # 排他创建；不会覆盖旧实验记录或输出到固定文档。
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(development_inputs(), stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
