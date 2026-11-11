"""只读目录 CLI，调用链：可信本地范围 → CatalogReader → Resolver/TaskAssembler。

用法：tools/registry/.venv/bin/python -m tools.registry.catalog_cli --help
search/describe/read 只返回 JSON；assemble 仅在明确指定新目录时导出本地任务包。
不存在 dispatch、模型出口或凭据参数；task 文件不能声明自己的审批和权限。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .catalog_reader import CATALOG_COMMIT, load_catalog, local_plan_scope
from .reference_resolver import MaterialError
from .task_assembler import TaskAssemblyError, assemble_test_context
from .validate_catalog import CatalogError, ROOT


def _unique_mapping(pairs):
    """拒绝重复键，避免 task 的权限/候选字段在人读与程序读之间出现歧义。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise MaterialError("TASK_JSON_DUPLICATE_KEY")
        result[key] = value
    return result


def _read_task(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise MaterialError("TASK_FILE_INVALID")
    with path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise MaterialError("TASK_FILE_INVALID")
    value = json.loads(raw, object_pairs_hook=_unique_mapping)
    if not isinstance(value, dict):
        raise MaterialError("TASK_FILE_INVALID")
    return value


def _export(context: dict, requested: Path) -> dict:
    """显式本地交付：新建 0700 目录和 0600 文件，任何已有目标都拒绝覆盖。

装配函数本身没有写入副作用。这里不归档原始实验数据，只导出已经按本地范围投影的
上下文和缺口。导出不是模型传输或实验派发，也不替换旧运行证据。
"""
    if requested.name in {"", ".", ".."}:
        raise MaterialError("OUTPUT_DIRECTORY_INVALID")
    destination = requested.parent.resolve(strict=True) / requested.name
    try:
        destination.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError:
        raise MaterialError("OUTPUT_ALREADY_EXISTS") from None
    gap_report = context["gaps_and_next_requests"]
    outputs = {"task-context.json": context, "readiness.json": gap_report["readiness"],
               "gaps.json": {key: value for key, value in gap_report.items() if key != "readiness"}}
    for name, body in outputs.items():
        # 独占创建防止意外覆盖；若中途磁盘故障，保留部分交付供诊断，不能重用目录。
        descriptor = os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(body, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    return {"status": "EXPORTED_PLAN_ONLY", "output_directory": str(destination),
            "files": list(outputs), "product_candidate_ref": None, "execution_authorized": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--catalog-commit", default=CATALOG_COMMIT)
    commands = parser.add_subparsers(dest="action", required=True)
    search = commands.add_parser("search")
    search.add_argument("--query")
    search.add_argument("--kind")
    search.add_argument("--maintainer-role")
    search.add_argument("--scenario-id")
    search.add_argument("--capability-kind")
    search.add_argument("--business-boundary")
    describe = commands.add_parser("describe")
    describe.add_argument("asset_id")
    describe.add_argument("--kind")
    read = commands.add_parser("read")
    read.add_argument("material_ref")
    read.add_argument("--start-line", type=int)
    read.add_argument("--end-line", type=int)
    assemble = commands.add_parser("assemble")
    assemble.add_argument("--task", type=Path, required=True)
    assemble.add_argument("--output-directory", type=Path)
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve(strict=True)
        scope = local_plan_scope(root, args.catalog_commit)
        reader = load_catalog(root, args.catalog_commit, scope)
        if args.action == "search":
            result = reader.search_catalog(query=args.query, kind=args.kind, maintainer_role=args.maintainer_role,
                                           scenario_id=args.scenario_id, capability_kind=args.capability_kind,
                                           business_boundary=args.business_boundary)
        elif args.action == "describe":
            result = reader.describe_asset(args.asset_id, kind=args.kind)
        elif args.action == "read":
            result = reader.resolver.read_registered_material(args.material_ref, args.start_line, args.end_line)
        else:
            result = assemble_test_context(reader, _read_task(args.task))
            if args.output_directory is not None:
                result = _export(result, args.output_directory)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (MaterialError, TaskAssemblyError, CatalogError) as error:
        print(json.dumps({"status": "FAILED", "error_code": getattr(error, "code", str(error))}, ensure_ascii=False))
        return 1
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        # 不将文件正文、绝对私有路径或底层解析异常写入错误输出。
        print(json.dumps({"status": "FAILED", "error_code": "CATALOG_OPERATION_UNCONFIRMED"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
