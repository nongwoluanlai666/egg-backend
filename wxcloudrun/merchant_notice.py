import copy
import hashlib
import hmac
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dt_time, timedelta, timezone as dt_timezone
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from django.conf import settings
from django.db import close_old_connections, transaction

from wxcloudrun.models import (
    MerchantNoticeDispatchJob,
    MerchantNoticeJobState,
    MerchantNoticeSendLog,
    MerchantNoticeSubscription,
    MerchantSnapshot,
)


logger = logging.getLogger('log')

DEFAULT_SPECIAL_KEYWORDS = ('炫彩', '棱镜球', '同乘', '祝福项坠', '残缺魔镜', '适格钥匙', '能力钥匙')
DEFAULT_SELECTED_GOODS = ('炫彩蛋', '棱镜球', '祝福项坠', '黑白炫彩蛋', '赛季炫彩蛋', '残缺魔镜', '适格钥匙', '能力钥匙')
REQUIRED_SPECIAL_KEYWORDS = ('残缺魔镜', '适格钥匙', '能力钥匙')
REQUIRED_DEFAULT_SELECTED_GOODS = ('残缺魔镜', '适格钥匙', '能力钥匙')
DEFAULT_NOTICE_ACTION = '打开蛋查查'
DEV_SELF_TEST_ITEM_NAMES = ('炫彩蛋', '棱镜球')
DEV_SELF_TEST_ACTION = '返回蛋查查'
ROUND_START_HOURS = (8, 12, 16, 20)
MERCHANT_PREFERRED_SOURCE_ONLY_START_MINUTE = 1
MERCHANT_PREFERRED_SOURCE_ONLY_END_MINUTE = 5
MERCHANT_SOURCE_PRIMARY = 'primary'
MERCHANT_SOURCE_BACKUP = 'backup'
WECHAT_STABLE_TOKEN_URL = 'https://api.weixin.qq.com/cgi-bin/stable_token'
WECHAT_SUBSCRIBE_SEND_URL = 'https://api.weixin.qq.com/cgi-bin/message/subscribe/send'
WATCH_JOB_KEY = 'merchant_watch'
MANUAL_SNAPSHOT_FINGERPRINT_PREFIX = 'manual-'
SNAPSHOT_DISPATCH_JOB_PREFIX = 'snapshot:'
MANUAL_DISPATCH_JOB_PREFIX = 'manual:'
SERVICE_REQUIRED_CONFIGS = (
    ('WECHAT_APP_ID', '微信小程序 AppID'),
    ('WECHAT_APP_SECRET', '微信小程序 AppSecret'),
    ('MERCHANT_NOTIFY_TEMPLATE_ID', '订阅消息模板 ID'),
)

_CURRENT_PAYLOAD_CACHE = {
    'payload': None,
    'expires_at': 0.0,
}
_DISPLAY_PAYLOAD_CACHE = {
    'payload': None,
    'expires_at': 0.0,
}
_ACCESS_TOKEN_CACHE = {
    'token': '',
    'expires_at': 0.0,
}
_ACCESS_TOKEN_CACHE_LOCK = threading.Lock()
_WECHAT_SESSION_LOCAL = threading.local()
GOODS_NAME_ALIASES = {
    '炫彩蛋': '炫彩精灵蛋',
}
PUBLIC_GOODS_NAME_ALIASES = {
    '炫彩精灵蛋': '炫彩蛋',
}
WECHAT_ACCESS_TOKEN_RETRYABLE_ERROR_CODES = {'40001', '40014', '42001'}


class MerchantNoticeError(Exception):
    pass


class MerchantNoticeConfigurationError(MerchantNoticeError):
    pass


class MerchantNoticePermissionError(MerchantNoticeError):
    pass


class MerchantNoticeValidationError(MerchantNoticeError):
    pass


class MerchantNoticeSourceError(MerchantNoticeError):
    pass


class MerchantNoticeSourceNotReadyError(MerchantNoticeSourceError):
    pass


def is_loopback_ip(value):
    ip = normalize_text(value, 64)
    return ip in {'127.0.0.1', '::1', 'localhost'}


def get_local_timezone():
    timezone_name = str(getattr(settings, 'MERCHANT_NOTICE_TIMEZONE', 'Asia/Shanghai') or 'Asia/Shanghai').strip()
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        return dt_timezone(timedelta(hours=8))


def get_local_now():
    return datetime.now(get_local_timezone())


def make_naive_local(value):
    if not value:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(get_local_timezone()).replace(tzinfo=None)


def parse_naive_local(value):
    if not value:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=get_local_timezone())
    return value.astimezone(get_local_timezone())


def format_iso_datetime(value):
    if not value:
        return ''
    normalized = parse_naive_local(value)
    return normalized.strftime('%Y-%m-%dT%H:%M:%S')


def normalize_text(value, max_length=255):
    if value is None:
        return ''
    normalized = ' '.join(str(value).replace('\u3000', ' ').split())
    return normalized[:max_length]


def parse_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def append_missing_text(items, additions, max_length=64):
    results = []
    seen = set()
    for item in list(items or []) + list(additions or []):
        normalized = normalize_text(item, max_length)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        results.append(normalized)
    return results


def get_trigger_guard_seconds():
    return max(parse_int(getattr(settings, 'MERCHANT_NOTIFY_TRIGGER_GUARD_SECONDS', 1800), 1800), 0)


def get_rewarded_increment_step():
    return max(parse_int(getattr(settings, 'MERCHANT_NOTICE_DAILY_REWARDED_STEP', 30), 30), 1)


def get_special_keywords():
    raw = str(getattr(settings, 'MERCHANT_NOTIFY_SPECIAL_KEYWORDS', '') or '').strip()
    if not raw:
        return list(DEFAULT_SPECIAL_KEYWORDS)

    keywords = []
    for item in raw.replace('，', ',').split(','):
        cleaned = normalize_text(item, 32)
        if cleaned and cleaned not in keywords:
            keywords.append(cleaned)
    return append_missing_text(keywords or list(DEFAULT_SPECIAL_KEYWORDS), REQUIRED_SPECIAL_KEYWORDS, 32)


def normalize_goods_name(value):
    normalized = normalize_text(value, 64)
    if not normalized:
        return ''
    return GOODS_NAME_ALIASES.get(normalized, normalized)


def export_goods_name(value):
    normalized = normalize_text(value, 64)
    if not normalized:
        return ''
    return PUBLIC_GOODS_NAME_ALIASES.get(normalized, normalized)


def export_goods_names(items):
    results = []
    seen = set()
    for item in items or []:
        exported = export_goods_name(item)
        if not exported or exported in seen:
            continue
        seen.add(exported)
        results.append(exported)
    return results


def dedupe_goods_names(items):
    results = []
    seen = set()
    for item in items or []:
        normalized = normalize_goods_name(item)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        results.append(normalized)
    return results


def get_default_selected_goods():
    raw = str(getattr(settings, 'MERCHANT_NOTIFY_DEFAULT_SELECTED_GOODS', '') or '').strip()
    if not raw:
        return list(DEFAULT_SELECTED_GOODS)

    configured = dedupe_goods_names(raw.replace('，', ',').split(',')) or list(DEFAULT_SELECTED_GOODS)
    return dedupe_goods_names(configured + list(REQUIRED_DEFAULT_SELECTED_GOODS))


def sort_goods_by_notice_priority(items):
    normalized = dedupe_goods_names(items)
    if len(normalized) <= 1:
        return normalized

    priority_index = {
        name: index
        for index, name in enumerate(get_default_selected_goods())
    }
    fallback_offset = len(priority_index)

    return [
        item for _index, item in sorted(
            enumerate(normalized),
            key=lambda pair: (
                priority_index.get(pair[1], fallback_offset + pair[0]),
                pair[0],
            ),
        )
    ]


def serialize_goods_names(items):
    return json.dumps(dedupe_goods_names(items), ensure_ascii=False)


def parse_selected_goods_value(value):
    if value in (None, ''):
        return []

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith('['):
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = []
        else:
            parsed = text.replace('，', ',').split(',')
    elif isinstance(value, (list, tuple, set)):
        parsed = list(value)
    else:
        parsed = []

    return dedupe_goods_names(parsed)


def has_special_keyword(name):
    item_name = normalize_text(name, 64)
    return any(keyword in item_name for keyword in get_special_keywords())


def get_merchant_source_priority_list():
    raw = str(getattr(settings, 'MERCHANT_SOURCE_PRIORITY', '') or '').strip()
    if not raw:
        raw = f'{MERCHANT_SOURCE_BACKUP},{MERCHANT_SOURCE_PRIMARY}'

    source_names = []
    for item in raw.replace('，', ',').split(','):
        normalized = normalize_text(item, 32).lower()
        if normalized in {MERCHANT_SOURCE_PRIMARY, MERCHANT_SOURCE_BACKUP} and normalized not in source_names:
            source_names.append(normalized)

    if not source_names:
        return [MERCHANT_SOURCE_BACKUP, MERCHANT_SOURCE_PRIMARY]
    return source_names


def get_merchant_watch_source_priority_list():
    raw = str(getattr(settings, 'MERCHANT_WATCH_SOURCE_PRIORITY', '') or '').strip()
    if not raw:
        raw = f'{MERCHANT_SOURCE_BACKUP},{MERCHANT_SOURCE_PRIMARY}'

    source_names = []
    for item in raw.replace('\uff0c', ',').split(','):
        normalized = normalize_text(item, 32).lower()
        if normalized in {MERCHANT_SOURCE_PRIMARY, MERCHANT_SOURCE_BACKUP} and normalized not in source_names:
            source_names.append(normalized)

    if not source_names:
        return [MERCHANT_SOURCE_BACKUP, MERCHANT_SOURCE_PRIMARY]
    return source_names


def build_absolute_image_url(path):
    value = str(path or '').strip()
    if not value:
        return ''
    if value.startswith('http://') or value.startswith('https://'):
        return value
    return urljoin(f"{settings.ROCO_UPSTREAM_BASE_URL.rstrip('/')}/", value.lstrip('/'))


def build_round_window(slot_date, round_number):
    base_date = slot_date if isinstance(slot_date, date) else datetime.strptime(str(slot_date), '%Y-%m-%d').date()
    round_index = max(1, min(parse_int(round_number, 1), len(ROUND_START_HOURS))) - 1
    start_hour = ROUND_START_HOURS[round_index]
    start_at = datetime.combine(base_date, dt_time(hour=start_hour), tzinfo=get_local_timezone())
    if round_index < len(ROUND_START_HOURS) - 1:
        end_hour = ROUND_START_HOURS[round_index + 1]
        end_at = datetime.combine(base_date, dt_time(hour=end_hour), tzinfo=get_local_timezone())
        return start_at, end_at
    end_at = datetime.combine(base_date + timedelta(days=1), dt_time(hour=ROUND_START_HOURS[0]), tzinfo=get_local_timezone())
    return start_at, end_at


def format_round_window(slot_date, round_number):
    start_at, end_at = build_round_window(slot_date, round_number)
    start_text = start_at.strftime('%m-%d %H:%M')
    if start_at.date() == end_at.date():
        return f'{start_text}-{end_at.strftime("%H:%M")}'
    return f'{start_text}-次日{end_at.strftime("%H:%M")}'


def format_round_notice_time(slot_date, round_number):
    start_at, _ = build_round_window(slot_date, round_number)
    return start_at.strftime('%m-%d %H:%M')


def format_round_label(round_number, total_rounds):
    return f'第 {round_number} / {total_rounds} 场'


def format_next_refresh_label(value):
    if not value:
        return ''
    normalized = parse_naive_local(value)
    return normalized.strftime('%m-%d %H:%M')


def parse_slot_date(value):
    text = normalize_text(value, 16)
    if not text:
        return get_local_now().date()
    try:
        return datetime.strptime(text, '%Y-%m-%d').date()
    except ValueError:
        return get_local_now().date()


def parse_next_refresh_at(value, slot_date, round_number):
    timestamp = parse_int(value, 0)
    if timestamp > 0:
        return datetime.fromtimestamp(timestamp, tz=get_local_timezone())
    return build_round_window(slot_date, round_number)[1]


def parse_item_availability_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return parse_naive_local(value)

    text = normalize_text(value, 32)
    if not text:
        return None

    try:
        return parse_naive_local(datetime.fromisoformat(text))
    except ValueError:
        pass

    for date_format in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S'):
        try:
            return parse_naive_local(datetime.strptime(text, date_format))
        except ValueError:
            continue
    return None


def attach_item_availability(item, start_at=None, end_at=None):
    normalized_item = dict(item)
    if start_at:
        normalized_item['availableStartAt'] = format_iso_datetime(start_at)
    if end_at:
        normalized_item['availableEndAt'] = format_iso_datetime(end_at)
    return normalized_item


def compute_payload_fingerprint(payload):
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def normalize_source_item(item):
    name = normalize_goods_name(item.get('name'))
    is_special = has_special_keyword(name)
    source_highlight = bool(item.get('is_highlight'))
    return {
        'name': name,
        'image': build_absolute_image_url(item.get('image')),
        'price': max(parse_int(item.get('price'), 0), 0),
        'purchaseLimit': max(parse_int(item.get('purchase_limit'), 0), 0),
        'isSpecial': is_special,
        'isHighlight': bool(is_special or source_highlight),
        'sourceHighlight': source_highlight,
    }


