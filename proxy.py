#!/usr/bin/env python3
"""
stagewise Reverse Proxy (Python)

Full lifecycle:
  1. Multi-account management with tokens
  2. Reverse-proxy LLM requests -> https://api.stagewise.io
  3. OpenAI-compatible API on localhost
  4. Multi-account support with 2 strategies:
     - specific:  use activeAccount only (default)
     - fill_first: burn through one account at a time

Usage:
  python proxy.py [--port 11434]   Start reverse proxy (default)
  python proxy.py --strategy fill_first  Use fill-first strategy

Compatible with OpenAI client libraries:
  openai.api_base = "http://localhost:11434/v1"
"""

import argparse
import json
import os
import sys
import ssl
import threading
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, urlencode, parse_qs
from http.client import HTTPSConnection
from pathlib import Path
import call_log

API_ORIGIN = "https://api.stagewise.io"
SYSTEM_PROMPT = "<soul><environment><authorities>stagewisestagewisestagewise"
DEFAULT_PORT = 11434
BASE_DIR = Path(__file__).parent
CONFIG_DIR = BASE_DIR / "data"
CONFIG_PATH = CONFIG_DIR / "config.json"

# ─── Strategy constants ─────────────────────────────────────────
STRATEGY_SPECIFIC = "specific"
STRATEGY_FILL_FIRST = "fill_first"

# Cooldown durations (seconds)
COOLDOWN_429 = 60
COOLDOWN_5XX = 30
DEFAULT_RETRY_AFTER = 30


class AccountState:
    def __init__(self, email):
        self.email = email
        self.banned = False
        self.banned_reason = ""
        self.cooldown_until = 0.0

    def unban(self):
        self.banned = False
        self.banned_reason = ""

    def set_cooldown(self, seconds, now=None):
        if now is None:
            now = time.time()
        self.cooldown_until = now + seconds

    def is_available(self, now=None):
        if self.banned:
            return False
        if now is None:
            now = time.time()
        return now >= self.cooldown_until

    def cooldown_remaining(self, now=None):
        if now is None:
            now = time.time()
        return max(0.0, self.cooldown_until - now)


# Module-level account state (thread-safe)
_account_states_lock = threading.Lock()
_account_states = {}
_config_lock = threading.Lock()


def get_account_state(email):
    with _account_states_lock:
        if email not in _account_states:
            _account_states[email] = AccountState(email)
        return _account_states[email]


def handle_upstream_status(email, status, cfg):
    """Apply cooldown/disable based on upstream status.
    Returns (out_status, retry_seconds):
      - out_status: status to send to client (403 -> 429 so downstream retries)
      - retry_seconds: Retry-After hint, or None
    403 (plan limit) -> disable account, re-enabled by refresh-usage.
    """
    if not email:
        return status, None

    if status == 429:
        state = get_account_state(email)
        state.set_cooldown(COOLDOWN_429)
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {email} cooled down {COOLDOWN_429}s (429)")
        return 429, COOLDOWN_429

    if 500 <= status < 600:
        state = get_account_state(email)
        state.set_cooldown(COOLDOWN_5XX)
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {email} cooled down {COOLDOWN_5XX}s (5xx)")
        return 429, COOLDOWN_5XX

    if status == 401:
        state = get_account_state(email)
        state.set_cooldown(COOLDOWN_429)
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {email} cooled down {COOLDOWN_429}s (401)")
        return 429, COOLDOWN_429

    if status in (402, 403):
        if email in cfg.get("accounts", {}):
            cfg["accounts"][email]["disabled"] = True
            save_config(cfg)
        retry = next_available_in(cfg)
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {email} disabled (status {status}), retry in {retry}s")
        return 429, retry

    return status, None


def next_available_in(cfg, now=None):
    """Seconds until the next cooled-down account becomes available."""
    if now is None:
        now = time.time()
    soonest = None
    for email in cfg.get("accounts", {}):
        if cfg["accounts"][email].get("disabled"):
            continue
        state = get_account_state(email)
        rem = state.cooldown_remaining(now)
        if rem > 0:
            if soonest is None or rem < soonest:
                soonest = rem
    if soonest is None:
        return DEFAULT_RETRY_AFTER
    return int(soonest) + 1


def get_available_accounts(cfg, now=None):
    """Return list of (email, account_info) for accounts not in cooldown, sorted."""
    if now is None:
        now = time.time()
    accounts = cfg.get("accounts", {})
    available = []
    for email in accounts:
        acct = accounts[email]
        if acct.get("disabled"):
            continue
        state = get_account_state(email)
        if state.is_available(now):
            available.append((email, acct))
    available.sort(key=lambda x: x[0])
    return available


def select_account(cfg, strategy, specific_email=None):
    """Select an account based on the strategy.
    Returns (email, account_info) or (None, None) if none available.
    """
    accounts = cfg.get("accounts", {})
    if not accounts:
        return None, None

    if strategy == STRATEGY_SPECIFIC:
        email = specific_email or cfg.get("activeAccount")
        if email and email in accounts:
            acct = accounts[email]
            if not acct.get("disabled"):
                state = get_account_state(email)
                if state.is_available():
                    return email, acct
        return None, None

    available = get_available_accounts(cfg)
    if not available:
        return None, None

    if strategy == STRATEGY_FILL_FIRST:
        return available[0]

    email = cfg.get("activeAccount")
    if email and email in accounts:
        acct = accounts[email]
        if not acct.get("disabled"):
            return email, acct
    return None, None


