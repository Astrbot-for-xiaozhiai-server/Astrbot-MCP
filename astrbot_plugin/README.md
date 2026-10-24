# MCP 服务桥接（AstrBot 插件）

把多个 MCP 服务器接入 AstrBot：

- 支持 `stdio`、`Streamable HTTP`、`SSE`
- 自动把每个 MCP 工具注册为 AstrBot LLM 工具
- 提供 `mcp_list_servers` / `mcp_list_tools` / `mcp_call_tool` 通用工具
- `/mcp` 指令用于查看状态、列出工具、手动调用、重连
- 可选的“知识库自动学习”：每轮回复后提取长期记忆并写入 `kb_remember`

## 安装

1. 把本目录复制到 `AstrBot/data/plugins/astrbot_plugin_mcp_bridge/`（目录名建议与 `metadata.yaml` 中的 `name` 一致）。
2. 重启 AstrBot，或在 WebUI 插件管理中重载。
3. 打开插件配置，填写 `servers_json`。

> 需要 AstrBot >= 4.5.7（动态工具 API）。`template_list` 可视化配置需要 AstrBot >= 4.10.4；旧版本直接使用 `servers_json`。

## 配置示例

```json
{
  "mcpServers": {
    "astrbot-mcp": {
      "transport": "stdio",
      "command": "/path/to/Astrbot-MCP/.venv/bin/python",
      "args": ["-m", "astrbot_mcp", "--config", "/path/to/Astrbot-MCP/config.yaml"],
      "cwd": "/path/to/Astrbot-MCP"
    },
    "remote-example": {
      "transport": "streamable_http",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    },
    "legacy-sse": {
      "transport": "sse",
      "url": "https://example.com/sse"
    }
  }
}
```

也支持数组、直接写“名称 → 配置”的对象，或单个服务器配置对象。每个服务器还支持：

| 字段 | 说明 |
| --- | --- |
| `enabled` | 是否启用，默认 true |
| `connect_timeout` | 连接超时（秒） |
| `tool_timeout` | 工具调用超时（秒） |
| `sse_read_timeout` | SSE / HTTP 流读取超时（秒） |
| `ignore_tools` | 不暴露给 LLM 的工具名，逗号分隔 |
| `args_json` / `env_json` / `headers_json` | 可视化配置中的 JSON 字符串字段 |

## 指令

```text
/mcp list
/mcp tools <server>
/mcp call <server> <tool> {"key": "value"}
/mcp reload
/mcp help
```

## 自动学习

在插件配置中开启：

```json
{
  "auto_learn": {
    "enabled": true,
    "knowledge_server": "astrbot-mcp",
    "tool_name": "kb_remember",
    "tags": "auto-learned",
    "learn_in_group": false
  }
}
```

`knowledge_server` 必须是上面配置里某个已连接、且提供 `kb_remember` 工具的服务器名。  
默认只在私聊触发，避免把群聊内容写入长期记忆；每轮会额外调用一次 LLM。

## 常见问题

- **stdio 命令被 AstrBot 拒绝**：使用 `python` / `uv` / `uvx` / `npx` 等白名单命令。推荐 `<venv>/bin/python -m astrbot_mcp`。
- **工具太多导致模型调用变慢**：设置 `max_tools_per_server`、`tool_denylist` 或服务器的 `ignore_tools`，也可以关闭 `auto_register` 只用 `mcp_call_tool`。
- **Docker 内连接失败**：容器内不要用 `127.0.0.1`，改用服务名或宿主机地址。