def normalize_primary_source_payload(raw_payload):
    slot_date = parse_slot_date(raw_payload.get('slot_date'))
    round_number = max(parse_int(raw_payload.get('round'), 1), 1)
    total_rounds = max(parse_int(raw_payload.get('total_rounds'), 4), 1)
    round_start_at, round_end_at = build_round_window(slot_date, round_number)
    items = [
        attach_item_availability(normalize_source_item(item), round_start_at, round_end_at)
        for item in (raw_payload.get('items') or [])
        if isinstance(item, dict) and normalize_goods_name(item.get('name'))
    ]
    next_refresh_at = parse_next_refresh_at(raw_payload.get('next_refresh_ts'), slot_date, round_number)
    special_item_names = [item['name'] for item in items if item['isSpecial']]
    fingerprint = compute_payload_fingerprint({
        'slotDate': slot_date.isoformat(),
        'round': round_number,
        'totalRounds': total_rounds,
        'items': items,
    })

    return {
        'slotDate': slot_date.isoformat(),
        'round': round_number,
        'totalRounds': total_rounds,
        'roundLabel': format_round_label(round_number, total_rounds),
        'timeWindowLabel': format_round_window(slot_date, round_number),
        'nextRefreshAt': format_iso_datetime(next_refresh_at),
        'nextRefreshLabel': format_next_refresh_label(next_refresh_at),
        'sourceUpdatedAt': normalize_text(raw_payload.get('updated_at'), 32),
        'items': items,
        'hasSpecialHit': bool(special_item_names),
        'specialItemNames': special_item_names,
        'fingerprint': fingerprint,
    }


def parse_backup_timestamp_ms(value):
    try:
        timestamp_ms = int(value)
    except (TypeError, ValueError):
        return None
    if timestamp_ms <= 0:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=get_local_timezone())


def normalize_backup_source_item(item):
    name = normalize_goods_name(item.get('name'))
    is_special = has_special_keyword(name)
    start_at = parse_backup_timestamp_ms(item.get('start_time'))
    end_at = parse_backup_timestamp_ms(item.get('end_time'))
    return {
        'name': name,
        'image': build_absolute_image_url(item.get('icon_url')),
        'price': 0,
        'purchaseLimit': 0,
        'isSpecial': is_special,
        'isHighlight': bool(is_special),
        'sourceHighlight': False,
        'startAt': start_at,
        'endAt': end_at,
    }


def find_backup_merchant_activity(raw_payload):
    data = raw_payload.get('data') if isinstance(raw_payload.get('data'), dict) else {}
    activities = data.get('merchantActivities') or []
    if not isinstance(activities, list):
        return {}

    for activity in activities:
        if isinstance(activity, dict) and normalize_text(activity.get('name'), 32) == '远行商人':
            return activity

    for activity in activities:
        if isinstance(activity, dict):
            return activity
    return {}


def find_current_backup_merchant_activity(raw_payload):
    data = raw_payload.get('data') if isinstance(raw_payload.get('data'), dict) else {}
    activities = data.get('merchantActivities') or []
    if not isinstance(activities, list):
        return {}

    merchant_activities = [
        activity for activity in activities
        if isinstance(activity, dict) and normalize_text(activity.get('name'), 32) == '远行商人'
    ]
    candidates = merchant_activities or [
        activity for activity in activities
        if isinstance(activity, dict)
    ]
    if not candidates:
        return {}

    current_time = get_local_now()
    active_candidates = []
    for activity in candidates:
        start_at = parse_backup_timestamp_ms(activity.get('start_time'))
        end_at = parse_backup_timestamp_ms(activity.get('end_time'))
        if start_at and start_at <= current_time and (not end_at or current_time < end_at):
            active_candidates.append(activity)
    if active_candidates:
        return sorted(
            active_candidates,
            key=lambda activity: parse_int(activity.get('start_time'), 0),
        )[0]

    current_date_text = current_time.date().isoformat()
    same_day_candidates = [
        activity for activity in candidates
        if normalize_text(activity.get('start_date'), 16) == current_date_text
    ]
    if same_day_candidates:
        return sorted(
            same_day_candidates,
            key=lambda activity: parse_int(activity.get('start_time'), 0),
        )[0]

    return candidates[0]


def infer_round_number_from_start(start_at, slot_date):
    if start_at:
        for round_number in range(1, len(ROUND_START_HOURS) + 1):
            round_start_at, _ = build_round_window(slot_date, round_number)
            if int(abs((start_at - round_start_at).total_seconds())) <= 60:
                return round_number

    now = start_at or get_local_now()
    for round_number in range(1, len(ROUND_START_HOURS) + 1):
        round_start_at, round_end_at = build_round_window(slot_date, round_number)
        if round_start_at <= now < round_end_at:
            return round_number
    if now < build_round_window(slot_date, 1)[0]:
        return 1
    return len(ROUND_START_HOURS)


def infer_round_number_from_now(slot_date, now=None):
    current_time = now or get_local_now()
    for round_number in range(1, len(ROUND_START_HOURS) + 1):
        round_start_at, round_end_at = build_round_window(slot_date, round_number)
        if round_start_at <= current_time < round_end_at:
            return round_number
    if current_time < build_round_window(slot_date, 1)[0]:
        return 1
    return len(ROUND_START_HOURS)


def is_same_round_start(value, round_start_at):
    if not value or not round_start_at:
        return False
    return int(abs((value - round_start_at).total_seconds())) <= 60


def is_backup_round_specific_item(item, round_start_at, round_end_at):
    start_at = item.get('startAt')
    end_at = item.get('endAt')
    if not is_same_round_start(start_at, round_start_at):
        return False
    if not end_at:
        return False
    return end_at <= round_end_at + timedelta(seconds=60)


def select_backup_round_items(normalized_items, slot_date, now=None):
    current_time = now or get_local_now()
    all_round_items = [
        item for item in normalized_items
        if not item.get('startAt') and not item.get('endAt')
    ]
    timed_items = [
        item for item in normalized_items
        if item.get('startAt')
    ]

    active_timed_items = [
        item for item in timed_items
        if item['startAt'] <= current_time and (not item.get('endAt') or current_time < item['endAt'])
    ]
    expected_round_number = infer_round_number_from_now(slot_date, now=current_time)
    expected_start_at, expected_end_at = build_round_window(slot_date, expected_round_number)
    round_specific_items = [
        item for item in active_timed_items
        if is_backup_round_specific_item(item, expected_start_at, expected_end_at)
    ]
    if round_specific_items:
        return expected_round_number, expected_start_at, all_round_items + active_timed_items

    return None, None, all_round_items


def normalize_backup_source_payload(raw_payload):
    if parse_int(raw_payload.get('code'), -1) != 0:
        raise MerchantNoticeSourceError(f'远行商人备用源返回异常: {raw_payload}')

    activity = find_current_backup_merchant_activity(raw_payload)
    if not activity:
        raise MerchantNoticeSourceError('远行商人备用源未返回 merchantActivities.远行商人 数据')

    slot_date = parse_slot_date(activity.get('start_date'))
    normalized_items = [
        normalize_backup_source_item(item)
        for item in (activity.get('get_props') or [])
        if isinstance(item, dict) and normalize_text(item.get('name'), 64)
    ]
    round_number, selected_start_at, selected_items = select_backup_round_items(normalized_items, slot_date)
    if not selected_start_at:
        raise MerchantNoticeSourceNotReadyError('backup merchant source current round is not ready')
    total_rounds = len(ROUND_START_HOURS)
    _, next_refresh_at = build_round_window(slot_date, round_number)
    source_updated_at = parse_backup_timestamp_ms(activity.get('created_at')) or get_local_now()
    items = []
    for item in selected_items:
        normalized_item = dict(item)
        item_start_at = normalized_item.pop('startAt', None) or selected_start_at
        item_end_at = normalized_item.pop('endAt', None) or next_refresh_at
        normalized_item = attach_item_availability(normalized_item, item_start_at, item_end_at)
        items.append(normalized_item)

    special_item_names = [item['name'] for item in items if item['isSpecial']]
    fingerprint = compute_payload_fingerprint({
        'slotDate': slot_date.isoformat(),
        'round': round_number,
        'totalRounds': total_rounds,
        'items': items,
    })

    return {
        'slotDate': slot_date.isoformat(),
        'round': round_number,
        'totalRounds': total_rounds,
        'roundLabel': format_round_label(round_number, total_rounds),
        'timeWindowLabel': format_round_window(slot_date, round_number),
        'nextRefreshAt': format_iso_datetime(next_refresh_at),
        'nextRefreshLabel': format_next_refresh_label(next_refresh_at),
        'sourceUpdatedAt': normalize_text(source_updated_at.strftime('%Y-%m-%d %H:%M:%S'), 32),
        'items': items,
        'hasSpecialHit': bool(special_item_names),
        'specialItemNames': special_item_names,
        'fingerprint': fingerprint,
    }


def build_primary_source_headers():
    return {
        'Accept': 'application/json,text/plain,*/*',
        'Referer': str(getattr(settings, 'MERCHANT_SOURCE_REFERER', '') or '').strip(),
        'User-Agent': str(getattr(settings, 'MERCHANT_SOURCE_USER_AGENT', '') or '').strip(),
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
    }


def build_backup_source_headers():
    return {
        'Accept': 'application/json,text/plain,*/*',
        'Referer': str(getattr(settings, 'MERCHANT_BACKUP_SOURCE_REFERER', '') or '').strip(),
        'User-Agent': str(getattr(settings, 'MERCHANT_SOURCE_USER_AGENT', '') or '').strip(),
        'X-API-Key': str(getattr(settings, 'MERCHANT_BACKUP_SOURCE_API_KEY', '') or '').strip(),
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
    }


def fetch_primary_source_payload():
    try:
        response = requests.get(
            settings.MERCHANT_SOURCE_URL,
            headers=build_primary_source_headers(),
            timeout=float(getattr(settings, 'MERCHANT_NOTIFY_FETCH_TIMEOUT_SECONDS', 8)),
        )
    except requests.RequestException as error:
        raise MerchantNoticeSourceError(f'远行商人主源请求失败: {error}') from error

    if response.status_code < 200 or response.status_code >= 300:
        raise MerchantNoticeSourceError(f'远行商人主源返回异常状态码: {response.status_code}')

    try:
        raw_payload = response.json()
    except ValueError as error:
        raise MerchantNoticeSourceError('远行商人主源返回了非 JSON 数据') from error

    return normalize_primary_source_payload(raw_payload if isinstance(raw_payload, dict) else {})


def fetch_backup_source_payload():
    api_key = str(getattr(settings, 'MERCHANT_BACKUP_SOURCE_API_KEY', '') or '').strip()
    if not api_key:
        raise MerchantNoticeSourceError('远行商人备用源未配置 MERCHANT_BACKUP_SOURCE_API_KEY')

    try:
        response = requests.get(
            settings.MERCHANT_BACKUP_SOURCE_URL,
            headers=build_backup_source_headers(),
            timeout=float(getattr(settings, 'MERCHANT_NOTIFY_FETCH_TIMEOUT_SECONDS', 8)),
        )
    except requests.RequestException as error:
        raise MerchantNoticeSourceError(f'远行商人备用源请求失败: {error}') from error

    if response.status_code < 200 or response.status_code >= 300:
        raise MerchantNoticeSourceError(f'远行商人备用源返回异常状态码: {response.status_code}')

    try:
        raw_payload = response.json()
    except ValueError as error:
        raise MerchantNoticeSourceError('远行商人备用源返回了非 JSON 数据') from error

    return normalize_backup_source_payload(raw_payload if isinstance(raw_payload, dict) else {})


def fetch_source_payload_from_priority(source_name):
    if source_name == MERCHANT_SOURCE_PRIMARY:
        return fetch_primary_source_payload()
    if source_name == MERCHANT_SOURCE_BACKUP:
        return fetch_backup_source_payload()
    raise MerchantNoticeSourceError(f'未知的远行商人数据源: {source_name}')


def fetch_source_payload(force=False, use_cache=True):
    cache_ttl = max(int(getattr(settings, 'MERCHANT_NOTICE_CACHE_TTL_SECONDS', 30) or 30), 0)
    if use_cache and not force and _CURRENT_PAYLOAD_CACHE['payload'] and _CURRENT_PAYLOAD_CACHE['expires_at'] > time.monotonic():
        return copy.deepcopy(_CURRENT_PAYLOAD_CACHE['payload'])

    errors = []
    for source_name in get_merchant_source_priority_list():
        try:
            normalized = fetch_source_payload_from_priority(source_name)
            _CURRENT_PAYLOAD_CACHE['payload'] = copy.deepcopy(normalized)
            _CURRENT_PAYLOAD_CACHE['expires_at'] = time.monotonic() + cache_ttl
            return normalized
        except MerchantNoticeSourceError as error:
            errors.append(f'{source_name}: {error}')

    raise MerchantNoticeSourceError('；'.join(errors) if errors else '远行商人数据源全部请求失败')


def is_same_payload_round(left, right):
    if not left or not right:
        return False
    return (
        normalize_text(left.get('slotDate'), 16) == normalize_text(right.get('slotDate'), 16)
        and parse_int(left.get('round'), 0) == parse_int(right.get('round'), 0)
    )


def build_item_lookup(payload):
    lookup = {}
    for item in (payload or {}).get('items') or []:
        if not isinstance(item, dict):
            continue
        item_name = normalize_goods_name(item.get('name'))
        if item_name and item_name not in lookup:
            lookup[item_name] = item
    return lookup


