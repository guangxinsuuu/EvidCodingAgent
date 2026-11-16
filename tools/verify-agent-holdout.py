#!/usr/bin/env python3
"""独立留出集：固定真实源码 → 合成 B/C → release_assembler → 冻结评估清单。

这里只准备规划器评估，不运行模型、SUT、数据库或远端实验。模型输入只包含中性
发布任务、实际 Git diff 和既有目录；人工答案、可判别输入及评分规程单独存放。
已有公开校准样本不在本集合中。留出集一经用于调参就必须作废其留出资格。
"""
from __future__ import annotations

import argparse
import ast
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.registry.reference_resolver import LocalGitReader, MaterialError  # noqa: E402
from tools.registry.release_assembler import assemble_release  # noqa: E402

# 仅复用隔离 Git/文件写入助手，不复用公开样本、提示、答案或运行结果。
_SPEC = importlib.util.spec_from_file_location("egap_calibration_helpers", ROOT / "tools/verify-agent-calibration.py")
_HELPERS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_HELPERS)

COMMERCIAL_PATH = "services/genai-support-sut/src/genai_support/integrations/commercial.py"
PERSISTENCE_PATH = "services/genai-support-sut/src/genai_support/integrations/persistence.py"
GOAL = _HELPERS.GOAL
HEADER = ('"""CALIBRATION_SYNTHETIC：固定真实源码的最小摘录/合成调用者。\n'
          '这不是完整客服系统，也不是一份真实产品 PR。仅供发布规划的隔离评估。\n"""\n\n')


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _payment_excerpt(source: str) -> str:
    """从真实 _assess 精确摘取付款关系谓词；锚点变化即拒绝，避免静默造样本。"""
    function = _HELPERS._function(source, "_assess")
    matches = [node.test.operand for node in ast.walk(ast.parse(function))
               if isinstance(node, ast.If) and isinstance(node.test, ast.UnaryOp)
               and isinstance(node.test.op, ast.Not)
               and any(isinstance(item, ast.Constant) and item.value == "SIMULATED_PAYMENT_FACT"
                       for statement in node.body for item in ast.walk(statement))]
    if len(matches) != 1:
        raise MaterialError("HOLDOUT_PAYMENT_ANCHOR_CHANGED")
    expression = ast.get_source_segment(function, matches[0])
    return (HEADER + "from datetime import datetime\nfrom typing import Any\n\n"
            + _HELPERS._function(source, "instant")
            + '\n\ndef payment_matches_invoice(payment: dict, context: dict, scope: dict, claimed: str) -> bool:\n'
            + '    """合成调用者：保留 _assess 的付款关系谓词，不代表整项退款资格。"""\n'
            + '    claimed = instant(claimed)\n'
            + '    return (' + expression + ')\n')


