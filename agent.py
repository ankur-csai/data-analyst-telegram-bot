"""Core LLM agent: one OpenAI Responses-API call chain per Telegram chat.

Tools available to the model:
  - hosted `web_search`            (resolved entirely server-side by OpenAI)
  - custom `fetch_url`/`run_python` (executed locally, see tools.py)

Multi-turn state is just the chain of `previous_response_id`s per chat_id,
kept in memory (see CHAT_STATE below) - good enough for a single grading
conversation that happens within minutes on one running instance.
"""
import json
import os
import re
import tempfile
import time

from openai import OpenAI

import tools

MODEL = os.environ.get("OPENAI_MODEL") or "gpt-4o"
MAX_TOOL_ROUNDS = 8
TURN_BUDGET_SECONDS = 150

SYSTEM_PROMPT = """You are a data-analyst agent answering questions sent over Telegram, \
often referencing MOSPI (Ministry of Statistics, India) or other public datasets.

Rules for every reply:
1. If the message explicitly asks you to reply with ONLY a JSON object (it will usually \
spell out a literal template, e.g. `{"answer": {"state": "<state name>"}, "log_url": "..."}` \
or `{"state": "<state name>"}`), your reply must be EXACTLY that JSON object and nothing else \
- no markdown fences, no explanation, no extra text before or after. Fill in every \
placeholder with the real computed value, in the exact shape asked for (same keys, same \
nesting, correct JSON types - numbers as numbers, not strings, unless the template shows a \
string). If the template includes a "log_url" key, put the exact literal string \
__LOG_URL__ as its value (a fixed placeholder token) - it will be substituted afterward, so \
do not try to guess a real URL yourself.
2. If the message is just context/setup for a later question in the same conversation (no \
explicit request for a JSON-only reply), reply with one short natural-language sentence \
acknowledging it - do not try to produce JSON for these.
3. Use the tools available to you: web_search to find data sources or facts, fetch_url to \
download a specific dataset (CSV/XLSX/HTML) once you know its URL, and run_python (pandas/ \
numpy available) to compute over downloaded data. Prefer fetching primary data over relying \
on memory for anything numeric.
4. Work efficiently - you have a limited time budget per message.
"""

LOG_URL_PLACEHOLDER = "__LOG_URL__"


class ChatState:
    def __init__(self):
        self.previous_response_id = None
        self.workdir = tempfile.mkdtemp(prefix="chat_")


CHAT_STATE = {}


def _get_state(chat_id):
    if chat_id not in CHAT_STATE:
        CHAT_STATE[chat_id] = ChatState()
    return CHAT_STATE[chat_id]


def _extract_function_calls(response):
    return [item for item in response.output if getattr(item, "type", None) == "function_call"]


def _extract_tool_trace(response):
    trace = []
    for item in response.output:
        t = getattr(item, "type", None)
        if t == "function_call":
            trace.append({"tool": item.name, "arguments": item.arguments})
        elif t == "web_search_call":
            trace.append({"tool": "web_search", "status": getattr(item, "status", None)})
    return trace


def _strip_fences(text):
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    return m.group(1) if m else text


def _substitute_log_url(obj, real_url):
    if isinstance(obj, dict):
        return {k: (real_url if v == LOG_URL_PLACEHOLDER else _substitute_log_url(v, real_url))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_log_url(v, real_url) for v in obj]
    return obj


def answer(chat_id, text, real_log_url):
    """Run one Telegram-message turn. Returns (reply_text, trace) where trace
    is a JSON-serializable record suitable for the run log."""
    client = OpenAI()
    state = _get_state(chat_id)
    start = time.monotonic()
    full_trace = []

    kwargs = {
        "model": MODEL,
        "instructions": SYSTEM_PROMPT,
        "tools": [{"type": "web_search"}, tools.FETCH_URL_SCHEMA, tools.RUN_PYTHON_SCHEMA],
        "input": [{"role": "user", "content": text}],
    }
    if state.previous_response_id:
        kwargs["previous_response_id"] = state.previous_response_id

    response = client.responses.create(**kwargs)
    full_trace.extend(_extract_tool_trace(response))

    rounds = 0
    while True:
        calls = _extract_function_calls(response)
        if not calls:
            break
        rounds += 1
        if rounds > MAX_TOOL_ROUNDS or (time.monotonic() - start) > TURN_BUDGET_SECONDS:
            full_trace.append({"note": "tool-round/time budget exceeded, forcing stop"})
            break

        outputs = []
        for call in calls:
            result = tools.dispatch(call.name, call.arguments, state.workdir)
            full_trace.append({"tool": call.name, "arguments": call.arguments, "result": result})
            outputs.append({
                "type": "function_call_output",
                "call_id": call.call_id,
                "output": json.dumps(result)[:tools.MAX_TOOL_OUTPUT_CHARS],
            })

        response = client.responses.create(
            model=MODEL,
            instructions=SYSTEM_PROMPT,
            tools=[{"type": "web_search"}, tools.FETCH_URL_SCHEMA, tools.RUN_PYTHON_SCHEMA],
            previous_response_id=response.id,
            input=outputs,
        )
        full_trace.extend(_extract_tool_trace(response))

    state.previous_response_id = response.id
    reply_text = _finalize_reply(client, response, real_log_url, full_trace)
    return reply_text, full_trace


def _finalize_reply(client, response, real_log_url, full_trace):
    candidate = _strip_fences(response.output_text or "")

    if not candidate[:1] in ("{", "["):
        # Doesn't look like it was even trying to be JSON - this is an
        # acknowledgement turn (setup/context message), send as-is.
        return candidate

    parsed = _try_parse_json(candidate)

    if parsed is None:
        # Looked like JSON but failed to parse - one repair round-trip.
        repair = client.responses.create(
            model=MODEL,
            instructions=SYSTEM_PROMPT,
            previous_response_id=response.id,
            input=[{
                "role": "user",
                "content": ("Your last reply must be ONLY a single valid JSON object matching "
                            "the requested template - no prose, no markdown fences. Reply again "
                            "with just the JSON object."),
            }],
        )
        candidate = _strip_fences(repair.output_text or "")
        parsed = _try_parse_json(candidate)
        full_trace.append({"note": "repair round-trip used"})

    if parsed is None:
        # Not a JSON-only ask (e.g. an acknowledgement turn) or the model
        # never converged - send its text as-is.
        return candidate

    parsed = _substitute_log_url(parsed, real_log_url)
    return json.dumps(parsed, ensure_ascii=False)


def _try_parse_json(text):
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return obj