def merge_display_payload(base_payload, supplement_payload):
    if not is_same_payload_round(base_payload, supplement_payload):
        return copy.deepcopy(base_payload)

    merged_payload = copy.deepcopy(base_payload)
    supplement_items = build_item_lookup(supplement_payload)
    seen_names = set()
    merged_items = []

    for item in merged_payload.get('items') or []:
        if not isinstance(item, dict):
            continue
        merged_item = dict(item)
        item_name = normalize_goods_name(merged_item.get('name'))
        supplement_item = supplement_items.get(item_name)
        if supplement_item:
            if parse_int(merged_item.get('price'), 0) <= 0 and parse_int(supplement_item.get('price'), 0) > 0:
                merged_item['price'] = parse_int(supplement_item.get('price'), 0)
            if (
                parse_int(merged_item.get('purchaseLimit'), 0) <= 0
                and parse_int(supplement_item.get('purchaseLimit'), 0) > 0
            ):
                merged_item['purchaseLimit'] = parse_int(supplement_item.get('purchaseLimit'), 0)
            if not normalize_text(merged_item.get('image'), 512) and normalize_text(supplement_item.get('image'), 512):
                merged_item['image'] = supplement_item.get('image')
            merged_item['isSpecial'] = bool(merged_item.get('isSpecial') or supplement_item.get('isSpecial'))
            merged_item['isHighlight'] = bool(merged_item.get('isHighlight') or supplement_item.get('isHighlight'))
            merged_item['sourceHighlight'] = bool(merged_item.get('sourceHighlight') or supplement_item.get('sourceHighlight'))
        if item_name:
            seen_names.add(item_name)
        merged_items.append(merged_item)

    for item_name, supplement_item in supplement_items.items():
        if item_name in seen_names:
            continue
        merged_items.append(copy.deepcopy(supplement_item))

    merged_payload['items'] = merged_items
    special_item_names = [item['name'] for item in merged_items if item.get('isSpecial')]
    merged_payload['hasSpecialHit'] = bool(special_item_names)
    merged_payload['specialItemNames'] = special_item_names
    merged_payload['fingerprint'] = compute_payload_fingerprint({
        'slotDate': merged_payload.get('slotDate'),
        'round': merged_payload.get('round'),
        'totalRounds': merged_payload.get('totalRounds'),
        'items': merged_items,
    })
    return merged_payload


def fetch_display_source_payload(force=False, use_cache=True):
    cache_ttl = max(int(getattr(settings, 'MERCHANT_NOTICE_CACHE_TTL_SECONDS', 30) or 30), 0)
    if (
        use_cache
        and not force
        and _DISPLAY_PAYLOAD_CACHE['payload']
        and _DISPLAY_PAYLOAD_CACHE['expires_at'] > time.monotonic()
    ):
        return copy.deepcopy(_DISPLAY_PAYLOAD_CACHE['payload'])

    errors = []
    primary_payload = None
    backup_payload = None

    try:
        backup_payload = fetch_backup_source_payload()
    except MerchantNoticeSourceError as error:
        errors.append(f'{MERCHANT_SOURCE_BACKUP}: {error}')

    try:
        primary_payload = fetch_primary_source_payload()
    except MerchantNoticeSourceError as error:
        errors.append(f'{MERCHANT_SOURCE_PRIMARY}: {error}')

    if backup_payload and primary_payload:
        payload = merge_display_payload(backup_payload, primary_payload)
    elif backup_payload:
        payload = backup_payload
    elif primary_payload:
        payload = primary_payload
    else:
        raise MerchantNoticeSourceError('；'.join(errors) if errors else '远行商人展示数据源全部请求失败')

    _DISPLAY_PAYLOAD_CACHE['payload'] = copy.deepcopy(payload)
    _DISPLAY_PAYLOAD_CACHE['expires_at'] = time.monotonic() + cache_ttl
    return payload


def get_preferred_source_only_window(now=None):
    current_time = now or get_local_now()
    for round_hour in ROUND_START_HOURS:
        round_start_at = datetime.combine(current_time.date(), dt_time(hour=round_hour), tzinfo=get_local_timezone())
        window_start_at = round_start_at + timedelta(minutes=MERCHANT_PREFERRED_SOURCE_ONLY_START_MINUTE)
        window_end_at = round_start_at + timedelta(minutes=MERCHANT_PREFERRED_SOURCE_ONLY_END_MINUTE)
        if window_start_at <= current_time < window_end_at:
            return {
                'active': True,
                'startAt': format_iso_datetime(window_start_at),
                'endAt': format_iso_datetime(window_end_at),
            }
    return {
        'active': False,
        'startAt': '',
        'endAt': '',
    }


def build_watch_source_candidate(source_name, payload, latest_snapshot=None):
    payload_summary = summarize_payload(payload)
    is_same_fingerprint = bool(latest_snapshot and latest_snapshot.fingerprint == payload.get('fingerprint'))
    is_ready = is_ready_snapshot_payload(payload)
    if not is_same_fingerprint and is_ready:
        status = 'ready_changed'
        rank = 4
    elif not is_same_fingerprint:
        status = 'changed_empty'
        rank = 3
    elif is_ready:
        status = 'unchanged'
        rank = 2
    else:
        status = 'unchanged_empty'
        rank = 1

    attempt_summary = dict(payload_summary)
    attempt_summary.update({
        'sourceName': source_name,
        'status': status,
        'isReady': is_ready,
        'isSameFingerprint': is_same_fingerprint,
    })
    return {
        'payload': payload,
        'payloadSummary': payload_summary,
        'attemptSummary': attempt_summary,
        'sourceName': source_name,
        'status': status,
        'rank': rank,
        'isReady': is_ready,
        'isSameFingerprint': is_same_fingerprint,
    }


def build_latest_snapshot_watch_probe(latest_snapshot, source_attempts, status, window=None):
    if not latest_snapshot:
        return None

    payload = serialize_snapshot(latest_snapshot)
    payload_summary = summarize_payload(payload)
    attempt_summary = dict(payload_summary)
    attempt_summary.update({
        'sourceName': 'latest_snapshot',
        'status': status,
        'isReady': is_ready_snapshot_payload(payload),
        'isSameFingerprint': True,
    })
    if window:
        attempt_summary.update({
            'windowStartAt': window['startAt'],
            'windowEndAt': window['endAt'],
        })
    source_attempts.append(attempt_summary)
    return {
        'payload': payload,
        'payloadSummary': payload_summary,
        'sourceName': 'latest_snapshot',
        'sourceAttempts': source_attempts,
    }


def fetch_watch_source_payload(latest_snapshot=None):
    errors = []
    source_attempts = []
    source_names = get_merchant_watch_source_priority_list()
    preferred_source = source_names[0]
    fallback_sources = source_names[1:]
    preferred_only_window = get_preferred_source_only_window()

    try:
        preferred_payload = fetch_source_payload_from_priority(preferred_source)
    except MerchantNoticeSourceNotReadyError as error:
        errors.append(f'{preferred_source}: {error}')
        source_attempts.append({
            'sourceName': preferred_source,
            'status': 'not_ready',
            'error': normalize_text(error, 255),
        })
        latest_probe = build_latest_snapshot_watch_probe(
            latest_snapshot,
            source_attempts,
            'unchanged_preferred_source_not_ready',
        )
        if latest_probe:
            return latest_probe
        raise MerchantNoticeSourceError('; '.join(errors))
    except MerchantNoticeSourceError as error:
        errors.append(f'{preferred_source}: {error}')
        source_attempts.append({
            'sourceName': preferred_source,
            'status': 'error',
            'error': normalize_text(error, 255),
        })
    else:
        candidate = build_watch_source_candidate(preferred_source, preferred_payload, latest_snapshot=latest_snapshot)
        source_attempts.append(candidate['attemptSummary'])
        return {
            'payload': candidate['payload'],
            'payloadSummary': candidate['payloadSummary'],
            'sourceName': candidate['sourceName'],
            'sourceAttempts': source_attempts,
        }

    if preferred_only_window['active']:
        for source_name in fallback_sources:
            source_attempts.append({
                'sourceName': source_name,
                'status': 'skipped_preferred_source_only_window',
                'windowStartAt': preferred_only_window['startAt'],
                'windowEndAt': preferred_only_window['endAt'],
            })
        latest_probe = build_latest_snapshot_watch_probe(
            latest_snapshot,
            source_attempts,
            'unchanged_preferred_source_only_window',
            window=preferred_only_window,
        )
        if latest_probe:
            return latest_probe
        raise MerchantNoticeSourceError('; '.join(errors))

    best_fallback_candidate = None
    for source_name in fallback_sources:
        try:
            payload = fetch_source_payload_from_priority(source_name)
        except MerchantNoticeSourceError as error:
            errors.append(f'{source_name}: {error}')
            source_attempts.append({
                'sourceName': source_name,
                'status': 'error',
                'error': normalize_text(error, 255),
            })
            continue

        candidate = build_watch_source_candidate(source_name, payload, latest_snapshot=latest_snapshot)
        source_attempts.append(candidate['attemptSummary'])
        if best_fallback_candidate is None or candidate['rank'] > best_fallback_candidate['rank']:
            best_fallback_candidate = candidate
        if candidate['status'] == 'ready_changed':
            return {
                'payload': candidate['payload'],
                'payloadSummary': candidate['payloadSummary'],
                'sourceName': candidate['sourceName'],
                'sourceAttempts': source_attempts,
            }

    if best_fallback_candidate:
        return {
            'payload': best_fallback_candidate['payload'],
            'payloadSummary': best_fallback_candidate['payloadSummary'],
            'sourceName': best_fallback_candidate['sourceName'],
            'sourceAttempts': source_attempts,
        }

    raise MerchantNoticeSourceError('; '.join(errors) if errors else 'merchant source payload fetch failed')


def load_items_json(value):
    try:
        parsed = json.loads(value or '[]')
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def serialize_snapshot(snapshot):
    items = load_items_json(snapshot.items_json)
    slot_date = snapshot.slot_date
    next_refresh_at = snapshot.next_refresh_at
    return {
        'slotDate': slot_date.isoformat(),
        'round': snapshot.round,
        'totalRounds': snapshot.total_rounds,
        'roundLabel': format_round_label(snapshot.round, snapshot.total_rounds),
        'timeWindowLabel': format_round_window(slot_date, snapshot.round),
        'nextRefreshAt': format_iso_datetime(next_refresh_at),
        'nextRefreshLabel': format_next_refresh_label(next_refresh_at),
        'items': items,
        'hasSpecialHit': bool(snapshot.has_special_hit),
        'specialItemNames': sort_goods_by_notice_priority([
            name for name in str(snapshot.special_item_names or '').split('、')
            if name
        ]),
        'fingerprint': snapshot.fingerprint,
    }


def summarize_payload(payload):
    items = payload.get('items') or []
    item_names = [
        normalize_text(item.get('name'), 64)
        for item in items
        if isinstance(item, dict) and normalize_text(item.get('name'), 64)
    ]
    return {
        'slotDate': normalize_text(payload.get('slotDate'), 16),
        'round': max(parse_int(payload.get('round'), 0), 0),
        'totalRounds': max(parse_int(payload.get('totalRounds'), 0), 0),
        'itemCount': len(item_names),
        'itemNames': item_names[:5],
        'hasSpecialHit': bool(payload.get('hasSpecialHit')),
        'specialItemNames': sort_goods_by_notice_priority(payload.get('specialItemNames') or []),
        'sourceUpdatedAt': normalize_text(payload.get('sourceUpdatedAt'), 32),
        'fingerprint': normalize_text(payload.get('fingerprint'), 64),
    }


def is_ready_snapshot_payload(payload):
    return len(payload.get('items') or []) > 0


def get_latest_snapshot(include_manual=False):
    queryset = MerchantSnapshot.objects.order_by('-slot_date', '-round', '-id')
    if not include_manual:
        queryset = queryset.exclude(fingerprint__startswith=MANUAL_SNAPSHOT_FINGERPRINT_PREFIX)
    return queryset.first()


def snapshot_has_items(snapshot):
    if not snapshot:
        return False
    return len(load_items_json(snapshot.items_json)) > 0


def is_same_snapshot_round(snapshot, payload):
    if not snapshot or not payload:
        return False
    return (
        snapshot.slot_date == parse_slot_date(payload.get('slotDate')) and
        snapshot.round == parse_int(payload.get('round'), 0)
    )


def build_job_state_defaults(now, guard_seconds):
    return {
        'guard_seconds': guard_seconds,
        'last_triggered_at': None,
        'last_completed_at': None,
        'last_status': MerchantNoticeJobState.STATUS_IDLE,
        'last_result_json': '{}',
        'created_at': now,
        'updated_at': now,
    }


def begin_watch_job_run(force=False, guard_seconds=None):
    now = make_naive_local(get_local_now())
    guard_seconds = max(parse_int(guard_seconds, get_trigger_guard_seconds()), 0)

    with transaction.atomic():
        state, _ = MerchantNoticeJobState.objects.select_for_update().get_or_create(
            job_key=WATCH_JOB_KEY,
            defaults=build_job_state_defaults(now, guard_seconds),
        )

        last_triggered_at = state.last_triggered_at
        if not force and last_triggered_at:
            elapsed_seconds = max((now - last_triggered_at).total_seconds(), 0)
            if elapsed_seconds < guard_seconds:
                remaining_seconds = int(max(guard_seconds - elapsed_seconds, 0))
                return {
                    'allowed': False,
                    'guardSeconds': guard_seconds,
                    'remainingSeconds': remaining_seconds,
                    'lastTriggeredAt': format_iso_datetime(last_triggered_at),
                    'lastStatus': state.last_status,
                }

        state.guard_seconds = guard_seconds
        state.last_triggered_at = now
        state.last_status = MerchantNoticeJobState.STATUS_RUNNING
        state.last_result_json = '{}'
        state.save(update_fields=[
            'guard_seconds',
            'last_triggered_at',
            'last_status',
            'last_result_json',
            'updated_at',
        ])

    return {
        'allowed': True,
        'guardSeconds': guard_seconds,
        'lastTriggeredAt': format_iso_datetime(now),
    }


