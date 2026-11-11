"""登记材料只读入口：ReadScope → provenance → 本地 Git 对象 → 摘要 → 有界片段。

这里读取的是登记时的字节，工作树不参与内容身份判断。正文中的命令只作为文本返回；
本模块不会导入产品模块、执行清单脚本、访问网络或向模型传输材料。
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import subprocess


MAX_BYTES = 2 * 1024 * 1024
DEFAULT_LINES = 80
MAX_FRAGMENT_LINES = 200
MAX_FRAGMENT_BYTES = 32768


class MaterialError(ValueError):
    """对调用方提供稳定码；不在异常中回显正文、Git stderr 或凭据。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise MaterialError(code)


def _registered_path(value: str) -> str:
    """仅校验仓库内规范路径，不查看工作树，历史文件已删除时仍可解析。"""
    _require(isinstance(value, str) and bool(value), "SOURCE_PATH_INVALID")
    path = PurePosixPath(value)
    _require(bool(path.parts) and not path.is_absolute() and ".." not in path.parts and str(path) == value
             and "\\" not in value and ":" not in value
             and not any(ord(char) < 32 or ord(char) == 127 for char in value), "SOURCE_PATH_INVALID")
    _require(path.parts[0] not in {".git", ".codex", ".agents", ".aws", ".ssh", "artifacts", "output"}
             and not any(part.startswith(".env") or part.endswith(".private.json")
                         or part in {".git", ".aws", ".ssh"} for part in path.parts), "PRIVATE_SOURCE_FORBIDDEN")
    return value


