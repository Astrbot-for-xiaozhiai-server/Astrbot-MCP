"""MCP client manager used by the AstrBot bridge plugin.

Each connection is owned by a dedicated asyncio task so that anyio cancel
scopes are entered and exited in the same task (required by the MCP SDK).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

try:
    import mcp
    from mcp import ClientSession
except ImportError:  # pragma: no cover - AstrBot ships mcp
    mcp = None  # type: ignore[assignment]
    ClientSession = None  # type: ignore[assignment]


@dataclass
class ServerSpec:
    name: str
    transport: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    connect_timeout: float = 30.0
    tool_timeout: float = 120.0
    sse_read_timeout: float = 300.0
    ignore_tools: set[str] = field(default_factory=set)

    @property
    def endpoint(self) -> str:
        return (
            self.command + " " + " ".join(self.args)
            if self.transport == "stdio"
            else self.url
        )


def _expand(value: Any) -> Any:
    return os.path.expandvars(value) if isinstance(value, str) else value


def _json_value(raw: Any, expected: type, field_name: str) -> Any:
    if raw is None or raw == "":
        return expected()
    if isinstance(raw, expected):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name} is not valid JSON: {exc}") from exc
        if isinstance(parsed, expected):
            return parsed
    raise ValueError(f"{field_name} must be a JSON {expected.__name__}")


def _parse_args(raw: Any) -> list[str]:
    if raw in (None, ""):
        return []
    if isinstance(raw, list):
        return [str(_expand(item)) for item in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(_expand(item)) for item in parsed]
        return shlex.split(raw)
    raise ValueError("args must be a JSON array or a shell-like string")


def _parse_env(raw: Any) -> dict[str, str]:
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        return {str(key): str(_expand(value)) for key, value in raw.items()}
    if isinstance(raw, list):
        result: dict[str, str] = {}
        for item in raw:
            if isinstance(item, str) and "=" in item:
                key, value = item.split("=", 1)
                result[key.strip()] = str(_expand(value.strip()))
        return result
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"env_json is not valid JSON: {exc}") from exc
        if isinstance(parsed, list):
            return _parse_env(parsed)
        if not isinstance(parsed, dict):
            raise ValueError("env_json must be a JSON object or array")
        return {str(key): str(_expand(value)) for key, value in parsed.items()}
    raise ValueError("env must be a JSON object or a list of KEY=VALUE strings")


def _parse_headers(raw: Any) -> dict[str, str]:
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        return {str(key): str(_expand(value)) for key, value in raw.items()}
    if isinstance(raw, str):
        parsed = _json_value(raw, dict, "headers_json")
        return {str(key): str(_expand(value)) for key, value in parsed.items()}
    raise ValueError("headers must be a JSON object")


def _parse_ignore(raw: Any) -> set[str]:
    if not raw:
        return set()
    if isinstance(raw, list):
        return {str(item).strip() for item in raw if str(item).strip()}
    return {part.strip() for part in re.split(r"[,;]", str(raw)) if part.strip()}


def _timeout(entry: dict[str, Any], key: str, default: float) -> float:
    try:
        return max(1.0, float(entry.get(key, default)))
    except (TypeError, ValueError):
        return default


def _enabled(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _build_spec(
    name: str,
    entry: dict[str, Any],
    *,
    default_connect_timeout: float,
    default_tool_timeout: float,
) -> ServerSpec:
    raw_transport = str(entry.get("transport") or "").strip().lower()
    transport = raw_transport.replace("-", "_")
    if transport in {"streamable_http", "http", "streamablehttp"}:
        transport = "streamable_http"
    elif transport == "sse":
        transport = "sse"
    elif transport == "stdio":
        transport = "stdio"
    elif entry.get("command"):
        transport = "stdio"
    elif entry.get("url"):
        transport = "streamable_http"
    else:
        raise ValueError(
            f"Server {name!r} has no transport: set command (stdio) or url (http/sse)"
        )

    connect_timeout = _timeout(entry, "connect_timeout", default_connect_timeout)
    tool_timeout = _timeout(entry, "tool_timeout", default_tool_timeout)
    sse_read_timeout = _timeout(entry, "sse_read_timeout", 300.0)
    ignore_tools = _parse_ignore(entry.get("ignore_tools"))

    if transport == "stdio":
        command = str(_expand(entry.get("command") or "")).strip()
        if not command:
            raise ValueError(f"Server {name!r}: command is required for stdio")
        return ServerSpec(
            name=name,
            transport=transport,
            command=command,
            args=_parse_args(entry.get("args_json", entry.get("args"))),
            env=_parse_env(entry.get("env_json", entry.get("env"))),
            cwd=str(_expand(entry.get("cwd") or "")).strip(),
            connect_timeout=connect_timeout,
            tool_timeout=tool_timeout,
            ignore_tools=ignore_tools,
        )

    url = str(_expand(entry.get("url") or "")).strip()
    if not url:
        raise ValueError(f"Server {name!r}: url is required for {transport}")
    return ServerSpec(
        name=name,
        transport=transport,
        url=url,
        headers=_parse_headers(entry.get("headers_json", entry.get("headers"))),
        connect_timeout=connect_timeout,
        tool_timeout=tool_timeout,
        sse_read_timeout=sse_read_timeout,
        ignore_tools=ignore_tools,
    )


def parse_servers(
    config: dict[str, Any],
    *,
    default_connect_timeout: float = 30.0,
    default_tool_timeout: float = 120.0,
    log: logging.Logger | None = None,
) -> list[ServerSpec]:
    """Parse both the visual ``servers`` list and ``servers_json`` text."""
    logger = log or logging.getLogger(__name__)
    entries: list[dict[str, Any]] = []

    raw_servers = config.get("servers")
    if isinstance(raw_servers, list):
        entries.extend(item for item in raw_servers if isinstance(item, dict))
    elif isinstance(raw_servers, dict):
        for name, item in raw_servers.items():
            if isinstance(item, dict):
                entries.append({**item, "name": item.get("name") or name})

    raw_json = config.get("servers_json")
    if raw_json:
        if isinstance(raw_json, str):
            try:
                data = json.loads(raw_json)
            except json.JSONDecodeError as exc:
                logger.warning("servers_json is not valid JSON: %s", exc)
                data = None
        else:
            data = raw_json

        if isinstance(data, dict) and "mcpServers" in data:
            data = data["mcpServers"]
        if isinstance(data, dict) and ("command" in data or "url" in data):
            entries.append({**data, "name": data.get("name") or "mcp"})
        elif isinstance(data, dict):
            for name, item in data.items():
                if isinstance(item, dict):
                    entries.append({**item, "name": item.get("name") or name})
        elif isinstance(data, list):
            entries.extend(item for item in data if isinstance(item, dict))

    top_level = config.get("mcpServers")
    if isinstance(top_level, dict):
        for name, item in top_level.items():
            if isinstance(item, dict):
                entries.append({**item, "name": item.get("name") or name})

    if not entries and ("command" in config or "url" in config):
        entries.append({**config, "name": config.get("name") or "mcp"})

    specs: dict[str, ServerSpec] = {}
    for entry in entries:
        name = str(entry.get("name") or entry.get("server_name") or "").strip()
        if not name:
            continue
        if not _enabled(entry.get("enabled", True)):
            specs.pop(name, None)
            continue
        try:
            specs[name] = _build_spec(
                name,
                entry,
                default_connect_timeout=default_connect_timeout,
                default_tool_timeout=default_tool_timeout,
            )
        except (ValueError, TypeError) as exc:
            logger.warning("Skipping MCP server %s: %s", name, exc)
    return list(specs.values())


class MCPServerConnection:
    """One MCP server session, owned by a dedicated asyncio task."""

    def __init__(self, spec: ServerSpec) -> None:
        self.spec = spec
        self.session: Any = None
        self.tools: list[Any] = []
        self.connected = False
        self.error = ""
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def name(self) -> str:
        return self.spec.name

    async def start(self) -> None:
        if self.connected and self._task and not self._task.done():
            return
        if self._task and not self._task.done():
            await self.stop()

        if mcp is None or ClientSession is None:
            raise RuntimeError(
                "The 'mcp' package is not installed in AstrBot's environment"
            )

        self._ready.clear()
        self._stop.clear()
        self.error = ""
        self._task = asyncio.create_task(self._run(), name=f"mcp-{self.name}")
        try:
            await asyncio.wait_for(
                self._ready.wait(),
                timeout=self.spec.connect_timeout * 2 + 10,
            )
        except asyncio.TimeoutError as exc:
            await self.stop()
            raise RuntimeError(
                f"Timed out connecting to MCP server {self.name!r}"
            ) from exc
        if not self.connected:
            raise RuntimeError(self.error or f"Failed to connect to {self.name!r}")

    async def _run(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                read_stream, write_stream = await self._open_transport(stack)
                session = await stack.enter_async_context(
                    ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=timedelta(seconds=self.spec.tool_timeout),
                    )
                )
                await asyncio.wait_for(
                    session.initialize(), timeout=self.spec.connect_timeout
                )
                result = await asyncio.wait_for(
                    session.list_tools(), timeout=self.spec.connect_timeout
                )
                self.tools = list(result.tools)
                self.session = session
                self.connected = True
                self.error = ""
                self._ready.set()
                await self._stop.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the plugin
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.connected = False
            self.session = None
            self._ready.set()

    async def _open_transport(self, stack: AsyncExitStack):
        spec = self.spec
        if spec.transport == "stdio":
            env = os.environ.copy()
            env.update(spec.env)
            params = mcp.StdioServerParameters(  # type: ignore[union-attr]
                command=spec.command,
                args=spec.args,
                env=env,
                cwd=spec.cwd or None,
            )
            return await stack.enter_async_context(mcp.stdio_client(params))  # type: ignore[union-attr]

        if spec.transport == "sse":
            from mcp.client.sse import sse_client

            return await stack.enter_async_context(
                sse_client(
                    spec.url,
                    headers=spec.headers or None,
                    timeout=spec.connect_timeout,
                    sse_read_timeout=spec.sse_read_timeout,
                )
            )

        try:
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as exc:
            raise RuntimeError(
                "Streamable HTTP transport is unavailable; install mcp>=1.9,<2"
            ) from exc

        streams = await stack.enter_async_context(
            streamablehttp_client(
                spec.url,
                headers=spec.headers or None,
                timeout=spec.connect_timeout,
                sse_read_timeout=spec.sse_read_timeout,
            )
        )
        return streams[0], streams[1]

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            except Exception:
                pass
        self.connected = False
        self.session = None

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        session = self.session
        if session is None:
            detail = self.error or "not connected"
            raise RuntimeError(f"MCP server {self.name!r} is unavailable: {detail}")
        return await asyncio.wait_for(
            session.call_tool(tool_name, arguments),
            timeout=timeout or self.spec.tool_timeout,
        )


class MCPServerManager:
    def __init__(
        self,
        config: dict[str, Any],
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.log = logger or logging.getLogger(__name__)
        self.default_tool_timeout = _timeout(config, "tool_timeout", 120.0)
        specs = parse_servers(
            config,
            default_connect_timeout=_timeout(config, "connect_timeout", 30.0),
            default_tool_timeout=self.default_tool_timeout,
            log=self.log,
        )
        self.connections: dict[str, MCPServerConnection] = {
            spec.name: MCPServerConnection(spec) for spec in specs
        }

    @property
    def names(self) -> list[str]:
        return list(self.connections)

    def get(self, name: str) -> MCPServerConnection | None:
        return self.connections.get(name)

    def has_connected(self) -> bool:
        return any(connection.connected for connection in self.connections.values())

    async def connect_all(self) -> dict[str, str]:
        async def connect_one(connection: MCPServerConnection) -> tuple[str, str]:
            try:
                await connection.start()
                self.log.info(
                    "Connected to MCP server %s (%s tools)",
                    connection.name,
                    len(connection.tools),
                )
                return connection.name, ""
            except Exception as exc:  # noqa: BLE001
                connection.error = str(exc)
                self.log.warning("MCP server %s failed: %s", connection.name, exc)
                return connection.name, str(exc)

        results = await asyncio.gather(
            *(connect_one(connection) for connection in self.connections.values()),
            return_exceptions=False,
        )
        return dict(results)

    async def close_all(self) -> None:
        await asyncio.gather(
            *(connection.stop() for connection in self.connections.values()),
            return_exceptions=True,
        )

    async def reload(self) -> dict[str, str]:
        await self.close_all()
        return await self.connect_all()

    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        connection = self.connections.get(server)
        if connection is None:
            available = ", ".join(self.connections) or "(none)"
            raise ValueError(f"Unknown MCP server {server!r}; configured: {available}")
        return await connection.call_tool(tool, arguments, timeout=timeout)

    def status(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for connection in self.connections.values():
            items.append(
                {
                    "name": connection.name,
                    "transport": connection.spec.transport,
                    "endpoint": connection.spec.endpoint,
                    "connected": connection.connected,
                    "tools": len(connection.tools),
                    "error": connection.error,
                }
            )
        return items

    def iter_tools(self):
        for connection in self.connections.values():
            if not connection.connected:
                continue
            for tool in connection.tools:
                yield connection.name, tool


def make_tool_name(prefix: str, server: str, tool: str, max_length: int = 64) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{prefix}_{server}_{tool}").strip("_")
    if not base:
        base = "mcp_tool"
    if len(base) > max_length:
        digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:6]
        base = base[: max(1, max_length - 7)] + "_" + digest
    return base


def normalize_input_schema(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "inputSchema", None)
    if not isinstance(schema, dict):
        schema = {}
    else:
        schema = dict(schema)
    schema.pop("$schema", None)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    if not isinstance(schema.get("properties"), dict):
        schema["properties"] = {}
    return schema


def result_to_text(result: Any) -> str:
    """Flatten an MCP ``CallToolResult`` into text for AstrBot commands/tools."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result

    content = getattr(result, "content", None)
    if content is None:
        return str(result)

    parts: list[str] = []
    for item in content:
        text = getattr(item, "text", None)
        item_type = getattr(item, "type", None)
        if text:
            parts.append(str(text))
        elif item_type == "image":
            parts.append("[image content]")
        elif item_type == "resource":
            resource = getattr(item, "resource", item)
            parts.append(str(getattr(resource, "text", resource)))
        elif item_type == "audio":
            parts.append("[audio content]")
        else:
            parts.append(str(item))

    text = "\n".join(part for part in parts if part).strip()
    if getattr(result, "isError", False):
        return f"[MCP error] {text or 'unknown error'}"
    return text