def finish_watch_job_run(status, result):
    now = make_naive_local(get_local_now())
    with transaction.atomic():
        state, _ = MerchantNoticeJobState.objects.select_for_update().get_or_create(
            job_key=WATCH_JOB_KEY,
            defaults=build_job_state_defaults(now, get_trigger_guard_seconds()),
        )
        state.last_completed_at = now
        state.last_status = status
        state.last_result_json = json.dumps(result or {}, ensure_ascii=False, sort_keys=True)
        state.save(update_fields=[
            'last_completed_at',
            'last_status',
            'last_result_json',
            'updated_at',
        ])


def get_current_payload_for_display():
    latest_snapshot = get_latest_snapshot()
    try:
        current_payload = fetch_display_source_payload(force=False, use_cache=True)
        return current_payload, False
    except MerchantNoticeSourceError:
        if latest_snapshot:
            return serialize_snapshot(latest_snapshot), True
        raise


def get_notice_service_status():
    missing_config_keys = []
    missing_config_labels = []

    for setting_key, display_label in SERVICE_REQUIRED_CONFIGS:
        if not str(getattr(settings, setting_key, '') or '').strip():
            missing_config_keys.append(setting_key)
            missing_config_labels.append(display_label)

    ready = not missing_config_keys
    if ready:
        message = '订阅提醒配置已就绪'
    else:
        message = f'缺少配置：{"、".join(missing_config_labels)}'

    return {
        'ready': ready,
        'missingConfigKeys': missing_config_keys,
        'missingConfigLabels': missing_config_labels,
        'message': message,
    }


def get_notice_service_ready():
    return get_notice_service_status()['ready']


def get_subscription_pending_count(record):
    if not record:
        return 0
    return max(parse_int(getattr(record, 'pending_count', 0), 0), 0)


def get_subscription_notify_count(record):
    if not record:
        return 0
    return max(parse_int(getattr(record, 'notify_count', 0), 0), 0)


def get_subscription_daily_increment_date(record):
    if not record:
        return None
    value = getattr(record, 'daily_increment_date', None)
    return value if isinstance(value, date) else None


def get_subscription_daily_increment_count(record, local_date=None):
    if not record:
        return 0
    normalized_date = local_date or get_local_now().date()
    if get_subscription_daily_increment_date(record) != normalized_date:
        return 0
    return max(parse_int(getattr(record, 'daily_increment_count', 0), 0), 0)


def get_subscription_daily_reward_unlock_count(record, local_date=None):
    if not record:
        return 0
    normalized_date = local_date or get_local_now().date()
    if get_subscription_daily_increment_date(record) != normalized_date:
        return 0
    return max(parse_int(getattr(record, 'daily_reward_unlock_count', 0), 0), 0)


def reset_subscription_daily_reward_gate(record, now=None):
    if not record:
        return []

    normalized_now = now or make_naive_local(get_local_now())
    local_date = normalized_now.date()
    updated_fields = []

    if get_subscription_daily_increment_date(record) != local_date:
        record.daily_increment_date = local_date
        record.daily_increment_count = 0
        record.daily_reward_unlock_count = 0
        updated_fields.extend([
            'daily_increment_date',
            'daily_increment_count',
            'daily_reward_unlock_count',
        ])

    return updated_fields