# ─── Config ─────────────────────────────────────────────────────

MODELS = [
    {"id": "claude-fable-5", "object": "model", "owned_by": "anthropic"},
    {"id": "claude-opus-4.8", "object": "model", "owned_by": "anthropic"},
    {"id": "claude-opus-4.7", "object": "model", "owned_by": "anthropic"},
    {"id": "claude-opus-4.6", "object": "model", "owned_by": "anthropic"},
    {"id": "claude-sonnet-4.6", "object": "model", "owned_by": "anthropic"},
    {"id": "claude-haiku-4.5", "object": "model", "owned_by": "anthropic"},
    {"id": "gpt-5.5", "object": "model", "owned_by": "openai"},
    {"id": "gpt-5.4", "object": "model", "owned_by": "openai"},
    {"id": "gpt-5.3-codex", "object": "model", "owned_by": "openai"},
    {"id": "gpt-5.3-chat", "object": "model", "owned_by": "openai"},
    {"id": "gpt-5.4-mini", "object": "model", "owned_by": "openai"},
    {"id": "gpt-5.4-nano", "object": "model", "owned_by": "openai"},
    {"id": "gemini-3.1-pro-preview", "object": "model", "owned_by": "google"},
    {"id": "gemini-3.5-flash", "object": "model", "owned_by": "google"},
    {"id": "gemini-3-flash-preview", "object": "model", "owned_by": "google"},
    {"id": "gemini-3.1-flash-lite", "object": "model", "owned_by": "google"},
    {"id": "kimi-k2.7-code", "object": "model", "owned_by": "moonshotai"},
    {"id": "kimi-k2.6", "object": "model", "owned_by": "moonshotai"},
    {"id": "kimi-k2.5", "object": "model", "owned_by": "moonshotai"},
    {"id": "qwen3-32b", "object": "model", "owned_by": "alibaba"},
    {"id": "qwen3-coder-30b-a3b-instruct", "object": "model", "owned_by": "alibaba"},
    {"id": "deepseek-v4-pro", "object": "model", "owned_by": "deepseek"},
    {"id": "deepseek-v4-flash", "object": "model", "owned_by": "deepseek"},
    {"id": "glm-5.2", "object": "model", "owned_by": "z-ai"},
    {"id": "glm-5.1", "object": "model", "owned_by": "z-ai"},
    {"id": "glm-5v-turbo", "object": "model", "owned_by": "z-ai"},
    {"id": "minimax-m3", "object": "model", "owned_by": "minimax"},
    {"id": "minimax-m2.7", "object": "model", "owned_by": "minimax"},
    {"id": "MiniMax-M2", "object": "model", "owned_by": "minimax"},
    {"id": "mimo-v2.5-pro", "object": "model", "owned_by": "xiaomi-mimo"},
    {"id": "mimo-v2.5", "object": "model", "owned_by": "xiaomi-mimo"},
]

MODEL_PROVIDER = {m["id"]: m["owned_by"] for m in MODELS}


def resolve_model(model):
    parts = model.rsplit("/", 1)
    name = parts[-1]
    prefix = MODEL_PROVIDER.get(name)
    if prefix:
        model = f"{prefix}/{name}"
    return model


def ensure_config_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config():
    try:
        cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
        if "token" in cfg and "accounts" not in cfg:
            email = (cfg.get("user") or {}).get("email", "default")
            cfg["accounts"] = {email: {"token": cfg["token"], "user": cfg.get("user")}}
            cfg["activeAccount"] = email
            save_config(cfg)
        cfg.setdefault("strategy", STRATEGY_SPECIFIC)
        return cfg
    except Exception:
        return {"token": None, "user": None, "port": DEFAULT_PORT, "strategy": STRATEGY_SPECIFIC}


def save_config(cfg):
    with _config_lock:
        ensure_config_dir()
        clean = {k: v for k, v in cfg.items() if k not in ("token", "user")}
        CONFIG_PATH.write_text(json.dumps(clean, indent=2, ensure_ascii=False), "utf-8")


def get_active_token(cfg=None):
    if cfg is None:
        cfg = load_config()
    active = cfg.get("activeAccount")
    if active and active in cfg.get("accounts", {}):
        return cfg["accounts"][active].get("token")
    return cfg.get("token")


# ─── Anthropic <-> OpenAI conversion ─────────────────────────────

_OAI_FINISH_TO_ANTHROPIC_STOP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
}


