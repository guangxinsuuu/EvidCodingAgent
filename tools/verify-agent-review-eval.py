#!/usr/bin/env python3
"""ReAct 审查循环的独立离线见证；输入任务与评估答案分开保存。

prepare 只创建两份准确合成 B/C、普通审查请求及冻结的评估依据。
evaluate 只取账本中经 ACCEPT_PLAN 接受的原始 compiled_check，重新做机械
绑定并用模型实际选择的输入核验受审纯函数。没有计划时明确 NOT_RUN，绝不
代入参考输入。这不是 Runtime Runner，也不产生当前候选覆盖或发布资格。
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.registry.reference_resolver import LocalGitReader, MaterialError
from tools.registry.release_assembler import assemble_review_request

_SPEC = importlib.util.spec_from_file_location("review_eval_helpers", ROOT / "tools/verify-agent-binding-holdout.py")
_H = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_H)

# Node 只运行受信绑定器，输入走 stdin；候选代码从不进入此进程。
# 原编译提案和合约不能被 Python 猜类型或自动补关联字段。
REBIND = r"""
import {readFileSync} from 'node:fs';
import {assessCheckBinding} from './packages/verify-runtime/dist/check-binding.js';
import {parseDecisionProposal} from './packages/contracts/dist/verify.js';
const input=JSON.parse(readFileSync(0,'utf8'));
const proposal=parseDecisionProposal(input.proposal);
const result=assessCheckBinding(input.bundle,proposal,input.contract);
process.stdout.write(JSON.stringify(result));
"""


def definitions(source: str) -> list[dict]:
    """评估专用变化，禁止把本函数或 rubric 装入 Agent 材料。

    预期关系来自已登记 R1/R4，产品函数仅提供实际观察。两项均使用正式
    能力已支持的字段，避开此前公开的接受时间、待处理数量和金额交换样本。
    """
    return [
        {"case_id": "review_a", "candidate": _H._replace(source,
            'and payment.get("currency") == context["billing"].get("currency")\n            ', ''),
         "requirement": "R4", "relation_kind": "BROKEN", "claim": "MAY_CHANGE",
         "input_field": "commercial_source.payment.currency", "critical_missing": "SIMULATED_PAYMENT_FACT",
         "primary": "EUR", "control": "USD", "extra": "GBP",
         "expected_primary": ["NEEDS_MORE_EVIDENCE", "ELIGIBLE"],
         "expected_control": ["ELIGIBLE", "ELIGIBLE"], "expected_extra": ["NEEDS_MORE_EVIDENCE", "ELIGIBLE"],
         "business_relation": "R4 要求付款币种与发票币种一致；相同金额不能抵消币种不一致。"},
        {"case_id": "review_b", "candidate": _H._replace(source,
            'event["term_number"] >= 2', 'event["term_number"] > 1'),
         "requirement": "R1", "relation_kind": "PRESERVED", "claim": "SHOULD_PRESERVE",
         "input_field": "commercial_source.events.0.term_number", "critical_missing": "RENEWAL_EVENT",
         "primary": 1, "control": 2, "extra": 100,
         "expected_primary": ["NEEDS_MORE_EVIDENCE", "NEEDS_MORE_EVIDENCE"],
         "expected_control": ["ELIGIBLE", "ELIGIBLE"], "expected_extra": ["ELIGIBLE", "ELIGIBLE"],
         "business_relation": "R1 仅允许第2期及以后续期；先检查精确整数类型后，>=2 与 >1 保持等价。"},
    ]


def followup_definitions(source: str) -> list[dict]:
    """第二批隔离变化；初轮已经公开的 a/b 保留为回归，不能换名充作未见任务。"""
    return [
        {"case_id": "review_c", "candidate": _H._replace(source,
            'contract["terms"].get("special_terms") is not False',
            'contract["terms"].get("special_terms") is None'),
         "requirement": "R1", "relation_kind": "BROKEN", "claim": "MAY_CHANGE",
         "input_field": "commercial_source.contracts.0.terms.special_terms", "critical_missing": "SPECIAL_CONTRACT_TERMS",
         "primary": True, "control": False,
         "expected_primary": ["NEEDS_MORE_EVIDENCE", "ELIGIBLE"],
         "expected_control": ["ELIGIBLE", "ELIGIBLE"],
         "business_relation": "R1 对明确存在特殊条款的合同要求人工补证；资料不是缺失值，不等于标准合同适用。"},
        {"case_id": "review_d", "candidate": _H._replace(source,
            'row.get("status") == "ACTIVE"', 'row.get("status") in {"ACTIVE"}'),
         "requirement": "R1", "relation_kind": "PRESERVED", "claim": "SHOULD_PRESERVE",
         "input_field": "commercial_source.contracts.0.status", "critical_missing": "APPLICABLE_CONTRACT",
         "primary": "DRAFT", "control": "ACTIVE",
         "expected_primary": ["NEEDS_MORE_EVIDENCE", "NEEDS_MORE_EVIDENCE"],
         "expected_control": ["ELIGIBLE", "ELIGIBLE"],
         "business_relation": "在已登记的字符串状态接口内，单元素集合成员判断保持 ACTIVE 合同筛选，DRAFT 仍不构成有效合同。"},
    ]


def suite_definitions(source: str, suite: str = "initial") -> list[dict]:
    """明确选择套件；未知名字不回退到旧例，避免把回归结果伪装为隔离结果。"""
    if suite == "initial":
        return definitions(source)
    if suite == "followup":
        return followup_definitions(source)
    raise ValueError("unknown evaluation suite")


def qualify_suite(source: str, cases: list[dict], contract: dict, suite: str) -> list[dict]:
    """初轮参考资格原样保留；新批次只登记两个实际不同条件，不重复计数。"""
    if suite == "initial":
        return _H.qualify(source, cases, contract)
    baseline = _H.reviewed_function(source)
    results = []
    for case in cases:
        candidate = _H.reviewed_function(case["candidate"])
        rows = []
        for label in ("primary", "control"):
            context = _H._override(contract["fixture"]["context"], case["input_field"], case[label], contract)
            old, new = baseline(deepcopy(context)), candidate(deepcopy(context))
            expected = case["expected_" + label]
            if [old["eligibility"], new["eligibility"]] != expected:
                raise ValueError("followup reference qualification failed")
            if any((case["critical_missing"] in result["missing_evidence"]) != (verdict == "NEEDS_MORE_EVIDENCE")
                   for result, verdict in zip((old, new), expected)):
                raise ValueError("followup reference missing fact mismatch")
            rows.append({"label": label, "inputs": {case["input_field"]: case[label]},
                         "context_sha256": _H.digest(context), "baseline": old, "candidate": new,
                         "expected_eligibility": expected, "expected_matches": True})
        results.append({"case_id": case["case_id"], "scope": "REVIEWED_SYNTHETIC_PURE_FUNCTION_ONLY",
                        "network_or_graph_execution": False, "rows": rows})
    return results


def _output_scope(path: Path) -> Path:
    """只在新实验/报告或临时位置写入；不能覆盖冻结文档和已有证据。"""
    resolved = path.resolve()
    allowed = [ROOT / "artifacts", ROOT / "reports", ROOT / "tmp", Path(tempfile.gettempdir()), Path("/private/tmp")]
    if not any(resolved.is_relative_to(base.resolve()) and resolved != base.resolve() for base in allowed):
        raise ValueError("output outside evidence scope")
    if resolved.name.lower() == "readme.md" or "docs" in resolved.parts:
        raise ValueError("frozen document output forbidden")
    return resolved


def _request(case_id: str, baseline: str, candidate: str) -> dict:
    """两项任务使用相同正常要求与措辞，不泄露故障类别或见证输入。"""
    titles = ["适用对象与政策选择", "窗口与原申请时点", "用量规则与完整性范围",
              "合同、发票与付款", "输出和失败语义", "重试与重新评估"]
    return {"schema_version": 1, "request_id": "review:" + case_id, "revision": 1,
            "requester_ref": "operator:local", "owner_ref": "owner:local",
            "goal": "审查客服商业判断模块重构：根据准确 diff 和适用要求提出高价值实验，检查应保持的业务行为。",
            "non_goals": ["不执行退款、公开消息或生产发布", "不以历史通过替代当前候选证据"],
            "baseline": {"commit": baseline, "reason": "本地合成比较基线，未声明为生产部署", "source_ref": "local:baseline"},
            "candidate": {"commit": candidate, "source_ref": "local:candidate"},
            "requirements": [{"id": f"R{index}", "kind": "PRESERVE", "statement": "保留" + title + "的已登记实验规则",
                "material_ref": f"rule:R{index}", "authority": "LOCAL_CALIBRATION", "status": "CONFIRMED_LOCAL",
                "owner_ref": "owner:local"} for index, title in enumerate(titles, 1)],
            "known_gaps": [], "predecessor_run_id": None}


def prepare(directory: Path, *, suite: str = "initial") -> dict:
    """实现与 Prompt 冻结后调用；公开结果只有身份、数量和文件路径。"""
    output = _output_scope(directory)
    if output.exists():
        raise ValueError("refusing to overwrite prepared evaluation")
    git = LocalGitReader(ROOT)
    source = git.read(_H.SOURCE_COMMIT, _H.SOURCE_PATH)
    support = {path: git.read(_H.SOURCE_COMMIT, path) for path in _H.SUPPORT_PATHS}
    contract_raw = (ROOT / _H.CONTRACT_PATH).read_bytes()
    contract = json.loads(contract_raw)
    if sha256(source).hexdigest() != contract["source_sha256"] or _H.digest(contract["fixture"]["context"]) != contract["fixture"]["sha256"]:
        raise ValueError("frozen authority identity mismatch")
    cases = suite_definitions(source.decode(), suite)
    qualifications = qualify_suite(source.decode(), cases, contract, suite)
    output.mkdir(mode=0o700, parents=True)
    (output / "model-input").mkdir(mode=0o700)
    (output / "evaluation-only").mkdir(mode=0o700)
    _H.private_json(output / "evaluation-only/authority-contract.json", contract)
    sample_role = "REGRESSION_ONLY" if suite == "initial" else "ISOLATED_UNTIL_INSPECTED_OR_USED_FOR_TUNING"
    manifest = {"schema_version": 1, "suite": suite, "sample_role": sample_role, "source_commit": _H.SOURCE_COMMIT, "source_path": _H.SOURCE_PATH,
                "scope": "REVIEWED_SYNTHETIC_PURE_FUNCTION_ONLY", "cases": []}
    for case in cases:
        repository = output / (case["case_id"] + "-repository")
        baseline, candidate = _H._pair(repository, source, case["candidate"], support)
        request = _request(case["case_id"], baseline, candidate)
        bundle = assemble_review_request(ROOT, request, candidate_root=repository, input_provenance="CALIBRATION_SYNTHETIC")
        bundle_path = "model-input/" + case["case_id"] + "-bundle.json"
        _H.private_json(output / bundle_path, bundle)
        _H.private_json(output / ("model-input/" + case["case_id"] + "-request.json"), request)
        rubric_path = "evaluation-only/" + case["case_id"] + "-rubric.json"
        _H.private_json(output / rubric_path, {key: value for key, value in case.items() if key != "candidate"})
        manifest["cases"].append({"case_id": case["case_id"], "baseline_sha": baseline, "candidate_sha": candidate,
                                  "repository": repository.name, "bundle_path": bundle_path, "rubric_path": rubric_path,
                                  "baseline_source_sha256": sha256(source).hexdigest(),
                                  "candidate_source_sha256": sha256(case["candidate"].encode()).hexdigest()})
    _H.private_json(output / "evaluation-only/manifest.json", manifest)
    _H.private_json(output / "evaluation-only/qualification.json", {"cases": qualifications})
    _H.private_json(output / "evaluation-only/criteria.json", {
        "scope": "KNOWN_REGRESSION_CASES" if suite == "initial" else "TWO_NEW_LOCAL_CHANGES_NOT_POPULATION_ACCURACY",
        "suite": suite, "sample_role": sample_role, "all_runs_retained": True,
        "initial_cases_status": "REGRESSION_ONLY_AFTER_FIRST_EVALUATION",
        "no_golden_input_completion": True, "missing_accepted_plan": "NOT_RUN",
        "acceptance": ["exact frozen task and accepted draft identity", "independently rebound original model inputs",
            "actual main/control witness exercises the changed business relation", "correct change claim and applicable rule",
            "model B/C predictions match observed outputs", "baseline normative assertion holds",
            "defect: candidate violates the normative assertion; benign: candidate retains it",
            "baseline main/control relation holds; benign relation also holds on candidate"],
        "manual_review_pending": ["quality of source-grounded explanations", "unnecessary reads and missed risks"],
        "no_runtime_dispatch_or_final_qualification": True,
        "after_inspection_or_tuning": "REGRESSION_ONLY_REQUIRE_NEW_UNSEEN_CHANGES"})
    files = sorted(path for folder in (output / "model-input", output / "evaluation-only") for path in folder.glob("*.json"))
    build_files = [path for folder in (ROOT / "packages/verify-runtime/dist", ROOT / "packages/contracts/dist")
                   for path in folder.glob("*.js") if not path.name.endswith(".test.js")]
    if not build_files:
        raise ValueError("runtime build required before freeze")
    source_files = {path for folder in (ROOT / "packages/verify-runtime/src", ROOT / "packages/contracts/src")
                    for path in folder.glob("*.ts") if not path.name.endswith(".test.ts")}
    source_files.update((ROOT / "services/verify-agent/src/verify_agent").glob("*.py"))
    for folder in (ROOT / "protocols/review-request", ROOT / "protocols/verify-agent"):
        source_files.update(folder.rglob("*.json"))
    source_files.update(ROOT / path for path in (
        "services/verify-agent/planner-prompt.txt", "tools/registry/release_assembler.py",
        "tools/registry/review_request.py", "tools/verify-agent-live.py"))
    frozen = {"schema_version": 1, "suite": suite, "sample_role": sample_role, "prepared_at": datetime.now(timezone.utc).isoformat(), "case_count": len(cases),
              "status": "PREPARED_NOT_MODEL_EVALUATED", "generator_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
              "helper_sha256": sha256((ROOT / "tools/verify-agent-binding-holdout.py").read_bytes()).hexdigest(),
              "files": [{"path": str(path.relative_to(output)), "sha256": sha256(path.read_bytes()).hexdigest()} for path in files],
              "implementation_sources": [{"path": str(path.relative_to(ROOT)), "sha256": sha256(path.read_bytes()).hexdigest()} for path in sorted(source_files)],
              "runtime_build": [{"path": str(path.relative_to(ROOT)), "sha256": sha256(path.read_bytes()).hexdigest()} for path in sorted(build_files)]}
    _H.private_json(output / "freeze.json", frozen)
    return {"status": "PREPARED", "suite": suite, "sample_role": sample_role, "case_count": len(cases), "scope": manifest["scope"], "freeze_path": str(output / "freeze.json"),
            "freeze_sha256": sha256((output / "freeze.json").read_bytes()).hexdigest(),
            "bundle_paths": [str(output / row["bundle_path"]) for row in manifest["cases"]]}


def _accepted_check(ledger: dict, bundle: dict) -> tuple[dict | None, dict]:
    """同时核对已完成接受操作、原草案操作与权威状态，不能挑未获接受的最好提案。"""
    run = ledger["run"]
    if run["bundle"] != bundle or str(run["id"]) != ledger["run_id"]:
        raise ValueError("evaluation task identity mismatch")
    operations = ledger["operations"]
    accepted = [row for row in operations if row["proposal"].get("schema_version") == 3
                and row["proposal"]["action_kind"] == "ACCEPT_PLAN" and row["status"] == "COMPLETED"
                and (row.get("result") or {}).get("status") == "PLAN_ACCEPTED_FOR_REVIEW"]
    counts = {"model_calls": len(ledger.get("calls", [])),
              "drafts": sum(row["proposal"]["action_kind"] == "PLAN_DRAFT" for row in operations),
              "accepted_plans": len(accepted), "runtime_status": run["state"]["status"]}
    if not accepted:
        return None, counts
    if len(accepted) != 1:
        raise ValueError("ambiguous accepted plan")
    receipt = accepted[0]["result"]
    draft = run["state"]["review_cycle"]["draft"]
    original = [row for row in operations if "plan:" + row["id"] == receipt["draft_ref"]]
    if (len(original) != 1 or original[0]["proposal"]["action_kind"] != "PLAN_DRAFT"
            or original[0]["status"] != "COMPLETED" or original[0]["result"]["status"] != "BOUND_PLAN"
            or draft["status"] != "BOUND_PLAN" or draft["ref"] != receipt["draft_ref"]
            or draft["digest"] != receipt["draft_digest"] or draft["proposal"] != original[0]["proposal"]
            or run["state"]["review_cycle"]["accepted_plan_ref"] != draft["ref"]
            or accepted[0]["proposal"]["payload"] != {"draft_ref": draft["ref"], "draft_digest": draft["digest"]}
            or original[0]["result"]["draft_digest"] != draft["digest"]
            or receipt["compiled_check"] != draft["compiled"]["payload"]
            or original[0]["result"]["compiled_check"] != receipt["compiled_check"]):
        raise ValueError("accepted draft binding mismatch")
    if run["state"]["status"] != "PLANNED" or run["state"].get("stop_reason") != "SCOPED_PLAN_ACCEPTED":
        raise ValueError("accepted plan state mismatch")
    # 编译器只可补机械身份；逐项确认离线运行的业务值确实来自模型原草案。
    raw = original[0]["proposal"]["payload"]["draft"]
    check = receipt["compiled_check"]
    spec = check["check_spec"]
    if (check["objective"] != raw["objective"] or spec["inputs"] != raw["inputs"]
            or spec["control"]["inputs"] != raw["control_inputs"]
            or spec["control"]["expected_relation"] != raw["control_relation"]
            or len(spec["observations"]) != 1 or spec["observations"][0]["field"] != raw["observation_field"]
            or len(spec["expectations"]) != 1
            or any(spec["expectations"][0][field] != raw[source] for field, source in
                   (("operator", "operator"), ("value", "expected_value"), ("requirement_ref", "requirement_ref")))
            or any(spec["change_relation"][field] != raw[field] for field in
                   ("diff_ref", "changed_fields", "claim", "requirement_ref", "expected_baseline", "expected_candidate"))):
        raise ValueError("compiled business values differ from model draft")
    return draft["compiled"], {**counts, "accepted_operation_id": accepted[0]["id"],
                               "draft_ref": draft["ref"], "draft_digest": draft["digest"]}


def relation_witness(definition: dict, contexts: dict, outputs: dict) -> bool:
    """按预登记关系核对模型自己的条件；不能仅凭两次结果恰好不同就计为有效。"""
    critical = definition["critical_missing"]
    if definition["relation_kind"] == "BROKEN":
        behavior = (critical in outputs["baseline"]["primary"]["missing_evidence"]
                    and outputs["candidate"]["primary"]["eligibility"] == "ELIGIBLE"
                    and all(outputs[v]["control"]["eligibility"] == "ELIGIBLE" for v in outputs))
        if definition["case_id"] == "review_a":
            relation = (contexts["primary"]["commercial_source"]["payment"]["currency"] != contexts["primary"]["billing"]["currency"]
                        and contexts["control"]["commercial_source"]["payment"]["currency"] == contexts["control"]["billing"]["currency"])
        elif definition["case_id"] == "review_c":
            relation = (_H._field(contexts["primary"], definition["input_field"]) is True
                        and _H._field(contexts["control"], definition["input_field"]) is False)
        else:
            raise ValueError("undefined broken relation witness")
        return behavior and relation
    if definition["case_id"] == "review_b":
        bad = lambda context: _H._field(context, definition["input_field"]) == 1
        good = lambda context: _H._field(context, definition["input_field"]) >= 2
    elif definition["case_id"] == "review_d":
        bad = lambda context: _H._field(context, definition["input_field"]) == "DRAFT"
        good = lambda context: _H._field(context, definition["input_field"]) == "ACTIVE"
    else:
        raise ValueError("undefined preserved relation witness")
    return (all(outputs["baseline"][label] == outputs["candidate"][label] for label in contexts)
            and any(bad(contexts[label]) and critical in outputs["baseline"][label]["missing_evidence"] for label in contexts)
            and any(good(contexts[label]) and outputs["baseline"][label]["eligibility"] == "ELIGIBLE" for label in contexts))


def evaluate(prepared: Path, case_id: str, evidence_file: Path) -> dict:
    """原样执行模型选择；实际 B/C 输出只用于预登记规则的局部离线见证。"""
    prepared = prepared.resolve()
    frozen = json.loads((prepared / "freeze.json").read_text())
    if sha256(Path(__file__).read_bytes()).hexdigest() != frozen["generator_sha256"]:
        raise ValueError("evaluation implementation changed after freeze")
    if sha256((ROOT / "tools/verify-agent-binding-holdout.py").read_bytes()).hexdigest() != frozen["helper_sha256"]:
        raise ValueError("evaluation helper changed after freeze")
    for base, rows in ((prepared, frozen["files"]), (ROOT, frozen["runtime_build"]), (ROOT, frozen["implementation_sources"])):
        for row in rows:
            path = (base / row["path"]).resolve()
            if not path.is_relative_to(base) or sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                raise ValueError("frozen evaluation material changed")
    manifest = json.loads((prepared / "evaluation-only/manifest.json").read_text())
    cases = [row for row in manifest["cases"] if row["case_id"] == case_id]
    if len(cases) != 1:
        raise ValueError("unknown evaluation case")
    case = cases[0]
    bundle = json.loads((prepared / case["bundle_path"]).read_text())
    ledger = json.loads(evidence_file.read_text())
    proposal, counts = _accepted_check(ledger, bundle)
    base_result = {"scope": manifest["scope"], "suite": manifest.get("suite", "initial"), "case_id": case_id, "run_id": ledger["run_id"], **counts,
                   "source_ledger_sha256": sha256(evidence_file.read_bytes()).hexdigest(),
                   "business_execution_via_runtime": False, "current_candidate_coverage_added": False,
                   "release_authorized": False, "population_accuracy_claim": False}
    if proposal is None:
        return {**base_result, "status": "NOT_RUN", "reason": "NO_ACCEPTED_PLAN", "effective_plan": False}
    contract = json.loads((prepared / "evaluation-only/authority-contract.json").read_text())
    node = shutil.which("node")
    if not node:
        raise ValueError("node required for deterministic rebinding")
    process = subprocess.run([node, "--input-type=module", "-e", REBIND], cwd=ROOT,
                             input=json.dumps({"bundle": bundle, "proposal": proposal, "contract": contract}),
                             text=True, capture_output=True, timeout=20,
                             env={key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ})
    if process.returncode != 0:
        raise ValueError("deterministic rebinding failed")
    binding = json.loads(process.stdout)
    if binding["status"] != "BOUND_PLAN":
        raise ValueError("accepted check no longer binds")
    compiled = binding["compiled_plan"]
    contexts = {label: compiled[label + "_context"] for label in ("primary", "control")}
    if any(_H.digest(context) != compiled[label + "_context_sha256"] for label, context in contexts.items()):
        raise ValueError("context identity mismatch")
    # 冻结文件摘要之外，再匹配固定源码与本脚本唯一变更，避免成为任意候选执行器。
    trusted_source = LocalGitReader(ROOT).read(_H.SOURCE_COMMIT, _H.SOURCE_PATH).decode()
    definition = next(row for row in suite_definitions(trusted_source, manifest.get("suite", "initial")) if row["case_id"] == case_id)
    git = LocalGitReader(prepared / case["repository"])
    outputs = {}
    for version in ("baseline", "candidate"):
        raw = git.read(case[version + "_sha"], _H.SOURCE_PATH)
        expected = trusted_source if version == "baseline" else definition["candidate"]
        if raw.decode() != expected or sha256(raw).hexdigest() != case[version + "_source_sha256"]:
            raise ValueError("reviewed code identity mismatch")
        function = _H.reviewed_function(raw.decode())
        outputs[version] = {label: function(deepcopy(context)) for label, context in contexts.items()}
    assertions = compiled["assertions"]
    observed = {row["observation_ref"]: row["field"] for row in assertions}
    relation = compiled["change_relation"]
    predictions = [{"version": version, "observation_ref": obs, "expected": expected,
                    "actual": _H._field({"decision": outputs[version]["primary"]}, observed[obs]),
                    "matches": _H._same_json_value(_H._field({"decision": outputs[version]["primary"]}, observed[obs]), expected)}
                   for version in ("baseline", "candidate") for obs, expected in relation["expected_" + version].items()]
    assertion_results = [{"version": version, **row,
                          "satisfied": _H._assertion(_H._field({"decision": outputs[version]["primary"]}, row["field"]), row["operator"], row["value"])}
                         for version in ("baseline", "candidate") for row in assertions]
    control = compiled["control_relation"]
    controls = []
    for version in ("baseline", "candidate"):
        field = observed[control["observation_ref"]]
        values = [_H._field({"decision": outputs[version][label]}, field) for label in ("primary", "control")]
        actual_relation = "SAME" if _H._same_json_value(*values) else "DIFFERENT"
        controls.append({"version": version, "actual_relation": actual_relation,
                         "expected_relation": control["expected_relation"], "matches": actual_relation == control["expected_relation"]})
    witness = relation_witness(definition, contexts, outputs)
    baseline_assertions = all(row["satisfied"] for row in assertion_results if row["version"] == "baseline")
    candidate_assertions = all(row["satisfied"] for row in assertion_results if row["version"] == "candidate")
    expected_assertion_behavior = baseline_assertions and (not candidate_assertions if definition["relation_kind"] == "BROKEN" else candidate_assertions)
    relation_matches = relation["claim"] == definition["claim"] and relation["requirement_ref"] == definition["requirement"]
    control_matches = controls[0]["matches"] and (definition["relation_kind"] == "BROKEN" or controls[1]["matches"])
    effective = witness and relation_matches and all(row["matches"] for row in predictions) and expected_assertion_behavior and control_matches
    return {**base_result, "status": "EVALUATED", "compiled_plan_sha256": _H.digest(compiled),
            "contexts_used_without_golden_completion": True, "outputs": outputs, "model_predictions": predictions,
            "assertion_results": assertion_results, "control_results": controls, "relation_witness": witness,
            "relation_claim_and_rule_match": relation_matches, "expected_assertion_behavior": expected_assertion_behavior,
            "effective_plan": effective, "manual_explanation_review": "PENDING"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--output-directory", type=Path, required=True)
    prepare_parser.add_argument("--suite", choices=["initial", "followup"], default="initial")
    evaluate_parser = commands.add_parser("evaluate")
    evaluate_parser.add_argument("--prepared", type=Path, required=True)
    evaluate_parser.add_argument("--case-id", choices=["review_a", "review_b", "review_c", "review_d"], required=True)
    evaluate_parser.add_argument("--full-evidence", type=Path, required=True)
    evaluate_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(args.output_directory, suite=args.suite)
        else:
            result = evaluate(args.prepared, args.case_id, args.full_evidence)
            output = _output_scope(args.output)
            _H.private_json(output, result)
            result = {key: result[key] for key in ("status", "case_id", "effective_plan", "scope")}
            result["output"] = str(output)
    except (MaterialError, OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "REJECTED", "error_code": getattr(error, "code", type(error).__name__)}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
