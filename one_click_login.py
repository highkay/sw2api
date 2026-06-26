""" stagewise 一键登录 + 提取 API token (v2 - 支持 INBOX 和 Junk) """
import sys, time, requests, re
from playwright.sync_api import sync_playwright

API = "https://www.appleemail.top"

email = sys.argv[1]
rtok = sys.argv[2]
cid = sys.argv[3]

print(f"=== {email} ===")

# 清空两个文件夹
for box in ("INBOX", "Junk"):
    requests.post(f"{API}/api/process-inbox" if box == "INBOX" else f"{API}/api/process-junk",
                  json={"refresh_token": rtok, "client_id": cid, "email": email})

captured = []

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()

    def on_response(resp):
        h = resp.headers
        if "set-auth-token" in h:
            captured.append(("header", h["set-auth-token"]))
            print(f"  [TOKEN-header] {h['set-auth-token'][:40]}...")
            with open("stagewise_tokens.txt", "a") as f:
                f.write(f"{email}|{h['set-auth-token']}\n")
        # Also check response body for token
        if "sign-in/email-otp" in resp.url and resp.status == 200:
            try:
                body = resp.json()
                if isinstance(body, dict):
                    tok = body.get("token") or (body.get("data") or {}).get("token")
                    if tok:
                        captured.append(("body", tok))
                        print(f"  [TOKEN-body] {tok[:40]}...")
                        with open("stagewise_tokens.txt", "a") as f:
                            f.write(f"{email}|{tok}\n")
            except:
                pass
    page.on("response", on_response)
    page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

    page.goto("https://console.stagewise.io", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(6000)
    page.fill("#email", email)
    page.click("button[type='submit']")
    page.wait_for_timeout(5000)
    print("  OTP sent")

    # 查 INBOX 和 Junk 两个文件夹
    otp = None
    for i in range(30):
        time.sleep(2)
        for box in ("INBOX", "Junk"):
            r = requests.post(f"{API}/api/mail-all", json={
                "refresh_token": rtok, "client_id": cid, "email": email, "mailbox": box,
            })
            if r.status_code == 200:
                for m in r.json().get("data", []):
                    codes = re.findall(r"\b(\d{6})\b", m.get("text", "") or "")
                    if codes:
                        otp = codes[-1]
                        print(f"  Found in {box}: {otp}")
                        break
            if otp:
                break
        if otp:
            break
        print(f"  wait... ({i+1}/30)")

    if not otp:
        print("  FAILED: no OTP")
        ctx.close(); browser.close(); sys.exit(1)

    page.fill("#otp", otp)
    page.wait_for_timeout(300)
    page.click("button[type='submit']")
    page.wait_for_timeout(8000)

    # 检查结果（容错：页面可能已导航）
    try:
        text = page.evaluate("document.body.innerText")
        if "error" in text.lower() and "invalid" in text.lower():
            print(f"  FAILED: {text[:200]}")
            ctx.close(); browser.close()
            # Still check captured tokens before exiting
            if captured:
                tok = captured[-1][1]
                print(f"\n  Token was captured despite error: {tok[:30]}...")
                with open("stagewise_tokens.txt", "a") as f:
                    f.write(f"{email}|{tok}\n")
            sys.exit(1)
    except Exception as e:
        print(f"  (Page navigation during check: {e})")

    print(f"  Login OK")

    ctx.close()
    browser.close()

    if captured:
        tok = captured[-1][1]
        print(f"\n=== SUCCESS ===")
        print(f"  {email}|{tok}")
        with open("stagewise_tokens.txt", "a") as f:
            f.write(f"{email}|{tok}\n")
    else:
        print("\nNo token captured")