def anthropic_request_to_openai(req):
    """Convert an Anthropic /messages request body into an OpenAI chat/completions body."""
    messages = []
    system = req.get("system")
    if system:
        if isinstance(system, str):
            messages.append({"role": "system", "content": system})
        elif isinstance(system, list):
            text = "".join(b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text")
            messages.append({"role": "system", "content": text})

    for m in req.get("messages", []):
        role = m.get("role", "user")
        content = m.get("content", "")
        if not isinstance(content, list):
            messages.append({"role": role, "content": content or ""})
            continue

        texts = []
        tool_calls = []
        tool_results = []
        for b in content:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "text":
                texts.append(b.get("text", ""))
            elif btype == "thinking":
                texts.append(f"[thinking] {b.get('thinking', '')}")
            elif btype == "image":
                src = b.get("source", {})
                if src.get("type") == "base64":
                    texts.append(f"[image: data:{src.get('media_type','image/png')};base64,{src.get('data','')[:50]}...]")
                elif src.get("type") == "url":
                    texts.append(f"[image: {src.get('url','')}]")
            elif btype == "video":
                src = b.get("source", {})
                if src.get("type") == "url":
                    texts.append(f"[video: {src.get('url','')}]")
                elif src.get("type") == "base64":
                    texts.append(f"[video: base64 {src.get('media_type','video/mp4')} ...]")
            elif btype == "tool_use":
                tool_calls.append({
                    "id": b.get("id", "toolu_" + uuid.uuid4().hex[:12]),
                    "type": "function",
                    "function": {
                        "name": b.get("name", ""),
                        "arguments": json.dumps(b.get("input", {}), ensure_ascii=False),
                    },
                })
            elif btype == "tool_result":
                tool_results.append({
                    "tool_call_id": b.get("tool_use_id", ""),
                    "content": b.get("content", ""),
                })

        if role == "assistant":
            text = "\n".join(texts)
            msg = {"role": "assistant", "content": text}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            messages.append(msg)
        elif role == "user":
            if texts:
                messages.append({"role": "user", "content": "\n".join(texts)})
            for tr in tool_results:
                content = tr["content"]
                if isinstance(content, list):
                    content = "\n".join(
                        c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
                    )
                if isinstance(content, dict):
                    content = json.dumps(content, ensure_ascii=False)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tr["tool_call_id"],
                    "content": str(content) if content else "",
                })

    oai = {
        "model": req.get("model"),
        "messages": messages,
        "stream": req.get("stream", False),
    }
    if "max_tokens" in req:
        oai["max_tokens"] = req["max_tokens"]
    if "temperature" in req:
        oai["temperature"] = req["temperature"]
    if "top_p" in req:
        oai["top_p"] = req["top_p"]
    if "top_k" in req:
        oai["top_k"] = req["top_k"]
    if "stop_sequences" in req:
        oai["stop"] = req["stop_sequences"]
    meta = req.get("metadata")
    if isinstance(meta, dict) and meta.get("user_id"):
        oai["user"] = meta["user_id"]
    # tools conversion: anthropic -> openai
    anth_tools = req.get("tools")
    if anth_tools:
        oai_tools = []
        for t in anth_tools:
            oai_tools.append({
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                },
            })
        oai["tools"] = oai_tools
    # tool_choice conversion
    anth_tc = req.get("tool_choice")
    if anth_tc:
        if isinstance(anth_tc, str):
            oai["tool_choice"] = "required" if anth_tc == "any" else anth_tc
        elif isinstance(anth_tc, dict):
            tc_type = anth_tc.get("type")
            if tc_type == "tool":
                oai["tool_choice"] = {"type": "function", "function": {"name": anth_tc.get("name", "")}}
            elif tc_type == "any":
                oai["tool_choice"] = "required"
            elif tc_type in ("auto", "none"):
                oai["tool_choice"] = tc_type
    # thinking -> reasoning mapping
    anth_thinking = req.get("thinking")
    if isinstance(anth_thinking, dict):
        if anth_thinking.get("type") == "enabled":
            effort = "high"
            bt = anth_thinking.get("budget_tokens")
            if bt is not None:
                if bt < 2048:
                    effort = "low"
                elif bt < 8192:
                    effort = "medium"
                else:
                    effort = "high"
            oai["reasoning"] = {"enabled": True, "effort": effort}
        elif anth_thinking.get("type") == "disabled":
            oai["reasoning"] = {"enabled": False}
    # top-level reasoning_effort (overrides thinking mapping if set)
    re_effort = req.get("reasoning_effort")
    if re_effort:
        mapping = {"low": "low", "medium": "high", "high": "high", "xhigh": "max"}
        oai.setdefault("reasoning", {})["effort"] = mapping.get(re_effort, "high")
    # output_config -> OpenAI response_format
    out_cfg = req.get("output_config")
    if isinstance(out_cfg, dict):
        fmt = out_cfg.get("format") or {}
        if fmt.get("type") == "json_schema":
            oai["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "schema": fmt.get("schema", {}),
                    "strict": True,
                },
            }
    return oai


