"""Fresh, timezone-aware clock context; never persist ephemeral 'now' as memory."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = 'Asia/Shanghai'

def is_valid_timezone(name):
    if not isinstance(name, str) or not name.strip(): return False
    try: ZoneInfo(name); return True
    except (ZoneInfoNotFoundError, ValueError): return False

def valid_timezone(name):
    return name if is_valid_timezone(name) else DEFAULT_TIMEZONE

def local_datetime(name=DEFAULT_TIMEZONE, timestamp=None):
    return datetime.fromtimestamp(timestamp, ZoneInfo(valid_timezone(name))) if timestamp is not None else datetime.now(ZoneInfo(valid_timezone(name)))

def clock_context(name=DEFAULT_TIMEZONE, timestamp=None):
    now = local_datetime(name, timestamp)
    day = '一二三四五六日'[now.weekday()]
    return (f'[会话时间] 本轮实时刷新\n当前时间：{now.isoformat(timespec="seconds")}，星期{day}；'
            f'时区：{valid_timezone(name)}；UTC：{now.astimezone(timezone.utc).isoformat(timespec="seconds")}。'
            f'昨天：{(now-timedelta(days=1)).date()}；今天：{now.date()}；明天：{(now+timedelta(days=1)).date()}。'
            '相对日期以本轮时间为准；若用户明确提供所在地/时区则按其时区理解，有歧义先确认。'
            '此时间来自主机时钟，不代表已获取天气、新闻、位置或用户的当前生活状态；相关实时事实需联网核实。')
