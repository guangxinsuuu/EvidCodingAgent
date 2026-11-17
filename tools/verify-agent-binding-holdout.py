#!/usr/bin/env python3
"""独立能力绑定留出评估；答案只在 evaluation-only 中，永不进入模型输入。

prepare → 固定完整 SUT 源码/可信 fixture → 三份隔离 Git B/C → 真实发布装配器
→ 冻结参考实验与接入门槛。evaluate → 已接受的 compiled_plan → 原样调用受审
合成 B/C 纯函数 → 核对模型预测及业务关系见证。它不是任意候选执行器，不接
Graph、数据库或网络；运行结果不回馈模型，也不把本地资格检查当企业业务 PASS。
"""
from __future__ import annotations

import argparse
import ast
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.registry.reference_resolver import LocalGitReader, MaterialError  # noqa: E402
from tools.registry.release_assembler import assemble_release  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("calibration_git_helpers", ROOT / "tools/verify-agent-calibration.py")
_HELPERS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_HELPERS)
SOURCE_COMMIT = "0f46c9b0602334db2b8955c018ed076c6c059c37"
SOURCE_PATH = "services/genai-support-sut/src/genai_support/integrations/commercial.py"
CONTRACT_PATH = "protocols/verify-agent/capabilities/commercial-decision.v1.json"
SUPPORT_PATHS = ["services/genai-support-sut/src/genai_support/__init__.py",
                 "services/genai-support-sut/src/genai_support/integrations/__init__.py",
                 "services/genai-support-sut/src/genai_support/integrations/http.py"]
GOAL = _HELPERS.GOAL


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return sha256(canonical(value).encode()).hexdigest()


