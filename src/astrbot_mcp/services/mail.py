"""SMTP / IMAP email tools (standard library only)."""

from __future__ import annotations

import asyncio
import email
import email.policy
import imaplib
import smtplib
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..config import MailConfig
from ..context import AppContext
from ..utils import json_dumps, strip_html, truncate_text


def _split_addresses(value: str) -> list[str]:
    return [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeDecodeError):
        return value


def _send_sync(
    config: MailConfig, message: EmailMessage, recipients: list[str]
) -> None:
    if config.smtp_ssl:
        server: smtplib.SMTP = smtplib.SMTP_SSL(
            config.smtp_host, config.smtp_port, timeout=config.timeout
        )
    else:
        server = smtplib.SMTP(
            config.smtp_host, config.smtp_port, timeout=config.timeout
        )

    try:
        server.ehlo()
        if not config.smtp_ssl and config.smtp_starttls:
            server.starttls()
            server.ehlo()
        if config.username:
            server.login(config.username, config.password)
        server.send_message(message, to_addrs=recipients)
    finally:
        try:
            server.quit()
        except smtplib.SMTPException:
            server.close()


def _imap_connect(config: MailConfig) -> imaplib.IMAP4:
    if config.imap_ssl:
        connection: imaplib.IMAP4 = imaplib.IMAP4_SSL(
            config.imap_host, config.imap_port, timeout=config.timeout
        )
    else:
        connection = imaplib.IMAP4(
            config.imap_host, config.imap_port, timeout=config.timeout
        )
        connection.starttls()
    if config.username:
        connection.login(config.username, config.password)
    return connection


