"""Mid-call instructions from n8n, sent as data on topic n8n.control."""
from __future__ import annotations

import json
from dataclasses import dataclass

TOPIC = "n8n.control"


@dataclass(frozen=True)
class Control:
    type: str
    text: str = ""


def parse_control(data: bytes, topic: str | None) -> Control | None:
    """A valid control message, or None for anything else."""
    if topic != TOPIC:
        return None
    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    kind = body.get("type")
    if kind == "say" and isinstance(body.get("text"), str) and body["text"].strip():
        return Control("say", body["text"].strip())
    if kind == "end":
        return Control("end")
    return None
