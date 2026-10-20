import hashlib
import hmac
import json
import re
import time
from .config import CORE_IDS


def canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sign(payload, key):
    return hmac.new(key.encode(), canonical(payload).encode(), hashlib.sha256).hexdigest()


def validate_plan(payload):
    expected = {"id", "core", "sha", "version", "store_head", "owner", "expires"}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError("Invalid approval fields")
    if payload["core"] not in CORE_IDS:
        raise ValueError("Unknown core")
    for key in ("sha", "store_head"):
        if not isinstance(payload[key], str) or not re.fullmatch(r"[a-f0-9]{40}", payload[key]):
            raise ValueError("Expected an exact commit SHA")
    if not re.fullmatch(r"[a-f0-9]{24}", payload["id"]):
        raise ValueError("Invalid request ID")
    if not re.fullmatch(r"\d{15,22}", payload["owner"]):
        raise ValueError("Invalid owner")
    if not isinstance(payload["version"], str) or not re.fullmatch(r"[A-Za-z0-9._+-]{1,100}", payload["version"]):
        raise ValueError("Invalid upstream version")
    if type(payload["expires"]) is not int or not time.time() < payload["expires"] <= time.time() + 25 * 3600:
        raise ValueError("Approval expired")
    return payload


def verify(payload, signature, key, owner=None):
    validate_plan(payload)
    if owner is not None and payload["owner"] != str(owner):
        raise ValueError("Owner mismatch")
    if not hmac.compare_digest(sign(payload, key), signature):
        raise ValueError("Invalid approval signature")
    return payload


def authorized(user_id, guild_id, channel_id, config, state=None):
    is_adm = (user_id == config.owner_id) if not state else state.is_admin(user_id, config.owner_id)
    return is_adm and (guild_id == config.guild_id) and (channel_id == config.channel_id)


def safe_text(text, limit=1800):
    return str(text).replace("@", "＠").replace("```", "'''")[:limit]