def build_reward_gate_state(record, local_date=None):
    normalized_date = local_date or get_local_now().date()
    reward_step = get_rewarded_increment_step()
    daily_increment_count = get_subscription_daily_increment_count(record, normalized_date)
    daily_reward_unlock_count = get_subscription_daily_reward_unlock_count(record, normalized_date)
    required_reward_unlock_count = daily_increment_count // reward_step
    requires_rewarded_ad = daily_reward_unlock_count < required_reward_unlock_count
    next_reward_gate_count = ((daily_increment_count // reward_step) + 1) * reward_step
    remaining_before_gate = 0 if requires_rewarded_ad else max(next_reward_gate_count - daily_increment_count, 0)

    return {
        'dailyIncrementDate': normalized_date.isoformat(),
        'dailyIncrementCount': daily_increment_count,
        'dailyRewardUnlockCount': daily_reward_unlock_count,
        'rewardStep': reward_step,
        'requiredRewardUnlockCount': required_reward_unlock_count,
        'requiresRewardedAd': requires_rewarded_ad,
        'nextRewardGateCount': next_reward_gate_count,
        'remainingBeforeGate': remaining_before_gate,
    }


def build_reward_unlock_required_message(record, local_date=None):
    reward_gate_state = build_reward_gate_state(record, local_date=local_date)
    current_count = max(reward_gate_state['dailyIncrementCount'], reward_gate_state['rewardStep'])
    return f'今日已累计增加 {current_count} 次提醒机会，请先完成一次激励视频后再继续累加。'


def get_effective_selected_goods(record):
    parsed = parse_selected_goods_value(getattr(record, 'selected_goods_json', '[]') if record else '[]')
    return parsed or get_default_selected_goods()


def build_subscription_preferences(record):
    selected_goods = export_goods_names(sort_goods_by_notice_priority(get_effective_selected_goods(record)))
    default_selected_goods = export_goods_names(get_default_selected_goods())
    return {
        'selectedGoods': selected_goods,
        'selectedGoodsCount': len(selected_goods),
        'defaultSelectedGoods': default_selected_goods,
    }


def build_subscription_state(record):
    pending_count = get_subscription_pending_count(record)
    notify_count = get_subscription_notify_count(record)
    preferences = build_subscription_preferences(record)
    reward_gate_state = build_reward_gate_state(record)
    if not record:
        return {
            'status': MerchantNoticeSubscription.STATUS_IDLE,
            'isActive': False,
            'buttonText': '订阅提醒(剩0次)',
            'helperText': '每次授权可增加1次提醒。',
            'subscribedAt': '',
            'consumedAt': '',
            'pendingCount': 0,
            'notifyCount': 0,
            **reward_gate_state,
            **preferences,
        }

    if pending_count > 0:
        effective_status = MerchantNoticeSubscription.STATUS_ACTIVE
        button_text = f'订阅提醒(剩{pending_count}次)'
        helper_text = ''
    elif record.status == MerchantNoticeSubscription.STATUS_CONSUMED or notify_count > 0:
        effective_status = MerchantNoticeSubscription.STATUS_CONSUMED
        button_text = '订阅提醒(剩0次)'
        helper_text = '上次已发完，可继续追加。'
    elif record.status == MerchantNoticeSubscription.STATUS_INVALID:
        effective_status = MerchantNoticeSubscription.STATUS_INVALID
        button_text = '订阅提醒(剩0次)'
        helper_text = '提醒已失效，请重新授权。'
    else:
        effective_status = MerchantNoticeSubscription.STATUS_IDLE
        button_text = '订阅提醒(剩0次)'
        helper_text = '每次授权可增加1次提醒。'

    return {
        'status': effective_status,
        'isActive': pending_count > 0,
        'buttonText': button_text,
        'helperText': helper_text,
        'subscribedAt': format_iso_datetime(record.subscribed_at),
        'consumedAt': format_iso_datetime(record.consumed_at),
        'pendingCount': pending_count,
        'notifyCount': notify_count,
        **reward_gate_state,
        **preferences,
    }


def build_subscription_defaults(openid, appid, now):
    return {
        'openid_hash': hashlib.sha256(f'{settings.SECRET_KEY}:{openid}'.encode('utf-8')).hexdigest(),
        'appid': normalize_text(appid, 64),
        'template_id': normalize_text(getattr(settings, 'MERCHANT_NOTIFY_TEMPLATE_ID', ''), 128),
        'status': MerchantNoticeSubscription.STATUS_IDLE,
        'subscribed_at': now,
        'consumed_at': None,
        'pending_count': 0,
        'daily_increment_date': now.date(),
        'daily_increment_count': 0,
        'daily_reward_unlock_count': 0,
        'selected_goods_json': serialize_goods_names(get_default_selected_goods()),
        'last_error_code': '',
        'last_error_message': '',
        'notify_count': 0,
        'created_at': now,
        'updated_at': now,
    }


def apply_subscription_identity(subscription, openid, appid=''):
    updated_fields = []
    normalized_appid = normalize_text(appid, 64)
    expected_openid_hash = hashlib.sha256(f'{settings.SECRET_KEY}:{openid}'.encode('utf-8')).hexdigest()
    expected_template_id = normalize_text(getattr(settings, 'MERCHANT_NOTIFY_TEMPLATE_ID', ''), 128)

    if subscription.openid_hash != expected_openid_hash:
        subscription.openid_hash = expected_openid_hash
        updated_fields.append('openid_hash')
    if normalized_appid and subscription.appid != normalized_appid:
        subscription.appid = normalized_appid
        updated_fields.append('appid')
    if subscription.template_id != expected_template_id:
        subscription.template_id = expected_template_id
        updated_fields.append('template_id')

    return updated_fields


def ensure_subscription_profile(openid, appid=''):
    now = make_naive_local(get_local_now())
    defaults = build_subscription_defaults(openid, appid, now)
    subscription, created = MerchantNoticeSubscription.objects.get_or_create(openid=openid, defaults=defaults)
    if created:
        return subscription, True

    updated_fields = apply_subscription_identity(subscription, openid, appid)
    if not parse_selected_goods_value(subscription.selected_goods_json):
        subscription.selected_goods_json = defaults['selected_goods_json']
        updated_fields.append('selected_goods_json')
    if updated_fields:
        subscription.updated_at = now
        updated_fields.append('updated_at')
        subscription.save(update_fields=updated_fields)
    return subscription, False


def sanitize_current_payload_for_client(payload):
    sanitized_payload = copy.deepcopy(payload or {})
    sanitized_items = []

    for item in sanitized_payload.get('items') or []:
        if not isinstance(item, dict):
            continue
        sanitized_item = dict(item)
        sanitized_item.pop('image', None)
        sanitized_item['name'] = export_goods_name(sanitized_item.get('name'))
        sanitized_items.append(sanitized_item)

    sanitized_payload['items'] = sanitized_items
    sanitized_payload['specialItemNames'] = export_goods_names(
        sort_goods_by_notice_priority(sanitized_payload.get('specialItemNames') or [])
    )
    return sanitized_payload


def build_current_response(openid=''):
    payload, is_fallback_data = get_current_payload_for_display()
    payload = sanitize_current_payload_for_client(payload)
    service_status = get_notice_service_status()
    subscription = None
    if openid:
        subscription = MerchantNoticeSubscription.objects.filter(openid=openid).first()

    return {
        'serviceReady': service_status['ready'],
        'serviceStatus': service_status,
        'serviceWarningText': (
            ''
            if service_status['ready']
            else f'后台提醒配置尚未完成：{service_status["message"]}，请先补齐云托管环境变量并重新部署。'
        ),
        'isFallbackData': bool(is_fallback_data),
        'specialKeywords': get_special_keywords(),
        'defaultSelectedGoods': export_goods_names(get_default_selected_goods()),
        'current': payload,
        'subscription': build_subscription_state(subscription),
    }


def get_or_create_subscription(openid, appid):
    now = make_naive_local(get_local_now())
    normalized_appid = normalize_text(appid, 64)

    with transaction.atomic():
        subscription, created = ensure_subscription_profile(openid, normalized_appid)
        subscription = MerchantNoticeSubscription.objects.select_for_update().get(pk=subscription.pk)

        updated_fields = apply_subscription_identity(subscription, openid, normalized_appid)
        updated_fields.extend(reset_subscription_daily_reward_gate(subscription, now=now))
        local_date = now.date()
        reward_gate_state = build_reward_gate_state(subscription, local_date=local_date)
        if reward_gate_state['requiresRewardedAd']:
            if updated_fields:
                subscription.updated_at = now
                updated_fields.append('updated_at')
                subscription.save(update_fields=list(dict.fromkeys(updated_fields)))
            raise MerchantNoticeValidationError(build_reward_unlock_required_message(subscription, local_date=local_date))

        subscription.status = MerchantNoticeSubscription.STATUS_ACTIVE
        subscription.subscribed_at = now
        subscription.consumed_at = None
        subscription.pending_count = get_subscription_pending_count(subscription) + 1
        subscription.daily_increment_date = local_date
        subscription.daily_increment_count = get_subscription_daily_increment_count(subscription, local_date) + 1
        subscription.last_error_code = ''
        subscription.last_error_message = ''
        subscription.updated_at = now
        updated_fields.extend([
            'status',
            'subscribed_at',
            'consumed_at',
            'pending_count',
            'daily_increment_date',
            'daily_increment_count',
            'last_error_code',
            'last_error_message',
            'updated_at',
        ])
        subscription.save(update_fields=list(dict.fromkeys(updated_fields)))

    return subscription, created


def prepare_subscription_next(openid, appid=''):
    service_status = get_notice_service_status()
    if not service_status['ready']:
        raise MerchantNoticeConfigurationError(f'远行提醒未完成通知配置：{service_status["message"]}')

    normalized_openid = normalize_text(openid, 128)
    if not normalized_openid:
        raise MerchantNoticePermissionError('未获取到当前用户身份，暂时无法开启提醒')

    subscription, created = ensure_subscription_profile(normalized_openid, appid)
    reward_gate_state = build_reward_gate_state(subscription)
    return {
        'created': created,
        'allowed': not reward_gate_state['requiresRewardedAd'],
        'requiresRewardedAd': reward_gate_state['requiresRewardedAd'],
        'subscription': build_subscription_state(subscription),
    }


def unlock_subscription_rewarded_gate(openid, appid=''):
    service_status = get_notice_service_status()
    if not service_status['ready']:
        raise MerchantNoticeConfigurationError(f'远行提醒未完成通知配置：{service_status["message"]}')

    normalized_openid = normalize_text(openid, 128)
    if not normalized_openid:
        raise MerchantNoticePermissionError('未获取到当前用户身份，暂时无法完成激励校验')

    now = make_naive_local(get_local_now())
    normalized_appid = normalize_text(appid, 64)

    with transaction.atomic():
        subscription, created = ensure_subscription_profile(normalized_openid, normalized_appid)
        subscription = MerchantNoticeSubscription.objects.select_for_update().get(pk=subscription.pk)

        updated_fields = apply_subscription_identity(subscription, normalized_openid, normalized_appid)
        updated_fields.extend(reset_subscription_daily_reward_gate(subscription, now=now))
        local_date = now.date()
        reward_gate_state = build_reward_gate_state(subscription, local_date=local_date)
        current_unlock_count = get_subscription_daily_reward_unlock_count(subscription, local_date)
        granted_unlock_count = max(reward_gate_state['requiredRewardUnlockCount'] - current_unlock_count, 0)

        if granted_unlock_count > 0:
            subscription.daily_reward_unlock_count = current_unlock_count + granted_unlock_count
            updated_fields.append('daily_reward_unlock_count')

        if updated_fields:
            subscription.updated_at = now
            updated_fields.append('updated_at')
            subscription.save(update_fields=list(dict.fromkeys(updated_fields)))

    updated_reward_gate_state = build_reward_gate_state(subscription)
    return {
        'created': created,
        'grantedUnlockCount': granted_unlock_count,
        'allowed': not updated_reward_gate_state['requiresRewardedAd'],
        'requiresRewardedAd': updated_reward_gate_state['requiresRewardedAd'],
        'subscription': build_subscription_state(subscription),
    }


def subscribe_next(openid, appid=''):
    service_status = get_notice_service_status()
    if not service_status['ready']:
        raise MerchantNoticeConfigurationError(f'远行提醒未完成通知配置：{service_status["message"]}')
    normalized_openid = normalize_text(openid, 128)
    if not normalized_openid:
        raise MerchantNoticePermissionError('未获取到当前用户身份，暂时无法开启提醒')

    subscription, created = get_or_create_subscription(normalized_openid, appid)
    return {
        'created': created,
        'grantedCount': 1,
        'subscription': build_subscription_state(subscription),
    }


def resolve_dev_self_test_miniprogram_state(env_version=''):
    normalized_env_version = normalize_text(env_version, 16)
    if normalized_env_version == 'trial':
        return 'trial'
    if normalized_env_version in {'release', 'formal'}:
        return 'formal'
    return 'developer'


def build_dev_self_test_payload(env_version='', remaining_count=0):
    now = get_local_now()
    return {
        'date2': now.strftime('%m-%d %H:%M'),
        'thing7': build_special_item_summary(DEV_SELF_TEST_ITEM_NAMES),
        'thing10': build_notice_advice_text(DEV_SELF_TEST_ACTION, remaining_count),
        'page': normalize_text(
            getattr(settings, 'MERCHANT_NOTIFY_PAGE', 'pages/merchant-notice/index'),
            255,
        ),
        'miniprogramState': resolve_dev_self_test_miniprogram_state(env_version),
        'campaignKey': f'dev-self-test-{format_iso_datetime(now)}-{time.time_ns()}',
    }


def send_dev_self_test_message(openid, appid='', env_version=''):
    service_status = get_notice_service_status()
    if not service_status['ready']:
        raise MerchantNoticeConfigurationError(f'远行提醒未完成通知配置：{service_status["message"]}')

    normalized_openid = normalize_text(openid, 128)
    if not normalized_openid:
        raise MerchantNoticePermissionError('未获取到当前用户身份，暂时无法发送测试通知')

    now = make_naive_local(get_local_now())
    subscription, _created = ensure_subscription_profile(normalized_openid, appid)
    subscription = MerchantNoticeSubscription.objects.filter(pk=subscription.pk).first()

    updated_fields = apply_subscription_identity(subscription, normalized_openid, appid)
    updated_fields.extend(reset_subscription_daily_reward_gate(subscription, now=now))
    if updated_fields:
        subscription.updated_at = now
        updated_fields.append('updated_at')
        subscription.save(update_fields=list(dict.fromkeys(updated_fields)))

    if get_subscription_pending_count(subscription) <= 0:
        raise MerchantNoticeValidationError('请先订阅至少 1 次提醒后，再发送开发模式测试通知')
    if subscription.status == MerchantNoticeSubscription.STATUS_INVALID:
        raise MerchantNoticeValidationError('当前提醒已失效，请重新授权订阅消息后再测试')

    payload = build_dev_self_test_payload(
        env_version=env_version,
        remaining_count=max(get_subscription_pending_count(subscription) - 1, 0),
    )
    snapshot, _snapshot_created = get_or_create_manual_snapshot(
        payload,
        campaign_key=payload['campaignKey'],
    )
    result = send_subscribe_message(
        subscription,
        snapshot,
        message_body=build_manual_subscribe_message_body(subscription, payload),
        special_item_names=payload.get('thing7'),
    )
    refreshed_subscription = MerchantNoticeSubscription.objects.filter(pk=subscription.pk).first()
    return {
        'status': result.get('status', ''),
        'messagePayload': {
            'date2': payload['date2'],
            'thing7': payload['thing7'],
            'thing10': payload['thing10'],
        },
        'snapshotFingerprint': snapshot.fingerprint,
        'subscription': build_subscription_state(refreshed_subscription),
        'result': result,
    }


def get_subscription_preferences(openid=''):
    normalized_openid = normalize_text(openid, 128)
    if not normalized_openid:
        raise MerchantNoticePermissionError('未获取到当前用户身份，暂时无法读取通知商品配置')

    subscription = MerchantNoticeSubscription.objects.filter(openid=normalized_openid).first()
    return {
        'subscription': build_subscription_state(subscription),
        'preferences': build_subscription_preferences(subscription),
    }


def update_subscription_preferences(openid, selected_goods, appid=''):
    normalized_openid = normalize_text(openid, 128)
    if not normalized_openid:
        raise MerchantNoticePermissionError('未获取到当前用户身份，暂时无法保存通知商品配置')

    normalized_goods = parse_selected_goods_value(selected_goods)
    if not normalized_goods:
        raise MerchantNoticeValidationError('请至少选择 1 个通知商品')

    subscription, created = ensure_subscription_profile(normalized_openid, appid)
    now = make_naive_local(get_local_now())
    subscription.selected_goods_json = serialize_goods_names(normalized_goods)
    if appid:
        subscription.appid = normalize_text(appid, 64)
    subscription.updated_at = now
    subscription.save(update_fields=[
        'selected_goods_json',
        'appid',
        'updated_at',
    ])
    return {
        'created': created,
        'subscription': build_subscription_state(subscription),
        'preferences': build_subscription_preferences(subscription),
    }


def build_wechat_access_token():
    appid = normalize_text(getattr(settings, 'WECHAT_APP_ID', ''), 128)
    secret = normalize_text(getattr(settings, 'WECHAT_APP_SECRET', ''), 128)
    if not appid or not secret:
        raise MerchantNoticeConfigurationError('未配置 WECHAT_APP_ID 或 WECHAT_APP_SECRET')

    if _ACCESS_TOKEN_CACHE['token'] and _ACCESS_TOKEN_CACHE['expires_at'] > time.monotonic():
        return _ACCESS_TOKEN_CACHE['token']

    with _ACCESS_TOKEN_CACHE_LOCK:
        if _ACCESS_TOKEN_CACHE['token'] and _ACCESS_TOKEN_CACHE['expires_at'] > time.monotonic():
            return _ACCESS_TOKEN_CACHE['token']

        try:
            response = requests.post(
                WECHAT_STABLE_TOKEN_URL,
                json={
                    'grant_type': 'client_credential',
                    'appid': appid,
                    'secret': secret,
                    'force_refresh': False,
                },
                timeout=float(getattr(settings, 'MERCHANT_NOTIFY_FETCH_TIMEOUT_SECONDS', 8)),
                verify=False,
            )
        except requests.RequestException as error:
            raise MerchantNoticeSourceError(f'微信 access_token 获取失败: {error}') from error

        try:
            data = response.json()
        except ValueError as error:
            raise MerchantNoticeSourceError('微信 access_token 接口返回了非 JSON 数据') from error

        token = normalize_text(data.get('access_token'), 512)
        if not token:
            raise MerchantNoticeSourceError(f'微信 access_token 获取失败: {data}')

        expires_in = max(parse_int(data.get('expires_in') or data.get('expire_in'), 7200), 120)
        _ACCESS_TOKEN_CACHE['token'] = token
        _ACCESS_TOKEN_CACHE['expires_at'] = time.monotonic() + max(expires_in - 120, 60)
        return token


def clear_wechat_access_token_cache():
    with _ACCESS_TOKEN_CACHE_LOCK:
        _ACCESS_TOKEN_CACHE['token'] = ''
        _ACCESS_TOKEN_CACHE['expires_at'] = 0.0


def should_retry_with_refreshed_access_token(error_code):
    return str(error_code or '').strip() in WECHAT_ACCESS_TOKEN_RETRYABLE_ERROR_CODES


def get_wechat_send_retry_count():
    return max(parse_int(getattr(settings, 'MERCHANT_NOTIFY_SEND_RETRY_COUNT', 2), 2), 0)


def get_wechat_send_retry_delay_seconds():
    return max(float(getattr(settings, 'MERCHANT_NOTIFY_SEND_RETRY_DELAY_SECONDS', 1.5) or 1.5), 0.1)


def get_wechat_subscribe_send_session():
    session = getattr(_WECHAT_SESSION_LOCAL, 'subscribe_send_session', None)
    if session is None:
        session = requests.Session()
        _WECHAT_SESSION_LOCAL.subscribe_send_session = session
    return session


def close_wechat_subscribe_send_session():
    session = getattr(_WECHAT_SESSION_LOCAL, 'subscribe_send_session', None)
    if session is None:
        return
    try:
        session.close()
    finally:
        _WECHAT_SESSION_LOCAL.subscribe_send_session = None


def post_wechat_subscribe_message(request_payload):
    access_token = build_wechat_access_token()
    retry_count = get_wechat_send_retry_count()
    retry_delay_seconds = get_wechat_send_retry_delay_seconds()
    last_error = None

    for attempt in range(retry_count + 1):
        try:
            response = get_wechat_subscribe_send_session().post(
                WECHAT_SUBSCRIBE_SEND_URL,
                params={'access_token': access_token},
                json=request_payload,
                timeout=float(getattr(settings, 'MERCHANT_NOTIFY_FETCH_TIMEOUT_SECONDS', 8)),
                verify=False,
            )
            try:
                return response.json()
            except ValueError as error:
                raise MerchantNoticeSourceError('微信订阅消息发送接口返回了非 JSON 数据') from error
        except requests.RequestException as error:
            close_wechat_subscribe_send_session()
            last_error = error
            if attempt >= retry_count:
                break
            logger.warning(
                'merchant notice send request retry attempt=%s/%s error=%s',
                attempt + 1,
                retry_count + 1,
                error,
            )
            time.sleep(retry_delay_seconds * (attempt + 1))

    raise MerchantNoticeSourceError(f'微信订阅消息发送失败: {last_error}') from last_error


def truncate_text(value, max_length):
    text = normalize_text(value, max_length * 4)
    if len(text) <= max_length:
        return text
    return text[:max_length]


def build_notice_advice_text(action_text, remaining_count):
    action = normalize_text(action_text, 12) or DEFAULT_NOTICE_ACTION
    remaining = max(parse_int(remaining_count, 0), 0)
    if remaining > 0:
        return truncate_text(f'{action}，剩{remaining}次提醒', 20)
    return truncate_text('订阅次数耗尽，需重新订阅！！', 20)


def build_special_item_summary(names):
    normalized_names = [
        normalize_text(name, 20)
        for name in export_goods_names(sort_goods_by_notice_priority(names))
        if normalize_text(name, 20)
    ]
    if not normalized_names:
        return '检测到珍贵货物'
    joined = '、'.join(normalized_names)
    if len(joined) <= 20:
        return joined
    first_name = normalized_names[0]
    if len(normalized_names) == 1:
        return truncate_text(first_name, 20)
    return truncate_text(f'{first_name}等{len(normalized_names)}件', 20)


def normalize_message_item_names(snapshot, item_names=None):
    if item_names:
        if isinstance(item_names, str):
            normalized = dedupe_goods_names(str(item_names).split('、'))
        else:
            normalized = dedupe_goods_names(item_names)
        if normalized:
            return sort_goods_by_notice_priority(normalized)

    return sort_goods_by_notice_priority(str(snapshot.special_item_names or '').split('、'))


def get_snapshot_item_names(snapshot):
    return dedupe_goods_names([
        item.get('name')
        for item in load_items_json(snapshot.items_json)
        if isinstance(item, dict)
    ])


def is_snapshot_item_notice_eligible(snapshot, item):
    if not isinstance(item, dict) or not normalize_goods_name(item.get('name')):
        return False

    available_start_at = parse_item_availability_datetime(
        item.get('availableStartAt') or item.get('startAt')
    )
    if not available_start_at:
        return True

    round_start_at, round_end_at = build_round_window(snapshot.slot_date, snapshot.round)
    return round_start_at <= available_start_at < round_end_at


def get_snapshot_notice_item_names(snapshot):
    return dedupe_goods_names([
        item.get('name')
        for item in load_items_json(snapshot.items_json)
        if is_snapshot_item_notice_eligible(snapshot, item)
    ])


def get_matching_selected_goods(subscription, snapshot):
    selected_goods = set(get_effective_selected_goods(subscription))
    if not selected_goods:
        return []
    return sort_goods_by_notice_priority([
        item_name
        for item_name in get_snapshot_notice_item_names(snapshot)
        if item_name in selected_goods
    ])


def get_dispatchable_subscription_queryset():
    return MerchantNoticeSubscription.objects.filter(
        status=MerchantNoticeSubscription.STATUS_ACTIVE,
        pending_count__gt=0,
    ).order_by('id')


def get_dispatchable_subscriptions():
    return list(get_dispatchable_subscription_queryset())


def build_manual_dispatch_filter_summary(payload):
    filters = payload if isinstance(payload, dict) else {}
    start_subscription_id = max(parse_int(filters.get('startSubscriptionId'), 0), 0)
    end_subscription_id = max(parse_int(filters.get('endSubscriptionId'), 0), 0)
    exclude_success_snapshot_id = max(parse_int(filters.get('excludeSuccessSnapshotId'), 0), 0)
    return {
        'startSubscriptionId': start_subscription_id or None,
        'endSubscriptionId': end_subscription_id or None,
        'excludeSuccessSnapshotId': exclude_success_snapshot_id or None,
    }


def get_manual_dispatch_queryset(payload=None):
    filter_summary = build_manual_dispatch_filter_summary(payload)
    queryset = get_dispatchable_subscription_queryset()

    if filter_summary['startSubscriptionId']:
        queryset = queryset.filter(id__gte=filter_summary['startSubscriptionId'])
    if filter_summary['endSubscriptionId']:
        queryset = queryset.filter(id__lte=filter_summary['endSubscriptionId'])
    if filter_summary['excludeSuccessSnapshotId']:
        successful_subscription_ids = MerchantNoticeSendLog.objects.filter(
            snapshot_id=filter_summary['excludeSuccessSnapshotId'],
            status=MerchantNoticeSendLog.STATUS_SUCCESS,
        ).values('subscription_id')
        queryset = queryset.exclude(id__in=successful_subscription_ids)

    return queryset


def build_manual_dispatch_targets(payload=None):
    message_payload = payload if isinstance(payload, dict) else {}
    subscription_ids = list(get_manual_dispatch_queryset(message_payload).values_list('id', flat=True))
    return [
        {
            'subscriptionId': subscription_id,
            'messagePayload': message_payload,
        }
        for subscription_id in subscription_ids
    ]


def build_snapshot_dispatch_targets(snapshot):
    targets = []
    for subscription, matched_names in iter_snapshot_dispatch_targets(snapshot):
        targets.append({
            'subscriptionId': subscription.id,
            'matchedNames': matched_names,
        })
    return targets


def split_dispatch_targets(targets, shard_count):
    normalized_targets = list(targets or [])
    if not normalized_targets:
        return []

    normalized_shard_count = max(min(parse_int(shard_count, 1), len(normalized_targets)), 1)
    shards = [[] for _ in range(normalized_shard_count)]
    for index, target in enumerate(normalized_targets):
        shards[index % normalized_shard_count].append(target)
    return [shard for shard in shards if shard]


def is_wechat_gateway_request_error_message(error_message):
    normalized = normalize_text(error_message, 512)
    if not normalized:
        return False
    return (
        'api.weixin.qq.com' in normalized
        and (
            'port=443' in normalized
            or 'HTTPSConnectionPool' in normalized
            or 'Failed to establish a new connection' in normalized
            or 'Read timed out' in normalized
            or 'ConnectTimeout' in normalized
            or 'Connection reset' in normalized
        )
    )


def iter_snapshot_dispatch_targets(snapshot):
    for subscription in get_dispatchable_subscription_queryset().iterator(chunk_size=100):
        matched_names = get_matching_selected_goods(subscription, snapshot)
        if matched_names:
            yield subscription, matched_names


def count_snapshot_dispatch_targets(snapshot):
    target_count = 0
    for _subscription, _matched_names in iter_snapshot_dispatch_targets(snapshot):
        target_count += 1
    return target_count


def build_subscribe_message_body(subscription, snapshot, matched_item_names=None):
    special_names = normalize_message_item_names(snapshot, matched_item_names)
    remaining_count = max(get_subscription_pending_count(subscription) - 1, 0)
    body = {
        'touser': subscription.openid,
        'template_id': str(getattr(settings, 'MERCHANT_NOTIFY_TEMPLATE_ID', '') or '').strip(),
        'page': str(getattr(settings, 'MERCHANT_NOTIFY_PAGE', 'pages/merchant-notice/index') or 'pages/merchant-notice/index').strip(),
        'data': {
            'date2': {
                'value': format_round_notice_time(snapshot.slot_date, snapshot.round),
            },
            'thing7': {
                'value': build_special_item_summary(special_names),
            },
            'thing10': {
                'value': build_notice_advice_text(DEFAULT_NOTICE_ACTION, remaining_count),
            },
        },
        'lang': 'zh_CN',
    }
    miniprogram_state = normalize_text(getattr(settings, 'MERCHANT_NOTIFY_MINIPROGRAM_STATE', ''), 16)
    if miniprogram_state:
        body['miniprogram_state'] = miniprogram_state
    return body


def build_manual_subscribe_message_body(subscription, payload):
    template_id = normalize_text(
        payload.get('templateId') or getattr(settings, 'MERCHANT_NOTIFY_TEMPLATE_ID', ''),
        128,
    )
    body = {
        'touser': subscription.openid,
        'template_id': template_id,
        'page': normalize_text(
            payload.get('page') or getattr(settings, 'MERCHANT_NOTIFY_PAGE', 'pages/merchant-notice/index'),
            255,
        ),
        'data': {
            'date2': {
                'value': normalize_text(payload.get('date2'), 32),
            },
            'thing7': {
                'value': truncate_text(payload.get('thing7'), 20),
            },
            'thing10': {
                'value': truncate_text(payload.get('thing10'), 20),
            },
        },
        'lang': 'zh_CN',
    }
    miniprogram_state = normalize_text(
        payload.get('miniprogramState') or getattr(settings, 'MERCHANT_NOTIFY_MINIPROGRAM_STATE', ''),
        16,
    )
    if miniprogram_state:
        body['miniprogram_state'] = miniprogram_state
    return body


def get_invalid_subscription_error_codes():
    return {'40003', '43101', '47003'}


def send_subscribe_message(subscription, snapshot, message_body=None, special_item_names=''):
    existing_log = MerchantNoticeSendLog.objects.filter(
        subscription=subscription,
        snapshot=snapshot,
    ).first()
    if existing_log and existing_log.status == MerchantNoticeSendLog.STATUS_SUCCESS:
        return {
            'status': 'skipped',
            'reason': 'already_sent',
        }

    request_payload = message_body or build_subscribe_message_body(
        subscription,
        snapshot,
        matched_item_names=special_item_names,
    )
    data = post_wechat_subscribe_message(request_payload)

    errcode = str(data.get('errcode', ''))
    errmsg = normalize_text(data.get('errmsg'), 255)
    if should_retry_with_refreshed_access_token(errcode):
        logger.warning(
            'merchant notice send will retry with refreshed access token subscription_id=%s snapshot_id=%s errcode=%s errmsg=%s',
            subscription.id,
            snapshot.id,
            errcode,
            errmsg,
        )
        clear_wechat_access_token_cache()
        data = post_wechat_subscribe_message(request_payload)

    errcode = str(data.get('errcode', ''))
    errmsg = normalize_text(data.get('errmsg'), 255)
    msg_id = normalize_text(data.get('msgid') or data.get('msg_id'), 64)
    special_item_names = normalize_text('、'.join(normalize_message_item_names(snapshot, special_item_names)), 255)
    template_id = normalize_text(
        request_payload.get('template_id') or getattr(settings, 'MERCHANT_NOTIFY_TEMPLATE_ID', ''),
        128,
    )
    now = make_naive_local(get_local_now())

    if not existing_log:
        existing_log = MerchantNoticeSendLog(
            subscription=subscription,
            snapshot=snapshot,
            template_id=template_id,
            special_item_names=special_item_names,
            created_at=now,
        )

    existing_log.template_id = template_id
    existing_log.special_item_names = special_item_names
    existing_log.msg_id = msg_id
    existing_log.error_code = errcode
    existing_log.error_message = errmsg

    if errcode in {'', '0'}:
        existing_log.status = MerchantNoticeSendLog.STATUS_SUCCESS
        existing_log.save()

        next_pending_count = max(get_subscription_pending_count(subscription) - 1, 0)
        subscription.pending_count = next_pending_count
        subscription.status = (
            MerchantNoticeSubscription.STATUS_ACTIVE
            if next_pending_count > 0
            else MerchantNoticeSubscription.STATUS_CONSUMED
        )
        subscription.consumed_at = now
        subscription.last_notified_snapshot = snapshot
        subscription.notify_count = max(parse_int(subscription.notify_count, 0), 0) + 1
        subscription.last_error_code = ''
        subscription.last_error_message = ''
        subscription.updated_at = now
        subscription.save(update_fields=[
            'status',
            'consumed_at',
            'pending_count',
            'last_notified_snapshot',
            'notify_count',
            'last_error_code',
            'last_error_message',
            'updated_at',
        ])
        logger.info(
            'merchant notice sent subscription_id=%s snapshot_id=%s pending_count=%s msg_id=%s',
            subscription.id,
            snapshot.id,
            next_pending_count,
            msg_id,
        )
        return {
            'status': 'success',
            'msgId': msg_id,
        }

    existing_log.status = MerchantNoticeSendLog.STATUS_FAILED
    existing_log.save()

    if errcode in get_invalid_subscription_error_codes():
        subscription.status = MerchantNoticeSubscription.STATUS_INVALID
        subscription.pending_count = 0
        subscription.consumed_at = now
    subscription.last_error_code = errcode
    subscription.last_error_message = errmsg
    subscription.updated_at = now
    subscription.save(update_fields=[
        'status',
        'consumed_at',
        'pending_count',
        'last_error_code',
        'last_error_message',
        'updated_at',
    ])
    logger.warning(
        'merchant notice send failed subscription_id=%s snapshot_id=%s errcode=%s errmsg=%s',
        subscription.id,
        snapshot.id,
        errcode,
        errmsg,
    )
    return {
        'status': 'failed',
        'errorCode': errcode,
        'errorMessage': errmsg,
    }


def create_snapshot_from_payload(payload):
    now = make_naive_local(get_local_now())
    snapshot, created = MerchantSnapshot.objects.get_or_create(
        fingerprint=payload['fingerprint'],
        defaults={
            'slot_date': parse_slot_date(payload['slotDate']),
            'round': payload['round'],
            'total_rounds': payload['totalRounds'],
            'next_refresh_at': make_naive_local(parse_naive_local(datetime.strptime(payload['nextRefreshAt'], '%Y-%m-%dT%H:%M:%S'))) if payload.get('nextRefreshAt') else None,
            'source_updated_at': payload.get('sourceUpdatedAt', ''),
            'items_json': json.dumps(payload['items'], ensure_ascii=False),
            'has_special_hit': payload['hasSpecialHit'],
            'special_item_names': '、'.join(sort_goods_by_notice_priority(payload['specialItemNames'])),
            'created_at': now,
        },
    )
    return snapshot, created


def resolve_manual_campaign_key(campaign_key=''):
    normalized_key = normalize_text(campaign_key, 64)
    if normalized_key:
        return normalized_key
    return f'manual-{format_iso_datetime(get_local_now())}-{time.time_ns()}'


def build_manual_snapshot_fingerprint(campaign_key):
    normalized_key = resolve_manual_campaign_key(campaign_key)
    digest = hashlib.sha256(normalized_key.encode('utf-8')).hexdigest()
    suffix_length = max(64 - len(MANUAL_SNAPSHOT_FINGERPRINT_PREFIX), 1)
    return f'{MANUAL_SNAPSHOT_FINGERPRINT_PREFIX}{digest[:suffix_length]}'


def get_manual_snapshot_defaults(payload):
    now = make_naive_local(get_local_now())
    base_snapshot = get_latest_snapshot(include_manual=False)
    if base_snapshot:
        slot_date = base_snapshot.slot_date
        round_number = base_snapshot.round
        total_rounds = base_snapshot.total_rounds
        next_refresh_at = base_snapshot.next_refresh_at
        items_json = base_snapshot.items_json
    else:
        slot_date = get_local_now().date()
        round_number = 1
        total_rounds = 1
        next_refresh_at = None
        items_json = '[]'

    return {
        'slot_date': slot_date,
        'round': round_number,
        'total_rounds': total_rounds,
        'next_refresh_at': next_refresh_at,
        'source_updated_at': normalize_text(payload.get('campaignKey') or 'manual_broadcast', 32),
        'items_json': items_json,
        'has_special_hit': True,
        'special_item_names': normalize_text(payload.get('thing7'), 255),
        'created_at': now,
    }


def get_or_create_manual_snapshot(payload, campaign_key=''):
    fingerprint = build_manual_snapshot_fingerprint(campaign_key)
    snapshot, created = MerchantSnapshot.objects.get_or_create(
        fingerprint=fingerprint,
        defaults=get_manual_snapshot_defaults(payload),
    )
    return snapshot, created


def serialize_dispatch_job(job):
    if not job:
        return {}
    return {
        'jobKey': job.job_key,
        'jobType': job.job_type,
        'status': job.status,
        'targetCount': job.target_count,
        'successCount': job.success_count,
        'failedCount': job.failed_count,
        'skippedCount': job.skipped_count,
        'lastError': job.last_error,
        'startedAt': format_iso_datetime(job.started_at),
        'finishedAt': format_iso_datetime(job.finished_at),
        'createdAt': format_iso_datetime(job.created_at),
    }


def parse_dispatch_job_payload(value):
    try:
        parsed = json.loads(value or '{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_snapshot_dispatch_job_key(snapshot):
    return f'{SNAPSHOT_DISPATCH_JOB_PREFIX}{snapshot.fingerprint}'


def build_manual_dispatch_job_key(campaign_key):
    return f'{MANUAL_DISPATCH_JOB_PREFIX}{resolve_manual_campaign_key(campaign_key)}'


def get_dispatch_worker_idle_seconds():
    return max(float(getattr(settings, 'MERCHANT_NOTICE_DISPATCH_WORKER_IDLE_SECONDS', 2) or 2), 0.5)


def get_dispatch_worker_stale_seconds():
    return max(parse_int(getattr(settings, 'MERCHANT_NOTICE_DISPATCH_WORKER_STALE_SECONDS', 600), 600), 60)


def get_dispatch_worker_concurrency():
    return max(parse_int(getattr(settings, 'MERCHANT_NOTICE_DISPATCH_CONCURRENCY', 20), 20), 1)


def get_dispatch_worker_heartbeat_seconds():
    return max(
        float(getattr(settings, 'MERCHANT_NOTICE_DISPATCH_WORKER_HEARTBEAT_SECONDS', 15) or 15),
        5,
    )


def get_dispatch_worker_progress_batch_size():
    return max(parse_int(getattr(settings, 'MERCHANT_NOTICE_DISPATCH_PROGRESS_BATCH_SIZE', 50), 50), 1)


def get_wechat_gateway_retry_delay_seconds():
    return max(float(getattr(settings, 'MERCHANT_NOTICE_WECHAT_443_RETRY_DELAY_SECONDS', 3) or 3), 0.1)


def get_wechat_gateway_refresh_threshold():
    return max(parse_int(getattr(settings, 'MERCHANT_NOTICE_WECHAT_443_REFRESH_THRESHOLD', 3), 3), 1)


def enqueue_dispatch_job(job_key, job_type, snapshot=None, payload=None, target_count=0):
    now = make_naive_local(get_local_now())
    payload_json = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
    defaults = {
        'job_type': job_type,
        'snapshot': snapshot,
        'status': MerchantNoticeDispatchJob.STATUS_PENDING,
        'payload_json': payload_json,
        'target_count': max(parse_int(target_count, 0), 0),
        'success_count': 0,
        'failed_count': 0,
        'skipped_count': 0,
        'last_error': '',
        'started_at': None,
        'finished_at': None,
        'created_at': now,
    }
    job, created = MerchantNoticeDispatchJob.objects.get_or_create(job_key=job_key, defaults=defaults)
    if created:
        return job, True

    updated_fields = []
    if snapshot and job.snapshot_id != snapshot.id:
        job.snapshot = snapshot
        updated_fields.append('snapshot')
    if job.payload_json != payload_json:
        job.payload_json = payload_json
        updated_fields.append('payload_json')
    if max(parse_int(target_count, 0), 0) > max(parse_int(job.target_count, 0), 0):
        job.target_count = max(parse_int(target_count, 0), 0)
        updated_fields.append('target_count')
    if updated_fields:
        job.updated_at = now
        updated_fields.append('updated_at')
        job.save(update_fields=updated_fields)
    return job, False


def finalize_dispatch_job(job, status, target_count, success_count, failed_count, skipped_count, last_error=''):
    now = make_naive_local(get_local_now())
    job.status = status
    job.target_count = max(parse_int(target_count, 0), 0)
    job.success_count = max(parse_int(success_count, 0), 0)
    job.failed_count = max(parse_int(failed_count, 0), 0)
    job.skipped_count = max(parse_int(skipped_count, 0), 0)
    job.last_error = normalize_text(last_error, 255)
    job.finished_at = now
    job.updated_at = now
    job.save(update_fields=[
        'status',
        'target_count',
        'success_count',
        'failed_count',
        'skipped_count',
        'last_error',
        'finished_at',
        'updated_at',
    ])

    if job.snapshot_id:
        job.snapshot.notification_target_count = job.target_count
        job.snapshot.notification_success_count = job.success_count
        if failed_count == 0:
            job.snapshot.notification_dispatched_at = now
        job.snapshot.save(update_fields=[
            'notification_target_count',
            'notification_success_count',
            'notification_dispatched_at',
        ])

    return serialize_dispatch_job(job)


def heartbeat_dispatch_job(job, target_count, success_count, failed_count, skipped_count, force=False):
    if not job:
        return

    processed_count = (
        max(parse_int(success_count, 0), 0)
        + max(parse_int(failed_count, 0), 0)
        + max(parse_int(skipped_count, 0), 0)
    )
    now_monotonic = time.monotonic()
    next_heartbeat_monotonic = float(getattr(job, '_next_heartbeat_monotonic', 0.0) or 0.0)
    should_flush = force or processed_count <= 0

    if not should_flush and processed_count % get_dispatch_worker_progress_batch_size() == 0:
        should_flush = True
    if not should_flush and now_monotonic >= next_heartbeat_monotonic:
        should_flush = True
    if not should_flush:
        return

    now = make_naive_local(get_local_now())
    MerchantNoticeDispatchJob.objects.filter(
        id=job.id,
        status=MerchantNoticeDispatchJob.STATUS_RUNNING,
    ).update(
        target_count=max(parse_int(target_count, 0), 0),
        success_count=max(parse_int(success_count, 0), 0),
        failed_count=max(parse_int(failed_count, 0), 0),
        skipped_count=max(parse_int(skipped_count, 0), 0),
        updated_at=now,
    )
    job.target_count = max(parse_int(target_count, 0), 0)
    job.success_count = max(parse_int(success_count, 0), 0)
    job.failed_count = max(parse_int(failed_count, 0), 0)
    job.skipped_count = max(parse_int(skipped_count, 0), 0)
    job.updated_at = now
    job._next_heartbeat_monotonic = now_monotonic + get_dispatch_worker_heartbeat_seconds()


class DispatchProgressTracker:
    def __init__(self, job, target_count):
        self.job = job
        self.target_count = max(parse_int(target_count, 0), 0)
        self.success_count = 0
        self.failed_count = 0
        self.skipped_count = 0
        self.last_error = ''
        self._lock = threading.Lock()
        self.job._next_heartbeat_monotonic = time.monotonic() + get_dispatch_worker_heartbeat_seconds()
        heartbeat_dispatch_job(self.job, self.target_count, 0, 0, 0, force=True)

    def record_success(self):
        self._record(success_delta=1)

    def record_skipped(self):
        self._record(skipped_delta=1)

    def record_failure(self, error_message=''):
        self._record(failed_delta=1, error_message=error_message)

    def flush(self, force=False):
        with self._lock:
            heartbeat_dispatch_job(
                self.job,
                self.target_count,
                self.success_count,
                self.failed_count,
                self.skipped_count,
                force=force,
            )

    def snapshot(self):
        with self._lock:
            return {
                'targetCount': self.target_count,
                'successCount': self.success_count,
                'failedCount': self.failed_count,
                'skippedCount': self.skipped_count,
                'lastError': self.last_error,
            }

    def _record(self, success_delta=0, failed_delta=0, skipped_delta=0, error_message=''):
        with self._lock:
            self.success_count += max(parse_int(success_delta, 0), 0)
            self.failed_count += max(parse_int(failed_delta, 0), 0)
            self.skipped_count += max(parse_int(skipped_delta, 0), 0)
            if error_message:
                self.last_error = normalize_text(error_message, 255)
            heartbeat_dispatch_job(
                self.job,
                self.target_count,
                self.success_count,
                self.failed_count,
                self.skipped_count,
            )


def load_dispatch_subscription(subscription_id):
    normalized_id = parse_int(subscription_id, 0)
    if normalized_id <= 0:
        return None
    return MerchantNoticeSubscription.objects.filter(id=normalized_id).first()


def send_dispatch_target(subscription, snapshot, target):
    if not subscription:
        return {
            'status': 'skipped',
            'reason': 'missing_subscription',
        }

    if target.get('messagePayload'):
        message_payload = target['messagePayload']
        message_body = build_manual_subscribe_message_body(subscription, message_payload)
        return send_subscribe_message(
            subscription,
            snapshot,
            message_body=message_body,
            special_item_names=message_payload.get('thing7'),
        )

    return send_subscribe_message(
        subscription,
        snapshot,
        special_item_names=target.get('matchedNames') or [],
    )


def send_dispatch_target_with_recovery(subscription, snapshot, target, worker_state):
    try:
        result = send_dispatch_target(subscription, snapshot, target)
        worker_state['consecutive_gateway_errors'] = 0
        return result
    except MerchantNoticeSourceError as error:
        error_message = str(error)
        if not is_wechat_gateway_request_error_message(error_message):
            worker_state['consecutive_gateway_errors'] = 0
            raise

        worker_state['consecutive_gateway_errors'] += 1
        if worker_state['consecutive_gateway_errors'] >= get_wechat_gateway_refresh_threshold():
            clear_wechat_access_token_cache()
            logger.warning(
                'merchant dispatch worker refreshing access token after consecutive gateway failures subscription_id=%s snapshot_id=%s failures=%s',
                getattr(subscription, 'id', 0),
                getattr(snapshot, 'id', 0),
                worker_state['consecutive_gateway_errors'],
            )
            worker_state['consecutive_gateway_errors'] = 0

        delay_seconds = get_wechat_gateway_retry_delay_seconds()
        logger.warning(
            'merchant dispatch worker retrying after gateway failure subscription_id=%s snapshot_id=%s delay_seconds=%s error=%s',
            getattr(subscription, 'id', 0),
            getattr(snapshot, 'id', 0),
            delay_seconds,
            error_message,
        )
        time.sleep(delay_seconds)
        try:
            retry_result = send_dispatch_target(subscription, snapshot, target)
        except MerchantNoticeSourceError as retry_error:
            if is_wechat_gateway_request_error_message(str(retry_error)):
                worker_state['consecutive_gateway_errors'] += 1
            else:
                worker_state['consecutive_gateway_errors'] = 0
            raise
        worker_state['consecutive_gateway_errors'] = 0
        return retry_result


def process_dispatch_target_slice(job, snapshot, targets, progress_tracker):
    close_old_connections()
    worker_state = {
        'consecutive_gateway_errors': 0,
    }
    last_error = ''

    try:
        for target in targets:
            subscription = load_dispatch_subscription(target.get('subscriptionId'))
            if not subscription:
                progress_tracker.record_skipped()
                worker_state['consecutive_gateway_errors'] = 0
                continue

            try:
                result = send_dispatch_target_with_recovery(subscription, snapshot, target, worker_state)
                if result['status'] == 'success':
                    progress_tracker.record_success()
                elif result['status'] == 'skipped':
                    progress_tracker.record_skipped()
                else:
                    error_message = result.get('errorMessage') or result.get('errorCode') or ''
                    progress_tracker.record_failure(error_message)
                    last_error = error_message or last_error
            except MerchantNoticeSourceError as error:
                last_error = str(error)
                progress_tracker.record_failure(last_error)
                logger.warning(
                    'merchant concurrent dispatch source failure subscription_id=%s snapshot_id=%s error=%s',
                    subscription.id,
                    getattr(snapshot, 'id', 0),
                    error,
                )
            except Exception as error:
                last_error = str(error)
                progress_tracker.record_failure(last_error)
                logger.exception(
                    'merchant concurrent dispatch unexpected failure subscription_id=%s snapshot_id=%s',
                    subscription.id,
                    getattr(snapshot, 'id', 0),
                )
    finally:
        close_wechat_subscribe_send_session()
        close_old_connections()

    return {
        'lastError': last_error,
        'processedCount': len(targets),
    }


def process_dispatch_targets_concurrently(job, snapshot, targets):
    target_list = list(targets or [])
    progress_tracker = DispatchProgressTracker(job, len(target_list))
    if not target_list:
        return progress_tracker.snapshot()

    concurrency = min(get_dispatch_worker_concurrency(), len(target_list))
    target_slices = split_dispatch_targets(target_list, concurrency)
    future_results = []

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix='merchant-dispatch') as executor:
        futures = [
            executor.submit(process_dispatch_target_slice, job, snapshot, target_slice, progress_tracker)
            for target_slice in target_slices
        ]
        for future in futures:
            future_results.append(future.result())

    progress_tracker.flush(force=True)
    summary = progress_tracker.snapshot()
    for future_result in future_results:
        if future_result.get('lastError'):
            summary['lastError'] = future_result['lastError']
    return summary


def process_dispatch_job(job):
    if not job:
        return {'status': 'idle'}

    payload = parse_dispatch_job_payload(job.payload_json)
    last_error = ''
    target_count = 0
    success_count = 0
    failed_count = 0
    skipped_count = 0

    try:
        if job.job_type == MerchantNoticeDispatchJob.JOB_TYPE_MANUAL:
            message_payload = payload.get('message') if isinstance(payload.get('message'), dict) else {}
            summary = process_dispatch_targets_concurrently(
                job,
                job.snapshot,
                build_manual_dispatch_targets(message_payload),
            )
        else:
            summary = process_dispatch_targets_concurrently(
                job,
                job.snapshot,
                build_snapshot_dispatch_targets(job.snapshot),
            )
        target_count = max(parse_int(summary.get('targetCount'), 0), 0)
        success_count = max(parse_int(summary.get('successCount'), 0), 0)
        failed_count = max(parse_int(summary.get('failedCount'), 0), 0)
        skipped_count = max(parse_int(summary.get('skippedCount'), 0), 0)
        last_error = summary.get('lastError') or ''
    except Exception as error:
        last_error = str(error)
        return finalize_dispatch_job(
            job,
            MerchantNoticeDispatchJob.STATUS_FAILED,
            target_count,
            success_count,
            failed_count + 1,
            skipped_count,
            last_error=last_error,
        )

    final_status = MerchantNoticeDispatchJob.STATUS_COMPLETED
    if failed_count > 0:
        final_status = MerchantNoticeDispatchJob.STATUS_PARTIAL_FAILED
    return finalize_dispatch_job(
        job,
        final_status,
        target_count,
        success_count,
        failed_count,
        skipped_count,
        last_error=last_error,
    )


def claim_next_dispatch_job():
    now = make_naive_local(get_local_now())
    stale_before = now - timedelta(seconds=get_dispatch_worker_stale_seconds())
    with transaction.atomic():
        MerchantNoticeDispatchJob.objects.filter(
            status=MerchantNoticeDispatchJob.STATUS_RUNNING,
            updated_at__lt=stale_before,
        ).update(
            status=MerchantNoticeDispatchJob.STATUS_PENDING,
            started_at=None,
            updated_at=now,
            last_error='worker_stale_reset',
        )

        job = MerchantNoticeDispatchJob.objects.select_for_update().filter(
            status=MerchantNoticeDispatchJob.STATUS_PENDING,
        ).order_by('id').first()
        if not job:
            return None

        job.status = MerchantNoticeDispatchJob.STATUS_RUNNING
        job.started_at = now
        job.finished_at = None
        job.last_error = ''
        job.updated_at = now
        job.save(update_fields=[
            'status',
            'started_at',
            'finished_at',
            'last_error',
            'updated_at',
        ])
        return job.id


def run_dispatch_worker_once():
    job_id = claim_next_dispatch_job()
    if not job_id:
        return {'status': 'idle'}

    job = MerchantNoticeDispatchJob.objects.select_related('snapshot').filter(id=job_id).first()
    if not job:
        return {'status': 'missing_job'}
    return process_dispatch_job(job)


def broadcast_manual_message(payload):
    service_status = get_notice_service_status()
    if not service_status['ready']:
        raise MerchantNoticeConfigurationError(f'远行提醒未完成通知配置：{service_status["message"]}')

    preview = build_manual_subscribe_message_body(type('PreviewSubscription', (), {'openid': 'preview'})(), payload)
    preview.pop('touser', None)
    campaign_key = resolve_manual_campaign_key(payload.get('campaignKey'))
    snapshot_fingerprint = build_manual_snapshot_fingerprint(campaign_key)
    filter_summary = build_manual_dispatch_filter_summary(payload)
    target_count = get_manual_dispatch_queryset(payload).count()

    if payload.get('dryRun'):
        return {
            'dryRun': True,
            'campaignKey': campaign_key,
            'snapshotFingerprint': snapshot_fingerprint,
            'targetCount': target_count,
            'filter': filter_summary,
            'messagePreview': preview,
        }

    if target_count <= 0:
        return {
            'dryRun': False,
            'created': False,
            'queued': False,
            'campaignKey': campaign_key,
            'snapshotFingerprint': snapshot_fingerprint,
            'targetCount': 0,
            'filter': filter_summary,
            'messagePreview': preview,
        }

    snapshot, _created_snapshot = get_or_create_manual_snapshot(payload, campaign_key=campaign_key)
    job, created = enqueue_dispatch_job(
        build_manual_dispatch_job_key(campaign_key),
        MerchantNoticeDispatchJob.JOB_TYPE_MANUAL,
        snapshot=snapshot,
        payload={'message': payload},
        target_count=target_count,
    )
    return {
        'dryRun': False,
        'created': created,
        'queued': job.status == MerchantNoticeDispatchJob.STATUS_PENDING,
        'campaignKey': campaign_key,
        'snapshotFingerprint': snapshot.fingerprint,
        'targetCount': target_count,
        'filter': filter_summary,
        'messagePreview': preview,
        'job': serialize_dispatch_job(job),
    }


def dispatch_snapshot_notifications(snapshot):
    service_status = get_notice_service_status()
    if not service_status['ready']:
        raise MerchantNoticeConfigurationError(f'远行提醒未完成通知配置：{service_status["message"]}')

    target_count = count_snapshot_dispatch_targets(snapshot)
    if target_count <= 0:
        snapshot.notification_target_count = 0
        snapshot.notification_success_count = 0
        snapshot.save(update_fields=[
            'notification_target_count',
            'notification_success_count',
        ])
        logger.info(
            'merchant snapshot no matching subscriptions snapshot_id=%s slot_date=%s round=%s items=%s special_items=%s',
            snapshot.id,
            snapshot.slot_date.isoformat(),
            snapshot.round,
            len(load_items_json(snapshot.items_json)),
            snapshot.special_item_names,
        )
        return {
            'status': 'no_matching_subscriptions',
            'targetCount': 0,
            'successCount': 0,
        }

    job, created = enqueue_dispatch_job(
        build_snapshot_dispatch_job_key(snapshot),
        MerchantNoticeDispatchJob.JOB_TYPE_SNAPSHOT,
        snapshot=snapshot,
        payload={'source': 'watch_current_merchant'},
        target_count=target_count,
    )
    logger.info(
        'merchant snapshot queued snapshot_id=%s job_key=%s created=%s target_count=%s special_items=%s',
        snapshot.id,
        job.job_key,
        created,
        target_count,
        snapshot.special_item_names,
    )
    return {
        'status': 'queued',
        'created': created,
        'targetCount': target_count,
        'successCount': job.success_count,
        'failedCount': job.failed_count,
        'skippedCount': job.skipped_count,
        'job': serialize_dispatch_job(job),
    }


def watch_current_merchant(timeout_seconds=None, poll_interval_seconds=None):
    timeout_seconds = float(timeout_seconds or getattr(settings, 'MERCHANT_NOTIFY_POLL_TIMEOUT_SECONDS', 600))
    poll_interval_seconds = float(poll_interval_seconds or getattr(settings, 'MERCHANT_NOTIFY_POLL_INTERVAL_SECONDS', 30))
    started_at = time.monotonic()
    attempt_count = 0
    latest_snapshot = get_latest_snapshot()
    last_observed_payload = {}
    last_source_attempts = []

    logger.info(
        'merchant watch begin latest_snapshot_id=%s latest_fingerprint=%s timeout_seconds=%s poll_interval_seconds=%s',
        latest_snapshot.id if latest_snapshot else None,
        latest_snapshot.fingerprint if latest_snapshot else '',
        timeout_seconds,
        poll_interval_seconds,
    )

    while True:
        attempt_count += 1
        watch_probe = fetch_watch_source_payload(latest_snapshot=latest_snapshot)
        payload = watch_probe['payload']
        payload_summary = dict(watch_probe['payloadSummary'])
        payload_summary['sourceName'] = watch_probe['sourceName']
        last_source_attempts = watch_probe['sourceAttempts']
        last_observed_payload = payload_summary
        logger.info(
            'merchant watch attempt=%s payload=%s source_attempts=%s',
            attempt_count,
            json.dumps(payload_summary, ensure_ascii=False, sort_keys=True),
            json.dumps(last_source_attempts, ensure_ascii=False, sort_keys=True),
        )
        if latest_snapshot and latest_snapshot.fingerprint == payload['fingerprint']:
            if time.monotonic() - started_at >= timeout_seconds:
                logger.info(
                    'merchant watch timeout unchanged attempt_count=%s latest_fingerprint=%s last_payload=%s source_attempts=%s',
                    attempt_count,
                    latest_snapshot.fingerprint,
                    json.dumps(last_observed_payload, ensure_ascii=False, sort_keys=True),
                    json.dumps(last_source_attempts, ensure_ascii=False, sort_keys=True),
                )
                return {
                    'status': 'timeout',
                    'attemptCount': attempt_count,
                    'latestFingerprint': latest_snapshot.fingerprint,
                    'lastObservedPayload': last_observed_payload,
                    'sourceAttempts': last_source_attempts,
                }
            time.sleep(max(poll_interval_seconds, 1))
            continue

        if not is_ready_snapshot_payload(payload):
            if time.monotonic() - started_at >= timeout_seconds:
                logger.info(
                    'merchant watch timeout waiting_ready_payload attempt_count=%s last_payload=%s source_attempts=%s',
                    attempt_count,
                    json.dumps(last_observed_payload, ensure_ascii=False, sort_keys=True),
                    json.dumps(last_source_attempts, ensure_ascii=False, sort_keys=True),
                )
                return {
                    'status': 'timeout_waiting_ready_payload',
                    'attemptCount': attempt_count,
                    'latestFingerprint': latest_snapshot.fingerprint if latest_snapshot else '',
                    'lastObservedPayload': last_observed_payload,
                    'sourceAttempts': last_source_attempts,
                }
            logger.info(
                'merchant watch waiting for non-empty payload attempt=%s source=%s slot_date=%s round=%s fingerprint=%s',
                attempt_count,
                payload_summary.get('sourceName', ''),
                payload_summary['slotDate'],
                payload_summary['round'],
                payload_summary['fingerprint'],
            )
            time.sleep(max(poll_interval_seconds, 1))
            continue

        if latest_snapshot and snapshot_has_items(latest_snapshot) and is_same_snapshot_round(latest_snapshot, payload):
            if time.monotonic() - started_at >= timeout_seconds:
                logger.info(
                    'merchant watch timeout same_round_changed attempt_count=%s latest_round=%s latest_fingerprint=%s last_payload=%s source_attempts=%s',
                    attempt_count,
                    latest_snapshot.round,
                    latest_snapshot.fingerprint,
                    json.dumps(last_observed_payload, ensure_ascii=False, sort_keys=True),
                    json.dumps(last_source_attempts, ensure_ascii=False, sort_keys=True),
                )
                return {
                    'status': 'timeout_same_round_changed',
                    'attemptCount': attempt_count,
                    'latestFingerprint': latest_snapshot.fingerprint,
                    'lastObservedPayload': last_observed_payload,
                    'sourceAttempts': last_source_attempts,
                }
            logger.info(
                'merchant watch waiting for newer round because current round already recorded attempt=%s latest_round=%s payload_round=%s fingerprint=%s',
                attempt_count,
                latest_snapshot.round,
                payload_summary['round'],
                payload_summary['fingerprint'],
            )
            time.sleep(max(poll_interval_seconds, 1))
            continue

        with transaction.atomic():
            snapshot, created = create_snapshot_from_payload(payload)
        logger.info(
            'merchant watch snapshot ready snapshot_id=%s created=%s payload=%s',
            snapshot.id,
            created,
            json.dumps(payload_summary, ensure_ascii=False, sort_keys=True),
        )
        if not latest_snapshot:
            return {
                'status': 'baseline_created',
                'attemptCount': attempt_count,
                'created': created,
                'sourceAttempts': last_source_attempts,
                'snapshot': serialize_snapshot(snapshot),
                'notification': {
                    'status': 'baseline_skipped',
                    'targetCount': 0,
                    'successCount': 0,
                },
            }

        notification = dispatch_snapshot_notifications(snapshot)
        return {
            'status': 'changed',
            'attemptCount': attempt_count,
            'created': created,
            'sourceAttempts': last_source_attempts,
            'snapshot': serialize_snapshot(snapshot),
            'notification': notification,
        }


def run_guarded_watch_current_merchant(timeout_seconds=None, poll_interval_seconds=None, force=False):
    guard_state = begin_watch_job_run(force=force, guard_seconds=get_trigger_guard_seconds())
    if not guard_state['allowed']:
        return {
            'status': 'skipped_guard_window',
            'guardSeconds': guard_state['guardSeconds'],
            'remainingSeconds': guard_state['remainingSeconds'],
            'lastTriggeredAt': guard_state['lastTriggeredAt'],
            'lastStatus': guard_state['lastStatus'],
        }

    try:
        result = watch_current_merchant(
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
    except Exception as error:
        finish_watch_job_run(MerchantNoticeJobState.STATUS_FAILED, {
            'status': 'failed',
            'error': str(error),
        })
        raise

    final_status = MerchantNoticeJobState.STATUS_COMPLETED
    if result.get('status') == 'timeout':
        final_status = MerchantNoticeJobState.STATUS_COMPLETED
    finish_watch_job_run(final_status, result)
    return result


def verify_job_token(token):
    configured_token = str(getattr(settings, 'MERCHANT_NOTIFY_JOB_TOKEN', '') or '').strip()
    if not configured_token:
        raise MerchantNoticeConfigurationError('未配置 MERCHANT_NOTIFY_JOB_TOKEN')

    provided_token = str(token or '').strip()
    if not provided_token:
        raise MerchantNoticePermissionError('缺少远行提醒任务令牌')

    if not hmac.compare_digest(provided_token, configured_token):
        raise MerchantNoticePermissionError('远行提醒任务令牌无效')


def verify_job_request(token, remote_ip=''):
    if is_loopback_ip(remote_ip):
        return

    configured_token = str(getattr(settings, 'MERCHANT_NOTIFY_JOB_TOKEN', '') or '').strip()
    if not configured_token:
        raise MerchantNoticeConfigurationError('未配置 MERCHANT_NOTIFY_JOB_TOKEN，且当前请求不是容器内回环调用')

    verify_job_token(token)
