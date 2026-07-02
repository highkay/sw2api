# sw2api

stagewise 多账号反向代理 + Web 管理面板。OpenAI 兼容接口，支持多账号自动切号、限额冷却。

同时提供 Anthropic Messages 兼容端点，方便 Claude Code、Anthropic SDK 或其他兼容客户端接入。

支持**手动输入 Token** 添加账号，也支持**批量导入**（`email|token` 格式）。

## 功能

- **反向代理** — 将 OpenAI 格式请求转发到 `api.stagewise.io/v1/ai/chat/completions`
- **Anthropic 兼容** — 支持 `/messages`、`/models`，自动转换 Anthropic Messages 与 OpenAI Chat Completions 格式
- **多账号管理** — 存储多个 stagewise 账号，自动轮换
- **手动添加账号** — 输入邮箱 + Token 即可添加，无需 OTP 登录
- **批量导入** — 粘贴多行 `email|token` 一键导入
- **两种调度策略**：
  - `specific` — 指定单独账号（默认），支持 `X-Account-Email` 请求头指定
  - `fill_first` — 填充：按邮箱排序连续使用第一个可用账号，不可用自动切下一个
- **限额自动切号** — 上游 403（限额）时禁用账号 + 返回 429+Retry-After，下游 retry 时自动用下一个可用账号
- **冷却系统** — 429 冷却 60s、5xx 冷却 30s（内存自动恢复）；401/402/403 永久 disabled
- **自动解禁** — 刷新 usage 时若所有窗口 `usedPercent < 100` 则自动解禁账号
- **Dashboard** — 实时查看账号状态（Available / Cooling / Disabled）、调用日志
- **3×2 分页账号视图** — 固定 3 列 2 行分页，Next/Prev 只切换本地页面，不刷新额度
- **Unblock 全量解禁** — 带进度条刷新所有账号 usage，自动解禁 disabled 但已有额度的账号

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 启动 Web 面板（含反代）
python webui.py
# 访问 http://localhost:8080 在 Accounts 页添加账号

# 或命令行启动反代
python proxy.py --port 11434 --strategy fill_first
```

## API 端点

### OpenAI 兼容

```text
Base URL: http://localhost:11434/v1
Models:   GET  /v1/models
Chat:     POST /v1/chat/completions
```

### Anthropic 兼容

```text
Base URL: http://localhost:11434
Models:   GET  /models
Messages: POST /messages
```

Anthropic 端点不需要 `/v1` 前缀。请求会被转换为上游 OpenAI Chat Completions 格式，响应会再转换回 Anthropic Messages 格式。

支持内容：

- 非流式与流式 `messages`
- `text`、`thinking`、`tool_use`、`tool_result` 基础转换
- `tool_choice`：`auto`、`any`、`none`、指定 `tool`
- OpenAI SSE 到 Anthropic SSE 的事件转换，包含递增的 `content_block.index`

示例：

```bash
curl http://localhost:11434/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "z-ai/glm-5.2",
    "max_tokens": 128,
    "messages": [
      {"role": "user", "content": "Reply with exactly: pong"}
    ]
  }'
```

流式请求：

```bash
curl -N http://localhost:11434/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "z-ai/glm-5.2",
    "max_tokens": 128,
    "stream": true,
    "messages": [
      {"role": "user", "content": "Reply with exactly one word: pong"}
    ]
  }'
```

Tool use 示例：

```bash
curl http://localhost:11434/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "z-ai/glm-5.2",
    "max_tokens": 160,
    "tools": [{
      "name": "get_weather",
      "description": "Get weather for a city",
      "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"]
      }
    }],
    "tool_choice": {"type": "tool", "name": "get_weather"},
    "messages": [
      {"role": "user", "content": "Use the tool to get weather for Paris."}
    ]
  }'
