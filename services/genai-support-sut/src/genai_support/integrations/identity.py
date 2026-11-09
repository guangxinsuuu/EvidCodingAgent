"""自有 API → Keycloak introspection → 本地服务器授权范围。

Token 仅交给配置的身份提供方，不转发给客服或计费系统；每次请求实时核验，
即使先前令牌尚未过期，身份服务不可用仍拒绝新业务操作。
"""

from dataclasses import dataclass
import math
import time
from urllib.parse import urlsplit

from .http import IntegrationError, JsonClient


@dataclass(frozen=True)
class Identity:
    subject: str
    tenant_ids: tuple[str, ...]


class KeycloakIdentity:
    def __init__(self, issuer: str, client_id: str, client_secret: str, audience: str,
                 subject_tenants: dict[str, list[str]], audit=None) -> None:
        parsed = urlsplit(issuer)
        if not parsed.path.startswith("/realms/") or parsed.path.count("/") != 2 \
                or not parsed.path.removeprefix("/realms/") or parsed.query or parsed.fragment:
            raise IntegrationError("IDENTITY_CONFIG_INVALID", 500, "REJECTED")
        if not client_id or not client_secret or not audience or not isinstance(subject_tenants, dict) \
                or any(not isinstance(sub, str) or not sub or not isinstance(tenants, list)
                       or any(not isinstance(tenant, str) or not tenant for tenant in tenants)
                       for sub, tenants in subject_tenants.items()):
            raise IntegrationError("IDENTITY_CONFIG_INVALID", 500, "REJECTED")
        self.issuer, self.audience = issuer.rstrip("/"), audience
        self._client_id, self._client_secret = client_id, client_secret
        self.subject_tenants = {subject: tuple(tenants) for subject, tenants in subject_tenants.items()}
        self.http = JsonClient(f"{parsed.scheme}://{parsed.netloc}", {}, "keycloak", audit)
        self.path = parsed.path + "/protocol/openid-connect/token/introspect"

    def authenticate(self, bearer_token: str) -> Identity:
        """不解码未验证 JWT 来授权；只使用实际 introspection 返回的有效身份。

        调用者须先检查恰好一个 Authorization 头，再传去掉 Bearer 前缀的值。
        audience、issuer、有效时间和 sub 都须匹配，tenant 完全来自服务器配置。
        """
        if not isinstance(bearer_token, str) or not 1 <= len(bearer_token) <= 16_384 \
                or any(char.isspace() for char in bearer_token):
            raise IntegrationError("UNAUTHORIZED", 401, "REJECTED")
        try:
            value = self.http.request("POST", self.path, form={"client_id": self._client_id,
                "client_secret": self._client_secret, "token": bearer_token, "token_type_hint": "access_token"})
        except IntegrationError:
            raise IntegrationError("IDENTITY_UNAVAILABLE", 503) from None
        aud = value.get("aud")
        audiences = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
        now, exp, nbf = time.time(), value.get("exp"), value.get("nbf", 0)
        if value.get("active") is not True or value.get("iss") != self.issuer \
                or self.audience not in audiences or type(exp) not in {int, float} or not math.isfinite(exp) or exp <= now \
                or type(nbf) not in {int, float} or not math.isfinite(nbf) or nbf > now or not isinstance(value.get("sub"), str):
            raise IntegrationError("UNAUTHORIZED", 401, "REJECTED")
        tenants = self.subject_tenants.get(value["sub"])
        if not tenants:
            raise IntegrationError("FORBIDDEN", 403, "REJECTED")
        return Identity(subject=value["sub"], tenant_ids=tenants)
