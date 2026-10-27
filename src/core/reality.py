"""Fresh, timezone-aware clock context; never persist ephemeral 'now' as memory."""
import math
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
            '只有最后一份[会话时间]快照表示当前处理时刻，前面的时钟快照属于当时。'
            '历史直述中的今天、昨天、刚才默认按各条原发言日期理解，不套用本轮日期；引述、转述或文中明确另设日期优先，不把引用强行套到发送日。'
            '发送时间缺失时只知道接收时间，日期未知的旧记录不推断为昨天或刚才。'
            '若用户明确提供所在地/时区则按其时区理解，有歧义先确认。'
            '此时间来自主机时钟，不代表已获取天气、新闻、位置或用户的当前生活状态；相关实时事实需联网核实。')


def valid_timestamp(value):
    """Finite Unix seconds, never a guessed date or the file's modification time."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            return None
        datetime.fromtimestamp(value, timezone.utc)
        datetime.fromtimestamp(value + 3*86400, timezone.utc)  # Leave room for timezone offsets.
    except (OverflowError, OSError, ValueError):
        return None
    return value


def message_timing(msg, received_at):
    """Keep each original envelope's source clock, including batches across midnight."""
    received_at = valid_timestamp(received_at)
    sources = msg.batch_messages or [{'msg_id': msg.msg_id, 'timestamp': msg.timestamp, 'content':msg.content}]
    times = []
    for source in sources:
        source = source if isinstance(source,dict) else {}
        raw = source.get('timestamp')
        try:
            at = valid_timestamp(raw / 1000) if not isinstance(raw, bool) and isinstance(raw, (int, float)) else None
        except (OverflowError,ValueError):
            at = None
        # A broken platform clock is not evidence that an event happened in the future.
        if at is not None and received_at is not None and at > received_at + 300:
            at = None
        times.append({'source_id': f"{msg.platform}:{msg.chat_type}:{msg.chat_id}:{msg.sender_id}:{source.get('msg_id', msg.msg_id)}",
                      'at': at})
    known = [row['at'] for row in times if row['at'] is not None]
    source = 'message' if len(known) == len(times) else ('partial' if known else 'received')
    at = min(known) if known else received_at
    return {'ts': at or 0, 'user_at': at, 'user_end_at': max(known) if known else at,
            'received_at': received_at, 'user_time_source': source, 'source_times': times}



def turn_time_context(turn, name=DEFAULT_TIMEZONE):
    """Immutable absolute clock annotations, separate from verbatim message bodies."""
    zone = valid_timezone(name)
    stamp = lambda value: local_datetime(zone, value).isoformat(timespec='seconds')
    user = valid_timestamp(turn.get('user_at'))
    legacy = valid_timestamp(turn.get('ts'))
    assistant = valid_timestamp(turn.get('assistant_at'))
    source = turn.get('user_time_source')
    parts = ['[消息时间记录] 时区：' + zone + '。']
    if turn.get('user'):
        if user is not None:
            label = '用户发送时间' if source in ('message', 'partial') else '用户来话接收时间（原发送时间未知）'
            end = valid_timestamp(turn.get('user_end_at'))
            value = stamp(user) + (' ～ ' + stamp(end) if end is not None and end > user else '')
            parts.append(label + '：' + value + '。')
        elif legacy is not None:
            parts.append('旧版记录时间：' + stamp(legacy) + '（未区分收发时刻，不据此断定事件发生日期）。')
        else:
            parts.append('用户原发言日期未知。')
        # Date groups retain midnight boundaries without repeating long IDs or bodies.
        rows = turn.get('source_times')
        if isinstance(rows, list) and len(rows) > 1:
            groups = []
            for index, row in enumerate(rows, 1):
                at = valid_timestamp(row.get('at')) if isinstance(row, dict) else None
                date = local_datetime(zone, at).date().isoformat() if at is not None else '日期未知'
                if groups and groups[-1][2] == date:
                    groups[-1][1] = index
                else:
                    groups.append([index, index, date])
            parts.append('原消息日期：' + '；'.join(('第' + str(a) + ('–' + str(b) if a != b else '') + '条=' + date) for a, b, date in groups) + '。')
    if turn.get('assistant'):
        if assistant is not None:
            parts.append('助手发送时间：'+stamp(assistant)+'。')
        elif not turn.get('user') and legacy is not None:
            parts.append('旧版助手回复记录时间：'+stamp(legacy)+'（未保留精确发送时间）。')
        else:
            parts.append('助手发送日期未知（不借用用户或本轮时间）。')
    if not turn.get('user') and not turn.get('assistant'):
        parts.append('日期未知。')
    if any(word in str(turn.get('user',''))+str(turn.get('assistant','')) for word in ('前天','昨天','今天','明天','后天','刚才')):
        parts.append('相对词按该原话语境解析；引述或另设日期优先，记录时间不等于事件时间。')
    return ''.join(parts)


def summary_time_context(turns, name=DEFAULT_TIMEZONE):
    known = [at for turn in turns for key in ('user_at', 'user_end_at', 'assistant_at', 'ts')
             if (at := valid_timestamp(turn.get(key))) is not None]
    if known:
        zone = valid_timezone(name)
        dates = ' ～ '.join(local_datetime(zone, at).isoformat(timespec='seconds') for at in (min(known), max(known)))
        scope = '本次可核实原始记录的时间范围：' + dates + '；时区：' + zone + '。'
    else:
        scope = '原始记录日期未知。'
    return ('[历史摘要时间说明] ' + scope +
            '只采用各事实明确保留的日期；未标注日期的片段时间未知，不以摘要生成、升级或当前时间补齐，不当作昨天或刚才。')
