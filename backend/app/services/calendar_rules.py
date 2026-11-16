"""工作日规则、节假日偏移与统一时区换算。

设计口径：

- 规则按「批次版本」管理，同一日期被多个生效批次覆盖时，以到达时间最晚的批次为准
  （晚到的依赖数据修正早到口径）；到达时间相同时取 id 更大的批次。
- 所有时间对内统一转成 UTC 比较与存储，对外同时给出批次标准时区的本地口径，
  跨时区承诺时间不再各算各的。
- 破线判定只承认经过工作日/节假日偏移修正后的承诺时刻，禁止按自然日直接相减破线。

本模块只依赖标准库（zoneinfo 在 Python 3.9+ 内置）。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.store import store

BATCH_MODULE = "calendar_rule_batch"

# 预警水位常量（跨模块共享，水位字符串只允许这三种）
WATER_NORMAL = "正常"
WATER_WARNING = "即将破线"
WATER_BREACHED = "已破线"

DEFAULT_TIMEZONE = "Asia/Shanghai"
# 老数据没有预警余量字段时的兜底口径，避免既有记录解析失败
DEFAULT_WARN_MARGIN_MINUTES = 60
# 裸日期（只有年月日）的承诺时间按当地工作日的下班时刻解释
DEFAULT_BARE_DATE_CLOCK = (23, 59, 59)
# 工作日历换算允许的最大保护跨度，防止异常数据导致死循环
MAX_SHIFT_DAYS = 366


def parse_timezone(tz_name: str | None):
    """把时区名称转成 ZoneInfo；非法名称回退默认时区而不是抛异常。"""
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            pass
    return ZoneInfo(DEFAULT_TIMEZONE)


def parse_datetime(value: Any, *, tz_name: str | None = None) -> datetime | None:
    """把多种写法的时间值解析成「带时区」的 datetime。

    支持：裸日期 ``2026-11-20``、不带时区的 ``2026-11-20 18:00:00``、
    带偏移的 ISO 串 ``...+08:00`` 以及 ``Z`` 结尾的 UTC 串。
    裸日期按批次时区当天下班时刻解释；不带时区的时间按批次时区解释。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        tz = parse_timezone(tz_name)
        return datetime(value.year, value.month, value.day,
                        *DEFAULT_BARE_DATE_CLOCK, tzinfo=tz)
    else:
        text = str(value).strip()
        if not text:
            return None
        # 裸日期串（YYYY-MM-DD）：按批次时区当天下班时刻解释
        if len(text) == 10:
            try:
                bare = date.fromisoformat(text)
            except ValueError:
                return None
            tz = parse_timezone(tz_name)
            return datetime(bare.year, bare.month, bare.day,
                            *DEFAULT_BARE_DATE_CLOCK, tzinfo=tz)
        text = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            # 兼容 ``2026/11/20 18:00`` 这类写法
            for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
    tz = parse_timezone(tz_name)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt


def to_utc(dt: datetime) -> datetime:
    """统一时区口径：内部比较、落库一律走 UTC。"""
    return dt.astimezone(timezone.utc)


def to_local(dt: datetime, tz_name: str | None) -> datetime:
    """对外输出时换回批次标准时区，保证展示与承诺口径一致。"""
    return dt.astimezone(parse_timezone(tz_name))


def iso_utc(dt: datetime) -> str:
    return to_utc(dt).isoformat()


def _parse_batch_date(value: Any) -> date | None:
    dt = parse_datetime(value, tz_name=DEFAULT_TIMEZONE)
    return dt.date() if dt else None


def list_active_batches() -> list[dict[str, Any]]:
    """只取已生效批次；草稿、已停用批次不参与口径判定。"""
    rows = store.rows(BATCH_MODULE)
    return [row for row in rows if row.get("状态", "已生效") == "已生效"]


