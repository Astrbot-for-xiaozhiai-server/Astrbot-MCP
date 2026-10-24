"""Sandboxed file-system tools."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..context import AppContext
from ..utils import decode_bytes, human_size, json_dumps, truncate_text


def _resolve(root: Path, user_path: str) -> Path:
    raw = Path(user_path or ".").expanduser()
    candidate = raw if raw.is_absolute() else root / raw
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"Path {user_path!r} escapes the sandbox root {str(root)!r}")
    return resolved


def _relative(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _entry(root: Path, path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": _relative(root, path),
        "name": path.name,
        "type": "dir" if path.is_dir() else "file",
        "size": stat.st_size,
        "size_human": human_size(stat.st_size),
        "modified": stat.st_mtime,
    }


def _read_text_file(path: Path, max_bytes: int, max_read_bytes: int) -> str:
    if not path.is_file():
        raise ValueError(f"Not a file: {path}")
    size = path.stat().st_size
    if size > max_read_bytes:
        raise ValueError(
            f"File is too large ({human_size(size)}); limit is "
            f"{human_size(max_read_bytes)}. Increase files.max_read_bytes or read a range."
        )
    data = path.read_bytes()[:max_bytes]
    if b"\x00" in data[:4096]:
        raise ValueError("File appears to be binary; text reading is not supported")
    return decode_bytes(data)


def register(mcp: FastMCP, ctx: AppContext) -> None:
    config = ctx.config.services.files
    root = ctx.ensure_dir(config.root)

    @mcp.tool()
    async def fs_list(
        path: str = ".",
        pattern: str = "*",
        recursive: bool = False,
        include_hidden: bool = False,
        max_results: int = 200,
    ) -> str:
        """列出沙箱目录中的文件与子目录。

        Args:
            path: 相对于沙箱根目录的路径，默认 "."。
            pattern: 文件名匹配模式，默认 "*"。
            recursive: 是否递归子目录。
            include_hidden: 是否包含以点开头的隐藏文件。
            max_results: 最多返回条目数，1-1000。
        """
        limit = max(1, min(int(max_results), 1000))

        def run() -> dict[str, Any]:
            target = _resolve(root, path)
            if not target.exists():
                raise ValueError(f"Path does not exist: {path}")
            if target.is_file():
                return {"root": str(root), "entries": [_entry(root, target)]}

            iterator = target.rglob(pattern) if recursive else target.glob(pattern)
            entries: list[dict[str, Any]] = []
            for item in iterator:
                if not include_hidden and any(
                    part.startswith(".") for part in item.relative_to(target).parts
                ):
                    continue
                try:
                    resolved = item.resolve()
                    if resolved != root and root not in resolved.parents:
                        continue
                    entries.append(_entry(root, item))
                except OSError:
                    continue
                if len(entries) >= limit:
                    break
            entries.sort(key=lambda entry: (entry["type"] != "dir", entry["path"]))
            return {
                "root": str(root),
                "path": _relative(root, target),
                "count": len(entries),
                "truncated": len(entries) >= limit,
                "entries": entries,
            }

        return json_dumps(await asyncio.to_thread(run))

    @mcp.tool()
    async def fs_read(
        path: str,
        start_line: int = 1,
        end_line: int = 0,
        max_bytes: int = 0,
    ) -> str:
        """读取沙箱内的文本文件，可按行截取。

        Args:
            path: 相对于沙箱根目录的文件路径。
            start_line: 起始行号（从 1 开始）。
            end_line: 结束行号；0 表示读到文件末尾。
            max_bytes: 最多读取的字节数；0 表示使用配置上限。
        """
        limit_bytes = max(1, int(max_bytes)) if max_bytes else config.max_read_bytes
        limit_bytes = min(limit_bytes, config.max_read_bytes)

        def run() -> dict[str, Any]:
            target = _resolve(root, path)
            text = _read_text_file(target, limit_bytes, config.max_read_bytes)
            lines = text.splitlines()
            start = max(1, int(start_line)) - 1
            end = int(end_line) if int(end_line) > 0 else len(lines)
            selected = lines[start:end]
            content = "\n".join(selected)
            return {
                "path": _relative(root, target),
                "encoding": "utf-8",
                "total_lines": len(lines),
                "start_line": start + 1,
                "end_line": min(end, len(lines)),
                "truncated": len(content.encode("utf-8")) >= limit_bytes,
                "content": truncate_text(content, limit_bytes),
            }

        return json_dumps(await asyncio.to_thread(run))

    @mcp.tool()
    async def fs_write(
        path: str,
        content: str,
        append: bool = False,
        create_dirs: bool = True,
    ) -> str:
        """向沙箱内写入文本文件（需开启 files.allow_write）。

        Args:
            path: 相对于沙箱根目录的文件路径。
            content: 要写入的文本内容。
            append: 是否追加而不是覆盖。
            create_dirs: 是否自动创建父目录。
        """
        if not config.allow_write:
            raise ValueError(
                "File writing is disabled (services.files.allow_write=false)"
            )
        data = content.encode("utf-8")
        if len(data) > config.max_write_bytes:
            raise ValueError(
                f"Content is too large ({human_size(len(data))}); limit is "
                f"{human_size(config.max_write_bytes)}"
            )

        def run() -> dict[str, Any]:
            target = _resolve(root, path)
            if target.is_dir():
                raise ValueError(f"Path is a directory: {path}")
            if create_dirs:
                target.parent.mkdir(parents=True, exist_ok=True)
            elif not target.parent.exists():
                raise ValueError(f"Parent directory does not exist: {target.parent}")
            with target.open("a" if append else "w", encoding="utf-8") as handle:
                handle.write(content)
            stat = target.stat()
            return {
                "path": _relative(root, target),
                "bytes_written": stat.st_size if append else len(data),
                "size": stat.st_size,
                "append": append,
            }

        return json_dumps(await asyncio.to_thread(run))

    @mcp.tool()
    async def fs_search(
        query: str,
        path: str = ".",
        pattern: str = "*",
        max_results: int = 50,
        use_regex: bool = False,
        case_sensitive: bool = False,
    ) -> str:
        """在沙箱文本文件中搜索内容。

        Args:
            query: 要查找的文本或正则表达式。
            path: 搜索起点，相对于沙箱根目录。
            pattern: 文件名过滤模式，例如 "*.py"。
            max_results: 最多返回的匹配行数，1-500。
            use_regex: 是否把 query 当作正则表达式。
            case_sensitive: 是否区分大小写。
        """
        limit = max(1, min(int(max_results), 500))
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            compiled = re.compile(query if use_regex else re.escape(query), flags)
        except re.error as exc:
            raise ValueError(f"Invalid regular expression: {exc}") from exc

        def run() -> dict[str, Any]:
            base = _resolve(root, path)
            if not base.exists():
                raise ValueError(f"Path does not exist: {path}")
            files = [base] if base.is_file() else base.rglob(pattern)
            matches: list[dict[str, Any]] = []
            scanned = 0
            for item in files:
                try:
                    resolved = item.resolve()
                    if resolved != root and root not in resolved.parents:
                        continue
                    if not resolved.is_file():
                        continue
                    if resolved.stat().st_size > config.max_read_bytes:
                        continue
                    data = resolved.read_bytes()
                except OSError:
                    continue
                if b"\x00" in data[:4096]:
                    continue
                scanned += 1
                for line_number, line in enumerate(
                    decode_bytes(data).splitlines(), start=1
                ):
                    if compiled.search(line):
                        matches.append(
                            {
                                "path": _relative(root, item),
                                "line": line_number,
                                "text": line.strip()[:500],
                            }
                        )
                        if len(matches) >= limit:
                            return {
                                "query": query,
                                "scanned_files": scanned,
                                "count": len(matches),
                                "truncated": True,
                                "matches": matches,
                            }
            return {
                "query": query,
                "scanned_files": scanned,
                "count": len(matches),
                "truncated": False,
                "matches": matches,
            }

        return json_dumps(await asyncio.to_thread(run))

    @mcp.tool()
    async def fs_stat(path: str) -> str:
        """查看沙箱内文件或目录的元信息。

        Args:
            path: 相对于沙箱根目录的路径。
        """
        target = _resolve(root, path)
        if not target.exists():
            raise ValueError(f"Path does not exist: {path}")
        return json_dumps(
            {
                "root": str(root),
                "exists": True,
                "is_file": target.is_file(),
                "is_dir": target.is_dir(),
                **_entry(root, target),
            }
        )

    @mcp.tool()
    async def fs_mkdir(path: str) -> str:
        """在沙箱内创建目录（需开启 files.allow_write）。

        Args:
            path: 相对于沙箱根目录的目录路径。
        """
        if not config.allow_write:
            raise ValueError(
                "File writing is disabled (services.files.allow_write=false)"
            )
        target = _resolve(root, path)
        target.mkdir(parents=True, exist_ok=True)
        return json_dumps({"path": _relative(root, target), "created": True})

    @mcp.tool()
    async def fs_delete(path: str, recursive: bool = False) -> str:
        """删除沙箱内的文件或目录（需开启 files.allow_delete）。

        Args:
            path: 相对于沙箱根目录的路径。
            recursive: 删除目录时必须为 true。
        """
        if not config.allow_delete:
            raise ValueError(
                "File deletion is disabled (services.files.allow_delete=false)"
            )

        def run() -> dict[str, Any]:
            target = _resolve(root, path)
            if target == root:
                raise ValueError("Refusing to delete the sandbox root")
            if not target.exists():
                raise ValueError(f"Path does not exist: {path}")
            if target.is_dir():
                if not recursive:
                    raise ValueError("Directory deletion requires recursive=true")
                import shutil

                shutil.rmtree(target)
            else:
                target.unlink()
            return {"path": _relative(root, target), "deleted": True}

        return json_dumps(await asyncio.to_thread(run))