def openai_response_to_anthropic(oai_resp, model):
    """Convert a non-streaming OpenAI chat.completion response into an Anthropic message."""
    choices = oai_resp.get("choices") or [{}]
    choice = choices[0]
    msg = choice.get("message", {}) or {}
    content = msg.get("content", "")
    tool_calls = msg.get("tool_calls")
    reasoning = msg.get("reasoning")
    blocks = []
    if reasoning:
        blocks.append({"type": "thinking", "thinking": reasoning, "signature": ""})
    if content:
        blocks.append({"type": "text", "text": content})
    if tool_calls:
        for tc in tool_calls:
            fn = (tc or {}).get("function", {}) or {}
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except Exception:
                args = {}
            blocks.append({
                "type": "tool_use",
                "id": tc.get("id", "toolu_" + uuid.uuid4().hex[:12]),
                "name": fn.get("name", ""),
                "input": args,
            })
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    finish = choice.get("finish_reason", "stop")
    stop_reason = _OAI_FINISH_TO_ANTHROPIC_STOP.get(finish, "end_turn")
    usage = oai_resp.get("usage", {}) or {}
    pt = usage.get("prompt_tokens", 0) or 0
    ct = usage.get("completion_tokens", 0) or 0
    pt_details = usage.get("prompt_tokens_details") or {}
    ct_details = usage.get("completion_tokens_details") or {}
    anth_usage = {
        "input_tokens": pt,
        "output_tokens": ct,
    }
    cache_read = pt_details.get("cached_tokens", 0) or 0
    cache_write = pt_details.get("cache_write_tokens", 0) or 0
    if cache_read:
        anth_usage["cache_read_input_tokens"] = cache_read
    if cache_write:
        anth_usage["cache_creation_input_tokens"] = cache_write
    return {
        "id": oai_resp.get("id", "msg_" + uuid.uuid4().hex),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": anth_usage,
    }


def stream_openai_to_anthropic(resp, wfile, model):
    """Convert an OpenAI SSE stream into an Anthropic SSE stream, writing to wfile.
    Returns (input_tokens, output_tokens)."""
    msg_id = "msg_" + uuid.uuid4().hex
    input_tokens = 0
    output_tokens = 0
    cache_creation = 0
    cache_read = 0
    stop_reason = "end_turn"
    started = False
    active_block_type = None
    active_block_index = None
    active_tool_key = None
    next_block_index = 0
    buf = b""
    last_emit_time = time.time()

    def emit(event_type, data):
        nonlocal last_emit_time
        wfile.write(f"event: {event_type}\n".encode("utf-8"))
        wfile.write(f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))
        wfile.flush()
        last_emit_time = time.time()

    def maybe_ping():
        nonlocal last_emit_time
        if time.time() - last_emit_time >= 15:
            wfile.write(b"event: ping\ndata: {\"type\": \"ping\"}\n\n")
            wfile.flush()
            last_emit_time = time.time()

    def ensure_message_started():
        nonlocal started
        if started:
            return
        emit("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id, "type": "message", "role": "assistant",
                "model": model, "content": [],
                "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": 0},
            },
        })
        started = True

    def start_block(block_type, **kw):
        nonlocal active_block_type, active_block_index, next_block_index, active_tool_key
        idx = next_block_index
        next_block_index += 1
        active_block_type = block_type
        active_block_index = idx
        active_tool_key = kw.pop("_tool_key", None)
        payload = {"type": "content_block_start", "index": idx, "content_block": {"type": block_type}}
        payload["content_block"].update(kw)
        emit("content_block_start", payload)

    def delta_block(block_type, **kw):
        idx = active_block_index
        payload = {"type": "content_block_delta", "index": idx, "delta": {"type": block_type}}
        payload["delta"].update(kw)
        emit("content_block_delta", payload)

    def stop_active_block():
        nonlocal active_block_type, active_block_index, active_tool_key
        if active_block_type is None:
            return
        emit("content_block_stop", {"type": "content_block_stop", "index": active_block_index})
        active_block_type = None
        active_block_index = None
        active_tool_key = None

    def ensure_block(block_type, **kw):
        tool_key = kw.get("_tool_key")
        if active_block_type == block_type and (block_type != "tool_use" or active_tool_key == tool_key):
            return
        stop_active_block()
        start_block(block_type, **kw)

    try:
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                if not line.startswith(b"data:"):
                    continue
                data_str = line[5:].strip().decode("utf-8", errors="replace")
                if data_str == "[DONE]":
                    continue
                try:
                    d = json.loads(data_str)
                except Exception:
                    continue

                if not started:
                    u = d.get("usage") or {}
                    input_tokens = u.get("prompt_tokens", 0) or input_tokens
                    ensure_message_started()

                choices = d.get("choices") or []
                if choices:
                    delta = choices[0].get("delta", {}) or {}
                    text = delta.get("content")
                    if text:
                        ensure_block("text", text="")
                        delta_block("text_delta", text=text)

                    reasoning = delta.get("reasoning")
                    if reasoning:
                        ensure_block("thinking", thinking="", signature="")
                        delta_block("thinking_delta", thinking=reasoning)

                    tcs = delta.get("tool_calls")
                    if tcs:
                        for tc in tcs:
                            fn = tc.get("function", {}) or {}
                            tool_key = tc.get("index", 0)
                            if tc.get("id") or active_block_type != "tool_use" or active_tool_key != tool_key:
                                ensure_block(
                                    "tool_use",
                                    id=tc.get("id", "toolu_" + uuid.uuid4().hex[:12]),
                                    name=fn.get("name", ""),
                                    input={},
                                    _tool_key=tool_key,
                                )
                            if fn.get("arguments"):
                                delta_block("input_json_delta", partial_json=fn["arguments"])

                    finish = choices[0].get("finish_reason")
                    if finish:
                        stop_reason = _OAI_FINISH_TO_ANTHROPIC_STOP.get(finish, "end_turn")

                u = d.get("usage") or {}
                if u:
                    output_tokens = u.get("completion_tokens", 0) or output_tokens
                    pt_details = u.get("prompt_tokens_details") or {}
                    cache_read = pt_details.get("cached_tokens", 0) or cache_read
                    cache_creation = pt_details.get("cache_write_tokens", 0) or cache_creation

                maybe_ping()
    finally:
        ensure_message_started()
        stop_active_block()
        usage = {"output_tokens": output_tokens}
        if cache_creation:
            usage["cache_creation_input_tokens"] = cache_creation
        if cache_read:
            usage["cache_read_input_tokens"] = cache_read
        emit("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": usage,
        })
        emit("message_stop", {"type": "message_stop"})

    return input_tokens, output_tokens


