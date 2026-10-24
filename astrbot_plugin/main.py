"""AstrBot bridge plugin for one or more MCP servers.

Features
- stdio / SSE / Streamable HTTP connections
- automatic registration of every MCP tool as an AstrBot LLM tool
- generic fallback tools: mcp_list_servers / mcp_list_tools / mcp_call_tool
- `/mcp` management commands
- optional knowledge-base auto-learning on every LLM response
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .client import (
    MCPServerManager,
    make_tool_name,
    normalize_input_schema,
    result_to_text,
)

try:  # AstrBot >= 4.5.1 dynamic tool API
    from astrbot.core.agent.tool import FunctionTool
except ImportError:  # pragma: no cover
    FunctionTool = None  # type: ignore[assignment]

if FunctionTool is not None:
    from pydantic.dataclasses import dataclass as pydantic_dataclass

    @pydantic_dataclass
    class MCPBridgeTool(FunctionTool):  # type: ignore[misc,valid-type]
        """FunctionTool that forwards calls to an MCP server."""

        server: str = ""
        tool_name: str = ""
        manager: Any = None

        async def call(self, context, **kwargs):
            timeout = getattr(context, "tool_call_timeout", None)
            return await self.manager.call_tool(
                self.server, self.tool_name, kwargs, timeout=timeout
            )

else:  # pragma: no cover
    MCPBridgeTool = None  # type: ignore[assignment]


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if value is None:
        return default
    return bool(value)


def _as_int(value: Any, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


class MCPBridgePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.plugin_config: dict[str, Any] = dict(config or {})
        self._manager = MCPServerManager(self.plugin_config, logger=self.logger)
        self._dynamic_tool_names: list[str] = []
        self._init_task: asyncio.Task[None] | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._auto_learn_semaphore = asyncio.Semaphore(1)
        self._learned_hashes: set[str] = set()

        try:
            loop = asyncio.get_running_loop()
            self._init_task = loop.create_task(self._async_init())
        except RuntimeError:
            self.logger.warning(
                "No running event loop; MCP servers will connect on first tool call"
            )

    # ------------------------------------------------------------------ setup
    async def _async_init(self) -> None:
        try:
            results = await self._manager.connect_all()
            failed = {name: error for name, error in results.items() if error}
            if _as_bool(self.plugin_config.get("auto_register"), True):
                self._register_dynamic_tools()
            connected = sum(1 for item in self._manager.status() if item["connected"])
            self.logger.info(
                "MCP bridge initialized: %s/%s server(s) connected, %s dynamic tool(s)",
                connected,
                len(results),
                len(self._dynamic_tool_names),
            )
            if failed:
                self.logger.warning("Failed MCP servers: %s", failed)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("MCP bridge initialization failed")

    def _register_dynamic_tools(self) -> None:
        if MCPBridgeTool is None:
            self.logger.warning(
                "FunctionTool API unavailable; only generic MCP tools are registered"
            )
            return

        self._remove_dynamic_tools()

        prefix = str(self.plugin_config.get("tool_prefix") or "mcp")
        max_length = _as_int(self.plugin_config.get("tool_name_max_length"), 64, 8)
        max_per_server = _as_int(self.plugin_config.get("max_tools_per_server"), 200, 1)
        denylist = {
            part.strip()
            for part in re.split(
                r"[,;]", str(self.plugin_config.get("tool_denylist") or "")
            )
            if part.strip()
        }

        used_names: set[str] = set()
        per_server: dict[str, int] = {}
        tools: list[Any] = []

        for server, mcp_tool in self._manager.iter_tools():
            if per_server.get(server, 0) >= max_per_server:
                continue
            original_name = str(mcp_tool.name)
            if original_name in denylist:
                continue

            llm_name = make_tool_name(prefix, server, original_name, max_length)
            if llm_name in denylist:
                continue
            if llm_name in used_names:
                suffix = 2
                while f"{llm_name}_{suffix}" in used_names:
                    suffix += 1
                llm_name = f"{llm_name}_{suffix}"[:max_length]
            used_names.add(llm_name)

            description = (
                str(getattr(mcp_tool, "description", "") or "").strip()
                or f"MCP tool {original_name}"
            )
            try:
                tools.append(
                    MCPBridgeTool(
                        name=llm_name,
                        description=f"[MCP:{server}] {description}",
                        parameters=normalize_input_schema(mcp_tool),
                        server=server,
                        tool_name=original_name,
                        manager=self._manager,
                    )
                )
            except Exception:
                self.logger.exception(
                    "Failed to create AstrBot tool for %s/%s", server, original_name
                )
                continue

            per_server[server] = per_server.get(server, 0) + 1

        if not tools:
            self.logger.info("No MCP tools to register")
            return

        try:
            self.context.add_llm_tools(*tools)
        except Exception:
            self.logger.exception("Failed to register dynamic MCP tools")
            return

        self._dynamic_tool_names = [tool.name for tool in tools]
        self.logger.info(
            "Registered %s MCP tool(s): %s",
            len(tools),
            ", ".join(self._dynamic_tool_names),
        )

    def _remove_dynamic_tools(self) -> None:
        names = set(self._dynamic_tool_names)
        if not names:
            return
        try:
            tool_manager = self.context.get_llm_tool_manager()
        except Exception:
            self._dynamic_tool_names = []
            return

        remove = getattr(tool_manager, "remove_func", None)
        if callable(remove):
            for name in names:
                try:
                    remove(name)
                except Exception:
                    pass
        elif hasattr(tool_manager, "func_list"):
            tool_manager.func_list = [
                tool
                for tool in tool_manager.func_list
                if getattr(tool, "name", "") not in names
            ]
        self._dynamic_tool_names = []

    # ---------------------------------------------------------- generic tools
    @filter.llm_tool(name="mcp_list_servers")
    async def mcp_list_servers(self, event: AstrMessageEvent):
        """列出已配置的 MCP 服务器、连接状态和工具数量。"""

        lines = ["MCP 服务器："]
        status = self._manager.status()
        if not status:
            lines.append("（未配置服务器）")
        for item in status:
            icon = "✅" if item["connected"] else "❌"
            lines.append(
                f"{icon} {item['name']} [{item['transport']}] "
                f"tools={item['tools']} {item['endpoint']}"
            )
            if item["error"]:
                lines.append(f"   error: {item['error']}")
        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="mcp_list_tools")
    async def mcp_list_tools(self, event: AstrMessageEvent, server: str):
        """列出某个 MCP 服务器提供的工具。

        Args:
            server(string): 服务器名称，可先调用 mcp_list_servers 获取
        """
        connection = self._manager.get(server)
        if connection is None:
            yield event.plain_result(
                f"未找到服务器 {server!r}。可用：{', '.join(self._manager.names) or '无'}"
            )
            return
        if not connection.connected:
            yield event.plain_result(
                f"服务器 {server!r} 未连接：{connection.error or 'unknown error'}"
            )
            return

        lines = [f"{server} 的工具（{len(connection.tools)}）："]
        for tool in connection.tools:
            summary = (getattr(tool, "description", "") or "").strip().splitlines()
            lines.append(f"- {tool.name}: {summary[0] if summary else ''}".strip())
        yield event.plain_result("\n".join(lines)[:3800])

    @filter.llm_tool(name="mcp_call_tool")
    async def mcp_call_tool(
        self,
        event: AstrMessageEvent,
        server: str,
        tool: str,
        arguments: str = "{}",
    ):
        """调用指定 MCP 服务器上的工具。

        Args:
            server(string): 服务器名称
            tool(string): 工具名称
            arguments(string): 参数 JSON 对象字符串，例如 {"query": "AstrBot"}；无参数时留空
        """
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError as exc:
            yield event.plain_result(f"参数不是合法 JSON：{exc}")
            return
        if not isinstance(parsed, dict):
            yield event.plain_result("arguments 必须是 JSON 对象")
            return

        try:
            result = await self._manager.call_tool(server, tool, parsed)
            text = result_to_text(result) or "（工具没有返回文本内容）"
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("MCP call %s/%s failed: %s", server, tool, exc)
            text = f"调用失败：{type(exc).__name__}: {exc}"
        yield event.plain_result(text[:3800])

    # -------------------------------------------------------------- commands
    @filter.command_group("mcp")
    def mcp(self):
        """MCP 服务管理指令组"""

    @mcp.command("help")
    async def mcp_cmd_help(self, event: AstrMessageEvent):
        text = (
            "MCP 指令：\n"
            "/mcp list - 查看服务器状态\n"
            "/mcp tools <server> - 查看工具列表\n"
            "/mcp call <server> <tool> <json> - 调用工具\n"
            "/mcp reload - 重连并重新注册工具（管理员）"
        )
        yield event.plain_result(text)

    @mcp.command("list")
    async def mcp_cmd_list(self, event: AstrMessageEvent):
        status = self._manager.status()
        if not status:
            yield event.plain_result("还没有配置 MCP 服务器。")
            return
        lines = ["MCP 服务器："]
        for item in status:
            icon = "✅" if item["connected"] else "❌"
            lines.append(
                f"{icon} {item['name']} [{item['transport']}] {item['tools']} tools"
            )
            if item["error"]:
                lines.append(f"   {item['error']}")
        yield event.plain_result("\n".join(lines))

    @mcp.command("tools")
    async def mcp_cmd_tools(self, event: AstrMessageEvent):
        rest = self._sub_args(event, "tools")
        if not rest:
            yield event.plain_result("用法：/mcp tools <server>")
            return
        connection = self._manager.get(rest)
        if connection is None:
            yield event.plain_result(
                f"未找到服务器 {rest!r}。可用：{', '.join(self._manager.names) or '无'}"
            )
            return
        if not connection.connected:
            yield event.plain_result(
                f"服务器 {rest!r} 未连接：{connection.error or 'unknown error'}"
            )
            return
        lines = [f"{rest} 的工具（{len(connection.tools)}）："]
        for tool in connection.tools:
            summary = (getattr(tool, "description", "") or "").strip().splitlines()
            lines.append(f"- {tool.name}: {summary[0] if summary else ''}".strip())
        yield event.plain_result("\n".join(lines)[:3800])

    @mcp.command("call")
    async def mcp_cmd_call(self, event: AstrMessageEvent):
        rest = self._sub_args(event, "call")
        parts = rest.split(maxsplit=2)
        if len(parts) < 2:
            yield event.plain_result(
                '用法：/mcp call <server> <tool> [json]，例如：/mcp call knowledge kb_search {"query": "AstrBot"}'
            )
            return
        server, tool = parts[0], parts[1]
        raw_arguments = parts[2] if len(parts) > 2 else "{}"
        try:
            arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as exc:
            yield event.plain_result(f"参数不是合法 JSON：{exc}")
            return
        if not isinstance(arguments, dict):
            yield event.plain_result("参数必须是 JSON 对象")
            return

        try:
            result = await self._manager.call_tool(server, tool, arguments)
            text = result_to_text(result) or "（工具没有返回文本内容）"
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("MCP call %s/%s failed: %s", server, tool, exc)
            text = f"调用失败：{type(exc).__name__}: {exc}"
        yield event.plain_result(text[:3800])

    @mcp.command("reload")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def mcp_cmd_reload(self, event: AstrMessageEvent):
        await self._manager.reload()
        self._register_dynamic_tools()
        status = self._manager.status()
        connected = sum(1 for item in status if item["connected"])
        yield event.plain_result(
            f"已重连：{connected}/{len(status)} 个服务器，"
            f"注册了 {len(self._dynamic_tool_names)} 个工具。"
        )

    # ------------------------------------------------------------ auto-learn
    @filter.on_llm_response()
    async def _on_llm_response(self, event: AstrMessageEvent, response) -> None:
        config = self.plugin_config.get("auto_learn") or {}
        if not isinstance(config, dict) or not _as_bool(config.get("enabled")):
            return

        if getattr(response, "is_chunk", False):
            return
        if getattr(response, "tools_call_name", None):
            return

        message = (getattr(event, "message_str", "") or "").strip()
        reply = (getattr(response, "completion_text", "") or "").strip()
        if not message or not reply:
            return
        if message.startswith(("/", "／")):
            return
        min_chars = _as_int(config.get("min_message_chars"), 20, 1)
        if len(message) < min_chars:
            return

        message_type = str(getattr(event, "get_message_type", lambda: "")())
        if "GROUP" in message_type.upper() and not _as_bool(
            config.get("learn_in_group"), False
        ):
            return

        server = str(config.get("knowledge_server") or "astrbot-mcp")
        connection = self._manager.get(server)
        if connection is None or not connection.connected:
            return

        max_chars = _as_int(config.get("max_conversation_chars"), 4000, 200)
        conversation = f"用户：{message}\n助手：{reply}"[:max_chars]
        digest = hashlib.sha256(conversation.encode("utf-8")).hexdigest()
        if digest in self._learned_hashes:
            return
        self._learned_hashes.add(digest)
        if len(self._learned_hashes) > 500:
            self._learned_hashes.clear()

        umo = getattr(event, "unified_msg_origin", "")
        task = asyncio.create_task(self._auto_learn(conversation, umo, config))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _auto_learn(
        self, conversation: str, umo: str, config: dict[str, Any]
    ) -> None:
        async with self._auto_learn_semaphore:
            try:
                provider_id = str(config.get("provider_id") or "").strip()
                if not provider_id:
                    provider_id = await self.context.get_current_chat_provider_id(
                        umo=umo
                    )
                if not provider_id:
                    return

                prompt = str(config.get("prompt") or "").strip()
                full_prompt = f"{prompt}\n\n--- 对话 ---\n{conversation}"
                response = await self.context.llm_generate(
                    chat_provider_id=provider_id, prompt=full_prompt
                )
                facts = (getattr(response, "completion_text", "") or "").strip()
                normalized = facts.strip("。.!！ \t\r\n").upper()
                if not normalized or normalized in {"NONE", "无", "没有"}:
                    return
                if len(facts) > 4000:
                    facts = facts[:4000]

                server = str(config.get("knowledge_server") or "astrbot-mcp")
                tool = str(config.get("tool_name") or "kb_remember")
                tags = str(config.get("tags") or "auto-learned")
                await self._manager.call_tool(
                    server,
                    tool,
                    {"content": facts, "tags": tags, "title": "自动学习"},
                    timeout=60,
                )
                self.logger.info("Auto-learned %s chars into %s", len(facts), server)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Auto-learn failed: %s", exc)

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _sub_args(event: AstrMessageEvent, subcommand: str) -> str:
        text = (getattr(event, "message_str", "") or "").strip()
        text = re.sub(r"^[/／]?\s*mcp\b", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(
            rf"^{re.escape(subcommand)}\b", "", text, flags=re.IGNORECASE
        ).strip()
        return text

    async def terminate(self) -> None:
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._init_task is not None:
            self._init_task.cancel()
            try:
                await self._init_task
            except (asyncio.CancelledError, Exception):
                pass
        await self._manager.close_all()
        self._dynamic_tool_names = []
