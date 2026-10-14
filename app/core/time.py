from datetime import datetime, timedelta, timezone


# 中国标准时间全年固定为 UTC+08:00，不依赖系统 tzdata 数据包。
SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def now_shanghai() -> datetime:
    """返回不带时区的北京时间，适配 MySQL DATETIME 字段。"""

    return datetime.now(SHANGHAI_TZ).replace(tzinfo=None, microsecond=0)
