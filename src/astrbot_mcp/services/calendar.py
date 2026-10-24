"""Local iCalendar (.ics) tools."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from icalendar import Calendar, Event, Timezone
from mcp.server.fastmcp import FastMCP

from ..context import AppContext
from ..utils import json_dumps, truncate_text

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _validate_name(name: str) -> str:
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            "Calendar name may only contain letters, digits, '_' and '-' (max 64 chars)"
        )
    return name


def _calendar_path(root: Path, name: str) -> Path:
    return root / f"{_validate_name(name)}.ics"


def _load_calendar(path: Path) -> Calendar:
    if path.exists():
        try:
            loaded = Calendar.from_ical(path.read_bytes())
        except Exception as exc:  # icalendar raises several parser exceptions
            raise ValueError(f"Cannot parse calendar {path.name}: {exc}") from exc
        if not isinstance(loaded, Calendar):
            raise ValueError(f"{path.name} is not a valid iCalendar file")
        return loaded
    calendar = Calendar()
    calendar.add("prodid", "-//astrbot-mcp//calendar//CN")
    calendar.add("version", "2.0")
    return calendar


def _local_tz():
    return datetime.now().astimezone().tzinfo or timezone.utc


def _parse_iso(value: str) -> date | datetime:
    text = value.strip()
    if not text:
        raise ValueError("Empty date/time value")
    if len(text) == 10:
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(
            f"Invalid ISO 8601 date/time: {value!r} (use e.g. 2026-10-24 or "
            "2026-10-24T09:00:00+08:00)"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_local_tz())
    return parsed


def _to_datetime(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        # icalendar may return a naive datetime when the .ics file has a TZID
        # but no matching VTIMEZONE component.
        return value if value.tzinfo is not None else value.replace(tzinfo=_local_tz())
    return datetime.combine(value, time.min, tzinfo=_local_tz())


def _serialize(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _event_value(component: Event, name: str) -> Any:
    value = component.get(name)
    return getattr(value, "dt", value)


def _ensure_timezone(calendar: Calendar, value: date | datetime) -> None:
    """Embed a VTIMEZONE so aware datetimes survive a round-trip."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        return
    tzid = (
        getattr(value.tzinfo, "key", None)
        or getattr(value.tzinfo, "zone", None)
        or value.tzname()
        or "Local"
    )
    for component in calendar.subcomponents:
        if component.name == "VTIMEZONE" and str(component.get("tzid", "")) == tzid:
            return
    try:
        calendar.add_component(Timezone.from_tzinfo(value.tzinfo, tzid=tzid))
    except Exception:  # noqa: BLE001 - timezone metadata is best-effort
        pass


