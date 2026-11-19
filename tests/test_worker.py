import asyncio
import os

import pytest

from n8n_worker.client import TurnResult, parse_start, parse_turn, render_instructions, resolve_turn
from n8n_worker.config import ConfigError, load
from n8n_worker.control import parse_control

BASE = {"N8N_BASE_URL": "http://n8n.test/webhook/", "N8N_BEARER": "not-a-real-token"}


def test_config_defaults_and_trailing_slash():
    c = load(BASE)
    assert c.n8n_base_url == "http://n8n.test/webhook"
    assert c.mode == "turn" and c.transfer == "n8n" and c.report_end is False
    assert c.path_turn == "/ar/v1/session/turn"
    assert (c.filler_after_ms, c.turn_timeout_ms) == (2500, 12000)


@pytest.mark.parametrize(
    "extra",
    [
        {"N8N_BASE_URL": ""},
        {"N8N_BEARER": " "},
        {"N8N_MODE": "realtime"},
        {"N8N_MODE": "duplex"},
        {"N8N_MODE": "realtime", "N8N_MCP_URL": "http://n8n/mcp/x", "N8N_MCP_TRANSPORT": "carrier-pigeon"},
        {"REALTIME_INSTRUCTIONS_FILE": "/nonexistent/instructions.txt"},
        {"N8N_TRANSFER": "worker"},
        {"N8N_END_CALL": "nobody"},
        {"FILLER_AFTER_MS": "12000"},
        {"DEFAULT_LANGUAGE": "pl"},
    ],
)
def test_config_rejects(extra):
    with pytest.raises(ConfigError):
        load({**BASE, **extra})


def test_lines_by_language_with_override_and_default():
    c = load({**BASE, "FALLBACK_LINE_PL": "Przepraszam.", "FILLER_LINE_EN": "Hold on."})
    assert c.fallback("pl-PL") == "Przepraszam."
    assert c.filler("en") == "Hold on."
    assert c.filler("fr") == "Hold on."
    assert c.filler("de").startswith("Einen")


def test_parse_start_prefers_the_session_id_n8n_issues():
    a = parse_start({"sessionId": "AB12", "speak": "Hi", "language": "de", "action": "continue"}, {"sessionId": "room-1"}, "en")
    assert (a.session_id, a.speak, a.language, a.action) == ("AB12", "Hi", "de", "none")
    b = parse_start({"speak": "Hi"}, {"sessionId": "room-1"}, "en")
    assert (b.session_id, b.language) == ("room-1", "en")
    assert parse_start({"action": "handoff"}, {}, "en").action == "handoff"
    assert parse_start("nonsense", {"sessionId": "r"}, "en").session_id == "r"


def test_parse_turn():
    assert parse_turn({"speak": "Hi", "action": "END"}) == TurnResult("Hi", "end")
    assert parse_turn({"speak": "Hi", "action": "continue"}).action == "none"
    assert parse_turn({"speak": "Hi", "action": "dance"}).action == "none"
    assert parse_turn({"speak": "Hi", "interruptible": False}).interruptible is False
    assert parse_turn({}) == TurnResult("")
    with pytest.raises(ValueError):
        parse_turn(["x"])


async def run(delay, *, fail=False, filler_after=0.05, timeout=0.2):
    fillers = []

    async def request():
        await asyncio.sleep(delay)
        if fail:
            raise RuntimeError("n8n answered 500")
        return TurnResult("answer")

    result = await resolve_turn(
        request, lambda: fillers.append(1),
        filler_after_s=filler_after, timeout_s=timeout, fallback_line="fallback",
    )
    return result, len(fillers)


async def test_fast_turn_has_no_filler():
    result, fillers = await run(0)
    assert result == TurnResult("answer") and fillers == 0


async def test_slow_turn_plays_one_filler_then_the_answer():
    result, fillers = await run(0.1)
    assert result == TurnResult("answer") and fillers == 1


