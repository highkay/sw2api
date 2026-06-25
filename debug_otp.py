#!/usr/bin/env python3
"""
stagewise OTP Login Debug Tool

Test the email OTP login flow step by step.

Usage:
  python debug_otp.py <email>               Send OTP
  python debug_otp.py <email> <otp>         Verify OTP and save token
"""

import json
import sys
from http.client import HTTPSConnection
from pathlib import Path


API_HOST = "api.stagewise.io"
CONFIG_PATH = Path.home() / ".stagewise-proxy" / "config.json"


def api_request(method, path, body=None, headers=None, timeout=15):
    conn = HTTPSConnection(API_HOST, timeout=timeout)
    req_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "stagewise-proxy/1.0 (Python)",
        "X-Stagewise-Client": "electron/1.10.2",
    }
    if headers:
        req_headers.update(headers)

    req_body = None
    if body is not None:
        req_body = json.dumps(body, ensure_ascii=False)
        req_headers["Content-Length"] = str(len(req_body.encode("utf-8")))

    conn.request(method, path, body=req_body, headers=req_headers)
    resp = conn.getresponse()

    resp_headers = {}
    for k, v in resp.getheaders():
        resp_headers[k.lower()] = v

    raw = resp.read().decode("utf-8", errors="replace")
    j = None
    try:
        j = json.loads(raw)
    except Exception:
        pass

    conn.close()
    return {"status": resp.status, "headers": resp_headers, "body": raw, "json": j}


def send_otp(email):
    print(f"\n-> Sending OTP to {email}...")
    r = api_request(
        "POST",
        "/v1/auth/email-otp/send-verification-otp",
        body={"email": email, "type": "sign-in"},
    )
    print(f"Status: {r['status']}")
    if r["json"]:
        err = r["json"].get("error")
        if err:
            print(f"Error: {err}")
        else:
            print("OK OTP sent successfully!")
    print(f"Headers:")
    for k, v in r["headers"].items():
        if not k.startswith(":"):
            print(f"  {k}: {v}")
    return r


def verify_otp(email, otp):
    print(f"\n-> Verifying OTP for {email}...")
    r = api_request(
        "POST",
        "/v1/auth/sign-in/email-otp",
        body={"email": email, "otp": otp},
    )
    print(f"Status: {r['status']}")

    set_auth = r["headers"].get("set-auth-token")
    print(f"set-auth-token: {set_auth[:30] + '...' if set_auth else 'NONE'}")

    print(f"Headers:")
    for k, v in r["headers"].items():
        if not k.startswith(":"):
            print(f"  {k}: {v}")

    j = r.get("json") or {}
    print(f"\nBody:")
    print(json.dumps(j, indent=2, ensure_ascii=False)[:800])

    token = set_auth or j.get("token") or (j.get("data") or {}).get("token")
    user = j.get("user") or (j.get("data") or {}).get("user")

    if token:
        print(f"\nOK Token: {token[:30]}...")
        config_dir = CONFIG_PATH.parent
        config_dir.mkdir(parents=True, exist_ok=True)
        config = {"token": token, "user": user, "port": 11434}
        CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False), "utf-8")
        print(f"OK Config saved to {CONFIG_PATH}")
    else:
        print("\nX No token found in response")

    return r


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python debug_otp.py <email>            Send OTP")
        print("  python debug_otp.py <email> <otp>      Verify OTP")
        sys.exit(1)

    email = sys.argv[1].strip()

    if len(sys.argv) >= 3:
        otp = sys.argv[2].strip()
        verify_otp(email, otp)
    else:
        send_otp(email)
        print("\nNow run: python debug_otp.py <email> <otp>")


if __name__ == "__main__":
    main()
