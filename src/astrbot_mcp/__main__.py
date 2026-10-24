"""Command-line entry point: ``python -m astrbot_mcp`` / ``astrbot-mcp``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from . import __version__
from .config import AppConfig, ConfigError
from .server import build_server, run_server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="astrbot-mcp",
        description="Multi-service MCP server for AstrBot.",
    )
    parser.add_argument(
        "-c",
        "--config",
        help="Path to config.yaml (default: ./config.yaml or $ASTRBOT_MCP_CONFIG)",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http", "streamable_http", "http"],
        help="Override server.transport",
    )
    parser.add_argument("--host", help="Override server.host")
    parser.add_argument("--port", type=int, help="Override server.port")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Override server.log_level",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Print the resolved configuration (secrets masked) and exit",
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="Print registered tool names and exit",
    )
    parser.add_argument(
        "--version", action="version", version=f"astrbot-mcp {__version__}"
    )
    return parser


def _apply_overrides(config: AppConfig, args: argparse.Namespace) -> None:
    if args.transport:
        normalized = args.transport.replace("_", "-")
        if normalized == "http":
            normalized = "streamable-http"
        config.server.transport = normalized  # type: ignore[assignment]
    if args.host:
        config.server.host = args.host
    if args.port:
        config.server.port = args.port
    if args.log_level:
        config.server.log_level = args.log_level


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    try:
        config = AppConfig.load(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    _apply_overrides(config, args)

    logging.basicConfig(
        level=getattr(logging, config.server.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if args.check_config:
        print(json.dumps(config.masked_dump(), ensure_ascii=False, indent=2))
        return 0

    if args.list_tools:
        mcp = build_server(config)

        async def list_tools() -> None:
            tools = await mcp.list_tools()
            for tool in tools:
                description = (tool.description or "").strip().splitlines()
                summary = description[0] if description else ""
                print(f"{tool.name}\t{summary}")
            print(f"# {len(tools)} tool(s)", file=sys.stderr)

        asyncio.run(list_tools())
        return 0

    try:
        run_server(config)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
