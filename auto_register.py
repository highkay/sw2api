"""stagewise 全自动注册脚本 v2

自动完成:
  1. 创建 Mail.tm 临时邮箱（或使用已有邮箱）
  2. Playwright 浏览器自动填写 → Turnstile 自动通过 → 发送 OTP
  3. appleemail API 自动读取 OTP（需要 Outlook 邮箱+refresh_token）
  4. 填写 OTP → 登录成功 → 拦截 set-auth-token → 提取 API Token
  5. 输出格式兼容 sw2api WebUI 批量导入

用法:
  # 使用 Mail.tm 临时邮箱（无法收 stagewise 邮件，仅示例）
  python auto_register.py

  # 使用 Outlook 邮箱 + appleemail API 自动收发
  python auto_register.py --outlook \
    --email user@outlook.com \
    --refresh-token "M.C5..." \
    --client-id "9e5f94bc-..."

  # 批量处理
  python auto_register.py --batch-file accounts.txt

依赖:
  pip install playwright requests
  playwright install chromium
"""

import argparse
import json
import os
import re
import sys
import time
import uuid

import requests

CONSOLE_URL = "https://console.stagewise.io"
APPLEEMAIL_API = "https://www.appleemail.top"


def login_with_playwright(email: str, get_otp_func, headless: bool = False) -> str:
    """使用 Playwright 完成 stagewise 登录，返回 API token。

    Args:
        email: stagewise 登录邮箱
        get_otp_func: 获取 OTP 的回调函数，接收 playwright page 对象
        headless: 是否无头模式

    Returns:
        stagewise API token (set-auth-token)
    """
    from playwright.sync_api import sync_playwright

    captured = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        def on_response(resp):
            h = resp.headers
            if "set-auth-token" in h:
                captured.append(h["set-auth-token"])

        page.on("response", on_response)
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

        # 打开页面
        page.goto(CONSOLE_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(6000)

        # 填写邮箱
        page.fill("#email", email)
        page.click("button[type='submit']")
        page.wait_for_timeout(5000)
        print("  [✓] 验证码请求已发送")

        # 获取 OTP
        otp = get_otp_func(page)
        if not otp:
            print("  [✗] 获取 OTP 失败")
            ctx.close()
            browser.close()
            return None

        print(f"  [✓] OTP: {otp}")

        # 输入 OTP
        page.fill("#otp", otp)
        page.wait_for_timeout(300)
        page.click("button[type='submit']")
        page.wait_for_timeout(8000)

        # 检查结果
        text = page.evaluate("document.body.innerText")
        if "error" in text.lower() and "invalid" in text.lower():
            print(f"  [✗] OTP 验证失败: {text[:200]}")
            ctx.close()
            browser.close()
            return None

        print("  [✓] 登录成功")

        ctx.close()
        browser.close()

        if captured:
            return captured[-1]
        return None


def appleemail_otp(email, refresh_token, client_id):
    """使用 appleemail API 获取 OTP 的工厂函数"""

    def get_otp(page):
        """等待 INBOX 中出现新的 stagewise OTP 邮件"""
        # 先获取已有邮件数作为基线
        r = requests.post(f"{APPLEEMAIL_API}/api/mail-all", json={
            "refresh_token": refresh_token, "client_id": client_id,
            "email": email, "mailbox": "INBOX",
        })
        existing_count = len(r.json().get("data", [])) if r.status_code == 200 else 0

        for i in range(30):
            time.sleep(2)
            r = requests.post(f"{APPLEEMAIL_API}/api/mail-all", json={
                "refresh_token": refresh_token, "client_id": client_id,
                "email": email, "mailbox": "INBOX",
            })
            if r.status_code == 200:
                msgs = r.json().get("data", [])
                # 只看新邮件
                for m in msgs[existing_count:]:
                    body = m.get("text", "") or ""
                    codes = re.findall(r"\b(\d{6})\b", body)
                    if codes:
                        return codes[-1]
            print(f"   等待 OTP 邮件... ({i+1}/30)")
        return None

    return get_otp


def mailtm_get_otp(temp_mail, expected_count=0):
    """使用 Mail.tm 获取 OTP（但 stagewise 可能不发到 Mail.tm）"""

    def get_otp(page):
        # 暂时无法获取
        return None

    return get_otp


def main():
    parser = argparse.ArgumentParser(description="stagewise 全自动注册 v2")
    parser.add_argument("--outlook", action="store_true", help="使用 Outlook + appleemail API")
    parser.add_argument("--email", type=str, help="邮箱地址")
    parser.add_argument("--refresh-token", type=str, help="Outlook refresh_token")
    parser.add_argument("--client-id", type=str, help="Azure client_id")
    parser.add_argument("--headless", action="store_true", help="无头模式")
    parser.add_argument("--output", "-o", type=str, help="输出文件")
    parser.add_argument("--batch-file", type=str, help="批量文件，每行: email|refresh_token|client_id")
    args = parser.parse_args()

    if args.batch_file:
        # 批量模式
        with open(args.batch_file, encoding="utf-8") as f:
            accounts = []
            for line in f:
                line = line.strip()
                if line and "|" in line:
                    parts = line.split("|")
                    if len(parts) >= 3:
                        accounts.append((parts[0], parts[1], parts[2]))
        print(f"批量处理 {len(accounts)} 个账号...")
        results = []
        for i, (email, rt, cid) in enumerate(accounts):
            print(f"\n--- {i+1}/{len(accounts)}: {email} ---")
            otp_fn = appleemail_otp(email, rt, cid)

            # 清空 INBOX
            requests.post(f"{APPLEEMAIL_API}/api/process-inbox", json={
                "refresh_token": rt, "client_id": cid, "email": email,
            })

            token = login_with_playwright(email, otp_fn, headless=args.headless)
            if token:
                results.append({"email": email, "token": token})
                print(f"  Token: {token[:30]}...")
            else:
                print(f"  失败")

            # 写入实时结果
            with open(args.output or "stagewise_tokens.txt", "w", encoding="utf-8") as f:
                for r in results:
                    f.write(f"{r['email']}|{r['token']}\n")

        print(f"\n完成: {len(results)}/{len(accounts)}")

    elif args.outlook and args.email and args.refresh_token and args.client_id:
        # 单账号模式
        otp_fn = appleemail_otp(args.email, args.refresh_token, args.client_id)

        # 清空 INBOX
        requests.post(f"{APPLEEMAIL_API}/api/process-inbox", json={
            "refresh_token": args.refresh_token,
            "client_id": args.client_id,
            "email": args.email,
        })

        print(f"\n登录 {args.email}...")
        token = login_with_playwright(args.email, otp_fn, headless=args.headless)

        if token:
            line = f"{args.email}|{token}"
            print(f"\n=== TOKEN ===")
            print(line)

            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    f.write(line + "\n")

            # 输出 WebUI 导入命令
            print(f"\n导入 WebUI:")
            print(f'  curl -X POST http://localhost:8080/api/accounts/add \\')
            print(f'    -H "Content-Type: application/json" \\')
            print(f'    -d \'{{"email": "{args.email}", "token": "{token}"}}\'')
        else:
            print("登录失败")
            sys.exit(1)

    else:
        parser.print_help()
        print("\n\n需要指定 --outlook 模式和邮箱信息")


if __name__ == "__main__":
    sys.exit(main())
