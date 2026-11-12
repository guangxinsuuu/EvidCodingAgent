"""可恢复的最小调查图：Runtime 投影 → 模型提案 → 动作准入 → 新投影。

LangGraph 组织控制流，PostgreSQL Runtime 是唯一持久执行账本。本图没有另一套
SQLite/内存“完成状态”，也不把 graph checkpoint 当作外部效果的幂等保障。
进程重启总从 load_and_reconcile 进入；已完成模型调用返回 pending_decision，
已登记动作通过 Runtime 投影恢复，未知模型调用阻塞而不自动重新推理。
"""
from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from .protocol import ProtocolError, validate_proposal
from .runtime_port import RuntimePort, RuntimeTransportError


RUN_STATUSES = {"ACTIVE", "PLANNED", "STOPPED", "CANCELLED", "BLOCKED"}


class AgentState(TypedDict, total=False):
    run_id: str
    projection: dict
    proposal: dict | None
    last_command_id: str | None
    last_submitted_id: str | None
    admission: dict | None
    iterations: int
    trace: list[dict]
    outcome: str | None


def _integer(value: object, minimum: int) -> bool:
    return type(value) is int and minimum <= value <= 9_007_199_254_740_991


def _validate_projection(projection: object, run_id: str) -> dict:
    """只检查图路由需要的硬条件；业务准入仍以 Runtime 的事务校验为准。"""
    if (not isinstance(projection, dict) or projection.get("run_id") != run_id
            or projection.get("status") not in RUN_STATUSES
            or not _integer(projection.get("decision_basis_revision"), 0)
            or not _integer(projection.get("candidate_epoch"), 1)
            or not isinstance(projection.get("context_view_ref"), str)
            or not isinstance(projection.get("next_command_id"), str)):
        raise ProtocolError("INVALID_RUNTIME_PROJECTION")
    pending = projection.get("pending_decision")
    if pending is not None and (not isinstance(pending, dict)
                                or pending.get("status") != "COMPLETED"
                                or not isinstance(pending.get("model_call_id"), str)):
        raise ProtocolError("INVALID_PENDING_DECISION")
    # V0.1 永远不能通过投影声称已有当前候选覆盖或 Final 资格。
    # 后续版本若支持执行，需要显式版本化，而不是放宽本期边界。
    if (projection.get("current_candidate_coverage") != []
            or not isinstance(projection.get("final_eligibility"), dict)
            or projection["final_eligibility"].get("eligible") is not False):
        raise ProtocolError("UNSUPPORTED_VERIFICATION_CLAIM")
    return projection


def _bound_proposal(proposal: object, projection: dict) -> dict:
    validated = validate_proposal(proposal)
    if (validated["based_on_decision_revision"] != projection["decision_basis_revision"]
            or validated["candidate_epoch"] != projection["candidate_epoch"]
            or validated["context_view_ref"] != projection["context_view_ref"]):
        raise ProtocolError("STALE_DECISION_BINDING")
    return validated


