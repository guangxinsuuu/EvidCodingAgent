"""客服适配器：Ingress 读取会话；Writeback 仅能写内部备注并重新读取确认。"""

from typing import Any

from .http import IntegrationError, JsonClient, list_field, segment


class ChatwootClient:
    def __init__(self, base_url: str, token: str, account_id: int, audit=None) -> None:
        if type(account_id) is not int or account_id < 1:
            raise IntegrationError("CHATWOOT_ACCOUNT_INVALID", 500, "REJECTED")
        self.account_id = account_id
        self.http = JsonClient(base_url, {"api_access_token": token}, "chatwoot", audit)
        self.prefix = f"/api/v1/accounts/{account_id}/conversations/"

    def get_conversation(self, conversation_id: int) -> dict[str, Any]:
        value = self.http.request("GET", self.prefix + segment(conversation_id))
        if value.get("id") != conversation_id:
            raise IntegrationError("CHATWOOT_CONVERSATION_MISMATCH")
        # 部分固定版本的会话响应不带 account_id；身份仍由 account-scoped URL 约束。
        if "account_id" in value and value["account_id"] != self.account_id:
            raise IntegrationError("CHATWOOT_ACCOUNT_MISMATCH")
        return value

    def get_messages(self, conversation_id: int) -> list[dict[str, Any]]:
        """逆向分页读取有限会话；过长时明确缺观察，不能把首页当完整历史。"""
        result, before = [], None
        for _ in range(20):
            body = self.http.request("GET", self.prefix + segment(conversation_id) + "/messages",
                                     query={"before": before} if before is not None else None)
            rows = list_field(body, "payload")
            if not rows:
                return sorted(result, key=lambda row: row["id"])
            ids = [row.get("id") for row in rows]
            if any(type(value) is not int for value in ids) or (before is not None and min(ids) >= before):
                raise IntegrationError("CHATWOOT_PAGINATION_INVALID")
            for row in rows:
                if row.get("conversation_id", conversation_id) != conversation_id:
                    raise IntegrationError("CHATWOOT_CONVERSATION_MISMATCH")
            result.extend(rows)
            before = min(ids)
        raise IntegrationError("CHATWOOT_OBSERVATION_INCOMPLETE")

    def create_private_note(self, conversation_id: int, content: str) -> dict[str, Any]:
        """仅一次 POST；私有性、目标会话和内容必须在 GET 回读中得到确认。

        调用方先登记持久发送意图。任何写后校验失败都按 UNKNOWN 交给对账，
        不能重试 POST。这里从接口形状上不暴露 private=False 或对客发送能力。
        """
        if not isinstance(content, str) or not 1 <= len(content) <= 16_384:
            raise IntegrationError("CHATWOOT_NOTE_INVALID", 422, "REJECTED")
        message = self.http.request("POST", self.prefix + segment(conversation_id) + "/messages",
                                    json_body={"content": content, "message_type": "outgoing", "private": True,
                                               "content_type": "text"}, write=True)
        try:
            if type(message.get("id")) is not int or message.get("private") is not True \
                    or message.get("conversation_id") != conversation_id:
                raise IntegrationError("CHATWOOT_WRITEBACK_INVALID")
            matches = [row for row in self.get_messages(conversation_id) if row["id"] == message["id"]]
            if len(matches) != 1 or matches[0].get("private") is not True or matches[0].get("content") != content:
                raise IntegrationError("CHATWOOT_WRITEBACK_INVALID")
            return matches[0]
        except IntegrationError:
            raise IntegrationError("CHATWOOT_WRITEBACK_UNKNOWN", 503, "UNKNOWN") from None


def customer_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只有公开 incoming 客户输入进入业务 revision，备注和状态变化不会递归触发。"""
    return [row for row in messages if row.get("private") is False and row.get("message_type") in (0, "incoming")]