def _definitions(raw_commercial: bytes, raw_persistence: bytes) -> list[dict]:
    """评估侧定义唯一人工变化与参考判据；此返回值不得作为模型上下文。"""
    baseline_a = _payment_excerpt(raw_commercial.decode("utf-8"))
    anchor_a = 'and payment.get("currency") == context["billing"].get("currency")'
    baseline_b = (HEADER + "from copy import deepcopy\nfrom typing import Any\n\n"
                  + _HELPERS._function(raw_persistence.decode("utf-8"), "_stable_fact_value")
                  + '\n\ndef stable_facts_changed(before: dict, after: dict) -> bool:\n'
                  + '    """合成比较入口；生产 assessment_facts_digest 使用同一稳定事实投影。"""\n'
                  + '    return _stable_fact_value(before) != _stable_fact_value(after)\n')
    anchor_b = 'if key not in observer_fields}'
    if baseline_a.count(anchor_a) != 1 or baseline_b.count(anchor_b) != 1:
        raise MaterialError("HOLDOUT_SOURCE_ANCHOR_CHANGED")
    return [
        {"case_id": "holdout_a", "path": COMMERCIAL_PATH, "baseline": baseline_a,
         "candidate": baseline_a.replace(anchor_a, ""),
         "source_symbol": "_assess payment predicate + instant", "requirement_refs": ["R4"],
         "intervention": "人工删除付款币种与发票币种的精确相等检查，其余付款条件不变。",
         "acceptable_scenarios": ["wrong_currency.initial", "NOVEL_CHECK with equivalent currency mismatch and matched control"],
         "acceptable_capabilities": ["unit.commercial", "check.business", "check.boundaries"],
         "reference_check": {
             "stage": "wrong_currency.initial；固定其他付款、scope、时点和完整资格前提",
             "setup": "发票 USD 1000；模拟付款 EUR 1000、PAID、同客户/订阅/发票、有收据且付款不晚于申请。",
             "control": "同一份输入只把付款币种改为 USD；另设金额或 scope 不匹配的拒绝对照。",
             "expected_relation": "两版本均应拒绝 EUR/USD 对应为已核验付款；纯谓词 B=False/C=True 揭示回归。若集成观察，应保持 NEEDS_MORE_EVIDENCE、SIMULATED_PAYMENT_FACT，不能仅凭 PAID 放行。USD 对照 B=C=True。",
             "observations": ["B/C commit 与实际函数输入", "原始 payment.currency 和 billing.currency", "付款谓词结果；集成模式另读冻结决定 billing_paid/missing_evidence/eligibility"],
             "qualification": "单测可证明所改谓词的 B/C 区分；目录历史结果或只比较 amount/status 的测试不能覆盖此变化。"},
         "required_semantics": ["识别遗漏币种关系", "指定币种不匹配且其他前提满足的输入", "指定同币种对照", "明确 B/C 及业务规则预期", "观察真实输入和判断结果"]},
        {"case_id": "holdout_b", "path": PERSISTENCE_PATH, "baseline": baseline_b,
         "candidate": baseline_b.replace(anchor_b, 'if key not in observer_fields and not key.endswith("_at")}'),
         "source_symbol": "_stable_fact_value; synthetic stable_facts_changed caller",
         "requirement_refs": ["R6", "R2"],
         "intervention": "人工将观察元数据剔除扩为所有 *_at 字段，误删 paid_at 等真实商业时点。",
         "acceptable_scenarios": ["NOVEL_CHECK for business-time versus observation-metadata changes", "v1_history.supplement extended with explicit relevant business-fact change"],
         "acceptable_capabilities": ["unit.snapshot", "check.boundaries", "unit.commercial"],
         "reference_check": {
             "stage": "新的稳定事实比较单测；可另定义 initial → explicit reevaluate，不冒充现有补充消息场景已覆盖",
             "setup": "固定原 claim.claimed_at；同一付款 paid_at 从原申请之前修正为之后，金额、币种、scope 和观察时间以外的字段保持相同。",
             "control": "只变化 observed_at/correlation_id 时稳定事实仍相等；改变 amount_minor 时必须不同。",
             "expected_relation": "仅 paid_at 真实变化时稳定事实必须不同；B=True/C=False 暴露候选误忽略。元数据对照 B=C=False，金额对照 B=C=True。若使用真实重新评估入口，应明确 reason,parent 与事实变化；普通重试仍复用原快照，不允许通过移动 claim 绕过窗口。",
             "observations": ["B/C commit", "before/after 原始输入与规范化投影", "stable_facts_changed 或 assessment_facts_digest 比较", "集成扩展另观察 NO_NEW_FACTS/新 revision 与冻结快照"],
             "qualification": "原始 claim 不可变不等于所有业务时点均可删除；只测 observed_at 去重不能发现这个回归。"},
         "required_semantics": ["区分业务时点和观察元数据", "使用业务时点变化作为区分输入", "设置仅观察元数据变化对照", "给出变化/不变关系", "不把普通重试与显式重新评估混为一谈"]},
    ]


def _namespace(source: str) -> dict:
    """只执行本生成器构造且已审核的纯函数摘录，限定资格检查的输入与依赖。

    不是通用候选执行器：拒绝任何非 import/function/docstring 顶层节点，只给纯
    函数所需的可信标准库符号。不会启动 SUT、导入候选包或读取候选工作区。
    """
    tree = ast.parse(source)
    if any(not isinstance(node, (ast.FunctionDef, ast.ImportFrom, ast.Expr)) for node in tree.body):
        raise ValueError("qualification source must be generated pure functions")
    functions = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef)], type_ignores=[])
    namespace = {"datetime": datetime, "Any": Any, "deepcopy": deepcopy}
    exec(compile(functions, "<trusted-holdout-pure-excerpt>", "exec"), namespace)
    return namespace