def stream_openai_json_to_anthropic(oai_resp, wfile, model):
    """Convert a non-streaming OpenAI response into Anthropic SSE events."""
    anth_resp = openai_response_to_anthropic(oai_resp, model)

    def emit(event_type, data):
        wfile.write(f"event: {event_type}\n".encode("utf-8"))
        wfile.write(f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))
        wfile.flush()

    usage = anth_resp.get("usage", {}) or {}
    start_usage = {"input_tokens": usage.get("input_tokens", 0), "output_tokens": 0}
    if usage.get("cache_read_input_tokens"):
        start_usage["cache_read_input_tokens"] = usage["cache_read_input_tokens"]
    if usage.get("cache_creation_input_tokens"):
        start_usage["cache_creation_input_tokens"] = usage["cache_creation_input_tokens"]

    emit("message_start", {
        "type": "message_start",
        "message": {
            "id": anth_resp.get("id", "msg_" + uuid.uuid4().hex),
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": start_usage,
        },
    })

    for idx, block in enumerate(anth_resp.get("content") or []):
        btype = block.get("type")
        if btype == "text":
            emit("content_block_start", {"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}})
            text = block.get("text", "")
            if text:
                emit("content_block_delta", {"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": text}})
        elif btype == "thinking":
            emit("content_block_start", {"type": "content_block_start", "index": idx, "content_block": {"type": "thinking", "thinking": "", "signature": block.get("signature", "")}})
            thinking = block.get("thinking", "")
            if thinking:
                emit("content_block_delta", {"type": "content_block_delta", "index": idx, "delta": {"type": "thinking_delta", "thinking": thinking}})
        elif btype == "tool_use":
            emit("content_block_start", {"type": "content_block_start", "index": idx, "content_block": {"type": "tool_use", "id": block.get("id", "toolu_" + uuid.uuid4().hex[:12]), "name": block.get("name", ""), "input": {}}})
            emit("content_block_delta", {"type": "content_block_delta", "index": idx, "delta": {"type": "input_json_delta", "partial_json": json.dumps(block.get("input", {}), ensure_ascii=False)}})
        else:
            emit("content_block_start", {"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}})
            emit("content_block_delta", {"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": json.dumps(block, ensure_ascii=False)}})
        emit("content_block_stop", {"type": "content_block_stop", "index": idx})

    delta_usage = {"output_tokens": usage.get("output_tokens", 0)}
    emit("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": anth_resp.get("stop_reason", "end_turn"), "stop_sequence": anth_resp.get("stop_sequence")},
        "usage": delta_usage,
    })
    emit("message_stop", {"type": "message_stop"})
    return usage.get("input_tokens", 0), usage.get("output_tokens", 0)


