"""Scripted mode: the model picks a tool, the script decides the words. Pure functions, no I/O beyond load."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

_TOKEN = re.compile(r"\{([^{}]+)\}")


class ScriptError(ValueError):
    """The script file is missing something the worker needs."""


@dataclass(frozen=True)
class Outcome:
    when: dict[str, Any]
    line: str = ""
    say: str = ""
    action: str = "none"


@dataclass(frozen=True)
class ScriptTool:
    name: str
    description: str
    params: dict[str, Any] = field(default_factory=dict)
    call: str = ""
    args: dict[str, str] = field(default_factory=dict)
    capture: dict[str, str] = field(default_factory=dict)
    outcomes: tuple[Outcome, ...] = ()


@dataclass(frozen=True)
class Script:
    instructions: str
    lines: dict[str, str]
    say_name: str
    say_description: str
    say_lines: dict[str, str]
    tools: tuple[ScriptTool, ...]
    session_arg: str = "sessionId"
    error_line: str = ""
    after_greeting: str = ""
    greeting: str = ""


def lookup(scope: Any, path: str) -> Any:
    """Value at a dotted path through dicts and lists. Missing parts give an empty string."""
    current = scope
    for part in path.strip().split("."):
        if isinstance(current, dict):
            current = current.get(part, "")
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return ""
    return current


_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December")
_WHEN = re.compile(r"^\s*(\w+)\s+(\d{1,2})\.(\d{1,2})\.?\s+(\d{1,2}):(\d{2})\s*$")


def en_when(label: str) -> str:
    """"Saturday 21.11. 14:00" becomes "Saturday the 21st of November at 2 pm": one fixed way to say a slot."""
    m = _WHEN.match(label)
    if not m or not 1 <= int(m.group(3)) <= 12:
        return label
    day, hour, minute = int(m.group(2)), int(m.group(4)), m.group(5)
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    clock = f"{hour % 12 or 12}" + ("" if minute == "00" else f":{minute}") + (" am" if hour < 12 else " pm")
    return f"{m.group(1)} the {day}{suffix} of {_MONTHS[int(m.group(3)) - 1]} at {clock}"


FILTERS = {"en_when": en_when}


def _fill(scope: dict[str, Any], token: str) -> str:
    path, _, name = token.partition("|")
    value = str(lookup(scope, path))
    return FILTERS[name.strip()](value) if name.strip() in FILTERS else value


def resolve(template: str, scope: dict[str, Any]) -> str:
    """Fill {path} and {path|filter} tokens, innermost first, so {state.slot{args.option}} works."""
    text = template
    for _ in range(4):
        filled = _TOKEN.sub(lambda m: _fill(scope, m.group(1)), text)
        if filled == text:
            break
        text = filled
    return text


def matches(when: dict[str, Any], result: dict[str, Any]) -> bool:
    """Every key must match. "*" means present and not empty."""
    for path, expected in when.items():
        actual = lookup(result, path)
        if expected == "*":
            if actual in ("", None, [], {}):
                return False
        elif actual != expected:
            return False
    return True


def pick(tool: ScriptTool, result: dict[str, Any]) -> Outcome | None:
    return next((o for o in tool.outcomes if matches(o.when, result)), None)


def parse_result(raw: Any) -> dict[str, Any]:
    """n8n tool results arrive as JSON text, often a one-item list. Anything else is a failed call."""
    value = raw
    for _ in range(6):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return {"ok": False, "error": "unreadable_result"}
        elif isinstance(value, list):
            value = value[0] if value else {}
        elif isinstance(value, dict) and "text" in value and "type" in value:
            value = value["text"]
        else:
            break
    return value if isinstance(value, dict) else {"ok": False, "error": "unreadable_result"}


def tool_schema(tool: ScriptTool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": {
            "type": "object", "properties": tool.params, "required": list(tool.params),
            "additionalProperties": False,
        },
    }


def say_schema(script: Script) -> dict[str, Any]:
    listing = " ".join(f"{key}: {when}" for key, when in script.say_lines.items())
    return {
        "name": script.say_name,
        "description": f"{script.say_description} Lines: {listing}",
        "parameters": {
            "type": "object",
            "properties": {"line": {"type": "string", "enum": list(script.say_lines)}},
            "required": ["line"], "additionalProperties": False,
        },
    }


def spoken(script: Script, outcome: Outcome | None, scope: dict[str, Any]) -> str:
    """The exact words for an outcome: a literal say template, else a catalogue line, else the error line."""
    if outcome is None:
        return resolve(script.error_line, scope)
    template = outcome.say or script.lines.get(outcome.line, "")
    return resolve(template, scope).strip() or resolve(script.error_line, scope)


def load_script(path: str) -> Script:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    lines = data.get("lines") or {}
    say = data.get("say") or {}
    tools = []
    for item in data.get("tools") or []:
        outcomes = tuple(
            Outcome(when=o.get("when") or {}, line=o.get("line", ""), say=o.get("say", ""), action=o.get("action", "none"))
            for o in item.get("outcomes") or []
        )
        tools.append(ScriptTool(
            name=item["name"], description=item.get("description", ""), params=item.get("params") or {},
            call=item.get("call", ""), args=item.get("args") or {}, capture=item.get("capture") or {},
            outcomes=outcomes,
        ))
    after = data.get("afterGreeting", "")
    used = {o.line for t in tools for o in t.outcomes if o.line} | set(say.get("lines") or {}) | ({after} if after else set())
    missing = sorted(used - set(lines))
    if missing:
        raise ScriptError(f"script lines not defined: {', '.join(missing)}")
    if not tools or not data.get("instructions"):
        raise ScriptError("script needs instructions and at least one tool")
    return Script(
        instructions=data["instructions"], lines=lines, say_name=say.get("name", "Say"),
        say_description=say.get("description", "Speaks one fixed line to the caller."),
        say_lines=say.get("lines") or {}, tools=tuple(tools),
        session_arg=data.get("sessionArg", "sessionId"), error_line=data.get("errorLine", ""), after_greeting=after,
        greeting=data.get("greeting", ""),
    )
