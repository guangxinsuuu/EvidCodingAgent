"""main → load_config：只解析显式客服配置；不加载.env或解析模型凭据。"""

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_FIXTURE_PATH = PROJECT_ROOT / "datasets/genai-support/synthetic-v1/sut"


class ConfigurationError(ValueError):
    """配置不可用；错误中只有字段名，不回显token或用户提供的值。"""


@dataclass(frozen=True)
class Settings:
    """仅限本机合成骨架。单个bearer绑定一个tenant，body不能选择租户。"""

    api_token: str = field(repr=False)
    tenant_id: str
    host: str = "127.0.0.1"
    port: int = 4321

    def __post_init__(self) -> None:
        # 拒绝全网监听与弱/多行token；此本地认证仍不等于企业IAM。
        if self.host != "127.0.0.1":
            raise ConfigurationError("INVALID_EGAP_SUPPORT_HOST")
        if type(self.port) is not int or not 0 <= self.port <= 65535:
            raise ConfigurationError("INVALID_EGAP_SUPPORT_PORT")
        if not isinstance(self.api_token, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{32,256}", self.api_token):
            raise ConfigurationError("INVALID_EGAP_SUPPORT_API_TOKEN")
        if not isinstance(self.tenant_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", self.tenant_id):
            raise ConfigurationError("INVALID_EGAP_SUPPORT_TENANT_ID")


def load_config(environment: Mapping[str, str]) -> Settings:
    """启动入口调用；token和tenant必须显式传入，端口0用于操作系统分配测试端口。"""
    raw_port = environment.get("EGAP_SUPPORT_PORT", "4321")
    if not re.fullmatch(r"[0-9]{1,5}", raw_port):
        raise ConfigurationError("INVALID_EGAP_SUPPORT_PORT")
    return Settings(
        api_token=environment.get("EGAP_SUPPORT_API_TOKEN", ""),
        tenant_id=environment.get("EGAP_SUPPORT_TENANT_ID", ""),
        host=environment.get("EGAP_SUPPORT_HOST", "127.0.0.1"),
        port=int(raw_port),
    )
