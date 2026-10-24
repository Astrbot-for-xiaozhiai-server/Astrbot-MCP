from pathlib import Path

import pytest

from astrbot_mcp.config import AppConfig, ConfigError


def test_env_expansion_and_path_resolution(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("TEST_TOKEN", "secret-value")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
server:
  transport: streamable_http
  auth_token: ${TEST_TOKEN}
services:
  files:
    root: ./data/files
  knowledge:
    db_path: ./data/kb.db
""",
        encoding="utf-8",
    )

    config = AppConfig.load(config_file)

    assert config.server.transport == "streamable-http"
    assert config.server.auth_token == "secret-value"
    assert Path(config.services.files.root) == (tmp_path / "data/files").resolve()
    assert (
        Path(config.services.knowledge.db_path) == (tmp_path / "data/kb.db").resolve()
    )


def test_missing_env_uses_default(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("NOT_SET_VALUE", raising=False)
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "server:\n  name: ${NOT_SET_VALUE:-fallback}\n", encoding="utf-8"
    )
    config = AppConfig.load(config_file)
    assert config.server.name == "fallback"


def test_masked_dump_hides_secrets(tmp_path: Path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
server:
  auth_token: top-secret
services:
  github:
    token: ghp_example
  mail:
    password: hunter2
""",
        encoding="utf-8",
    )
    masked = AppConfig.load(config_file).masked_dump()
    assert masked["server"]["auth_token"] == "***"
    assert masked["services"]["github"]["token"] == "***"
    assert masked["services"]["mail"]["password"] == "***"


def test_invalid_chunking_rejected(tmp_path: Path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
services:
  knowledge:
    chunk_size: 100
    chunk_overlap: 100
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        AppConfig.load(config_file)
