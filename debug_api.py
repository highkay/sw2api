#!/usr/bin/env python3
"""
stagewise API Debug Tool

Tests all known API endpoints with the stored token.

Usage:
  python debug_api.py                  # Test all endpoints
  python debug_api.py --llm            # Also test LLM endpoint
  python debug_api.py --llm-only       # Only test LLM
  python debug_api.py --model MODEL    # Use specific model for LLM test
"""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.client import HTTPSConnection
from pathlib import Path


CONFIG_PATH = Path.home() / ".stagewise-proxy" / "config.json"
API_HOST = "api.stagewise.io"


def load_token():
    try:
        cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
        return cfg.get("token")
    except Exception:
        return None


def api_request(method, path, token, body=None, timeout=15):
    conn = HTTPSConnection(API_HOST, timeout=timeout)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "X-Stagewise-Client": "electron/1.10.2",
    }
    req_body = None
    if body is not None:
        req_body = json.dumps(body, ensure_ascii=False)
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(req_body.encode("utf-8")))

    conn.request(method, path, body=req_body, headers=headers)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8", errors="replace")
    resp_headers = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()

    j = None
    try:
        j = json.loads(raw)
    except Exception:
        pass

    return {"status": resp.status, "headers": resp_headers, "body": raw, "json": j}


def test_endpoint(name, method, path, token, body=None, show_body=500):
    print(f"\n{'=' * 50}")
    print(f"  {method} {path}")
    print(f"  [{name}]")
    print(f"{'=' * 50}")
    try:
        r = api_request(method, path, token, body)
        print(f"Status: {r['status']}")
        set_auth = r["headers"].get("set-auth-token")
        if set_auth:
            print(f"set-auth-token: {set_auth[:30]}...")
        body_preview = r["body"][:show_body]
        print(f"Body: {body_preview}")
        if len(r["body"]) > show_body:
            print(f"  ... ({len(r['body'])} bytes total)")
        return r
    except Exception as e:
        print(f"ERROR: {e}")
        return None


def main():
    token = load_token()
    if not token:
        print("X No token found. Run: python proxy.py --login")
        sys.exit(1)

    print(f"Token (first 30): {token[:30]}...")
    print(f"Host: {API_HOST}")

    do_llm = "--llm" in sys.argv
    llm_only = "--llm-only" in sys.argv
    model = "gpt-4o-mini"
    for i, arg in enumerate(sys.argv):
        if arg == "--model" and i + 1 < len(sys.argv):
            model = sys.argv[i + 1]

    if not llm_only:
        endpoints = [
            ("Session", "GET", "/v1/auth/get-session"),
            ("Subscription", "GET", "/v1/billing/plan"),
            ("Usage Current", "GET", "/v1/usage/current"),
            ("Usage History", "GET", "/v1/usage/history?days=7"),
            ("User Info", "GET", "/v1/auth/user"),
            ("AI Models", "GET", "/v1/ai/models"),
            ("Credits", "GET", "/v1/credits"),
        ]
        with ThreadPoolExecutor(max_workers=len(endpoints)) as pool:
            futs = {pool.submit(test_endpoint, name, method, path, token): name for name, method, path in endpoints}
            for f in as_completed(futs):
                f.result()

    if do_llm or llm_only:
        print(f"\n{'=' * 50}")
        print(f"  POST /v1/ai/chat/completions (model={model})")
        print(f"  [LLM Test]")
        print(f"{'=' * 50}")
        try:
            r = api_request("POST", "/v1/ai/chat/completions", token, {
                "model": model,
                "messages": [{"role": "user", "content": "Say hello in exactly 3 words."}],
                "max_tokens": 20,
                "stream": False,
            }, timeout=30)
            print(f"Status: {r['status']}")
            if r["json"]:
                choices = r["json"].get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "")
                    print(f"Response: {content}")
                usage = r["json"].get("usage", {})
                if usage:
                    print(f"Usage: prompt={usage.get('prompt_tokens')}, completion={usage.get('completion_tokens')}")
            else:
                print(f"Body: {r['body'][:300]}")
        except Exception as e:
            print(f"ERROR: {e}")

    print(f"\n{'=' * 50}")
    print("  Done.")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