```

## 添加账号

### 手动添加（WebUI）

在 WebUI 的 **Accounts** 页，填写 Email 和 Token 后点击 "Add Account"。

### 批量导入（WebUI）

在 Accounts 页展开 "Batch Import"，粘贴以下格式内容：

```
email1@example.com|token1_string_here
email2@example.com|token2_string_here
email3@example.com|token3_string_here
```

### API 添加

```bash
# 单个添加
curl -X POST http://localhost:8080/api/accounts/add \
  -H "Content-Type: application/json" \
  -d '{"email": "user@example.com", "token": "<your-token>"}'

# 批量导入（JSON 格式）
curl -X POST http://localhost:8080/api/accounts/add-batch \
  -H "Content-Type: application/json" \
  -d '{"accounts": [{"email": "user1@example.com", "token": "<token1>"}, {"email": "user2@example.com", "token": "<token2>"}]}'

# 批量导入（纯文本格式）
curl -X POST http://localhost:8080/api/accounts/add-batch \
  -H "Content-Type: application/json" \
  -d '{"batch_text": "user1@example.com|token1\nuser2@example.com|token2"}'
```

## Web 界面

```
http://localhost:8080/             Dashboard
http://localhost:8080/accounts     多账号管理 + 添加/批量导入
http://localhost:8080/chat         LLM 聊天
http://localhost:8080/api-explorer  API 调试
http://localhost:8080/proxy        反代控制 + 策略切换
```

### Accounts 操作

- `Next` / `Prev`：只切换本地分页，不请求 usage，不刷新额度。
- `Refresh`：刷新当前页 6 个账号 usage，并自动解禁当前页中额度恢复的 disabled 账号。
- `Unblock`：按每页 6 个账号逐页刷新所有账号 usage，显示进度条，并自动解禁所有额度恢复的 disabled 账号。
- `Update`：按 token 过期时间顺序刷新所有账号 token，显示进度条。

## 策略说明

| 策略 | 行为 | 适用场景 |
|---|---|---|
| `specific` | 始终使用 `activeAccount`，支持 `X-Account-Email` 请求头指定 | 单一账号直连 |
| `fill_first` | 按邮箱排序，连续使用第一个可用账号，不可用自动切下一个 | 最大化利用滚动窗口配额 |

## 限额与切号流程

```
请求 → 账号A → 上游 403 (PLAN_LIMIT_EXCEEDED)
         ↓
反代: disabled[A] = True + 返回 429 + Retry-After
         ↓
下游 CLI/SDK 收到 429 → 自动 retry
         ↓
retry 选号: A 已 disabled 被跳过 → 账号B → 成功
```

- 上游 429 → 账号冷却 60s（内存，自动恢复）+ 透传 429+Retry-After
- 上游 5xx → 账号冷却 30s（内存，自动恢复）+ 改写 429+Retry-After
- 上游 401/402/403 → 账号永久 disabled，下次刷新 usage 且限额窗口 < 100% 时自动解禁
- 所有账号都不可用 → 返回 429 + Retry-After: <最近可用账号恢复秒数>

## 验证

本项目当前已验证：

- `python -m py_compile webui.py proxy.py call_log.py`
- `GET /models` 返回 Anthropic 风格模型列表
- `POST /messages` 使用 `z-ai/glm-5.2` 非流式返回 `message` JSON
- `POST /messages` 使用 `z-ai/glm-5.2` 流式返回完整 SSE 事件序列
- `tool_choice` 指定工具时返回 `tool_use`，流式多 content block 的 index 为 `0,1,...`
- OpenAI 与 Anthropic 流式调用均能写入调用日志，`input_tokens` / `output_tokens` 正常显示
- Accounts `Unblock` 对 69 个账号逐页刷新验证通过，进度条和自动解禁逻辑正常

注意：代理当前会默认启用 reasoning 并注入内置 system prompt，因此部分模型会优先返回 `thinking` block。这是代理行为，不是 Anthropic 端点格式错误。

## 项目结构

```
proxy.py           — 反向代理服务器
webui.py           — Flask Web 面板
call_log.py        — 调用日志
templates/
  index.html       — 前端界面
data/              — 运行时配置（config.json，已 gitignore）
```

---

社区：[Linux.do](https://linux.do/)
