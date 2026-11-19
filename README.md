# n8n-livekit-worker

A LiveKit agent worker that makes n8n the brain of a phone call. The worker handles audio (answer, listen,
speak); n8n decides what happens. Configured by environment only.

Status: a reference implementation, built as-is for evaluation and tested on a lab (LiveKit Cloud, a
Twilio SIP trunk, self-hosted n8n). It is not an official n8n product, carries no support commitment and
is not affiliated with LiveKit. Companion to the `n8n-nodes-livekit` community node package, which covers
the control plane (transfer, rooms, dispatch, webhook events).

## Modes

Chosen with `N8N_MODE`.

| Mode | Who talks | n8n's part | Measured on the lab |
|---|---|---|---|
| `turn` | Speech-to-text, then n8n, then text-to-speech. n8n sits where the model would | start, every turn, end webhooks | as slow as the n8n dialogue workflow; 3 to 4 s per turn with an LLM in it |
| `realtime` | A speech-to-speech model with its own wording | start, MCP tools, end | 0.3 to 0.7 s to first audio |
| `scripted` | The model only picks a tool; a script file gives the words; one voice speaks them | start, MCP tools, end | about 1 s per turn with cached audio |

Turn mode keeps every model call behind your own gateway. Realtime mode is the fastest and the least
predictable: wording differs on every call. Scripted mode gives the same words and voice on every call.

### Scripted mode

- The realtime model runs with text output and a required tool choice. Text it writes on its own is never
  spoken.
- The script file (`N8N_SCRIPT_FILE`, see `examples/script.en.json`) defines the tools the model sees, the
  n8n MCP tool behind each, and one line per outcome. The worker adds the session id and remembered values
  (for example slot ids) to the n8n call itself.
- Lines are synthesized per sentence, trimmed and cached on disk (`TTS_CACHE_DIR`). A cached sentence is
  the same audio on every call; a new one is synthesized while the earlier ones play.
- `{path|en_when}` rewrites a label such as `Saturday 21.11. 14:00` into one spoken form.

## Contract

All calls are `POST`, JSON, `Authorization: Bearer <N8N_BEARER>`. Paths are configurable.

| Call | When | Request | Response |
|---|---|---|---|
| start | caller joined | `correlationId`, `room`, `participant` (identity, kind, attributes), `uui` (`version`, `raw`, `language`, `msisdn`), `sip.callId`, `gateway.vendor` | `sessionId` (n8n may issue its own), `speak`, `language`, `action`, optional `voice` |
| turn (turn mode) | each final transcript | `sessionId`, `correlationId`, `transcript` and `text` (same value), `language`, `turn` | `speak`, `action` (`none` or `continue`, `end`, `handoff`) |
| end (`N8N_REPORT_END=true`) | call over | `sessionId`, `correlationId`, `endReason`, `durationMs`, `turns` | ignored |
| handoff (`N8N_PATH_HANDOFF` set) | the handover line has played | `sessionId`, `correlationId`, `room`, `identity`, `summary` | `ok` |

Handoff: the worker says the handover line, then asks n8n to transfer the caller. n8n does it with the
LiveKit node's SIP Transfer Participant operation. Without `N8N_PATH_HANDOFF` the worker stops listening
and waits for n8n to act.

