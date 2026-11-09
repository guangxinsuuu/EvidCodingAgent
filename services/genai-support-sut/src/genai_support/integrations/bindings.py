"""服务器侧映射：Chatwoot account/contact → SaaS tenant → Lago external customer。

不依赖邮箱相同、不把正文自报 invoice/customer 视为授权；测试 seed 生成候选映射，
部署入口加载后保持不可变。case 列表仅供实验选择，不参与产品对象授权。
"""

from copy import deepcopy

from .http import IntegrationError


class BindingRegistry:
    def __init__(self, configuration: dict) -> None:
        if configuration.get("schema_version") != 1 or not isinstance(configuration.get("customers"), list):
            raise IntegrationError("BINDING_CONFIG_INVALID", 500, "REJECTED")
        self._rows = {}
        for row in configuration["customers"]:
            if not isinstance(row, dict) or not isinstance(row.get("tenant_id"), str) or not row["tenant_id"] \
                    or type(row.get("chatwoot_account_id")) is not int or row["chatwoot_account_id"] < 1 \
                    or type(row.get("chatwoot_contact_id")) is not int or row["chatwoot_contact_id"] < 1 \
                    or not isinstance(row.get("lago_customer_id"), str) or not row["lago_customer_id"] \
                    or not isinstance(row.get("allowed_subjects"), list) \
                    or not row["allowed_subjects"] or any(not isinstance(sub, str) or not sub for sub in row["allowed_subjects"]):
                raise IntegrationError("BINDING_CONFIG_INVALID", 500, "REJECTED")
            # 同一实例/account/contact 不得在两条记录中分别指向不同 tenant。
            key = (row["chatwoot_account_id"], row["chatwoot_contact_id"])
            if key in self._rows:
                raise IntegrationError("BINDING_CONFIG_AMBIGUOUS", 500, "REJECTED")
            self._rows[key] = deepcopy(row)

    def resolve(self, tenant_id: str, account_id: int, contact_id: int, subject: str | None = None) -> dict:
        row = self._rows.get((account_id, contact_id))
        if row is None or row["tenant_id"] != tenant_id \
                or (subject is not None and subject not in row["allowed_subjects"]):
            raise IntegrationError("CUSTOMER_NOT_AUTHORIZED", 403, "REJECTED")
        return deepcopy(row)

    def authorize_account(self, tenant_id: str, account_id: int, subject: str) -> None:
        """会话读取之前先缩小到允许的 account；读取后还必须 resolve contact。"""
        if not any(row["tenant_id"] == tenant_id and row["chatwoot_account_id"] == account_id
                   and subject in row["allowed_subjects"] for row in self._rows.values()):
            raise IntegrationError("CUSTOMER_NOT_AUTHORIZED", 403, "REJECTED")