# ─── HTTP Handler ───────────────────────────────────────────────
class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {args[0]}")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        if self.path == "/v1/models":
            self._handle_models()
            return
        if self.path == "/models" or self.path.startswith("/models?"):
            self._handle_anthropic_models()
            return
        self._proxy_request()

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/messages" or path == "/v1/messages":
            self._handle_anthropic_messages()
            return
        self._proxy_request()

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, x-api-key, anthropic-version, x-stainless-*, X-Account-Email")

    def _handle_models(self):
        resp = {"object": "list", "data": MODELS}
        body = json.dumps(resp, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_anthropic_models(self):
        now_iso = "2025-01-01T00:00:00Z"
        data = [
            {
                "id": m["id"],
                "display_name": m["id"],
                "created_at": now_iso,
                "type": "model",
            }
            for m in MODELS
        ]
        resp = {
            "data": data,
            "has_more": False,
            "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None,
        }
        body = json.dumps(resp, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _select_request_account(self):
        """Select an account for this request based on strategy.
        Returns (email, account_info, token) or (None, None, None).
        """
        cfg = load_config()
        if not cfg:
            return None, None, None

        strategy = cfg.get("strategy", STRATEGY_SPECIFIC)

        specific_email = None
        if strategy == STRATEGY_SPECIFIC:
            specific_email = self.headers.get("X-Account-Email") or cfg.get("activeAccount")

        email, acct = select_account(cfg, strategy, specific_email)
        if email and acct:
            return email, acct, acct.get("token")
        return None, None, None

    def _proxy_request(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        is_chat = "/chat/completions" in self.path

        email, acct, token = self._select_request_account()
        if not token:
            ts = time.strftime("%H:%M:%S")
            cur_cfg = load_config()
            retry = next_available_in(cur_cfg)
            print(f"[{ts}] No available account (strategy={cur_cfg.get('strategy', '?')}), retry in {retry}s")
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", str(retry))
            self._cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": {
                    "code": "account_unavailable",
                    "message": "All accounts are cooling down, retry later",
                }
            }).encode("utf-8"))
            return

        target = urlparse(API_ORIGIN)
        conn = HTTPSConnection(target.hostname, target.port or 443, timeout=120)

        path = self.path
        req_model = "unknown"

        if is_chat:
            path = path.replace("/v1/chat/completions", "/v1/ai/chat/completions")
            upstream_headers = {
                "authorization": f"Bearer {token}",
                "content-type": "application/json",
                "user-agent": "ai-sdk/openai-compatible/2.0.47 ai-sdk/provider-utils/4.0.27 runtime/node.js/24",
                "x-stagewise-client": "electron/1.13.0",
                "Host": target.hostname,
            }
            if body:
                req_model = "unknown"
                try:
                    req = json.loads(body.decode("utf-8"))
                    req_model = req.get("model", "unknown")
                    req["model"] = resolve_model(req["model"])
                    reasoning_effort = req.pop("reasoning_effort", None)
                    if reasoning_effort is not None:
                        req.setdefault("reasoning", {})["effort"] = reasoning_effort
                    req.setdefault("reasoning", {"enabled": True, "effort": "high"})
                    req.setdefault("provider", {"require_parameters": True})
                    req.setdefault("messages", []).insert(0, {"role": "system", "content": SYSTEM_PROMPT})
                    body = json.dumps(req, ensure_ascii=False).encode("utf-8")
                except Exception:
                    pass
            if body:
                upstream_headers["Content-Length"] = str(len(body))
        else:
            upstream_headers = {}
            for key, val in self.headers.items():
                lk = key.lower()
                if lk in ("host", "connection", "proxy-connection", "keep-alive",
                           "transfer-encoding", "content-length"):
                    continue
                upstream_headers[key] = val
            upstream_headers["Authorization"] = f"Bearer {token}"
            upstream_headers["Host"] = target.hostname
            if body and "Content-Type" not in upstream_headers:
                upstream_headers["Content-Type"] = "application/json"
            if body:
                upstream_headers["Content-Length"] = str(len(body))

        try:
            conn.request(self.command, path, body=body, headers=upstream_headers)
            resp = conn.getresponse()

            set_auth = resp.getheader("set-auth-token")
            if set_auth:
                ts = time.strftime("%H:%M:%S")
                print(f"[{ts}] Token rotated for {email}")
                cfg = load_config()
                if email in cfg.get("accounts", {}):
                    cfg["accounts"][email]["token"] = set_auth
                    save_config(cfg)

            status = resp.status
            out_status, retry_secs = handle_upstream_status(email, status, load_config())
            rewritten = out_status != status

            if rewritten:
                resp.read()
                body_out = json.dumps({
                    "error": {
                        "message": "Account rate-limited or disabled, please retry",
                        "type": "account_unavailable",
                        "code": "account_unavailable",
                    }
                }, ensure_ascii=False).encode("utf-8")
                self.send_response(out_status)
                self.send_header("Content-Type", "application/json")
                if retry_secs:
                    self.send_header("Retry-After", str(retry_secs))
                self._cors_headers()
                self.send_header("Content-Length", str(len(body_out)))
                self.end_headers()
                try:
                    self.wfile.write(body_out)
                except BrokenPipeError:
                    pass
                conn.close()
                call_log.record(req_model if is_chat else "unknown", email, 0, 0, "fail")
                return

            self.send_response(resp.status)
            skip = {"set-auth-token", "connection", "keep-alive", "transfer-encoding"}
            has_retry_after = False
            for key, val in resp.getheaders():
                if key.lower() not in skip:
                    self.send_header(key, val)
                    if key.lower() == "retry-after":
                        has_retry_after = True
            if status == 429 and not has_retry_after and retry_secs:
                self.send_header("Retry-After", str(retry_secs))
            self._cors_headers()
            self.end_headers()

            streaming = False
            ct = resp.getheader("content-type", "")
            if "text/event-stream" in ct:
                streaming = True

            tokens_used = 0
            model = "unknown"
            i_tokens = 0
            o_tokens = 0
            if streaming:
                last_data = None
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        last_data = chunk
                    except BrokenPipeError:
                        break
                if last_data:
                    for line in last_data.decode("utf-8", errors="replace").split("\n"):
                        if line.startswith("data: ") and "[DONE]" not in line:
                            try:
                                d = json.loads(line[6:])
                                model = d.get("model", model)
                                u = d.get("usage", {})
                                i_tokens = u.get("prompt_tokens", 0) or i_tokens
                                o_tokens = u.get("completion_tokens", 0) or o_tokens
                                tokens_used = u.get("total_tokens", 0) or tokens_used
                            except Exception:
                                pass
            else:
                data = resp.read()
                self.wfile.write(data)
                if resp.status == 200:
                    try:
                        d = json.loads(data.decode("utf-8", errors="replace"))
                        model = d.get("model", model)
                        u = d.get("usage", {})
                        i_tokens = u.get("prompt_tokens", 0)
                        o_tokens = u.get("completion_tokens", 0)
                        tokens_used = u.get("total_tokens", 0)
                    except Exception:
                        pass

            conn.close()

            if tokens_used > 0:
                call_log.record(model, email, i_tokens, o_tokens)
            else:
                call_log.record(req_model if is_chat else "unknown", email, 0, 0, "fail")

        except Exception as e:
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] Upstream error for {email}: {e}")
            if not self.headers_sent:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self._cors_headers()
                self.end_headers()
            self.wfile.write(json.dumps({
                "error": {"message": f"Upstream error: {e}", "type": "proxy_error"}
            }).encode("utf-8"))
            call_log.record(req_model if is_chat else "unknown", email, 0, 0, "error")

    def _handle_anthropic_messages(self):
        self._anth_resp_sent = False
        """Accept an Anthropic /messages request, convert to OpenAI, proxy upstream,
        then convert the OpenAI response back to Anthropic format."""
        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length) if content_length > 0 else b""

        try:
            anth_req = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.end_headers()
            self._anth_resp_sent = True
            self.wfile.write(json.dumps({
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "Invalid JSON body"},
            }).encode("utf-8"))
            return

        req_model = anth_req.get("model", "unknown")
        want_stream = bool(anth_req.get("stream", False))

        oai_req = anthropic_request_to_openai(anth_req)
        oai_req["model"] = resolve_model(oai_req.get("model") or req_model)
        oai_req.setdefault("reasoning", {"enabled": True, "effort": "high"})
        oai_req.setdefault("provider", {"require_parameters": True})
        oai_req.setdefault("messages", [])
        if oai_req["messages"] and oai_req["messages"][0].get("role") == "system":
            oai_req["messages"][0]["content"] = SYSTEM_PROMPT + "\n\n" + oai_req["messages"][0]["content"]
        else:
            oai_req["messages"].insert(0, {"role": "system", "content": SYSTEM_PROMPT})

        email, acct, token = self._select_request_account()
        if not token:
            ts = time.strftime("%H:%M:%S")
            cur_cfg = load_config()
            retry = next_available_in(cur_cfg)
            print(f"[{ts}] No available account (strategy={cur_cfg.get('strategy', '?')}), retry in {retry}s")
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", str(retry))
            self._cors_headers()
            self.end_headers()
            self._anth_resp_sent = True
            self.wfile.write(json.dumps({
                "type": "error",
                "error": {
                    "type": "overloaded_error",
                    "message": "All accounts are cooling down, retry later",
                },
            }).encode("utf-8"))
            return

        body = json.dumps(oai_req, ensure_ascii=False).encode("utf-8")
        target = urlparse(API_ORIGIN)
        conn = HTTPSConnection(target.hostname, target.port or 443, timeout=120)
        upstream_headers = {
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "user-agent": "ai-sdk/openai-compatible/2.0.47 ai-sdk/provider-utils/4.0.27 runtime/node.js/24",
            "x-stagewise-client": "electron/1.13.0",
            "Host": target.hostname,
            "Content-Length": str(len(body)),
        }

        try:
            conn.request("POST", "/v1/ai/chat/completions", body=body, headers=upstream_headers)
            resp = conn.getresponse()

            set_auth = resp.getheader("set-auth-token")
            if set_auth:
                ts = time.strftime("%H:%M:%S")
                print(f"[{ts}] Token rotated for {email}")
                cfg = load_config()
                if email in cfg.get("accounts", {}):
                    cfg["accounts"][email]["token"] = set_auth
                    save_config(cfg)

            status = resp.status
            out_status, retry_secs = handle_upstream_status(email, status, load_config())
            rewritten = out_status != status

            if rewritten:
                resp.read()
                err_body = json.dumps({
                    "type": "error",
                    "error": {
                        "type": "overloaded_error",
                        "message": "Account rate-limited or disabled, please retry",
                    },
                }, ensure_ascii=False).encode("utf-8")
                self.send_response(out_status)
                self.send_header("Content-Type", "application/json")
                if retry_secs:
                    self.send_header("Retry-After", str(retry_secs))
                self._cors_headers()
                self.send_header("Content-Length", str(len(err_body)))
                self.end_headers()
                self._anth_resp_sent = True
                try:
                    self.wfile.write(err_body)
                except BrokenPipeError:
                    pass
                conn.close()
                call_log.record(req_model, email, 0, 0, "fail")
                return

            ct = resp.getheader("content-type", "")
            upstream_streaming = "text/event-stream" in ct

            if want_stream:
                i_tokens = 0
                o_tokens = 0
                if resp.status != 200:
                    data = resp.read()
                    err_msg = "Upstream error"
                    try:
                        err_data = json.loads(data.decode("utf-8", errors="replace"))
                        err_msg = (err_data.get("error") or {}).get("message", str(err_data))[:200]
                    except Exception:
                        err_msg = data.decode("utf-8", errors="replace")[:200]
                    body_out = json.dumps({
                        "type": "error",
                        "error": {"type": "api_error", "message": err_msg},
                    }, ensure_ascii=False).encode("utf-8")
                    self.send_response(resp.status)
                    self.send_header("Content-Type", "application/json")
                    self._cors_headers()
                    self.send_header("Content-Length", str(len(body_out)))
                    self.end_headers()
                    self._anth_resp_sent = True
                    try:
                        self.wfile.write(body_out)
                    except BrokenPipeError:
                        pass
                    conn.close()
                    call_log.record(req_model, email, 0, 0, "fail")
                    return

                if upstream_streaming:
                    self.send_response(resp.status)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self._cors_headers()
                    self.end_headers()
                    self._anth_resp_sent = True
                    try:
                        i_tokens, o_tokens = stream_openai_to_anthropic(resp, self.wfile, req_model)
                    except BrokenPipeError:
                        pass
                else:
                    data = resp.read()
                    try:
                        oai_resp = json.loads(data.decode("utf-8", errors="replace"))
                    except Exception:
                        self.send_response(502)
                        self.send_header("Content-Type", "application/json")
                        self._cors_headers()
                        self.end_headers()
                        self._anth_resp_sent = True
                        self.wfile.write(json.dumps({
                            "type": "error",
                            "error": {"type": "api_error", "message": "Bad upstream response"},
                        }).encode("utf-8"))
                        conn.close()
                        call_log.record(req_model, email, 0, 0, "fail")
                        return

                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self._cors_headers()
                    self.end_headers()
                    self._anth_resp_sent = True
                    try:
                        i_tokens, o_tokens = stream_openai_json_to_anthropic(oai_resp, self.wfile, req_model)
                    except BrokenPipeError:
                        pass
                conn.close()
                if i_tokens or o_tokens:
                    call_log.record(req_model, email, i_tokens, o_tokens)
                else:
                    call_log.record(req_model, email, 0, 0, "fail")
                return

            # Non-streaming
            data = resp.read()
            if resp.status != 200:
                err_msg = "Upstream error"
                try:
                    err_data = json.loads(data.decode("utf-8", errors="replace"))
                    err_msg = (err_data.get("error") or {}).get("message", str(err_data))[:200]
                except Exception:
                    err_msg = data.decode("utf-8", errors="replace")[:200]
                body_out = json.dumps({
                    "type": "error",
                    "error": {"type": "api_error", "message": err_msg},
                }, ensure_ascii=False).encode("utf-8")
                self.send_response(resp.status)
                self.send_header("Content-Type", "application/json")
                self._cors_headers()
                self.send_header("Content-Length", str(len(body_out)))
                self.end_headers()
                self._anth_resp_sent = True
                try:
                    self.wfile.write(body_out)
                except BrokenPipeError:
                    pass
                conn.close()
                call_log.record(req_model, email, 0, 0, "fail")
                return

            try:
                oai_resp = json.loads(data.decode("utf-8", errors="replace"))
            except Exception:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self._cors_headers()
                self.end_headers()
                self._anth_resp_sent = True
                self.wfile.write(json.dumps({
                    "type": "error",
                    "error": {"type": "api_error", "message": "Bad upstream response"},
                }).encode("utf-8"))
                conn.close()
                call_log.record(req_model, email, 0, 0, "fail")
                return

            anth_resp = openai_response_to_anthropic(oai_resp, req_model)
            out_body = json.dumps(anth_resp, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.send_header("Content-Length", str(len(out_body)))
            self.end_headers()
            self._anth_resp_sent = True
            try:
                self.wfile.write(out_body)
            except BrokenPipeError:
                pass
            conn.close()
            u = anth_resp.get("usage", {})
            if u.get("input_tokens") or u.get("output_tokens"):
                call_log.record(req_model, email, u.get("input_tokens", 0), u.get("output_tokens", 0))
            else:
                call_log.record(req_model, email, 0, 0, "fail")

        except Exception as e:
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] Upstream error (anthropic) for {email}: {e}")
            if not getattr(self, '_anth_resp_sent', False):
                try:
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    self._cors_headers()
                    self.end_headers()
                    self._anth_resp_sent = True
                    self.wfile.write(json.dumps({
                        "type": "error",
                        "error": {"type": "api_error", "message": f"Upstream error: {e}"},
                    }).encode("utf-8"))
                except Exception:
                    pass
            call_log.record(req_model, email, 0, 0, "error")