async def test_timeout_is_a_fallback_handoff():
    result, fillers = await run(1)
    assert fillers == 1
    assert (result.speak, result.action, result.reason, result.failed) == ("fallback", "handoff", "U;PE", True)


async def test_error_is_a_fallback_handoff_without_filler():
    result, fillers = await run(0, fail=True)
    assert fillers == 0 and result.action == "handoff" and result.failed


def test_control_messages():
    assert parse_control(b'{"type":"say","text":" hi "}', "n8n.control").text == "hi"
    assert parse_control(b'{"type":"end"}', "n8n.control").type == "end"
    for data, topic in [
        (b'{"type":"say","text":"hi"}', "other"),
        (b'{"type":"say","text":""}', "n8n.control"),
        (b'{"type":"dance"}', "n8n.control"),
        (b"not json", "n8n.control"),
        (b'["say"]', "n8n.control"),
        (b"\xff\xfe", "n8n.control"),
    ]:
        assert parse_control(data, topic) is None


def test_start_payload_carries_generic_and_reference_fields():
    import os
    os.environ.update(BASE)
    from n8n_worker.agent import start_payload

    attrs = {"sip.phoneNumber": "+15550100", "sip.callID": "SCL_1", "sip.h.user-to-user": "1;abc;VIN;+48;pl"}
    p = start_payload(load(BASE), room="call-1", correlation_id="c-1", identity="sip_1", kind="SIP", attributes=attrs)
    assert p["sessionId"] == "call-1" and p["correlationId"] == "c-1"
    assert p["uui"] == {"version": "1", "raw": "1;abc;VIN;+48;pl", "language": "en", "msisdn": "+15550100"}
    assert p["sip"] == {"callId": "SCL_1"} and p["gateway"]["vendor"] == "n8n-livekit-worker"
    assert p["participant"]["kind"] == "SIP"
    quiet = start_payload(
        load({**BASE, "N8N_SEND_CALLER_NUMBER": "false"}),
        room="r", correlation_id="c", identity="i", kind="SIP", attributes={"sip.phoneNumber": "+15550100"},
    )
    assert "msisdn" not in quiet["uui"] and quiet["uui"]["raw"] == ""
    seeded = start_payload(
        load({**BASE, "TEST_CALLER_VIN": "VINTEST", "TEST_CALLER_MSISDN": "+440000"}),
        room="r", correlation_id="c-9", identity="i", kind="SIP", attributes={"sip.phoneNumber": "+15550100"},
    )
    assert seeded["uui"]["raw"] == "1;c-9;VINTEST;+440000;en" and seeded["uui"]["msisdn"] == "+440000"
    header_wins = start_payload(
        load({**BASE, "TEST_CALLER_VIN": "VINTEST"}),
        room="r", correlation_id="c", identity="i", kind="SIP", attributes={"sip.h.user-to-user": "1;x;REALVIN;+48;pl"},
    )
    assert header_wins["uui"]["raw"] == "1;x;REALVIN;+48;pl" and "vin" not in header_wins["uui"]


def test_realtime_config_and_instruction_rendering():
    c = load({**BASE, "N8N_MODE": "realtime", "N8N_MCP_URL": "http://n8n/mcp/tools", "N8N_MCP_ALLOWED_TOOLS": "A, B",
              "REALTIME_INSTRUCTIONS": "Speak {{languageName}}. Session {{sessionId}}. Retailer {{dealer}}."})
    assert c.mode == "realtime" and c.mcp_allowed_tools == ("A", "B") and c.mcp_transport == "streamable_http"
    start = parse_start({"sessionId": "AB12", "language": "de", "customer": {"dealer": "West"}}, {}, "en")
    assert render_instructions(c.realtime_instructions, start) == "Speak German. Session AB12. Retailer West."
    bare = parse_start({"sessionId": "X"}, {}, "en")
    assert render_instructions("{{languageName}} {{dealer}}", bare) == "English none"