def private_json(path: Path, value: Any) -> None:
    """以独占文件保留证据；拒绝覆盖先前评估或预登记。"""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _replace(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise MaterialError("BINDING_HOLDOUT_SOURCE_ANCHOR_CHANGED")
    result = source.replace(old, new)
    ast.parse(result)
    return result


def definitions(source: str) -> list[dict]:
    """评估者独立登记三项变化；模型只看到实际 diff，不看到本函数的注释或答案。"""
    return [
        {"case_id": "binding_a", "candidate": _replace(source,
            'instant(acceptance["accepted_at"]) > renewed', 'instant(acceptance["accepted_at"]) > claimed'),
         "requirement": "R1", "relation_kind": "BROKEN", "claim": "MAY_CHANGE",
         "input_field": "commercial_source.contracts.0.acceptance.accepted_at",
         "changed_symbol": "accepted_at", "critical_missing": "ACCEPTED_CONTRACT_SOURCE",
         "intervention": "合同接受时间的上界从续费时点改成申请时点，错误容许续费后、申请前才接受合同。",
         "primary": "2026-11-14T00:00:00Z", "control": "2026-11-01T00:00:00Z",
         "extra": "2026-11-20T00:00:00Z",
         "expected_primary": ["NEEDS_MORE_EVIDENCE", "ELIGIBLE"],
         "expected_control": ["ELIGIBLE", "ELIGIBLE"],
         "expected_extra": ["NEEDS_MORE_EVIDENCE", "NEEDS_MORE_EVIDENCE"],
         "business_relation": "合同接受不晚于续费时点；申请时点更晚不能追认此前的有效合同接受。",
         "witness_rule": "主或对照中的 accepted_at 严格晚于固定续费且不晚于固定申请，B 必须缺 ACCEPTED_CONTRACT_SOURCE，C 错误放行；另一组在续费之前且 B/C 一致。"},
        {"case_id": "binding_b", "candidate": _replace(source,
            ' or coverage.get("pending_count") != 0', ''),
         "requirement": "R3", "relation_kind": "BROKEN", "claim": "MAY_CHANGE",
         "input_field": "commercial_source.coverage.pending_count",
         "changed_symbol": "pending_count", "critical_missing": "USAGE_COMPLETENESS",
         "intervention": "删除 coverage 待处理数量必须为零的条件，使 SEALED 标签掩盖仍有待处理事件。",
         "primary": 1, "control": 0, "extra": 1000,
         "expected_primary": ["NEEDS_MORE_EVIDENCE", "ELIGIBLE"],
         "expected_control": ["ELIGIBLE", "ELIGIBLE"],
         "expected_extra": ["NEEDS_MORE_EVIDENCE", "ELIGIBLE"],
         "business_relation": "完整性要求 SEALED 与 pending_count=0 同时成立；观察到零用量不能替代未完成事件的闭合证明。",
         "witness_rule": "主或对照 pending_count>0 且其余固定前提保持，B 缺 USAGE_COMPLETENESS、C 错误放行；另一组 pending_count=0 且 B/C 一致。"},
        {"case_id": "binding_c", "candidate": _replace(source,
            'payment["amount_minor"] == context["billing"]["total_amount_cents"]',
            'context["billing"]["total_amount_cents"] == payment["amount_minor"]'),
         "requirement": "R4", "relation_kind": "PRESERVED", "claim": "SHOULD_PRESERVE",
         "input_field": "commercial_source.payment.amount_minor",
         "changed_symbol": "amount_minor", "critical_missing": "SIMULATED_PAYMENT_FACT",
         "intervention": "只交换两个已检查为整数的金额相等比较操作数，付款与发票金额必须一致的关系保持。",
         "primary": 101, "control": 100, "extra": 99,
         "expected_primary": ["NEEDS_MORE_EVIDENCE", "NEEDS_MORE_EVIDENCE"],
         "expected_control": ["ELIGIBLE", "ELIGIBLE"],
         "expected_extra": ["NEEDS_MORE_EVIDENCE", "NEEDS_MORE_EVIDENCE"],
         "business_relation": "整数金额相等比较具有对称性；交换操作数不会允许多付或少付，不能把语法变化自动判成回归。",
         "witness_rule": "两组必须分别包含金额匹配和不匹配；每组 B/C 输出一致，匹配组 ELIGIBLE，不匹配组缺 SIMULATED_PAYMENT_FACT；模型应主张保持而非凭空报错。"},
    ]


def _field(context: dict, path: str) -> Any:
    cursor: Any = context
    for part in path.split("."):
        cursor = cursor[int(part)] if isinstance(cursor, list) else cursor[part]
    return cursor


def _override(fixture: dict, name: str, value: Any, contract: dict) -> dict:
    """只按正式合同允许的真实标量路径写入；不修复其他相依字段或注入期望。"""
    fields = {field["name"]: field for field in contract["inputs"]}
    if name not in fields:
        raise ValueError("unsupported witness input")
    field = fields[name]
    kind = field["type"]
    if (kind == "integer" and type(value) is not int) or (kind == "boolean" and type(value) is not bool) or (kind == "string" and not isinstance(value, str)):
        raise ValueError("witness type mismatch")
    if "enum" in field and value not in field["enum"]:
        raise ValueError("witness enum mismatch")
    if kind == "integer" and not field.get("minimum", -2**53) <= value <= field.get("maximum", 2**53):
        raise ValueError("witness range mismatch")
    if field.get("format") == "date-time":
        if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("witness time requires timezone")
    result = deepcopy(fixture)
    parts = name.split(".")
    parent = _field(result, ".".join(parts[:-1]))
    if isinstance(parent, list):
        parent[int(parts[-1])] = value
    else:
        if parts[-1] not in parent:
            raise ValueError("new context field forbidden")
        parent[parts[-1]] = value
    return result


def reviewed_function(source: str):
    """执行经过本工具固定变更审核的完整商业函数集合，不执行模块顶层导入或客户端。

    Git 材料仍是完整 commercial.py；资格调用保留全部顶层纯函数 AST 及其原始
    函数体，只省去不会参与商业决定的网络客户端类/导入。此入口只由 prepare 或
    已核验冻结摘要的 evaluate 调用，绝不接收模型给出的代码或任意源码路径。
    """
    tree = ast.parse(source)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    namespace = {"datetime": datetime, "timezone": timezone, "Decimal": Decimal,
                 "InvalidOperation": InvalidOperation, "sha256": sha256, "json": json, "Any": Any}
    exec(compile(ast.Module(body=functions, type_ignores=[]), "<reviewed-commercial-decision>", "exec"), namespace)
    return namespace["commercial_decision"]


def qualify(source: str, cases: list[dict], contract: dict) -> list[dict]:
    """固定实参运行受审 B/C，预先证明回归样本可区分、良性样本保持且对照有效。"""
    baseline = reviewed_function(source)
    results = []
    for case in cases:
        candidate = reviewed_function(case["candidate"])
        rows = []
        for label in ("primary", "control", "extra"):
            context = _override(contract["fixture"]["context"], case["input_field"], case[label], contract)
            old, new = baseline(deepcopy(context)), candidate(deepcopy(context))
            expected = case["expected_" + label]
            if [old["eligibility"], new["eligibility"]] != expected:
                raise ValueError("pre-registered witness failed qualification")
            rows.append({"label": label, "inputs": {case["input_field"]: case[label]},
                         "context_sha256": digest(context), "baseline": old, "candidate": new,
                         "expected_eligibility": expected, "expected_matches": True})
        results.append({"case_id": case["case_id"], "scope": "REVIEWED_SYNTHETIC_PURE_FUNCTION_ONLY",
                        "network_or_graph_execution": False, "rows": rows})
    return results


def _pair(directory: Path, source: bytes, candidate: str, support: dict[str, bytes]) -> tuple[str, str]:
    """完整可导入包随 B/C 冻结；Git 差异仅为商业文件中的唯一受审变化。"""
    directory.mkdir(mode=0o700)
    _HELPERS._git(directory, "init", "--quiet", "--initial-branch=holdout")
    for path, content in support.items():
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    commits = []
    for label, content in (("baseline", source), ("candidate", candidate.encode())):
        target = directory / SOURCE_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        _HELPERS._git(directory, "add", "--", SOURCE_PATH, *support)
        _HELPERS._git(directory, "commit", "--quiet", "-m", "Explicit synthetic binding " + label)
        commits.append(_HELPERS._git(directory, "rev-parse", "HEAD"))
    return commits[0], commits[1]


def prepare(output: Path) -> dict:
    """准备后仅公开摘要；实现冻结之前不得向主线程披露本目录内的案例语义。"""
    output = output.resolve()
    allowed = [ROOT / "artifacts", ROOT / "tmp", Path(tempfile.gettempdir()).resolve(), Path("/private/tmp")]
    if not any(output.is_relative_to(parent.resolve()) and output != parent.resolve() for parent in allowed):
        raise ValueError("output outside evidence/temporary scope")
    if output.exists():
        raise ValueError("refusing to overwrite frozen evidence")
    git = LocalGitReader(ROOT)
    source = git.read(SOURCE_COMMIT, SOURCE_PATH)
    support = {path: git.read(SOURCE_COMMIT, path) for path in SUPPORT_PATHS}
    contract_raw = (ROOT / CONTRACT_PATH).read_bytes()
    contract = json.loads(contract_raw)
    if sha256(source).hexdigest() != contract["source_sha256"] or digest(contract["fixture"]["context"]) != contract["fixture"]["sha256"]:
        raise ValueError("source or fixture contract mismatch")
    cases = definitions(source.decode())
    qualifications = qualify(source.decode(), cases, contract)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(mode=0o700)
    (output / "model-input").mkdir(mode=0o700)
    (output / "evaluation-only").mkdir(mode=0o700)
    private_json(output / "evaluation-only/authority-contract.json", contract)
    manifest = {"schema_version": 1, "source_commit": SOURCE_COMMIT, "input_provenance": "CALIBRATION_SYNTHETIC",
                "not_real_product_prs": True, "source_path": SOURCE_PATH, "source_sha256": sha256(source).hexdigest(),
                "source_git_blob": git._git("rev-parse", SOURCE_COMMIT + ":" + SOURCE_PATH).decode().strip(),
                "contract_path": CONTRACT_PATH, "contract_file_sha256": sha256(contract_raw).hexdigest(),
                "contract_canonical_sha256": digest(contract), "fixture_sha256": contract["fixture"]["sha256"],
                "support_files": [{"path": path, "sha256": sha256(raw).hexdigest()} for path, raw in support.items()], "cases": []}
    for case in cases:
        repo = output / (case["case_id"] + "-repository")
        baseline, candidate = _pair(repo, source, case["candidate"], support)
        bundle = assemble_release(ROOT, baseline, candidate, GOAL, candidate_root=repo, input_provenance="CALIBRATION_SYNTHETIC")
        contracts = bundle.get("planning_contracts", [])
        if not any(item.get("capability_ref") == contract["capability_ref"] for item in contracts):
            raise ValueError("release assembler has not projected the authority contract yet")
        bundle_path = "model-input/" + case["case_id"] + "-bundle.json"
        private_json(output / bundle_path, bundle)
        rubric = {key: value for key, value in case.items() if key != "candidate"}
        rubric["reference_check"] = {
            "capability_ref": contract["capability_ref"], "scenario_ref": contract["scenario_ref"],
            "stage_ref": contract["stage_ref"], "contract_ref": contract["id"], "fixture_ref": contract["fixture"]["id"],
            "primary_inputs": [{"name": case["input_field"], "value": str(case["primary"])}],
            "control_inputs": [{"name": case["input_field"], "value": str(case["control"])}],
            "observations": ["decision.eligibility", "decision.missing_evidence"],
            "primary_rule_expectation": "NEEDS_MORE_EVIDENCE and contains " + case["critical_missing"],
            "primary_control_relation": "DIFFERENT", "baseline_candidate_claim": case["claim"],
            "expected_baseline": case["expected_primary"][0], "expected_candidate": case["expected_primary"][1],
            "preconditions": "仅改一个绑定字段；其余值严格来自同一可信 fixture；B/C、主/对照均使用新拷贝；无自动补齐相依事实。"}
        rubric["scoring"] = {"source_grounding": "0–2: actual diff/source and applicable rule support the reasoning",
            "discriminating_input": "0–2: own compiled inputs actually witness this relation; benign case includes matched/mismatched control",
            "expected_relation": "0–2: exact typed model B/C predictions agree with evaluated outputs",
            "observation_and_control": "0–2: actual bound outputs and meaningful control relation; not generic process names",
            "qualified_scope": "0–2: fully bound pure fixture stage and honest unmet live execution prerequisites",
            "sixth_hard_gate": "Correct MAY_CHANGE versus SHOULD_PRESERVE and independent witness satisfied; keyword mention alone is insufficient",
            "structural_binding_reported_separately": True,
            "no_golden_input_repair": True, "missing_compiled_plan": "NOT_RUN / FAIL, never replace it with reference inputs"}
        rubric_path = "evaluation-only/" + case["case_id"] + "-rubric.json"
        private_json(output / rubric_path, rubric)
        manifest["cases"].append({"case_id": case["case_id"], "baseline_sha": baseline, "candidate_sha": candidate,
                                  "baseline_source_sha256": sha256(source).hexdigest(),
                                  "candidate_source_sha256": sha256(case["candidate"].encode()).hexdigest(),
                                  "repository": repo.name, "bundle_path": bundle_path, "rubric_path": rubric_path})
    private_json(output / "evaluation-only/qualification.json", {"cases": qualifications})
    private_json(output / "evaluation-only/manifest.json", manifest)
    private_json(output / "evaluation-only/stability-gate.json", {
        "schema_version": 1, "case_count": 3, "runs_per_case": 2, "planned_runs": 6,
        "run_identity": "fresh Runtime/run state per attempt; all runs retained, no best-of selection",
        "seeds": [19, 31], "settings_frozen_before_disclosure": True,
        "bounded_runtime_repairs_allowed": True, "all_originals_and_repairs_scored": True,
        "gate": "6/6 final proposals BOUND_PLAN and manual >=8/10; discriminating_input and expected_relation each 2; sixth relation hard gate true; own compiled inputs concretely checked",
        "any_failure": "DO_NOT_ENABLE_RUNTIME_EXPERIMENT_DISPATCH",
        "sample_size_limitation": "Six runs over three changes are only this turn's conservative admission threshold, not proof of population reliability or production readiness.",
        "after_tuning": "These cases become regression-only; fresh heldout changes are required."})
    files = sorted(path for folder in (output / "model-input", output / "evaluation-only") for path in folder.glob("*.json"))
    frozen = {"schema_version": 1, "frozen_at": datetime.now(timezone.utc).isoformat(), "cases": 3, "planned_runs": 6,
              "source_commit": SOURCE_COMMIT, "generator_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
              "status": "PREPARED_BEFORE_IMPLEMENTATION_FREEZE_NOT_MODEL_EVALUATED",
              "files": [{"path": str(path.relative_to(output)), "sha256": sha256(path.read_bytes()).hexdigest()} for path in files]}
    private_json(output / "freeze.json", frozen)
    return {"status": "PREPARED", "frozen_at": frozen["frozen_at"], "case_count": 3,
            "freeze_path": str(output / "freeze.json"), "freeze_sha256": sha256((output / "freeze.json").read_bytes()).hexdigest()}


def _same_json_value(actual: Any, expected: Any) -> bool:
    """JSON 只有 number 类型；整数/浮点表示差异不制造错误，boolean 仍独立。"""
    if type(actual) in {int, float} and type(expected) in {int, float}:
        return actual == expected
    return type(actual) is type(expected) and actual == expected


def _assertion(actual: Any, operator: str, expected: Any) -> bool:
    """按已编译类型比较模型自己的断言；空值、缺字段不自动解释成成功。"""
    if operator in {"EQUALS", "UNCHANGED"}:
        return _same_json_value(actual, expected)
    if operator == "NOT_EQUALS":
        return not _same_json_value(actual, expected)
    if operator == "CONTAINS":
        return isinstance(actual, (str, list)) and expected in actual
    if operator == "PRESENT":
        return actual is not None
    if operator == "ABSENT":
        return actual is None
    if operator in {"GREATER_THAN", "LESS_THAN"}:
        return (actual > expected if operator == "GREATER_THAN" else actual < expected) if type(actual) in {int, float} and type(expected) in {int, float} else False
    raise ValueError("unsupported compiled assertion")


def evaluate_compiled(prepared: Path, case_id: str, compiled: dict | None) -> dict:
    """只运行已绑定的原样 compiled contexts；不能绑定时明确 NOT_RUN，绝不补 golden。

    返回机械实测，不取代人工五维评分。每项预测使用模型原值，与同一冻结 B/C
    实际纯函数输出比较。关系见证单独核对，避免模型拿无关输入碰巧满足字符串。
    """
    if compiled is None:
        return {"status": "NOT_RUN", "reason": "NO_BOUND_COMPILED_PLAN", "relation_witness": False}
    frozen = json.loads((prepared / "freeze.json").read_text())
    if any(sha256((prepared / item["path"]).read_bytes()).hexdigest() != item["sha256"] for item in frozen["files"]):
        raise ValueError("frozen evaluation files changed")
    manifest = json.loads((prepared / "evaluation-only/manifest.json").read_text())
    matches = [row for row in manifest["cases"] if row["case_id"] == case_id]
    if len(matches) != 1:
        raise ValueError("unknown reviewed case")
    case = matches[0]
    rubric = json.loads((prepared / case["rubric_path"]).read_text())
    contract = json.loads((prepared / "evaluation-only/authority-contract.json").read_text())
    if compiled.get("contract_sha256") != digest(contract) or compiled.get("fixture_sha256") != contract["fixture"]["sha256"]:
        raise ValueError("compiled contract identity mismatch")
    if compiled.get("capability_ref") != contract["capability_ref"] or compiled.get("stage_ref") != contract["stage_ref"]:
        raise ValueError("compiled target mismatch")
    contexts = {label: compiled[label + "_context"] for label in ("primary", "control")}
    if any(digest(context) != compiled[label + "_context_sha256"] for label, context in contexts.items()):
        raise ValueError("compiled context digest mismatch")
    git = LocalGitReader(prepared / case["repository"])
    outputs = {}
    for version in ("baseline", "candidate"):
        source = git.read(case[version + "_sha"], manifest["source_path"])
        if sha256(source).hexdigest() != case[version + "_source_sha256"]:
            raise ValueError("reviewed source identity mismatch")
        evaluate = reviewed_function(source.decode())
        outputs[version] = {label: evaluate(deepcopy(context)) for label, context in contexts.items()}
    assertions = compiled["assertions"]
    observed = {row["observation_ref"]: row["field"] for row in assertions}
    relation = compiled["change_relation"]
    predictions = []
    for version in ("baseline", "candidate"):
        for obs, expected in relation["expected_" + version].items():
            actual = _field({"decision": outputs[version]["primary"]}, observed[obs])
            predictions.append({"version": version, "observation_ref": obs, "expected": expected,
                                "actual": actual, "matches": _assertion(actual, "EQUALS", expected)})
    assertion_results = [{"version": version, **row,
                          "actual": _field({"decision": outputs[version]["primary"]}, row["field"]),
                          "satisfied": _assertion(_field({"decision": outputs[version]["primary"]}, row["field"]), row["operator"], row["value"])}
                         for version in ("baseline", "candidate") for row in assertions]
    control_results = []
    control = compiled["control_relation"]
    for version in ("baseline", "candidate"):
        field = observed[control["observation_ref"]]
        first = _field({"decision": outputs[version]["primary"]}, field)
        second = _field({"decision": outputs[version]["control"]}, field)
        actual_relation = "SAME" if _same_json_value(first, second) else "DIFFERENT"
        control_results.append({"version": version, "field": field, "primary": first, "control": second,
                                "expected_relation": control["expected_relation"], "actual_relation": actual_relation,
                                "satisfied": actual_relation == control["expected_relation"]})
    critical = rubric["critical_missing"]
    if rubric["relation_kind"] == "BROKEN":
        broken = [label for label in contexts if critical in outputs["baseline"][label]["missing_evidence"] and
                  outputs["candidate"][label]["eligibility"] == "ELIGIBLE"]
        matched = [label for label in contexts if outputs["baseline"][label]["eligibility"] == "ELIGIBLE" and
                   outputs["candidate"][label]["eligibility"] == "ELIGIBLE"]
        witnessed = bool(broken and matched)
    else:
        witnessed = (all(outputs["baseline"][label] == outputs["candidate"][label] for label in contexts)
                     and any(_field(contexts[label], rubric["input_field"]) != contexts[label]["billing"]["total_amount_cents"]
                             and critical in outputs["baseline"][label]["missing_evidence"] for label in contexts)
                     and any(_field(contexts[label], rubric["input_field"]) == contexts[label]["billing"]["total_amount_cents"]
                             and outputs["baseline"][label]["eligibility"] == "ELIGIBLE" for label in contexts))
    input_values = {label: _field(context, rubric["input_field"]) for label, context in contexts.items()}
    return {"status": "EVALUATED_REVIEWED_SYNTHETIC_PURE_FUNCTION_ONLY", "compiled_plan_sha256": digest(compiled),
            "contexts_used_without_golden_completion": True, "critical_input_values": input_values,
            "outputs": outputs, "model_predictions": predictions, "predictions_match": all(p["matches"] for p in predictions),
            "assertion_results": assertion_results, "control_results": control_results,
            "relation_claim_matches": relation["claim"] == rubric["claim"], "relation_witness": witnessed,
            "relation_requirement_matches": relation["requirement_ref"] == rubric["requirement"],
            "sixth_hard_gate": witnessed and relation["claim"] == rubric["claim"]
                and relation["requirement_ref"] == rubric["requirement"] and all(p["matches"] for p in predictions),
            "business_execution_via_runtime": False, "release_authorized": False}


def main() -> int:
    parser = argparse.ArgumentParser(description="准备独立绑定留出集，或离线核对已绑定的受审计划")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("prepare")
    create.add_argument("--output-directory", type=Path, required=True)
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--prepared", type=Path, required=True)
    evaluate.add_argument("--case-id", required=True)
    evaluate.add_argument("--compiled-plan", type=Path, required=True)
    evaluate.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(args.output_directory)
        else:
            result = evaluate_compiled(args.prepared, args.case_id, json.loads(args.compiled_plan.read_text()))
            private_json(args.output, result)
            result = {"status": result["status"], "output": str(args.output)}
    except (MaterialError, OSError, ValueError, KeyError) as error:
        print(json.dumps({"status": "REJECTED", "error": getattr(error, "code", type(error).__name__)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
