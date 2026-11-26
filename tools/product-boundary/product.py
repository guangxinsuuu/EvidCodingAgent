#!/usr/bin/env python3
"""pnpm support:* → EGAP 受信宿主 → 固定 Git 构建上下文 / 独立 Docker 工程。

不运行产品 checkout 里的 deploy.py，也不使用产品 .venv。候选只在构建与运行
容器内执行；宿主复制固定 Git 对象，不携带产品工作树中的 .local/凭据。
此本机 Docker 信任域仍不等于企业沙盒、远端构建签名或 IAM。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from uuid import uuid4

import deployment

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.registry.product_repository import ProductGitReader
from tools.registry.reference_resolver import LocalGitReader, MaterialError

STATE = ROOT / "artifacts/product-host"
BUILDS = STATE / "builds"
PYTHON = Path(__file__).resolve().parent / ".venv/bin/python"
SYSTEM_ENV = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checkout(repo: Path) -> ProductGitReader:
    return ProductGitReader(repo, product_id="evid-support", authority_root=ROOT)


def build_context(repo: Path, commit: str, destination: Path) -> dict:
    """Git blob → 新的空目录；拒绝软链接/子模块，不执行 export-ignore、filter 或 hook。

    构建上下文来自已提交普通文件，工作树中即使有明文凭据也不进入 Docker。
    清单覆盖传给构建器的字节；Dockerfile 对镜像内容的选择仍属于待审查产品变化。
    """
    git = checkout(repo)
    if not re.fullmatch(r"[a-f0-9]{40}", commit) or git._git("cat-file", "-t", commit).strip() != b"commit":
        raise MaterialError("PRODUCT_COMMIT_REQUIRED")
    listing = git._git("ls-tree", "-r", "-z", commit).split(b"\0")
    reader = LocalGitReader(repo)
    files, total = [], 0
    for entry in filter(None, listing):
        meta, raw_path = entry.split(b"\t", 1)
        mode, kind, blob = meta.split()
        path = raw_path.decode("utf-8")
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise MaterialError("BUILD_CONTEXT_REGULAR_FILES_ONLY")
        if any(part in {".local", "artifacts", ".venv"} for part in Path(path).parts):
            raise MaterialError("BUILD_CONTEXT_PRIVATE_PATH")
        raw = reader.read(commit, path)
        total += len(raw)
        if total > 20 * 1024 * 1024 or len(files) >= 2000:
            raise MaterialError("BUILD_CONTEXT_TOO_LARGE")
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        target.chmod(0o755 if mode == b"100755" else 0o644)
        files.append({"path": path, "blob": blob.decode(), "sha256": hashlib.sha256(raw).hexdigest(), "mode": mode.decode()})
    if not (destination / "Dockerfile").is_file():
        raise MaterialError("PRODUCT_DOCKERFILE_REQUIRED")
    return {"repository_url": git.profile["repository_url"], "commit": commit,
            "tree": git._git("rev-parse", commit + "^{tree}").decode().strip(), "files": files,
            "bytes": total, "scope": "LOCAL_SYNTHETIC_PRODUCT"}


def build(repo: Path, commit: str) -> dict:
    """构建前登记操作；未知结果不由此入口自动重试，build-status 查询同一 tag。"""
    operation = uuid4().hex
    BUILDS.mkdir(parents=True, exist_ok=True)
    record_path = BUILDS / (operation + ".json")
    tag = "evid-support-build:" + operation
    with tempfile.TemporaryDirectory(prefix="evid-context-") as temporary:
        context = Path(temporary) / "context"
        context.mkdir()
        source = build_context(repo, commit, context)
        record = {"schema_version": 1, "operation_id": operation, "tag": tag, "status": "BUILDING", "source": source,
                  "egap_commit": deployment.command(["git", "rev-parse", "HEAD"]), "host_source_sha256": sha(Path(__file__))}
        deployment.save(record_path, record)
        try:
            result = subprocess.run(["docker", "build", "--build-arg", "PRODUCT_REVISION=" + commit,
                "--tag", tag, str(context)], cwd=ROOT, capture_output=True, text=True, timeout=600,
                env={key:value for key,value in os.environ.items() if key in SYSTEM_ENV})
        except subprocess.TimeoutExpired:
            record["status"] = "UNKNOWN"
            deployment.save(record_path, record)
            return {"status": "UNKNOWN", "operation_id": operation}
        log = BUILDS / (operation + ".private.log")
        with log.open("w") as stream:
            os.chmod(log, 0o600)
            stream.write(result.stdout + result.stderr)
        if result.returncode:
            record["status"] = "FAILED"
            deployment.save(record_path, record)
            return {"status": "FAILED", "operation_id": operation}
    return reconcile_build(operation)


def reconcile_build(operation: str) -> dict:
    """只查询原构建的唯一 tag；不重复执行 Dockerfile 或下载依赖。"""
    if not re.fullmatch(r"[a-f0-9]{32}", operation):
        raise MaterialError("BUILD_OPERATION_INVALID")
    path = BUILDS / (operation + ".json")
    record = json.loads(path.read_text())
    image = json.loads(deployment.command(["docker", "image", "inspect", record["tag"]]))[0]
    if image["Config"].get("Labels", {}).get("org.opencontainers.image.revision") != record["source"]["commit"]:
        raise MaterialError("BUILD_REVISION_MISMATCH")
    record.update(status="BUILT", image_id=image["Id"])
    deployment.save(path, record)
    deployment.save(BUILDS / (image["Id"].removeprefix("sha256:") + ".image.json"), record)
    return {"status": "BUILT", "operation_id": operation, "image_id": image["Id"], "product_commit": record["source"]["commit"]}


def registered_directory(identity: str | None) -> Path:
    if identity is None:
        identity = json.loads((STATE / "active.json").read_text())["deployment_id"]
    if not re.fullmatch(r"evid-sut-[a-f0-9]{12}", identity):
        raise MaterialError("DEPLOYMENT_ID_INVALID")
    directory = deployment.DEPLOYMENTS / identity
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest["deployment_id"] != identity or sha(directory / "compose.json") != manifest["compose_sha256"]:
        raise MaterialError("DEPLOYMENT_SPEC_CHANGED")
    return directory


def start(directory: Path) -> dict:
    """只启动登记工程，不更新候选或凭据；Compose 身份和文件摘要由调用方先检查。"""
    deployment.command(["docker", "compose", "-f", str(directory / "compose.json"), "up", "-d"])
    receipt = deployment.inspect(directory)
    if set(receipt["endpoints"]) != {"api", "database"}:
        raise MaterialError("DEPLOYMENT_ENDPOINTS_NOT_READY")
    if any(row["state"] != "running" for row in receipt["containers"] if row["role"] != "init"):
        raise MaterialError("PRODUCT_START_INCOMPLETE")
    deployment.save(STATE / "active.json", {"deployment_id": receipt["deployment_id"]})
    return {"status": "STARTED_NOT_BUSINESS_VERIFIED", "deployment_id": receipt["deployment_id"],
        "product_commit": receipt["product_revision"], "image_id": receipt["product_image_id"], "endpoints": receipt["endpoints"]}


def up(repo: Path, image: str) -> dict:
    """只接纳有本宿主构建回执的固定镜像；产品自贴 commit label 不足以获得部署资格。"""
    product = checkout(repo)
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise MaterialError("IMMUTABLE_IMAGE_REQUIRED")
    record = json.loads((BUILDS / (image.removeprefix("sha256:") + ".image.json")).read_text())
    if record["status"] != "BUILT" or record["image_id"] != image or record["source"]["repository_url"] != product.profile["repository_url"]:
        raise MaterialError("TRUSTED_BUILD_RECEIPT_REQUIRED")
    if product._git("rev-parse", record["source"]["commit"] + "^{tree}").decode().strip() != record["source"]["tree"]:
        raise MaterialError("PRODUCT_TREE_MISMATCH")
    directory = deployment.prepare(image)
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest["product_revision"] != record["source"]["commit"]:
        raise MaterialError("DEPLOYMENT_BUILD_MISMATCH")
    manifest.update(compose_sha256=sha(directory / "compose.json"), build_operation_id=record["operation_id"],
        product_repository_url=product.profile["repository_url"], egap_host_commit=deployment.command(["git", "rev-parse", "HEAD"]),
        egap_host_source_sha256=sha(Path(__file__)))
    deployment.save(directory / "manifest.json", manifest)
    return start(registered_directory(directory.name))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "build", "build-status", "up", "start", "status", "smoke", "stop", "down"))
    parser.add_argument("--product-repo", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--image")
    parser.add_argument("--operation")
    parser.add_argument("--deployment")
    args = parser.parse_args()
    if args.action == "setup":
        uv = ROOT / "tmp/genai-tooling/bin/uv"
        if not uv.is_file():
            raise MaterialError("PINNED_UV_REQUIRED")
        deployment.command([str(uv), "sync", "--locked", "--project", str(Path(__file__).parent),
            "--python", sys.executable, "--no-managed-python", "--cache-dir", str(ROOT / "tmp/uv-cache")])
        result = {"status": "HOST_ENV_READY"}
    elif args.action == "build":
        if not args.product_repo or not args.commit:
            raise MaterialError("PRODUCT_REPO_AND_COMMIT_REQUIRED")
        result = build(args.product_repo, args.commit)
    elif args.action == "build-status":
        result = reconcile_build(args.operation or "")
    elif args.action == "up":
        if not args.product_repo:
            raise MaterialError("PRODUCT_REPOSITORY_REQUIRED")
        result = up(args.product_repo, args.image or "")
    else:
        directory = registered_directory(args.deployment)
        if args.action == "start":
            result = start(directory)
        elif args.action == "status":
            receipt = deployment.inspect(directory)
            result = {"status": "OBSERVED", "deployment_id": directory.name, "product_commit": receipt["product_revision"],
                "endpoints": receipt["endpoints"], "roles": {row["role"]:row["state"] for row in receipt["containers"]}}
        elif args.action == "smoke":
            if not args.product_repo or not PYTHON.is_file():
                raise MaterialError("PRODUCT_REPO_AND_HOST_SETUP_REQUIRED")
            output = ROOT / "artifacts/product-boundary" / ("run-" + uuid4().hex)
            environment = {key:value for key,value in os.environ.items() if key in SYSTEM_ENV}
            os.execve(PYTHON, [str(PYTHON), str(Path(__file__).with_name("observe_product.py")), "--deployment", str(directory),
                "--product-repo", str(args.product_repo.resolve()), "--output", str(output)], environment)
        else:
            # down 保留专有卷；删除业务数据必须是另一个明确操作，不能由清理默认触发。
            deployment.inspect(directory)
            deployment.command(["docker", "compose", "-f", str(directory / "compose.json"), args.action])
            result = {"status": "STOPPED" if args.action == "stop" else "CONTAINERS_REMOVED_DATA_RETAINED", "deployment_id": directory.name}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") not in {"FAILED", "UNKNOWN"} else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (MaterialError, ValueError, KeyError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "INCOMPLETE", "error_code": getattr(error, "code", "PRODUCT_HOST_OPERATION_FAILED")}))
        raise SystemExit(2)