def qualify(definitions: list[dict]) -> list[dict]:
    """先证明每个留出样本存在实际 B/C 差异及不受影响对照，再冻结评分规程。"""
    results = []
    for case in definitions:
        baseline, candidate = _namespace(case["baseline"]), _namespace(case["candidate"])
        if case["case_id"] == "holdout_a":
            scope = {"customer_id": "customer-holdout", "subscription_id": "subscription-holdout", "invoice_id": "invoice-holdout"}
            payment = {**scope, "source_kind": "SIMULATED_PAYMENT", "authority_scope": "LOCAL_LAB",
                       "status": "PAID", "currency": "EUR", "amount_minor": 1000,
                       "receipt_ref": "receipt-holdout", "paid_at": "2026-11-16T10:00:00Z"}
            context = {"billing": {"currency": "USD", "total_amount_cents": 1000}}
            samples = [("discriminating", payment, False, True),
                       ("matched_control", {**payment, "currency": "USD"}, True, True),
                       ("amount_control", {**payment, "currency": "USD", "amount_minor": 999}, False, False),
                       ("scope_control", {**payment, "currency": "USD", "invoice_id": "other"}, False, False)]
            rows = []
            for label, value, expected_b, expected_c in samples:
                args = [value, context, scope, "2026-11-17T10:00:00Z"]
                rows.append({"label": label, "inputs": args,
                             "baseline": baseline["payment_matches_invoice"](*args),
                             "candidate": candidate["payment_matches_invoice"](*args),
                             "expected_baseline": expected_b, "expected_candidate": expected_c})
        else:
            before = {"claim": {"claimed_at": "2026-11-17T10:00:00Z"},
                      "payment": {"paid_at": "2026-11-16T10:00:00Z", "amount_minor": 1000},
                      "observed_at": "2026-11-17T11:00:00Z", "correlation_id": "read-1"}
            business_change = deepcopy(before)
            business_change["payment"]["paid_at"] = "2026-11-18T10:00:00Z"
            metadata_change = {**before, "observed_at": "2026-11-17T12:00:00Z", "correlation_id": "read-2"}
            amount_change = deepcopy(before)
            amount_change["payment"]["amount_minor"] = 1001
            rows = []
            for label, after, expected_b, expected_c in (
                ("discriminating", business_change, True, False),
                ("metadata_control", metadata_change, False, False),
                ("amount_control", amount_change, True, True),
            ):
                rows.append({"label": label, "inputs": [before, after],
                             "baseline": baseline["stable_facts_changed"](before, after),
                             "candidate": candidate["stable_facts_changed"](before, after),
                             "expected_baseline": expected_b, "expected_candidate": expected_c})
        if not all(row["baseline"] == row["expected_baseline"] and row["candidate"] == row["expected_candidate"] for row in rows):
            raise ValueError("held-out witness failed qualification")
        results.append({"case_id": case["case_id"], "status": "QUALIFIED_PURE_EXCERPT_ONLY", "samples": rows,
                        "not_sut_execution": True, "not_business_pass": True})
    return results


