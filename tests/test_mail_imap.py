"""IMAP/SMTP transport tests using fake standard-library clients."""

from __future__ import annotations

import email.policy
from email.message import EmailMessage
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from astrbot_mcp.server import build_server
from astrbot_mcp.services import mail as mail_service


def build_message(
    uid: str,
    subject: str,
    body: str,
    *,
    sender: str = "Alice <alice@example.com>",
    html: bool = False,
    attachment: tuple[str, bytes] | None = None,
) -> bytes:
    """Serialize a realistic message exactly like a real SMTP peer would."""
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "bot@example.com"
    message["Subject"] = subject
    message["Date"] = "Fri, 24 Oct 2026 10:00:00 +0000"
    message["Message-ID"] = f"<{uid}@example.com>"
    if attachment is not None:
        name, payload = attachment
        message.set_content(body)
        message.add_attachment(
            payload, maintype="application", subtype="pdf", filename=name
        )
    elif html:
        message.set_content(body, subtype="html")
    else:
        message.set_content(body)
    return message.as_bytes()


class FakeIMAP:
    """Minimal ``imaplib.IMAP4`` stand-in covering the calls mail.py makes."""

    def __init__(
        self,
        messages: dict[str, bytes],
        *,
        select_status: str = "OK",
        fail_uids: set[str] | None = None,
    ) -> None:
        self.messages = messages
        self.select_status = select_status
        self.fail_uids = fail_uids or set()
        self.searches: list[tuple[Any, ...]] = []
        self.selected: list[tuple[str, bool]] = []
        self.fetched: list[tuple[str, str]] = []
        self.logged_out = False

    def select(self, mailbox: str, readonly: bool = False):
        self.selected.append((mailbox, readonly))
        if self.select_status != "OK":
            return self.select_status, [b"0"]
        return "OK", [b"1"]

    def uid(self, command: str, *args: Any):
        if command == "SEARCH":
            self.searches.append(args)
            uids = sorted(self.messages)
            return "OK", [b" ".join(uid.encode() for uid in uids)]
        if command == "FETCH":
            uid, criteria = str(args[0]), str(args[1])
            self.fetched.append((uid, criteria))
            raw = self.messages.get(uid)
            if raw is None or uid in self.fail_uids:
                return "NO", [None]
            if "HEADER.FIELDS" in criteria:
                header = raw.split(b"\n\n", 1)[0]
                return "OK", [(b"{0}", header + b"\r\n\r\n"), b")"]
            return "OK", [(b"{0}", raw), b")"]
        return "BAD", [None]

    def logout(self) -> None:
        self.logged_out = True


def install_imap(monkeypatch: pytest.MonkeyPatch, connection: FakeIMAP) -> FakeIMAP:
    monkeypatch.setattr(mail_service, "_imap_connect", lambda config: connection)
    return connection


MAILBOX = {
    "1": build_message("1", "欢迎使用 AstrBot", "这是第一封邮件。"),
    "2": build_message(
        "2",
        "HTML 邮件",
        "<html><body><h1>标题</h1><p>正文内容</p></body></html>",
        html=True,
    ),
    "3": build_message(
        "3",
        "带附件",
        "纯文本正文",
        attachment=("report.pdf", b"%PDF-1.4 fake"),
    ),
}


