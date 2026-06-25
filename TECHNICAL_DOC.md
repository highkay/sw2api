# stagewise-py 技术文档

> stagewise 逆向代理管理工具集 — Python 实现  
> 版本：1.0 | 目标应用：stagewise v1.10.2 (Electron)  
> 平台：Windows (DPAPI 必需)

---

## 目录

1. [项目概述](#1-项目概述)
2. [架构设计](#2-架构设计)
3. [stagewise 应用逆向分析](#3-stagewise-应用逆向分析)
4. [模块详细文档](#4-模块详细文档)
   - 4.1 [proxy.py — 反向代理服务器](#41-proxypy--反向代理服务器)
   - 4.2 [decrypt_session.py — DPAPI 会话解密](#42-decrypt_sessionpy--dpapi-会话解密)
   - 4.3 [webui.py — Flask Web 管理界面](#43-webuipy--flask-web-管理界面)
   - 4.4 [debug_api.py — API 调试工具](#44-debug_apipy--api-调试工具)
   - 4.5 [debug_otp.py — OTP 登录调试工具](#45-debug_otppy--otp-登录调试工具)
   - 4.6 [machine_id.py — Machine ID 管理工具](#46-machine_idpy--machine-id-管理工具)
5. [WebUI 前端文档](#5-webui-前端文档)
6. [API 接口参考](#6-api-接口参考)
7. [加密与安全机制](#7-加密与安全机制)
8. [已知限制与问题](#8-已知限制与问题)
9. [安装与运行](#9-安装与运行)
10. [文件结构](#10-文件结构)

---

## 1. 项目概述

本项目是一套 Python 工具集，用于管理 stagewise 桌面应用（Electron）的认证会话，并提供 OpenAI 兼容的反向代理服务。

**核心功能：**

| 功能 | 说明 |
|------|------|
| Email OTP 登录 | 通过 stagewise 官方 API 进行邮箱验证码登录 |
| DPAPI 会话解密 | 从 Electron safeStorage 加密的 auth-session.json 中提取 token |
| 反向代理 | 提供 OpenAI 兼容 API，将请求代理至 `api.stagewise.io` |
| Web 管理界面 | Flask 驱动的全功能 WebUI，集成所有功能 |
| API 调试 | 测试所有 stagewise API 端点 |
| Machine ID 管理 | 查看/重置/伪造设备标识 |

---

## 2. 架构设计

```
┌─────────────────────────────────────────────────┐
│                  用户 / LLM 客户端               │
│          (OpenAI SDK / curl / WebUI)             │
└──────────────┬──────────────────┬───────────────┘
               │                  │
        HTTP :11434         HTTP :8080
               │                  │
       ┌───────▼───────┐  ┌──────▼──────┐
       │  proxy.py /   │  │  webui.py   │
       │  内嵌代理服务器 │  │  (Flask)    │
       └───────┬───────┘  └──────┬──────┘
               │                  │
               │   ┌──────────────┘
               │   │ 共享配置
               │   │ ~/.stagewise-proxy/config.json
               │   └──────────────┐
               │                  │
       ┌───────▼──────────────────▼──────┐
       │        HTTPS 请求转发            │
       │    api.stagewise.io:443         │
       │                                 │
       │  /v1/auth/*     认证端点        │
       │  /v1/billing/*  计费端点        │
       │  /v1/usage/*    用量端点        │
       │  /v1/ai/*       LLM 代理端点    │
       └─────────────────────────────────┘

本地文件系统：
  %APPDATA%\stagewise\stagewise\
    ├── auth-session.json    (DPAPI + AES-256-GCM 加密)
    ├── identity.json        (Machine ID)
  %APPDATA%\stagewise\session\
    └── Local State          (Chromium 加密主密钥)
  ~/.stagewise-proxy\
    └── config.json          (代理 token 缓存)
```

**关键设计决策：**

- **纯 Python 实现**：不依赖 Node.js，使用 Python 标准库 + Flask + cryptography
- **DPAPI 双重回退**：优先使用 `pywin32` 的 `win32crypt.CryptUnprotectData`，失败时回退到 PowerShell 调用
- **Token 自动轮换**：stagewise 服务端通过 `set-auth-token` 响应头自动轮换 token，所有模块均处理此机制
- **会话自动刷新**：proxy.py 每 300 秒主动刷新会话，避免 token 过期
- **配置共享**：所有脚本通过 `~/.stagewise-proxy/config.json` 共享 token

---

## 3. stagewise 应用逆向分析

### 3.1 应用基本信息

| 属性 | 值 |
|------|-----|
| 应用名称 | stagewise |
| 版本 | 1.10.2 |
| 框架 | Electron |
| 安装路径 | `C:\Users\zhang\AppData\Local\stagewise\app-1.10.2\` |
| 源码包 | `resources\app.asar` |
| 反编译路径 | `app_asar_extracted\.vite\build\main-COfp7snp.js` |
| API 源 | `https://api.stagewise.io` |
| 认证框架 | Better Auth |

### 3.2 认证流程

```
用户输入邮箱
    │
    ▼
POST /v1/auth/email-otp/send-verification-otp
    Body: {"email": "user@example.com", "type": "sign-in"}
    │
    ▼ (用户收到 6 位 OTP)
    │
POST /v1/auth/sign-in/email-otp
    Body: {"email": "user@example.com", "otp": "123456"}
    │
    ▼
响应头 set-auth-token: <新token>   ← 主要 token 来源
响应体 {"user": {...}, "session": {...}}
    │
    ▼
Token 存储：
  1. Electron safeStorage 加密 → auth-session.json (磁盘)
  2. 内存中用于 API 请求 (Bearer Token)
```

### 3.3 API 认证方式

所有需要认证的 API 请求使用 `Authorization: Bearer <token>` 头。

**特殊响应头：**

| 响应头 | 说明 |
|--------|------|
| `set-auth-token` | Token 自动轮换，客户端必须更新存储的 token |
| `x-pow-challenge` | Proof-of-Work 挑战（未启用） |
| `x-pow-reason` | PoW 原因说明 |

**允许的自定义请求头：**

```
x-visitor-id, x-request-id, x-pow-solution,
x-stagewise-client, x-captcha-response
```

**CORS 暴露的头：**

```
set-auth-token, x-pow-challenge, x-pow-reason
```

### 3.4 已知 API 端点

| 端点 | 方法 | 认证 | 说明 |
|------|------|------|------|
| `/v1/auth/email-otp/send-verification-otp` | POST | 否 | 发送 OTP |
| `/v1/auth/sign-in/email-otp` | POST | 否 | 验证 OTP 登录 |
| `/v1/auth/get-session` | GET | 是 | 获取当前会话信息 |
| `/v1/auth/sign-out` | POST | 是 | 注销 |
| `/v1/auth/user` | GET | 是 | 获取用户信息 |
| `/v1/billing/plan` | GET | 是 | 获取订阅计划 |
| `/v1/usage/current` | GET | 是 | 当前用量 |
| `/v1/usage/history?days=N` | GET | 是 | 历史用量 |
| `/v1/ai/models` | GET | 是 | 可用 AI 模型列表 |
| `/v1/ai/chat/completions` | POST | 是 | LLM 聊天补全 (OpenAI 兼容) |
| `/v1/credits` | GET | 是 | 积分查询 |

---

## 4. 模块详细文档

### 4.1 proxy.py — 反向代理服务器

**文件**：`proxy.py` (427 行)  
**用途**：独立运行的反向代理，提供 OpenAI 兼容 API

#### 命令行用法

```bash
python proxy.py --login            # 交互式 OTP 登录
python proxy.py --status           # 检查会话状态
python proxy.py --logout           # 注销并清除 token
python proxy.py [--port 11434]     # 启动反向代理（默认端口 11434）
```

#### 核心机制

**请求代理流程：**

```
客户端请求 → ProxyHandler
    │
    ├─ GET /v1/models → 返回内置模型列表（不转发）
    │
    └─ 其他请求 → 转发至 api.stagewise.io
         │
         ├─ 注入 Authorization: Bearer <token>
         ├─ 替换 Host 头
         ├─ 过滤 hop-by-hop 头
         │
         └─ 响应回传
              ├─ 检查 set-auth-token 头 → 自动更新 token
              ├─ SSE 流式响应 → 逐块转发 (4096 bytes/chunk)
              └─ 普通响应 → 一次性回传
```

**Token 自动刷新：**

- 后台守护线程每 300 秒调用 `GET /v1/auth/get-session`
- 如果服务端返回 `set-auth-token` 头，自动更新内存中的 token 和 `config.json`
- 如果返回 401/403，清除 token 并终止刷新线程

**内置模型列表：**

```python
MODELS = [
    {"id": "claude-fable-5",        "owned_by": "anthropic"},
    {"id": "claude-opus-4.8",       "owned_by": "anthropic"},
    {"id": "claude-sonnet-4.5",     "owned_by": "anthropic"},
    {"id": "gpt-5.5",              "owned_by": "openai"},
    {"id": "gpt-5.4",              "owned_by": "openai"},
    {"id": "gpt-5",                "owned_by": "openai"},
    {"id": "o3-pro",               "owned_by": "openai"},
    {"id": "gemini-3-flash-preview","owned_by": "google"},
    {"id": "gemini-2.5-pro",       "owned_by": "google"},
    {"id": "deepseek-v4-pro",      "owned_by": "deepseek"},
    {"id": "kimi-k2.5",            "owned_by": "moonshotai"},
    {"id": "MiniMax-M2",           "owned_by": "minimax"},
]
```

**CORS 支持：**

```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: GET, POST, PUT, DELETE, OPTIONS
Access-Control-Allow-Headers: Content-Type, Authorization, x-stainless-*
```

**配置文件**：`~/.stagewise-proxy/config.json`

```json
{
  "token": "C0ptGjNIKP6XDUUgdW2B3Xu9pFxG87Jq...",
  "user": {
    "email": "user@example.com",
    "id": "uuid",
    "name": "User Name"
  },
  "port": 11434
}
```

**与 OpenAI SDK 兼容用法：**

```python
import openai

client = openai.OpenAI(
    api_key="any-key",      # 代理忽略此值，使用自己的 token
    base_url="http://localhost:11434/v1"
)

response = client.chat.completions.create(
    model="claude-fable-5",
    messages=[{"role": "user", "content": "Hello"}],
    stream=True
)
```

---

### 4.2 decrypt_session.py — DPAPI 会话解密

**文件**：`decrypt_session.py` (175 行)  
**用途**：从 Electron safeStorage 加密的 auth-session.json 中提取认证 token

#### 命令行用法

```bash
python decrypt_session.py           # 解密并显示会话数据
python decrypt_session.py --save    # 解密并将 token 保存到 config.json
```

#### 解密链路

```
auth-session.json (二进制)
    │
    ▼ 读取原始字节
    │
    ├─ 前缀 v10/v20? → 是：Chromium 加密
    │   │
    │   ├─ bytes[3:15]  → 12字节 AES-GCM Nonce
    │   ├─ bytes[15:]   → 密文 + 16字节认证标签
    │   │
    │   └─ AES-256-GCM 解密 (master_key, nonce, ciphertext+tag, no AAD)
    │
    └─ 非标准前缀 → 尝试 UTF-8 解码（明文存储）

Local State → os_crypt.encrypted_key
    │
    ▼ Base64 解码
    │
    ├─ 前5字节 == "DPAPI" → 剥离前缀
    │
    ▼ DPAPI CryptUnprotectData
    │
    └─ 32字节 AES-256 Master Key
```

#### DPAPI 解密双重回退策略

```python
def dpapi_decrypt(encrypted):
    # 方法1：pywin32 (推荐，最快)
    try:
        import win32crypt
        result = win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)
        # CryptUnprotectData 返回 (description, data) 元组
        if isinstance(result, tuple):
            return result[1]
        return result
    except ImportError:
        pass

    # 方法2：PowerShell 回退
    # 适用于未安装 pywin32 的环境
    ps_cmd = (
        "Add-Type -AssemblyName System.Security; "
        "$b = [Convert]::FromBase64String('<base64>'); "
        "$r = [System.Security.Cryptography.ProtectedData]::Unprotect("
        "$b, $null, 'CurrentUser'); "
        "[Convert]::ToBase64String($r)"
    )
    # subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd])
```

**为什么不用 ctypes 直接调用？**

直接通过 ctypes 调用 `CryptUnprotectData` 需要手动定义 `DATA_BLOB` 结构体和函数签名，代码脆弱且容易出错。`win32crypt` 提供了 Pythonic 封装，而 PowerShell 回退则利用 .NET 的 `System.Security.Cryptography.ProtectedData`，两者都是官方支持的 API。

#### 关键文件路径

| 文件 | 路径 | 说明 |
|------|------|------|
| auth-session.json | `%APPDATA%\stagewise\stagewise\auth-session.json` | 加密的会话数据 |
| Local State | `%APPDATA%\stagewise\session\Local State` | Chromium 加密主密钥 |
| identity.json | `%APPDATA%\stagewise\stagewise\identity.json` | Machine ID |

#### Local State 文件结构

```json
{
  "os_crypt": {
    "encrypted_key": "RFBBUEkBAAAA...（Base64 编码的 DPAPI 加密密钥）"
  }
}
```

解密步骤：
1. Base64 解码 `encrypted_key`
2. 验证前 5 字节为 `DPAPI`（ASCII）
3. 剥离前缀，剩余部分为 DPAPI 加密的 master key
4. 调用 `CryptUnprotectData` 获得明文 32 字节 AES-256 密钥

---

### 4.3 webui.py — Flask Web 管理界面

**文件**：`webui.py` (509 行)  
**用途**：集成所有功能的 Web 管理界面

#### 命令行用法

```bash
python webui.py                    # 启动 WebUI (默认 :8080)
python webui.py --port 9090        # 自定义端口
python webui.py --host 127.0.0.1   # 自定义绑定地址
```

#### 架构

```
webui.py
    │
    ├─ Flask Web 服务器 (:8080)
    │   ├─ 页面路由: GET /
    │   └─ API 路由: /api/*
    │
    └─ 内嵌代理服务器 (threading)
        ├─ 启动: POST /api/proxy/start
        └─ 停止: POST /api/proxy/stop
```

#### WebUI 与 proxy.py 的区别

| 特性 | proxy.py | webui.py 内嵌代理 |
|------|----------|-------------------|
| 运行方式 | 独立进程 | Flask 内的守护线程 |
| 模型列表 | 内置 12 个模型 | 无（直接转发） |
| 日志输出 | 终端实时打印 | 静默（log_message 被重写） |
| Token 刷新 | 后台线程每 300 秒 | 无（依赖手动刷新） |
| 配置管理 | 自己读写 config.json | 自己读写 config.json |
| 适用场景 | 生产使用 | 调试/管理 |

#### 依赖项

| 库 | 用途 | 必须 |
|----|------|------|
| flask | Web 服务器 | 是 |
| cryptography | AES-256-GCM 解密 | 是 |
| pywin32 | DPAPI 解密 (win32crypt) | 否 (有 PowerShell 回退) |

---

### 4.4 debug_api.py — API 调试工具

**文件**：`debug_api.py` (145 行)  
**用途**：命令行批量测试所有 stagewise API 端点

#### 命令行用法

```bash
python debug_api.py                    # 测试所有端点（不含 LLM）
python debug_api.py --llm              # 测试所有端点 + LLM
python debug_api.py --llm-only         # 仅测试 LLM
python debug_api.py --model gpt-5      # 指定 LLM 模型
```

#### 测试端点列表

| 端点 | 名称 | 默认测试 |
|------|------|---------|
| `GET /v1/auth/get-session` | Session | ✓ |
| `GET /v1/billing/plan` | Subscription | ✓ |
| `GET /v1/usage/current` | Usage Current | ✓ |
| `GET /v1/usage/history?days=7` | Usage History | ✓ |
| `GET /v1/auth/user` | User Info | ✓ |
| `GET /v1/ai/models` | AI Models | ✓ |
| `GET /v1/credits` | Credits | ✓ |
| `POST /v1/ai/chat/completions` | LLM Test | 需 `--llm` 参数 |

#### 输出格式

每个端点显示：
- HTTP 状态码
- `set-auth-token` 头（如果存在）
- 响应体前 500 字节

---

### 4.5 debug_otp.py — OTP 登录调试工具

**文件**：`debug_otp.py` (132 行)  
**用途**：分步调试 OTP 登录流程

#### 命令行用法

```bash
python debug_otp.py user@example.com              # 发送 OTP
python debug_otp.py user@example.com 123456       # 验证 OTP
```

#### 调试输出

包含完整的 HTTP 响应头和响应体，用于排查认证问题。

关键观察点：
- `set-auth-token` 响应头是否存在
- 响应体中 `token` 字段的位置（顶层 / `data.token` / `session.token`）

---

### 4.6 machine_id.py — Machine ID 管理工具

**文件**：`machine_id.py` (90 行)  
**用途**：管理 stagewise 的设备标识

#### 命令行用法

```bash
python machine_id.py                       # 显示当前 Machine ID
python machine_id.py --reset               # 生成新的随机 UUID
python machine_id.py --set <uuid>          # 设置指定的 Machine ID
```

#### Machine ID 分析

stagewise 的 Machine ID 是一个随机 UUID v4，存储在 `identity.json` 中：

```json
{
  "machineId": "17e20dc7-51c0-4b78-a285-dc7ff50f1cca"
}
```

**重要发现**：此 ID 并非基于硬件指纹生成，而是纯随机值。首次启动时生成，之后固定不变。重置后服务端将视为新设备。

**可能的用途**：
- 绕过设备限制（如果存在）
- 模拟不同设备身份
- 重置用量配额（如果与 Machine ID 绑定）

---

## 5. WebUI 前端文档

**文件**：`templates/index.html` (521 行)  
**技术栈**：纯 HTML + CSS + JavaScript（无框架依赖）

### 5.1 界面布局

```
┌─────────────────────────────────────────┐
│  stagewise WebUI    [v1.10.2]  状态指示  │ ← .header
├─────────────────────────────────────────┤
│ Dashboard │ Login │ Decrypt │ API │ ... │ ← .tabs
├─────────────────────────────────────────┤
│                                         │
│           面板内容区 (.main)              │
│                                         │
└─────────────────────────────────────────┘
```

### 5.2 六个功能面板

| 面板 | ID | 功能 |
|------|-----|------|
| Dashboard | `panel-dashboard` | 会话状态、用量进度条、订阅信息、代理状态 |
| Login | `panel-login` | 邮箱输入 → 发送 OTP → 输入验证码 → 登录 |
| Decrypt | `panel-decrypt` | 解密查看 / 解密并保存 token |
| API Explorer | `panel-api` | 自定义请求 + 快捷按钮 + LLM 测试 |
| Proxy | `panel-proxy` | 启动/停止代理 + 端口配置 + 使用说明 |
| Machine ID | `panel-machineid` | 查看/设置/重置 Machine ID |

### 5.3 前端 API 调用

所有 API 调用通过 `api(path, opts)` 统一处理：

```javascript
async function api(path, opts={}) {
    const r = await fetch(path, {
        headers: opts.body ? {'Content-Type':'application/json'} : {},
        method: opts.method || (opts.body ? 'POST' : 'GET'),
        body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    return await r.json();
}
```

**选项参数：**

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `body` | object | undefined | 请求体（自动 JSON 序列化） |
| `method` | string | 自动 | HTTP 方法（有 body 时 POST，否则 GET） |
| `silent` | boolean | false | 失败时不显示 toast 提示 |
| `throwErr` | boolean | true | 失败时显示错误 toast |

### 5.4 CSS 设计系统

**颜色变量：**

```css
--bg:       #0f1117    /* 页面背景 */
--bg2:      #161822    /* 卡片/头部背景 */
--bg3:      #1e2030    /* 输入框/代码块背景 */
--border:   #2a2d3e    /* 边框颜色 */
--text:     #c8cad8    /* 主文字 */
--text2:    #8b8fa3    /* 次要文字 */
--accent:   #7c5cfc    /* 主强调色（紫） */
--accent2:  #6246db    /* 强调色 hover */
--green:    #4ade80    /* 成功/运行中 */
--red:      #f87171    /* 错误/停止 */
--yellow:   #fbbf24    /* 警告/Free 计划 */
--blue:     #60a5fa    /* 信息/Token */
```

**响应式设计**：640px 以下网格布局自动切换为单列。

---

## 6. API 接口参考

### WebUI 后端 API

所有接口基础路径：`http://localhost:8080/api/`

#### 6.1 状态与信息

**GET /api/status**

获取综合状态（含会话验证、token 预览、代理状态）。

响应示例：
```json
{
  "hasToken": true,
  "tokenPreview": "C0ptGjNIKP6XDUUgdW2B...",
  "user": {"email": "user@example.com", "id": "uuid"},
  "machineId": "17e20dc7-51c0-4b78-a285-dc7ff50f1cca",
  "proxyRunning": false,
  "proxyPort": 11434,
  "sessionValid": true,
  "sessionUser": "user@example.com",
  "sessionExpiresAt": "2026-07-01T02:58:49.246Z",
  "tokenRotated": false
}
```

> **注意**：此接口会主动调用 `GET /v1/auth/get-session` 验证 token，如果服务端返回 `set-auth-token`，会自动更新 `config.json`。

**GET /api/usage**

当前用量信息。

响应示例：
```json
{
  "plan": "free",
  "prepaidBalance": 0,
  "windows": [
    {"type": "daily", "usedPercent": 2.85, "exceeded": false, "resetsAt": "..."},
    {"type": "weekly", "usedPercent": 1.14, "exceeded": false, "resetsAt": "..."},
    {"type": "monthly", "usedPercent": 0.71, "exceeded": false, "resetsAt": "..."}
  ]
}
```

**GET /api/subscription**

订阅计划信息。

**GET /api/usage-history?days=7**

历史用量（默认 7 天）。

#### 6.2 认证

**POST /api/send-otp**

发送 OTP 验证码。

请求体：
```json
{"email": "user@example.com"}
```

**POST /api/verify-otp**

验证 OTP 并登录。

请求体：
```json
{"email": "user@example.com", "otp": "123456"}
```

成功响应：
```json
{
  "success": true,
  "token": "C0ptGjNIKP6XDUUgdW2B...",
  "user": {"email": "user@example.com", "id": "uuid", "name": "..."}
}
```

**POST /api/logout**

注销并清除本地 token。

#### 6.3 解密

**POST /api/decrypt-session**

解密 auth-session.json 并返回完整会话数据。

响应示例：
```json
{
  "success": true,
  "data": {
    "session": {"token": "...", "expiresAt": "..."},
    "user": {"email": "...", "id": "..."}
  }
}
```

**POST /api/decrypt-and-save**

解密 auth-session.json 并将 token 保存到 `config.json`。

#### 6.4 API 探索

**POST /api/test-endpoint**

代理任意 API 请求。

请求体：
```json
{
  "method": "GET",
  "path": "/v1/auth/get-session",
  "body": null
}
```

**POST /api/llm-test**

测试 LLM 端点。

请求体：
```json
{
  "model": "anthropic/claude-fable-5",
  "prompt": "Say hello in exactly 3 words."
}
```

#### 6.5 代理控制

**POST /api/proxy/start**

启动内嵌代理服务器。

请求体：
```json
{"port": 11434}
```

**POST /api/proxy/stop**

停止代理服务器。

**GET /api/proxy/status**

代理状态查询。

#### 6.6 Machine ID

**GET /api/machine-id**

获取当前 Machine ID。

**POST /api/machine-id/reset**

生成新的随机 Machine ID。

**POST /api/machine-id/set**

设置指定的 Machine ID。

请求体：
```json
{"machineId": "new-uuid-here"}
```

---

## 7. 加密与安全机制

### 7.1 Electron safeStorage (Chromium os_crypt)

stagewise 使用 Electron 的 `safeStorage` API 加密敏感数据。在 Windows 上，这底层使用 Chromium 的 `os_crypt` 模块：

```
                    ┌─────────────────────┐
                    │   Local State        │
                    │   os_crypt           │
                    │   .encrypted_key     │
                    └─────────┬───────────┘
                              │
                    Base64 Decode
                              │
                    ┌─────────▼───────────┐
                    │  "DPAPI" + 加密数据   │
                    │  (5 bytes prefix)    │
                    └─────────┬───────────┘
                              │
                    Strip "DPAPI" prefix
                              │
                    ┌─────────▼───────────┐
                    │  DPAPI 加密的        │
                    │  Master Key          │
                    └─────────┬───────────┘
                              │
                 CryptUnprotectData
                 (CurrentUser scope)
                              │
                    ┌─────────▼───────────┐
                    │  32-byte AES-256     │
                    │  Master Key (明文)    │
                    └─────────┬───────────┘
                              │
            ┌─────────────────┼─────────────────┐
            │                 │                   │
    ┌───────▼──────┐  ┌──────▼───────┐  ┌───────▼──────┐
    │ auth-session │  │ 其他加密值    │  │ cookies 等   │
    │ .json        │  │              │  │              │
    └───────┬──────┘  └──────────────┘  └──────────────┘
            │
    "v10" + Nonce(12) + Ciphertext+Tag(16)
            │
    AES-256-GCM Decrypt
    (master_key, nonce, ciphertext_and_tag, no_AAD)
            │
    ┌───────▼──────┐
    │ JSON 明文     │
    │ session data  │
    └──────────────┘
```

### 7.2 DPAPI (Data Protection API)

Windows DPAPI 是操作系统级别的数据保护机制：

- **作用域**：`CurrentUser` — 只有同一 Windows 用户可以解密
- **加密层次**：DPAPI 使用用户的 Windows 登录凭据派生加密密钥
- **安全性**：即使管理员也无法解密其他用户的 DPAPI 数据（除非知道密码）

### 7.3 AES-256-GCM 参数

| 参数 | 值 |
|------|-----|
| 算法 | AES-256-GCM |
| 密钥长度 | 256 bits (32 bytes) |
| Nonce 长度 | 96 bits (12 bytes) |
| Tag 长度 | 128 bits (16 bytes) |
| AAD | None |
| 前缀 | `v10` (3 bytes) |

---

## 8. 已知限制与问题

### 8.1 LLM 端点 401 问题

**现象**：`POST /v1/ai/chat/completions` 始终返回 401，无论使用何种认证格式。

**根因**：Free 计划用户无法使用 AI 代理端点。服务端返回误导性错误消息 "Missing or invalid session"，实际是权限不足。

**已尝试的方法**（均失败）：

- Bearer Token 直接传递
- 添加 `X-Stagewise-Client: electron/1.10.2` 头
- 添加 `X-Visitor-ID` 头
- 模拟完整的 Electron 请求头
- 不同的模型名称格式

**结论**：这不是代码 Bug，而是服务端的付费墙。Pro/Enterprise 计划应该可以正常使用。

### 8.2 WebUI 内嵌代理的限制

| 限制 | 说明 |
|------|------|
| 无 Token 自动刷新 | 依赖 Dashboard 页面访问触发 session 验证 |
| 无模型列表 | `/v1/models` 请求会被转发到上游（proxy.py 有内置列表） |
| 无请求日志 | `log_message` 被静默 |
| 单例运行 | 同一进程只能运行一个代理实例 |

### 8.3 DPAPI 解密的限制

- **Windows 限定**：DPAPI 是 Windows 专有 API，macOS/Linux 需要不同的解密方法
- **用户绑定**：只能由同一 Windows 用户解密
- **pywin32 可选**：未安装时回退到 PowerShell，性能较慢（约 1-2 秒 vs 毫秒级）

---

## 9. 安装与运行

### 9.1 环境要求

- Python 3.8+
- Windows 10/11（DPAPI 必需）
- stagewise v1.10.2 已安装并至少登录过一次（用于解密功能）

### 9.2 安装依赖

```bash
pip install flask cryptography pywin32
```

> `pywin32` 可选，未安装时自动回退到 PowerShell 解密。

### 9.3 快速开始

**方式一：WebUI（推荐）**

```bash
python webui.py
# 浏览器打开 http://localhost:8080
```

**方式二：独立代理**

```bash
# 首次使用：登录
python proxy.py --login

# 启动代理
python proxy.py

# 配置 OpenAI 客户端
# base_url = http://localhost:11434/v1
# api_key = any (代理会替换为真实 token)
```

**方式三：从现有会话提取 token**

```bash
python decrypt_session.py --save
# token 自动保存到 ~/.stagewise-proxy/config.json
# 之后可以直接启动代理
python proxy.py
```

### 9.4 典型工作流

```
1. 启动 WebUI
   python webui.py

2. 打开浏览器 → http://localhost:8080

3. 两种获取 token 的方式（二选一）：
   a) Login 面板 → 输入邮箱 → 发送 OTP → 输入验证码
   b) Decrypt 面板 → 点击 "Decrypt & Save Token"
      （需要 stagewise 应用已登录）

4. Dashboard 面板 → 查看会话状态和用量

5. Proxy 面板 → 启动代理 → 配置 LLM 客户端

6. API Explorer → 测试各端点
```

---

## 10. 文件结构

```
stagewise-py/
├── webui.py              # Flask Web 管理界面 (509 行)
├── proxy.py              # 独立反向代理服务器 (427 行)
├── decrypt_session.py    # DPAPI 会话解密工具 (175 行)
├── debug_api.py          # API 端点调试工具 (145 行)
├── debug_otp.py          # OTP 登录调试工具 (132 行)
├── machine_id.py         # Machine ID 管理工具 (90 行)
├── templates/
│   └── index.html        # WebUI 前端页面 (521 行)
└── TECHNICAL_DOC.md      # 本文档
```

**外部依赖文件：**

```
%APPDATA%\stagewise\stagewise\
├── auth-session.json     # 加密的会话数据
└── identity.json         # Machine ID

%APPDATA%\stagewise\session\
└── Local State           # Chromium 加密主密钥

~/.stagewise-proxy\
└── config.json           # 代理配置 (token 缓存)
```

---

*文档生成时间：2026-06-24*
