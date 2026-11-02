"""飞书事件回调适配层，不承载园区业务流程和数据库写入。"""

import hashlib
import hmac
import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class FeishuSettings:
    verification_token: str | None
    encrypt_key: str | None


def get_feishu_settings() -> FeishuSettings:
    """从环境变量读取飞书回调配置，避免把密钥写入代码或数据库。"""
    return FeishuSettings(
        verification_token=os.getenv("FEISHU_VERIFICATION_TOKEN"),
        encrypt_key=os.getenv("FEISHU_ENCRYPT_KEY"),
    )


def verify_callback(payload: dict, raw_body: bytes, headers: dict[str, str]) -> bool:
    settings = get_feishu_settings()
    if not settings.verification_token:
        return False

    # URL 验证和事件回调都必须匹配飞书开放平台配置的 Verification Token。
    token = payload.get("token") or payload.get("header", {}).get("token")
    if not token or not hmac.compare_digest(str(token), settings.verification_token):
        return False

    # 配置 Encrypt Key 后，进一步校验飞书回调签名。
    if settings.encrypt_key:
        timestamp = headers.get("x-lark-request-timestamp", "")
        nonce = headers.get("x-lark-request-nonce", "")
        signature = headers.get("x-lark-signature", "")
        if not timestamp or not nonce or not signature:
            return False
        digest = hashlib.sha256(
            f"{timestamp}{nonce}{settings.encrypt_key}".encode("utf-8") + raw_body
        ).hexdigest()
        return hmac.compare_digest(digest, signature)

    return True


def parse_message_event(payload: dict) -> dict | None:
    """提取飞书消息事件为统一流程入口字段。"""
    header = payload.get("header") or {}
    if header.get("event_type") != "im.message.receive_v1":
        return None

    event = payload.get("event") or {}
    message = event.get("message") or {}
    if message.get("message_type") != "text":
        return None

    try:
        content = json.loads(message.get("content") or "{}")
    except (TypeError, json.JSONDecodeError):
        return None

    text = str(content.get("text") or "").strip()
    sender_id = (event.get("sender") or {}).get("sender_id") or {}
    open_id = sender_id.get("open_id")
    event_id = header.get("event_id") or message.get("message_id")
    if not text or not open_id or not event_id:
        return None

    return {
        "event_id": str(event_id),
        "open_id": str(open_id),
        "text": text,
    }