class LocalGitReader:
    """只读本地已存在的 commit/blob；禁用 replace 对象、懒获取和所有网络协议。

    Resolver 和 CatalogReader 调用 read；git ls-tree 确认文件类型，cat-file 取得原始
    blob。不会 checkout、使用 smudge filter 或执行仓库中的脚本。
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._verified_commits: set[str] = set()

    def _git(self, *args: str) -> bytes:
        # 去掉能把命令重定向到其他仓库/替代对象库的环境变量。只把固定命令参数交给 Git。
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0")
        try:
            result = subprocess.run(
                ["git", "--no-replace-objects", "--literal-pathspecs", "-c", "protocol.allow=never",
                 "-c", "core.fsmonitor=false", *args], cwd=self.root, env=env,
                capture_output=True, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise MaterialError("SOURCE_READ_FAILED") from None
        if result.returncode:
            raise MaterialError("SOURCE_OBJECT_UNAVAILABLE")
        return result.stdout

    def read(self, commit: str, path: str) -> bytes:
        """返回指定完整 commit 下普通文件的原始字节；缺失、拒绝和读取失败分开报告。"""
        _require(isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit), "SOURCE_REVISION_INVALID")
        path = _registered_path(path)
        if commit not in self._verified_commits:
            try:
                kind = self._git("cat-file", "-t", commit).strip()
            except MaterialError as error:
                if error.code == "SOURCE_OBJECT_UNAVAILABLE":
                    raise MaterialError("SOURCE_REVISION_UNAVAILABLE") from None
                raise
            _require(kind == b"commit", "SOURCE_REVISION_INVALID")
            self._verified_commits.add(commit)
        # 每个祖先也要检查：Git 树中的软链接不能作为目录间接引入外部材料。
        parts = PurePosixPath(path).parts
        entry = None
        for length in range(1, len(parts) + 1):
            prefix = "/".join(parts[:length])
            listing = self._git("ls-tree", "-z", commit, "--", prefix)
            _require(bool(listing), "SOURCE_FILE_MISSING")
            rows = listing.rstrip(b"\0").split(b"\0")
            _require(len(rows) == 1, "SOURCE_PATH_INVALID")
            entry = rows[0].split(b"\t", 1)[0].split()
            _require(len(entry) == 3, "SOURCE_READ_FAILED")
            mode, kind, _ = entry
            _require(mode != b"120000", "SOURCE_SYMLINK_FORBIDDEN")
            _require(mode != b"160000", "SOURCE_SUBMODULE_FORBIDDEN")
            if length < len(parts):
                _require(kind == b"tree", "SOURCE_FILE_MISSING")
        mode, kind, object_id = entry
        _require(mode in (b"100644", b"100755") and kind == b"blob", "SOURCE_NOT_REGULAR_FILE")
        object_id = object_id.decode("ascii")
        size = self._git("cat-file", "-s", object_id).strip()
        _require(size.isdigit() and int(size) <= MAX_BYTES, "MATERIAL_TOO_LARGE")
        raw = self._git("cat-file", "blob", object_id)
        _require(len(raw) == int(size), "SOURCE_READ_FAILED")
        return raw


@dataclass(frozen=True)
class ReadScope:
    """由可信调用方建立的权限；材料或模型传入的 approved 字段不能扩展它。"""

    principal_id: str
    purpose: str
    allowed_materials: frozenset[str]
    allow_metadata: bool = True
    allow_content: bool = True
    model_egress_allowed: bool = False

    def __post_init__(self):
        _require(isinstance(self.principal_id, str) and bool(self.principal_id), "READ_SCOPE_INVALID")
        _require(isinstance(self.purpose, str) and bool(self.purpose), "READ_SCOPE_INVALID")
        _require(isinstance(self.allowed_materials, frozenset)
                 and all(isinstance(item, str) for item in self.allowed_materials), "READ_SCOPE_INVALID")
        _require(all(type(value) is bool for value in
                     (self.allow_metadata, self.allow_content, self.model_egress_allowed)), "READ_SCOPE_INVALID")


class ReferenceResolver:
    """将登记引用解析成可追溯材料；授权先于取正文，摘要校验先于片段截取。"""

    def __init__(self, git: LocalGitReader, records: list[dict], scope: ReadScope):
        _require(isinstance(scope, ReadScope), "READ_SCOPE_INVALID")
        self.git = git
        self.scope = scope
        self._records = {}
        _require(isinstance(records, list), "PROVENANCE_FILES_INVALID")
        for row in records:
            _require(isinstance(row, dict), "PROVENANCE_FILES_INVALID")
            path = _registered_path(row.get("path"))
            _require(path not in self._records, "PROVENANCE_DUPLICATE_PATH")
            _require(isinstance(row.get("git_commit"), str) and re.fullmatch(r"[0-9a-f]{40}", row["git_commit"])
                     and row.get("digest_algorithm") == "SHA256_RAW_BYTES"
                     and isinstance(row.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]),
                     "PROVENANCE_REVISION_INVALID")
            self._records[path] = dict(row)

    def _record(self, ref: str) -> dict:
        _registered_path(ref)
        _require(ref in self._records, "MATERIAL_NOT_REGISTERED")
        return self._records[ref]

    def _read_verified_bytes(self, ref: str) -> bytes:
        """仅供可信目录验证器校验固定来源；内部调用也必须经过完整正文授权。

        此函数不授予内容展示或模型传输权限，也不能注册为 Agent 可调用工具。
        """
        row = self._record(ref)
        _require(self.scope.purpose == "LOCAL_PLAN", "MATERIAL_PURPOSE_DENIED")
        _require(ref in self.scope.allowed_materials and self.scope.allow_metadata and self.scope.allow_content,
                 "MATERIAL_ACCESS_DENIED")
        raw = self.git.read(row["git_commit"], ref)
        _require(hashlib.sha256(raw).hexdigest() == row["sha256"], "MATERIAL_DIGEST_MISMATCH")
        # 路径白名单是主要边界；明显的私钥/访问令牌正文另外拒绝，且不回显命中内容。
        _require(not re.search(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:ghp_[A-Za-z0-9]{30,}|AKIA[A-Z0-9]{16})\b", raw),
                 "MATERIAL_SECRET_CONTENT_FORBIDDEN")
        return raw

    def read_registered_material(self, material_ref: str, start_line: int | None = None,
                                 end_line: int | None = None, purpose: str = "LOCAL_PLAN") -> dict:
        """读取获准登记资料；返回完整身份与有界片段，缺权限时不尝试 Git 内容读取。

        行号从 1 开始。显式超出原文范围会失败；合法的大范围会截断并报告，避免把整份
        原始业务材料意外塞进上下文。本接口仅支持本地计划，不承担模型出口授权。
        """
        row = self._record(material_ref)
        _require(purpose == self.scope.purpose == "LOCAL_PLAN", "MATERIAL_PURPOSE_DENIED")
        _require(material_ref in self.scope.allowed_materials and self.scope.allow_metadata, "MATERIAL_ACCESS_DENIED")
        result = {"material_ref": material_ref, "git_commit": row["git_commit"],
                  "sha256": row["sha256"], "digest_algorithm": row["digest_algorithm"],
                  "purpose": purpose, "model_egress_authorized": False}
        if not self.scope.allow_content:
            return {**result, "status": "METADATA_ONLY", "content": None, "content_verification": "NOT_READ"}
        raw = self._read_verified_bytes(material_ref)
        try:
            lines = raw.decode("utf-8").splitlines(keepends=True)
        except UnicodeError:
            raise MaterialError("MATERIAL_ENCODING_UNSUPPORTED") from None
        total = len(lines)
        _require(all(value is None or type(value) is int for value in (start_line, end_line)), "MATERIAL_RANGE_INVALID")
        start = 1 if start_line is None else start_line
        requested_end = min(total, start + DEFAULT_LINES - 1) if end_line is None else end_line
        if not lines and start_line is None and end_line is None:
            start, requested_end = 0, 0
        else:
            _require(1 <= start <= requested_end <= total, "MATERIAL_RANGE_INVALID")
        end = min(requested_end, start + MAX_FRAGMENT_LINES - 1)
        content = "".join(lines[start - 1:end]) if total else ""
        byte_truncated = len(content.encode("utf-8")) > MAX_FRAGMENT_BYTES
        if byte_truncated:
            content = content.encode("utf-8")[:MAX_FRAGMENT_BYTES].decode("utf-8", errors="ignore")
            end = start + len(content.splitlines()) - 1
        return {**result, "status": "READABLE", "content_verification": "SHA256_VERIFIED", "content": content,
                "start_line": start, "end_line": end, "total_lines": total,
                "requested_start_line": start_line, "requested_end_line": end_line,
                "truncated": byte_truncated or start > 1 or end < total,
                "byte_truncated": byte_truncated}