def _extract_body(message: email.message.Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", "replace")
        for part in message.walk():
            if part.get_content_type() == "text/html" and not part.get_filename():
                payload = part.get_payload(decode=True) or b""
                html = payload.decode(part.get_content_charset() or "utf-8", "replace")
                return strip_html(html)["text"]
        return ""

    payload = message.get_payload(decode=True) or b""
    charset = message.get_content_charset() or "utf-8"
    text = payload.decode(charset, "replace")
    if message.get_content_type() == "text/html":
        return strip_html(text)["text"]
    return text


def _fetch_uids(connection: imaplib.IMAP4, limit: int, unread_only: bool) -> list[str]:
    status, data = connection.uid("SEARCH", None, "UNSEEN" if unread_only else "ALL")
    if status != "OK" or not data:
        return []
    raw = data[0]
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", "ignore")
    uids = [item for item in str(raw).split() if item]
    return uids[-limit:]


def register(mcp: FastMCP, ctx: AppContext) -> None:
    config = ctx.config.services.mail

    @mcp.tool()
    async def mail_send(
        to: str,
        subject: str,
        body: str,
        html_body: str = "",
        cc: str = "",
        bcc: str = "",
    ) -> str:
        """发送邮件（需配置 SMTP）。

        Args:
            to: 收件人，多个地址用逗号分隔。
            subject: 邮件主题。
            body: 纯文本正文。
            html_body: 可选的 HTML 正文。
            cc: 抄送地址，逗号分隔。
            bcc: 密送地址，逗号分隔。
        """
        if not config.smtp_host:
            raise ValueError(
                "Mail is not configured (services.mail.smtp_host is empty)"
            )

        to_list = _split_addresses(to)
        if not to_list:
            raise ValueError("At least one recipient is required")
        cc_list = _split_addresses(cc)
        bcc_list = _split_addresses(bcc)

        message = EmailMessage()
        sender = config.from_addr or config.username
        if not sender:
            raise ValueError("Set services.mail.from_addr or username")
        message["From"] = formataddr(("AstrBot", sender))
        message["To"] = ", ".join(to_list)
        if cc_list:
            message["Cc"] = ", ".join(cc_list)
        message["Subject"] = subject
        message.set_content(body)
        if html_body:
            message.add_alternative(html_body, subtype="html")

        recipients = to_list + cc_list + bcc_list
        await asyncio.to_thread(_send_sync, config, message, recipients)
        return json_dumps(
            {"sent": True, "to": to_list, "cc": cc_list, "bcc_count": len(bcc_list)}
        )

    @mcp.tool()
    async def mail_list(
        limit: int = 10,
        mailbox: str = "INBOX",
        unread_only: bool = False,
    ) -> str:
        """列出邮箱中的邮件头（需配置 IMAP）。

        Args:
            limit: 最多返回多少封，1-100。
            mailbox: 邮箱文件夹，默认 INBOX。
            unread_only: 只看未读邮件。
        """
        if not config.imap_host:
            raise ValueError(
                "Mail is not configured (services.mail.imap_host is empty)"
            )
        count = max(1, min(int(limit), 100))

        def run() -> list[dict[str, Any]]:
            connection = _imap_connect(config)
            try:
                status, _ = connection.select(mailbox, readonly=True)
                if status != "OK":
                    raise ValueError(f"Cannot open mailbox {mailbox!r}")
                messages: list[dict[str, Any]] = []
                for uid in reversed(_fetch_uids(connection, count, unread_only)):
                    status, data = connection.uid(
                        "FETCH",
                        uid,
                        "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])",
                    )
                    if status != "OK" or not data:
                        continue
                    header_bytes = b""
                    for item in data:
                        if isinstance(item, tuple) and len(item) > 1:
                            header_bytes = item[1]
                            break
                    if not header_bytes:
                        continue
                    parsed = email.message_from_bytes(
                        header_bytes, policy=email.policy.default
                    )
                    messages.append(
                        {
                            "uid": uid,
                            "from": _decode_header(parsed.get("From")),
                            "to": _decode_header(parsed.get("To")),
                            "subject": _decode_header(parsed.get("Subject")),
                            "date": _decode_header(parsed.get("Date")),
                            "message_id": parsed.get("Message-ID", ""),
                        }
                    )
                return messages
            finally:
                try:
                    connection.logout()
                except imaplib.IMAP4.error:
                    pass

        messages = await asyncio.to_thread(run)
        return json_dumps(
            {
                "mailbox": mailbox,
                "unread_only": unread_only,
                "count": len(messages),
                "messages": messages,
            }
        )

    @mcp.tool()
    async def mail_read(
        uid: str = "",
        mailbox: str = "INBOX",
        max_chars: int = 10000,
    ) -> str:
        """读取一封邮件的正文（需配置 IMAP）。

        Args:
            uid: 邮件 UID；留空则读取最新一封。
            mailbox: 邮箱文件夹，默认 INBOX。
            max_chars: 正文最大字符数，100-100000。
        """
        if not config.imap_host:
            raise ValueError(
                "Mail is not configured (services.mail.imap_host is empty)"
            )
        limit = max(100, min(int(max_chars), 100_000))

        def run() -> dict[str, Any]:
            connection = _imap_connect(config)
            try:
                status, _ = connection.select(mailbox, readonly=True)
                if status != "OK":
                    raise ValueError(f"Cannot open mailbox {mailbox!r}")
                target_uid = uid.strip()
                if not target_uid:
                    uids = _fetch_uids(connection, 1, unread_only=False)
                    if not uids:
                        raise ValueError("Mailbox is empty")
                    target_uid = uids[-1]

                status, data = connection.uid("FETCH", target_uid, "(RFC822)")
                if status != "OK" or not data:
                    raise ValueError(f"Message not found: uid={target_uid}")
                raw = b""
                for item in data:
                    if isinstance(item, tuple) and len(item) > 1:
                        raw = item[1]
                        break
                if not raw:
                    raise ValueError(f"Message body is empty: uid={target_uid}")

                parsed = email.message_from_bytes(raw, policy=email.policy.default)
                return {
                    "uid": target_uid,
                    "from": _decode_header(parsed.get("From")),
                    "to": _decode_header(parsed.get("To")),
                    "subject": _decode_header(parsed.get("Subject")),
                    "date": _decode_header(parsed.get("Date")),
                    "text": truncate_text(_extract_body(parsed), limit),
                }
            finally:
                try:
                    connection.logout()
                except imaplib.IMAP4.error:
                    pass

        return json_dumps(await asyncio.to_thread(run))