def generate(output_directory: Path, source_commit: str | None = None) -> dict:
    """仅创建全新评估目录；评估答案与模型 bundle 分开，并对所有文件冻结摘要。

    调用者必须先独立准备本集合，再冻结 Runtime/Prompt，最后才允许第一次真实
    模型评估。不得依据结果修改实现后重复宣称同一集合为未见留出集。
    """
    output = output_directory.resolve()
    allowed = [ROOT / "artifacts", ROOT / "tmp", Path(tempfile.gettempdir()).resolve(), Path("/private/tmp")]
    if not any(output.is_relative_to(parent.resolve()) and output != parent.resolve() for parent in allowed):
        raise MaterialError("HOLDOUT_OUTPUT_OUTSIDE_TEMP_SCOPE")
    if output.exists():
        raise MaterialError("HOLDOUT_OUTPUT_ALREADY_EXISTS")
    git = LocalGitReader(ROOT)
    commit = source_commit or git._git("rev-parse", "HEAD").decode("ascii").strip()
    raw_commercial, raw_persistence = git.read(commit, COMMERCIAL_PATH), git.read(commit, PERSISTENCE_PATH)
    definitions = _definitions(raw_commercial, raw_persistence)
    qualifications = qualify(definitions)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(mode=0o700)
    (output / "model-input").mkdir(mode=0o700)
    (output / "evaluation-only").mkdir(mode=0o700)
    manifest = {"schema_version": 1, "input_provenance": "CALIBRATION_SYNTHETIC",
                "not_a_real_product_pr": True, "source_commit": commit,
                "source_blobs": [{"path": path, "git_blob": git._git("rev-parse", commit + ":" + path).decode().strip(), "sha256": _sha(raw)}
                                 for path, raw in ((COMMERCIAL_PATH, raw_commercial), (PERSISTENCE_PATH, raw_persistence))],
                "scope": "READ_ONLY_PLANNING_HOLDOUT_NOT_SUT_VERIFICATION", "cases": [],
                "evaluation_policy": {"first_attempt_only": True, "max_runs_per_case": 1,
                    "runtime_feedback_revision_allowed_within_initial_run": True,
                    "retuning_invalidates_holdout_status": True,
                    "mechanical_runtime_acceptance_is_not_semantic_quality": True,
                    "model_must_not_receive_evaluation_only_files": True}}
    for case in definitions:
        baseline, candidate = _HELPERS._pair(output / (case["case_id"] + "-repository"), case["path"], case["baseline"], case["candidate"])
        bundle = assemble_release(ROOT, baseline, candidate, GOAL, candidate_root=output / (case["case_id"] + "-repository"), input_provenance="CALIBRATION_SYNTHETIC")
        relative_bundle = "model-input/" + case["case_id"] + "-bundle.json"
        _HELPERS._private_json(output / relative_bundle, bundle)
        rubric = {key: value for key, value in case.items() if key not in {"baseline", "candidate"}}
        rubric.update(baseline_sha=baseline, candidate_sha=candidate,
                      scoring={"dimensions": {"source_grounding": "Read relevant diff/candidate plus applicable rule; cite materials accurately.",
                          "discriminating_input": "Specify input that distinguishes B/C, without unrelated blockers.",
                          "expected_relation": "State the required business relation and falsifiable B/C expectation.",
                          "observation_and_control": "Specify input/result observation and matched unchanged control.",
                          "qualified_scope": "Choose compatible capability/stage, or explicitly propose missing capability via NOVEL_CHECK. No dispatch or PASS claim."},
                          "scale": "Each dimension 0=absent/wrong, 1=partial, 2=concrete and correct.",
                          "pass": "At least 8/10, discriminating_input=2, expected_relation=2, no invented execution/PASS, no scenario misuse silently treated as coverage.",
                          "unknown": "Missing/incomplete model record is NOT_EVALUABLE, never PASS.",
                          "rejected_proposals": "Score original and revised proposals separately; report all costs and whether feedback actually resolved defects."})
        relative_rubric = "evaluation-only/" + case["case_id"] + "-rubric.json"
        _HELPERS._private_json(output / relative_rubric, rubric)
        manifest["cases"].append({"case_id": case["case_id"], "baseline_sha": baseline, "candidate_sha": candidate,
                                  "bundle_path": relative_bundle, "rubric_path": relative_rubric,
                                  "baseline_sha256": _sha(case["baseline"].encode()), "candidate_sha256": _sha(case["candidate"].encode())})
    _HELPERS._private_json(output / "evaluation-only/qualification.json", {"cases": qualifications})
    _HELPERS._private_json(output / "evaluation-only/manifest.json", manifest)
    files = sorted(path for folder in (output / "model-input", output / "evaluation-only") for path in folder.glob("*.json"))
    freeze = {"schema_version": 1, "frozen_at": datetime.now(timezone.utc).isoformat(),
              "case_count": len(definitions), "source_commit": commit,
              "generator_sha256": _sha(Path(__file__).read_bytes()),
              "evaluation_status": "PREPARED_BEFORE_RUNTIME_PROMPT_FREEZE_NOT_MODEL_EVALUATED",
              "files": [{"path": str(path.relative_to(output)), "sha256": _sha(path.read_bytes())} for path in files]}
    _HELPERS._private_json(output / "freeze.json", freeze)
    return {"status": "HOLDOUT_PREPARED", "frozen_at": freeze["frozen_at"], "case_count": len(definitions),
            "freeze_path": str(output / "freeze.json"), "freeze_sha256": _sha((output / "freeze.json").read_bytes())}


def main() -> int:
    parser = argparse.ArgumentParser(description="独立冻结新发布变化留出样本；不运行模型或 SUT")
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--source-commit")
    args = parser.parse_args()
    try:
        result = generate(args.output_directory, args.source_commit)
    except (MaterialError, OSError, ValueError) as error:
        print(json.dumps({"status": "REJECTED", "error_code": getattr(error, "code", "HOLDOUT_PREPARATION_FAILED")}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
