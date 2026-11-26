"""受信产品登记 → 独立 Git checkout → 带真实来源身份的材料命名空间。

产品仓库的物理路径是 src/...；历史能力合同中的逻辑名仍为
services/genai-support-sut/src/...。映射显式记录，不能把逻辑名冒充实际 Git 路径。
模型不能提供注册文件或扩展读取白名单；本机 origin 核验也不声称远端签名证明。
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from .reference_resolver import LocalGitReader, MaterialError, _registered_path

REGISTRATION = Path(__file__).resolve().parents[2] / "config/external-products.v1.json"


class ProductGitReader(LocalGitReader):
    def __init__(self, root: Path, *, product_id: str, authority_root: Path):
        super().__init__(root)
        if self.root == authority_root.resolve():
            raise MaterialError("PRODUCT_REPOSITORY_MUST_BE_SEPARATE")
        profiles = json.loads(REGISTRATION.read_text())["products"]
        matches = [profile for profile in profiles if profile["id"] == product_id]
        if len(matches) != 1:
            raise MaterialError("PRODUCT_NOT_REGISTERED")
        self.profile = matches[0]
        self.prefix = self.profile["logical_prefix"]
        # 工作树必须是真正独立的仓库根，不能给子目录而让 Git 向上回落到 EGAP。
        actual = self._git("rev-parse", "--show-toplevel").decode().strip()
        if Path(actual).resolve() != self.root:
            raise MaterialError("PRODUCT_REPOSITORY_ROOT_REQUIRED")
        origin = self._git("config", "--get", "remote.origin.url").decode().strip()
        if origin != self.profile["repository_url"]:
            raise MaterialError("PRODUCT_REPOSITORY_ORIGIN_MISMATCH")

    def logical_path(self, physical: str) -> str:
        return self.prefix + _registered_path(physical)

    def physical_path(self, logical: str) -> str:
        if not logical.startswith(self.prefix):
            raise MaterialError("SOURCE_FILE_MISSING")
        return _registered_path(logical[len(self.prefix):])

    def allowed(self, logical: str) -> bool:
        physical = self.physical_path(logical)
        return physical in self.profile["source_files"] or any(physical.startswith(p) for p in self.profile["source_prefixes"])

    def read(self, commit: str, path: str) -> bytes:
        return super().read(commit, self.physical_path(path))

    def binding(self, baseline: str, candidate: str, egap_root: Path) -> dict:
        """分别冻结产品 B/C 的树身份和装配器安装版本；不使用 EGAP HEAD 代替 SUT。"""
        def revision(commit: str) -> dict:
            if not re.fullmatch(r"[a-f0-9]{40}", commit):
                raise MaterialError("PRODUCT_COMMIT_INVALID")
            return {"commit": commit, "tree": self._git("rev-parse", commit + "^{tree}").decode().strip()}
        return {"schema_version": 1, "product_id": self.profile["id"],
            "repository_url": self.profile["repository_url"], "logical_prefix": self.prefix,
            "baseline": revision(baseline), "candidate": revision(candidate),
            "egap_commit": LocalGitReader(egap_root)._git("rev-parse", "HEAD").decode().strip(),
            "authority": "OPERATOR_REGISTERED_LOCAL_CHECKOUT", "remote_attestation": False}