def select_batch(on_date: date) -> dict[str, Any] | None:
    """选某一日期适用的规则批次：区间命中后，到达时间最晚者胜。

    晚到批次代表更新版本的工作日/节假日口径，因此同区间冲突时由它覆盖旧批次，
    这正是「不同日期规则批次冲突时采用哪一版」的唯一裁决入口。
    """
    candidates: list[dict[str, Any]] = []
    for row in list_active_batches():
        start = _parse_batch_date(row.get("生效起始日期"))
        end = _parse_batch_date(row.get("生效截止日期"))
        if start and end and start <= on_date <= end:
            candidates.append(row)
    if not candidates:
        return None

    def sort_key(row: dict[str, Any]):
        arrived = parse_datetime(row.get("到达时间"), tz_name="UTC")
        arrived_ts = arrived.timestamp() if arrived else 0.0
        return arrived_ts, int(row.get("id", 0))

    return max(candidates, key=sort_key)


def is_workday(on_date: date, batch: dict[str, Any]) -> bool:
    """工作日判定：补班日优先于节假日，最后才看星期集合。"""
    iso_day = on_date.isoformat()
    makeup = set(batch.get("补班日列表") or [])
    holidays = set(batch.get("节假日列表") or [])
    if iso_day in makeup:
        return True
    if iso_day in holidays:
        return False
    workday_set = set(batch.get("工作日集合") or [1, 2, 3, 4, 5])
    return on_date.isoweekday() in workday_set


def shift_commitment(
    dt_local: datetime,
    batch: dict[str, Any],
    *,
    shift_mode: str | None,
) -> tuple[datetime, dict[str, Any]]:
    """承诺时刻落在非工作日时按节假日口径偏移。

    顺延 → 下一个工作日；提前 → 上一个工作日；其余取值不偏移。
    返回偏移后的本地时刻与判定说明（供预警结果留痕）。
    """
    original_date = dt_local.date()
    tz = dt_local.tzinfo or parse_timezone(batch.get("标准时区"))
    detail = {
        "原承诺日期": original_date.isoformat(),
        "是否工作日": is_workday(original_date, batch),
        "节假日偏移口径": shift_mode or "不偏移",
        "偏移工作日数": 0,
    }
    if detail["是否工作日"] or shift_mode not in ("顺延", "提前"):
        detail["偏移后承诺日期"] = original_date.isoformat()
        return dt_local, detail

    step = 1 if shift_mode == "顺延" else -1
    cursor = original_date
    moved = 0
    for _ in range(MAX_SHIFT_DAYS):
        cursor = cursor + timedelta(days=step)
        moved += 1
        if is_workday(cursor, batch):
            break
    shifted = datetime(cursor.year, cursor.month, cursor.day,
                       dt_local.hour, dt_local.minute, dt_local.second,
                       tzinfo=tz)
    detail["偏移工作日数"] = moved
    detail["偏移后承诺日期"] = cursor.isoformat()
    return shifted, detail


def workday_minutes_between(
    start_utc: datetime,
    end_utc: datetime,
    batch: dict[str, Any],
) -> int:
    """按工作日历统计两个 UTC 时刻之间落在工作日内的分钟数。

    非工作日（周末/节假日）整天不计入，用于输出「工作日口径余量」，
    与自然日相减的旧口径明确区分。
    """
    if end_utc <= start_utc:
        return 0
    tz = parse_timezone(batch.get("标准时区"))
    start_local = start_utc.astimezone(tz)
    end_local = end_utc.astimezone(tz)
    total = 0.0
    day = start_local.date()
    guard = 0
    while day <= end_local.date() and guard <= MAX_SHIFT_DAYS:
        guard += 1
        if is_workday(day, batch):
            day_start = datetime(day.year, day.month, day.day, tzinfo=tz)
            day_end = day_start + timedelta(days=1)
            window_start = max(start_local, day_start)
            window_end = min(end_local, day_end)
            if window_end > window_start:
                total += (window_end - window_start).total_seconds() / 60.0
        day = day + timedelta(days=1)
    return int(total)