# --- scripted mode ---

from n8n_worker.script import load_script, parse_result, pick, resolve, say_schema, spoken, tool_schema  # noqa: E402

EXAMPLE_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "examples", "script.en.json")


def _script():
    return load_script(EXAMPLE_SCRIPT)


def _tool(script, name):
    return next(t for t in script.tools if t.name == name)


def test_resolve_nested_token():
    scope = {"args": {"option": 2}, "state": {"slot1": "A", "slot2": "B"}}
    assert resolve("{state.slot{args.option}}", scope) == "B"
    assert resolve("{state.missing}", scope) == ""


def test_parse_result_unwraps_n8n_shapes():
    assert parse_result('[{"ok": true, "x": 1}]') == {"ok": True, "x": 1}
    assert parse_result({"type": "text", "text": '[{"ok": false}]'}) == {"ok": False}
    assert parse_result('{"type": "text", "text": "[{\\"ok\\": true}]"}') == {"ok": True}
    assert parse_result("not json")["ok"] is False


def test_script_lines_are_fixed_per_outcome():
    script = _script()
    verify = _tool(script, "VerifyIdentity")
    ok = {"ok": True, "greetingName": "Ms Example"}
    scope = {"args": {}, "state": {"name": "Ms Example"}, "result": ok}
    assert spoken(script, pick(verify, ok), scope).startswith("Thank you, Ms Example.")
    assert pick(verify, {"ok": False, "error": "not_identified", "attempts": 1}).line == "pin_retry"
    second = pick(verify, {"ok": False, "error": "not_identified", "attempts": 2})
    assert (second.line, second.action) == ("pin_failed", "handoff")
    assert spoken(script, pick(verify, {"ok": False, "error": "backend_unavailable"}), scope) == script.error_line


def test_script_slot_offers_and_end():
    script = _script()
    find = _tool(script, "FindSlots")
    two = {"ok": True, "slots": [{"slotId": "a", "when": "Mon"}, {"slotId": "b", "when": "Tue"}]}
    assert pick(find, two).line == "offer_two"
    assert pick(find, {"ok": True, "slots": [{"slotId": "a", "when": "Mon"}]}).line == "offer_one"
    assert pick(find, {"ok": True, "slots": []}).line == "no_slots"
    end = pick(_tool(script, "EndCall"), {"ok": True, "say": "Bye."})
    assert (spoken(script, end, {"result": {"say": "Bye."}}), end.action) == ("Bye.", "end")
    assert pick(_tool(script, "RequestAgent"), {"ok": True}).action == "handoff"


def test_script_schemas():
    script = _script()
    assert say_schema(script)["parameters"]["properties"]["line"]["enum"][0] == "wait"
    assert tool_schema(_tool(script, "ChooseSlot"))["parameters"]["required"] == ["option"]


def test_voice_trim_cuts_silence():
    from array import array

    from n8n_worker.voice_cache import sentences, trim
    quiet, loud = array("h", [0] * 24000), array("h", [5000] * 2400)
    out = trim((quiet + loud + quiet).tobytes())
    assert 2400 * 2 <= len(out) <= (2400 + 24000 * 200 // 1000) * 2
    assert sentences("One. Two? Three!") == ["One.", "Two?", "Three!"]


def test_slot_label_is_spoken_one_way():
    from n8n_worker.script import en_when
    assert en_when("Saturday 21.11. 14:00") == "Saturday the 21st of November at 2 pm"
    assert en_when("Monday 2.12. 10:30") == "Monday the 2nd of December at 10:30 am"
    assert en_when("Friday 13.3. 12:00") == "Friday the 13th of March at 12 pm"
    assert en_when("tomorrow morning") == "tomorrow morning"
    assert resolve("{r.when|en_when}", {"r": {"when": "Saturday 21.11. 09:00"}}) == "Saturday the 21st of November at 9 am"