def main():
    parser = argparse.ArgumentParser(description="stagewise Reverse Proxy")
    parser.add_argument("--port", "-p", type=int, default=DEFAULT_PORT, help=f"Proxy port (default: {DEFAULT_PORT})")
    parser.add_argument("--strategy", type=str,
                        choices=[STRATEGY_SPECIFIC, STRATEGY_FILL_FIRST],
                        help=f"Account selection strategy (default: {STRATEGY_SPECIFIC})")
    args = parser.parse_args()

    cfg = load_config()

    if args.strategy:
        cfg["strategy"] = args.strategy
        save_config(cfg)

    token = get_active_token(cfg)
    if not token:
        if cfg.get("accounts"):
            print("! No active account set. Use activeAccount in config or switch via WebUI.")
        else:
            print("! No accounts found. Add accounts via the WebUI or directly in config.")
        if not cfg.get("accounts"):
            print("  Continuing without accounts. Requests will return 429.")

    port = args.port or cfg.get("port", DEFAULT_PORT)
    server = HTTPServer(("0.0.0.0", port), ProxyHandler)

    print()
    print("=" * 50)
    print(f"   stagewise Proxy - Listening on :{port}")
    print(f"   Upstream: {API_ORIGIN}")
    print(f"   Strategy: {cfg.get('strategy', STRATEGY_SPECIFIC)}")
    print(f"   Accounts: {len(cfg.get('accounts', {}))}")
    print(f"   OpenAI-compatible:  http://localhost:{port}/v1")
    print(f"   Anthropic-compatible: http://localhost:{port}/messages")
    print("=" * 50)
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
