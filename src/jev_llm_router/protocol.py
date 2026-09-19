"""Responses protocol inspection without changing tool, image, or reasoning payloads."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .state import RouteError


def user_texts(payload: dict[str, Any]) -> list[str]:
    data = payload.get("input", [])
    if isinstance(data, str):
        return [data] if data.strip() else []
    texts = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict) or item.get("role") != "user":
                continue
            content = item.get("content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "\n".join(part["text"] for part in content
                                 if isinstance(part, dict)
                                 and part.get("type") in {"input_text", "text"}
                                 and isinstance(part.get("text"), str))
            else:
                text = ""
            if text.strip():
                texts.append(text)
    return texts


def output_keys(item: Any) -> list[str]:
    if not isinstance(item, dict):
        return []
    keys = []
    for field, prefix in (("id", "item:"), ("call_id", "call:"),
                          ("encrypted_content", "opaque:")):
        value = item.get(field)
        if isinstance(value, str) and value:
            keys.append(prefix + value)
    return keys


def input_keys(payload: dict[str, Any]) -> list[str]:
    keys = []
    previous = payload.get("previous_response_id")
    if previous is not None:
        if not isinstance(previous, str) or not previous:
            raise RouteError("previous_response_id must be a nonempty string")
        keys.append("response:" + previous)
    data = payload.get("input", [])
    if isinstance(data, list):
        last_user = max((i for i, item in enumerate(data)
                         if isinstance(item, dict) and item.get("role") == "user"), default=-1)
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            kind = item.get("type", "")
            encrypted = item.get("encrypted_content")
            if isinstance(encrypted, str) and encrypted:
                keys.append("opaque:" + encrypted)
            if kind == "item_reference" and isinstance(item.get("id"), str):
                keys.append("item:" + item["id"])
            # Historical completed tools do not pin new, fully explicit user turns.
            if i > last_user and isinstance(kind, str) and (
                kind.endswith("_call") or kind.endswith("_call_output")
            ):
                call = item.get("call_id")
                if isinstance(call, str) and call:
                    keys.append("call:" + call)
    return list(dict.fromkeys(keys))


class SSEObserver:
    """Observe bounded SSE frames; transport forwards original decoded bytes separately.

    Oversized frames are discarded only from this metadata observer, never the stream.
    A subsequent untracked opaque continuation fails closed rather than being rerouted.
    """

    def __init__(self, callback: Callable[[dict], None], limit: int = 262144):
        self.callback = callback
        self.limit = limit
        self.line = bytearray()
        self.data: list[bytes] = []
        self.size = 0
        self.discard = False
        self.line_overflow = False

    def feed(self, chunk: bytes) -> None:
        parts = chunk.split(b"\n")
        for index, part in enumerate(parts):
            if not self.line_overflow:
                if len(self.line) + len(part) > self.limit:
                    self.line.clear()
                    self.line_overflow = self.discard = True
                    self.data.clear()
                else:
                    self.line.extend(part)
            if index == len(parts) - 1:
                break
            line = bytes(self.line).rstrip(b"\r")
            overflow = self.line_overflow
            self.line.clear()
            self.line_overflow = False
            if not line and not overflow:
                if self.data and not self.discard:
                    try:
                        event = json.loads(b"\n".join(self.data))
                    except (ValueError, UnicodeError, RecursionError):
                        pass
                    else:
                        if isinstance(event, dict):
                            self.callback(event)
                self.data.clear()
                self.size = 0
                self.discard = False
            elif line.startswith(b"data:") and not self.discard:
                value = line[5:]
                if value.startswith(b" "):
                    value = value[1:]
                self.size += len(value)
                if self.size > self.limit:
                    self.discard = True
                    self.data.clear()
                else:
                    self.data.append(value)