def register(mcp: FastMCP, ctx: AppContext) -> None:
    config = ctx.config.services.calendar
    root = ctx.ensure_dir(config.root)

    @mcp.tool()
    async def calendar_list(
        calendar_name: str = "",
    ) -> str:
        """列出本地日历文件。

        Args:
            calendar_name: 可选，只查看指定日历。
        """
        names = (
            [_validate_name(calendar_name)]
            if calendar_name
            else sorted(path.stem for path in root.glob("*.ics"))
        )
        items: list[dict[str, Any]] = []
        for name in names:
            path = _calendar_path(root, name)
            if not path.exists():
                continue
            calendar = _load_calendar(path)
            events = [item for item in calendar.subcomponents if item.name == "VEVENT"]
            items.append(
                {
                    "name": name,
                    "path": path.name,
                    "events": len(events),
                    "size": path.stat().st_size,
                    "modified": path.stat().st_mtime,
                }
            )
        return json_dumps({"calendars": items})

    @mcp.tool()
    async def calendar_list_events(
        days: int = 7,
        calendar_name: str = "",
        include_past: bool = False,
        max_results: int = 50,
    ) -> str:
        """列出未来一段时间内的日程。

        Args:
            days: 向后查看的天数，1-365。
            calendar_name: 指定日历；留空表示所有日历。
            include_past: 是否包含已经结束的日程。
            max_results: 最多返回条数，1-500。
        """
        horizon_days = max(1, min(int(days), 365))
        limit = max(1, min(int(max_results), 500))
        now = datetime.now(_local_tz())
        horizon = now + timedelta(days=horizon_days)

        names = (
            [_validate_name(calendar_name)]
            if calendar_name
            else sorted(path.stem for path in root.glob("*.ics"))
        )

        events: list[dict[str, Any]] = []
        for name in names:
            path = _calendar_path(root, name)
            if not path.exists():
                continue
            calendar = _load_calendar(path)
            for component in calendar.subcomponents:
                if component.name != "VEVENT":
                    continue
                start_raw = _event_value(component, "dtstart")
                end_raw = _event_value(component, "dtend")
                if start_raw is None and end_raw is None:
                    continue

                start = _to_datetime(start_raw) if start_raw is not None else None
                if end_raw is not None:
                    end = _to_datetime(end_raw)
                else:
                    duration = component.decoded("duration", None)
                    end = (
                        start + duration
                        if start is not None and duration is not None
                        else start
                    )

                reference_end = end or start
                if (
                    not include_past
                    and reference_end is not None
                    and reference_end < now
                ):
                    continue
                if start is not None and start > horizon:
                    continue

                events.append(
                    {
                        "calendar": name,
                        "uid": str(component.get("uid", "")),
                        "summary": str(component.get("summary", "")),
                        "start": start.isoformat() if start is not None else None,
                        "end": end.isoformat() if end is not None else None,
                        "all_day": isinstance(start_raw, date)
                        and not isinstance(start_raw, datetime),
                        "location": str(component.get("location", "")),
                        "description": truncate_text(
                            str(component.get("description", "")), 500
                        ),
                    }
                )

        events.sort(key=lambda item: item["start"] or "")
        return json_dumps(
            {
                "window_start": now.isoformat(),
                "window_end": horizon.isoformat(),
                "count": len(events[:limit]),
                "truncated": len(events) > limit,
                "events": events[:limit],
            }
        )

    @mcp.tool()
    async def calendar_add_event(
        title: str,
        start: str,
        end: str = "",
        description: str = "",
        location: str = "",
        calendar_name: str = "",
    ) -> str:
        """新增日程（需开启 calendar.allow_write）。

        Args:
            title: 日程标题。
            start: 开始时间，ISO 8601，例如 2026-10-24T09:00:00+08:00 或 2026-10-24。
            end: 结束时间；留空时按开始时间自动加 1 小时（全天则加 1 天）。
            description: 描述。
            location: 地点。
            calendar_name: 日历名称；留空使用默认日历。
        """
        if not config.allow_write:
            raise ValueError(
                "Calendar writes are disabled (services.calendar.allow_write=false)"
            )

        name = _validate_name(calendar_name or config.default_calendar)
        start_value = _parse_iso(start)
        if end:
            end_value = _parse_iso(end)
        elif isinstance(start_value, datetime):
            end_value = start_value + timedelta(hours=1)
        else:
            end_value = start_value + timedelta(days=1)

        start_is_date = isinstance(start_value, date) and not isinstance(
            start_value, datetime
        )
        end_is_date = isinstance(end_value, date) and not isinstance(
            end_value, datetime
        )
        if start_is_date != end_is_date:
            raise ValueError("start and end must both be dates or both be datetimes")

        path = _calendar_path(root, name)
        calendar = _load_calendar(path)
        event = Event()
        uid = str(uuid4())
        event.add("uid", uid)
        event.add("dtstamp", datetime.now(timezone.utc))
        event.add("summary", title)
        event.add("dtstart", start_value)
        event.add("dtend", end_value)
        if description:
            event.add("description", description)
        if location:
            event.add("location", location)
        _ensure_timezone(calendar, start_value)
        _ensure_timezone(calendar, end_value)
        calendar.add_component(event)
        path.write_bytes(calendar.to_ical())

        return json_dumps(
            {
                "created": True,
                "calendar": name,
                "uid": uid,
                "summary": title,
                "start": _serialize(start_value),
                "end": _serialize(end_value),
                "all_day": start_is_date,
            }
        )

    @mcp.tool()
    async def calendar_delete_event(
        uid: str,
        calendar_name: str = "",
    ) -> str:
        """按 UID 删除日程（需开启 calendar.allow_write）。

        Args:
            uid: 日程 UID，可从 calendar_list_events 获取。
            calendar_name: 指定日历；留空表示在所有日历中查找。
        """
        if not config.allow_write:
            raise ValueError(
                "Calendar writes are disabled (services.calendar.allow_write=false)"
            )

        uid = uid.strip()
        if not uid:
            raise ValueError("uid is required")
        names = (
            [_validate_name(calendar_name)]
            if calendar_name
            else sorted(path.stem for path in root.glob("*.ics"))
        )

        deleted = 0
        for name in names:
            path = _calendar_path(root, name)
            if not path.exists():
                continue
            calendar = _load_calendar(path)
            remaining = []
            changed = False
            for component in calendar.subcomponents:
                if component.name == "VEVENT" and str(component.get("uid", "")) == uid:
                    deleted += 1
                    changed = True
                    continue
                remaining.append(component)
            if changed:
                calendar.subcomponents = remaining
                path.write_bytes(calendar.to_ical())

        if deleted == 0:
            raise ValueError(f"No event found with uid {uid!r}")
        return json_dumps({"deleted": deleted, "uid": uid})
