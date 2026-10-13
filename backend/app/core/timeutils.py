"""
时间序列化工具

背景：模型列的默认值是 `datetime.utcnow()`（naive UTC，库里存的是 UTC 墙上时间），
直接 `.isoformat()` 出来的字符串**不带时区标记**。浏览器 `new Date('2026-10-13T07:42:49')`
会按**本地时区**解析，于是在 UTC+8 环境里全部偏早 8 小时（后台列表显示成「8 小时前」）。

约定：出 API 的时间一律带 UTC 标记（`...Z`），由前端自行换算本地时间。
"""
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo


def to_utc_iso(value: Any) -> Optional[str]:
    """把 naive UTC datetime 输出成带 `Z` 的 ISO 8601 字符串。

    - None → None
    - naive datetime → 视为 UTC
    - aware datetime → 归一到 UTC
    - 非 datetime（例如已经是字符串）→ 原样 str()
    """
    if value is None:
        return None
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_now_iso() -> str:
    """当前时刻的 UTC ISO 字符串（带 `Z`）。"""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# 业务时区：与 Celery 配置、用量策略里的 quota_reset_timezone 保持一致
BUSINESS_TIMEZONE = "Asia/Shanghai"


def to_business_tz(value: Optional[datetime]) -> Optional[datetime]:
    """把库里存的 naive UTC 时间换算到业务时区（Asia/Shanghai）。

    用于**服务端渲染**的页面：那边不能依赖浏览器换算，必须先把 UTC 换成用户看到的本地时间，
    否则跨月/跨日时会显示成前一天或上个月（例如 01:00 CST 的记录会落回前一天）。
    """
    if value is None:
        return None
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo(BUSINESS_TIMEZONE))