def build_graph(runtime: RuntimePort, *, max_iterations: int = 32):
    """构建有界调查图；runtime 是受信启动时注入的端口，不是模型提供的工具。

每个 decide 使用服务器给出的稳定 command_id。读取反馈后，由服务器的新
decision_basis_revision/next_command_id 决定下一次调用；本图不自行重试 Provider。
max_iterations 只是进程保护，不增加模型/读取预算，也不把暂停转换成业务成功。
"""
    if type(max_iterations) is not int or not 1 <= max_iterations <= 64:
        raise ValueError("INVALID_ITERATION_LIMIT")

    def trace(state: AgentState, node: str, **detail) -> list[dict]:
        return [*state.get("trace", []), {"node": node, **detail}]

    def load_and_reconcile(state: AgentState) -> dict:
        try:
            projection = _validate_projection(runtime.get_run_projection(state["run_id"]), state["run_id"])
            return {"projection": projection, "proposal": None,
                    "trace": trace(state, "load_and_reconcile", status=projection["status"],
                                   decision_basis_revision=projection["decision_basis_revision"])}
        except (RuntimeTransportError, ProtocolError) as error:
            return {"outcome": str(error), "trace": trace(state, "load_and_reconcile", error_code=str(error))}

    def decide_next_action(state: AgentState) -> dict:
        projection = state["projection"]
        command_id = projection["next_command_id"]
        try:
            response = runtime.decide(state["run_id"], command_id, projection["decision_basis_revision"])
            status = response.get("status")
            if status not in {"COMPLETED", "INVALID", "UNKNOWN", "BLOCKED", "DISPATCHED", "CANCELLED"}:
                raise ProtocolError("INVALID_MODEL_CALL_STATE")
            proposal = _bound_proposal(response.get("proposal"), projection) if status == "COMPLETED" else None
            return {"proposal": proposal, "last_command_id": command_id,
                    "iterations": state.get("iterations", 0) + 1,
                    "trace": trace(state, "decide_next_action", status=status,
                                   model_call_id=response.get("model_call_id"),
                                   action_kind=proposal["action_kind"] if proposal else None)}
        except (RuntimeTransportError, ProtocolError) as error:
            # 不知道服务是否已接受本次请求时立刻交回操作者；再次启动会先 reconcile。
            return {"outcome": str(error), "trace": trace(state, "decide_next_action", error_code=str(error))}

    def register_action(state: AgentState) -> dict:
        try:
            pending = state["projection"].get("pending_decision")
            candidate = state.get("proposal") or (pending.get("proposal") if pending else None)
            proposal = _bound_proposal(candidate, state["projection"])
            admission = runtime.submit_proposal(state["run_id"], proposal)
            if not isinstance(admission.get("status"), str):
                raise ProtocolError("INVALID_ADMISSION_RESPONSE")
            return {"proposal": proposal, "admission": admission, "last_submitted_id": proposal["proposal_id"],
                    "trace": trace(state, "register_action", proposal_id=proposal["proposal_id"],
                                   action_kind=proposal["action_kind"], status=admission["status"],
                                   operation_id=admission.get("operation_id"))}
        except (RuntimeTransportError, ProtocolError) as error:
            return {"outcome": str(error), "trace": trace(state, "register_action", error_code=str(error))}

    def ingest_and_update(state: AgentState) -> dict:
        try:
            projection = _validate_projection(runtime.get_run_projection(state["run_id"]), state["run_id"])
            return {"projection": projection, "proposal": None,
                    "trace": trace(state, "ingest_and_update", status=projection["status"],
                                   decision_basis_revision=projection["decision_basis_revision"])}
        except (RuntimeTransportError, ProtocolError) as error:
            return {"outcome": str(error), "trace": trace(state, "ingest_and_update", error_code=str(error))}

    def route(state: AgentState) -> str:
        if state.get("outcome") is not None:
            return "finish"
        projection = state["projection"]
        if projection["status"] != "ACTIVE":
            return "finish"
        if projection.get("pending_decision") is not None:
            if projection["pending_decision"].get("proposal", {}).get("proposal_id") == state.get("last_submitted_id"):
                return "finish"
            return "register_action"
        if state.get("iterations", 0) >= max_iterations:
            return "finish"
        # 未知/在途请求，或服务投影尚未推进，不以 LLM 重复轮询等待。
        if state.get("last_command_id") == projection["next_command_id"]:
            return "finish"
        return "decide_next_action"

    def after_decide(state: AgentState) -> str:
        if state.get("outcome") is not None:
            return "finish"
        return "register_action" if state.get("proposal") else "ingest_and_update"

    def finish(state: AgentState) -> dict:
        if state.get("outcome"):
            outcome = state["outcome"]
        elif state["projection"]["status"] != "ACTIVE":
            outcome = state["projection"]["status"]
        elif state.get("iterations", 0) >= max_iterations:
            outcome = "LOCAL_STEP_LIMIT"
        else:
            outcome = "WAITING_RUNTIME"
        return {"outcome": outcome, "trace": trace(state, "finish", outcome=outcome)}

    graph = StateGraph(AgentState)
    graph.add_node("load_and_reconcile", load_and_reconcile)
    graph.add_node("decide_next_action", decide_next_action)
    graph.add_node("register_action", register_action)
    graph.add_node("ingest_and_update", ingest_and_update)
    graph.add_node("finish", finish)
    graph.add_edge(START, "load_and_reconcile")
    graph.add_conditional_edges("load_and_reconcile", route,
                                ["decide_next_action", "register_action", "finish"])
    graph.add_conditional_edges("decide_next_action", after_decide,
                                ["register_action", "ingest_and_update", "finish"])
    graph.add_conditional_edges("register_action", lambda state: "finish" if state.get("outcome") else "ingest_and_update",
                                ["finish", "ingest_and_update"])
    graph.add_conditional_edges("ingest_and_update", route,
                                ["decide_next_action", "register_action", "finish"])
    graph.add_edge("finish", END)
    return graph.compile()


def run_agent(runtime: RuntimePort, run_id: str, *, max_iterations: int = 32) -> dict:
    """CLI → run_agent → LangGraph → RuntimePort；返回脱敏的流程/任务投影。

关闭第三方自动 trace，防止运行环境遗留的 LangSmith 配置把本地业务资料外发。
本期仅通过 Runtime 保存可审查的实际模型输入、响应和操作账本。
"""
    with tracing_context(enabled=False):
        state = build_graph(runtime, max_iterations=max_iterations).invoke(
            {"run_id": run_id, "iterations": 0, "trace": [], "outcome": None,
             "proposal": None, "last_command_id": None, "last_submitted_id": None, "admission": None},
            config={"recursion_limit": max_iterations * 4 + 8, "callbacks": []},
        )
    projection = state.get("projection", {})
    safe_keys = ("status", "decision_basis_revision", "candidate_epoch", "context_view_ref",
                 "task_identity", "budgets", "model_calls", "operations", "hypotheses",
                 "read_receipts", "current_candidate_coverage", "final_eligibility", "latest_feedback")
    return {"schema_version": 1, "run_id": run_id, "agent_version": "0.1.0",
            "outcome": state["outcome"], "iterations_this_process": state["iterations"],
            "authority": "TS_POSTGRESQL_RUNTIME", "checkpoint_mode": "REBUILD_FROM_RUNTIME_PROJECTION",
            "business_verdict": "NOT_EVALUATED", "trace": state["trace"],
            "projection": {key: projection[key] for key in safe_keys if key in projection}}