def mail_config(**overrides: Any) -> dict[str, Any]:
    base = {
        "smtp_host": "smtp.example.com",
        "imap_host": "imap.example.com",
        "username": "bot@example.com",
        "password": "secret",
        "from_addr": "bot@example.com",
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------------ mail_list
@pytest.mark.asyncio
async def test_mail_list_headers_newest_first(config_factory, payload, monkeypatch):
    connection = install_imap(monkeypatch, FakeIMAP(MAILBOX))
    mcp = build_server(config_factory(mail=mail_config()))

    data = payload(await mcp.call_tool("mail_list", {"limit": 2}))
    assert data["mailbox"] == "INBOX"
    assert data["count"] == 2
    # Mailbox uids are ascending, the tool reverses them so uid 3 comes first.
    assert [item["uid"] for item in data["messages"]] == ["3", "2"]
    newest = data["messages"][0]
    assert newest["subject"] == "带附件"
    assert newest["from"] == "Alice <alice@example.com>"
    assert newest["to"] == "bot@example.com"
    assert newest["message_id"] == "<3@example.com>"

    assert connection.selected == [("INBOX", True)]
    assert connection.searches == [(None, "ALL")]
    assert connection.logged_out is True


@pytest.mark.asyncio
async def test_mail_list_unread_only_uses_unseen(config_factory, payload, monkeypatch):
    connection = install_imap(monkeypatch, FakeIMAP(MAILBOX))
    mcp = build_server(config_factory(mail=mail_config()))

    data = payload(
        await mcp.call_tool(
            "mail_list",
            {"limit": 1, "mailbox": "Sent", "unread_only": True},
        )
    )
    assert data["unread_only"] is True
    assert data["count"] == 1
    assert connection.selected == [("Sent", True)]
    assert connection.searches == [(None, "UNSEEN")]


# ------------------------------------------------------------------ mail_read
@pytest.mark.asyncio
async def test_mail_read_by_uid_and_latest(config_factory, payload, monkeypatch):
    install_imap(monkeypatch, FakeIMAP(MAILBOX))
    mcp = build_server(config_factory(mail=mail_config()))

    explicit = payload(await mcp.call_tool("mail_read", {"uid": "1"}))
    assert explicit["uid"] == "1"
    assert explicit["subject"] == "欢迎使用 AstrBot"
    assert "第一封邮件" in explicit["text"]

    latest = payload(await mcp.call_tool("mail_read", {}))
    assert latest["uid"] == "3"
    assert "纯文本正文" in latest["text"]


@pytest.mark.asyncio
async def test_mail_read_strips_html_and_skips_attachments(
    config_factory, payload, monkeypatch
):
    install_imap(monkeypatch, FakeIMAP(MAILBOX))
    mcp = build_server(config_factory(mail=mail_config()))

    html_mail = payload(await mcp.call_tool("mail_read", {"uid": "2"}))
    assert "标题" in html_mail["text"]
    assert "正文内容" in html_mail["text"]
    assert "<h1>" not in html_mail["text"]

    with_attachment = payload(await mcp.call_tool("mail_read", {"uid": "3"}))
    assert with_attachment["text"].strip() == "纯文本正文"
    assert "PDF-1.4" not in with_attachment["text"]


@pytest.mark.asyncio
async def test_mail_read_truncates_body(config_factory, payload, monkeypatch):
    box = {"1": build_message("1", "长邮件", "字" * 5000)}
    install_imap(monkeypatch, FakeIMAP(box))
    mcp = build_server(config_factory(mail=mail_config()))

    data = payload(await mcp.call_tool("mail_read", {"uid": "1", "max_chars": 100}))
    assert data["text"].endswith("...[truncated]")
    assert len(data["text"]) < 5000


# -------------------------------------------------------------------- errors
@pytest.mark.asyncio
async def test_mail_imap_errors(config_factory, monkeypatch):
    broken = install_imap(monkeypatch, FakeIMAP(MAILBOX, select_status="NO"))
    mcp = build_server(config_factory(mail=mail_config()))
    with pytest.raises(ToolError):
        await mcp.call_tool("mail_list", {})
    assert broken.logged_out is True

    missing = install_imap(monkeypatch, FakeIMAP(MAILBOX, fail_uids={"1"}))
    with pytest.raises(ToolError):
        await mcp.call_tool("mail_read", {"uid": "1"})
    assert missing.logged_out is True

    empty = install_imap(monkeypatch, FakeIMAP({}))
    with pytest.raises(ToolError):
        await mcp.call_tool("mail_read", {})
    assert empty.logged_out is True


# ------------------------------------------------------------- connect logic
def test_imap_connect_ssl_and_starttls(monkeypatch):
    captured: list[dict[str, Any]] = []

    class FakeSSL:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            captured.append(
                {"kind": "ssl", "host": host, "port": port, "timeout": timeout}
            )

        def login(self, username: str, password: str) -> None:
            captured[-1]["login"] = (username, password)

    class FakePlain(FakeSSL):
        def __init__(self, host: str, port: int, timeout: float) -> None:
            super().__init__(host, port, timeout)
            captured[-1]["kind"] = "plain"

        def starttls(self) -> None:
            captured[-1]["starttls"] = True

    monkeypatch.setattr(mail_service.imaplib, "IMAP4_SSL", FakeSSL)
    monkeypatch.setattr(mail_service.imaplib, "IMAP4", FakePlain)

    ssl_config = mail_service.MailConfig(
        imap_host="imap.example.com", username="u", password="p"
    )
    mail_service._imap_connect(ssl_config)
    assert captured[-1]["kind"] == "ssl"
    assert captured[-1]["port"] == 993
    assert captured[-1]["login"] == ("u", "p")
    assert "starttls" not in captured[-1]

    plain_config = mail_service.MailConfig(
        imap_host="imap.example.com",
        imap_port=143,
        imap_ssl=False,
        username="u",
        password="p",
    )
    mail_service._imap_connect(plain_config)
    assert captured[-1]["kind"] == "plain"
    assert captured[-1]["starttls"] is True
    assert captured[-1]["login"] == ("u", "p")


def test_send_sync_starttls_flow(monkeypatch):
    calls: list[str] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            assert host == "smtp.example.com"
            assert port == 587
            calls.append("connect")

        def ehlo(self) -> None:
            calls.append("ehlo")

        def starttls(self) -> None:
            calls.append("starttls")

        def login(self, username: str, password: str) -> None:
            calls.append(f"login:{username}:{password}")

        def send_message(self, message, to_addrs) -> None:
            calls.append(f"send:{','.join(to_addrs)}")

        def quit(self) -> None:
            calls.append("quit")

        def close(self) -> None:  # pragma: no cover - only on failure
            calls.append("close")

    monkeypatch.setattr(mail_service.smtplib, "SMTP", FakeSMTP)

    config = mail_service.MailConfig(
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_ssl=False,
        smtp_starttls=True,
        username="bot@example.com",
        password="secret",
    )
    message = EmailMessage()
    message["Subject"] = "hi"
    message.set_content("body")
    mail_service._send_sync(config, message, ["a@example.com"])

    assert calls == [
        "connect",
        "ehlo",
        "starttls",
        "ehlo",
        "login:bot@example.com:secret",
        "send:a@example.com",
        "quit",
    ]


def test_extract_body_multipart_alternative():
    message = EmailMessage()
    message.set_content("纯文本")
    message.add_alternative("<p>HTML</p>", subtype="html")
    parsed = mail_service.email.message_from_bytes(
        message.as_bytes(), policy=email.policy.default
    )
    assert mail_service._extract_body(parsed).strip() == "纯文本"
