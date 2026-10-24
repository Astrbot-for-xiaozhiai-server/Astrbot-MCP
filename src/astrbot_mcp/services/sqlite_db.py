"""SQLite tools restricted to a sandbox directory."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP

from ..context import AppContext
from ..utils import json_dumps


def _resolve_db(root: Path, database: str, *, must_exist: bool) -> Path:
    raw = Path(database).expanduser()
    candidate = raw if raw.is_absolute() else root / raw
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(
            f"Database {database!r} escapes the sandbox root {str(root)!r}"
        )
    if must_exist and not resolved.is_file():
        raise ValueError(f"Database does not exist: {database}")
    return resolved


def _connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        uri = f"file:{quote(str(path))}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10)
    else:
        connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _normalize_params(params: list[Any] | None) -> list[Any] | tuple[Any, ...]:
    if not params:
        return ()
    return params


def register(mcp: FastMCP, ctx: AppContext) -> None:
    config = ctx.config.services.sqlite
    root = ctx.ensure_dir(config.root)

    @mcp.tool()
    async def sqlite_list_tables(database: str) -> str:
        """列出 SQLite 数据库中的表和视图。

        Args:
            database: 数据库文件名（相对于 sqlite.root）或沙箱内的路径。
        """
        path = _resolve_db(root, database, must_exist=True)

        def run() -> list[dict[str, Any]]:
            connection = _connect(path, readonly=True)
            try:
                rows = connection.execute(
                    "SELECT name, type FROM sqlite_master "
                    "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
                    "ORDER BY name"
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                connection.close()

        return json_dumps(
            {"database": database, "tables": await asyncio.to_thread(run)}
        )

    @mcp.tool()
    async def sqlite_describe(database: str, table: str) -> str:
        """查看 SQLite 表结构。

        Args:
            database: 数据库文件名或沙箱内的路径。
            table: 表名或视图名。
        """
        path = _resolve_db(root, database, must_exist=True)

        def run() -> list[dict[str, Any]]:
            connection = _connect(path, readonly=True)
            try:
                rows = connection.execute(
                    'SELECT name, type, "notnull", dflt_value, pk '
                    "FROM pragma_table_info(?)",
                    (table,),
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                connection.close()

        columns = await asyncio.to_thread(run)
        if not columns:
            raise ValueError(f"Table or view not found: {table}")
        return json_dumps({"database": database, "table": table, "columns": columns})

    @mcp.tool()
    async def sqlite_query(
        database: str,
        sql: str,
        params: list[Any] | None = None,
        max_rows: int = 0,
    ) -> str:
        """以只读方式执行 SQL 查询。

        Args:
            database: 数据库文件名或沙箱内的路径。
            sql: 单条 SQL 查询语句（SELECT / WITH / PRAGMA 等）。
            params: 绑定参数列表，使用 ? 占位符。
            max_rows: 最多返回行数；0 表示使用配置上限。
        """
        path = _resolve_db(root, database, must_exist=True)
        limit = max(1, min(int(max_rows) or config.max_rows, 5000))

        def run() -> dict[str, Any]:
            connection = _connect(path, readonly=True)
            try:
                cursor = connection.execute(sql, _normalize_params(params))
                rows = cursor.fetchmany(limit + 1)
                truncated = len(rows) > limit
                rows = rows[:limit]
                columns = (
                    [description[0] for description in cursor.description]
                    if cursor.description
                    else []
                )
                return {
                    "columns": columns,
                    "rows": [dict(row) for row in rows],
                    "row_count": len(rows),
                    "truncated": truncated,
                }
            except sqlite3.DatabaseError as exc:
                raise ValueError(f"SQLite error: {exc}") from exc
            finally:
                connection.close()

        result = await asyncio.to_thread(run)
        return json_dumps({"database": database, "sql": sql, **result})

    @mcp.tool()
    async def sqlite_execute(
        database: str,
        sql: str,
        params: list[Any] | None = None,
    ) -> str:
        """执行单条写操作 SQL（需开启 sqlite.allow_write）。

        Args:
            database: 数据库文件名或沙箱内的路径；不存在时会创建。
            sql: 单条 INSERT / UPDATE / DELETE / CREATE 等语句。
            params: 绑定参数列表，使用 ? 占位符。
        """
        if not config.allow_write:
            raise ValueError(
                "SQLite writes are disabled (services.sqlite.allow_write=false)"
            )
        path = _resolve_db(root, database, must_exist=False)
        path.parent.mkdir(parents=True, exist_ok=True)

        def run() -> dict[str, Any]:
            connection = _connect(path, readonly=False)
            try:
                cursor = connection.execute(sql, _normalize_params(params))
                connection.commit()
                return {
                    "row_count": cursor.rowcount,
                    "last_row_id": cursor.lastrowid,
                }
            except sqlite3.DatabaseError as exc:
                connection.rollback()
                raise ValueError(f"SQLite error: {exc}") from exc
            finally:
                connection.close()

        result = await asyncio.to_thread(run)
        return json_dumps({"database": database, "sql": sql, **result})
