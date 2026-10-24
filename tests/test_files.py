from pathlib import Path

import pytest

from astrbot_mcp.services.files import _resolve


def test_resolve_stays_inside_sandbox(tmp_path: Path):
    root = tmp_path / "files"
    root.mkdir()
    assert _resolve(root, "a/b.txt") == (root / "a/b.txt").resolve()
    with pytest.raises(ValueError):
        _resolve(root, "../escape.txt")
    with pytest.raises(ValueError):
        _resolve(root, "/etc/passwd")


def test_symlink_escape_rejected(tmp_path: Path):
    root = tmp_path / "files"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)

    with pytest.raises(ValueError):
        _resolve(root, "link.txt")