n8n can speak into a live call with the LiveKit node's Send Data on topic `n8n.control`:
`{"type":"say","text":"..."}` or `{"type":"end"}`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | | LiveKit connection |
| `N8N_BASE_URL`, `N8N_BEARER` | | n8n webhook base and token (required) |
| `N8N_MODE` | `turn` | `turn`, `realtime` or `scripted` |
| `N8N_AGENT_NAME` | empty | name for explicit dispatch; empty joins every room |
| `N8N_PATH_START`, `N8N_PATH_TURN`, `N8N_PATH_END` | `/ar/v1/session/...` | path overrides |
| `N8N_PATH_HANDOFF` | empty | n8n endpoint that transfers the caller |
| `N8N_REPORT_END` | `false` | post the end record |
| `N8N_END_CALL` | `n8n` | `worker` deletes the room on `end` instead of leaving that to n8n |
| `N8N_MCP_URL`, `N8N_MCP_TOKEN`, `N8N_MCP_ALLOWED_TOOLS` | | n8n MCP Server Trigger with the tools (realtime, scripted) |
| `N8N_SCRIPT_FILE` | | the script (scripted mode) |
| `N8N_TOOL_END`, `N8N_TOOL_HANDOFF` | `EndCall`, `RequestAgent` | tool names that end the call or hand it off |
| `REALTIME_MODEL`, `REALTIME_VOICE`, `REALTIME_VAD_THRESHOLD`, `REALTIME_VAD_SILENCE_MS` | `gpt-realtime`, `marin`, `0.7`, `700` | realtime model and its speech detection |
| `REALTIME_INSTRUCTIONS_FILE` | | instructions for realtime mode; `{{sessionId}}`, `{{languageName}}`, `{{dealer}}` are filled |
| `STT_MODEL`, `STT_BASE_URL`, `STT_API_KEY` | `gpt-4o-mini-transcribe` | OpenAI-compatible speech-to-text (turn mode) |
| `TTS_MODEL`, `TTS_VOICE`, `TTS_INSTRUCTIONS`, `TTS_SPEED`, `TTS_BASE_URL`, `TTS_API_KEY` | `gpt-4o-mini-tts`, `alloy` | OpenAI-compatible text-to-speech |
| `TTS_CACHE_DIR` | `.cache/tts` | voice cache (scripted mode) |
| `N8N_SEND_CALLER_NUMBER` | `true` | send the caller's number as `uui.msisdn` |
| `TEST_CALLER_VIN`, `TEST_CALLER_MSISDN` | empty | test only: identity sent as the UUI when a call has no User-to-User header |
| `FILLER_AFTER_MS`, `TURN_TIMEOUT_MS`, `FILLER_LINE_<LANG>`, `FALLBACK_LINE_<LANG>` | `2500`, `12000` | turn mode timing and lines |
| `N8N_TRANSCRIPT_DIR` | empty | test aid: a JSONL transcript per call (heard, said, tool calls). Off by default |

Secrets arrive as environment variables. By default nothing about a call is written to disk and recording
is off; log lines carry a session id, an event name and a UTC time. Set `hide_phone_number` on the SIP
dispatch rule to keep the caller's number out of room names.

## Run

```
python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
cp .env.example .env    # fill it in, then export it
.venv/bin/python -m livekit.agents start n8n_worker/agent.py
```

Route calls to it with a LiveKit SIP dispatch rule that names the agent (`N8N_AGENT_NAME`).

- `scripts/stub_n8n.py`: a stand-in for the start, turn and end endpoints.
- `scripts/contract_check.py`: scripted start, turn and end against n8n, no call.
- `scripts/script_check.py`: runs a script's tools against n8n and prints each line, no call.
- `scripts/voice_check.py --script`: renders the script's fixed lines into the voice cache.

## Things learned on real calls

- An inbound SIP call keeps ringing until an agent publishes audio; joining the room is not enough.
- The realtime API refuses a required tool choice before tools are registered; the worker sets it after start.
- With a separate text-to-speech voice, the model's speech detection can hear the agent's own audio as echo
  on a phone line. A threshold of 0.7 held; 0.5 cut prompts short.
- Line noise at pickup can cut a greeting. In scripted mode the caller is not heard during the opening.
- LiveKit refuses a SIP transfer on a number rented from LiveKit; transfers need a trunk whose provider
  accepts SIP REFER.

## Not built

Dockerfile and manifests, a spoken filler for slow tools in scripted mode, languages other than English
for the time filter, script lines served by n8n instead of a file.

## Licence

MIT, see `LICENSE`.
