import copy
import hashlib
import hmac
import json
import logging
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone as dt_timezone
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from django.conf import settings
from django.db import transaction

from wxcloudrun.models import (
    MerchantNoticeJobState,
    MerchantNoticeSendLog,
    MerchantNoticeSubscription,
    MerchantSnapshot,
)


logger = logging.getLogger('log')

DEFAULT_SPECIAL_KEYWORDS = ('炫彩', '棱镜球', '同乘', '祝福项坠')
DEFAULT_NOTICE_ADVICE = '建议点击打开蛋查查，并开启下次提醒'
ROUND_START_HOURS = (8, 12, 16, 20)
WECHAT_TOKEN_URL = 'https://api.weixin.qq.com/cgi-bin/token'
WECHAT_SUBSCRIBE_SEND_URL = 'https://api.weixin.qq.com/cgi-bin/message/subscribe/send'
WATCH_JOB_KEY = 'merchant_watch'
MANUAL_SNAPSHOT_FINGERPRINT_PREFIX = 'manual-'
SERVICE_REQUIRED_CONFIGS = (
    ('WECHAT_APP_ID', '微信小程序 AppID'),
    ('WECHAT_APP_SECRET', '微信小程序 AppSecret'),
    ('MERCHANT_NOTIFY_TEMPLATE_ID', '订阅消息模板 ID'),
)

_CURRENT_PAYLOAD_CACHE = {
    'payload': None,
    'expires_at': 0.0,
}
_ACCESS_TOKEN_CACHE = {
    'token': '',
    'expires_at': 0.0,
}


class MerchantNoticeError(Exception):
    pass


class MerchantNoticeConfigurationError(MerchantNoticeError):
    pass


class MerchantNoticePermissionError(MerchantNoticeError):
    pass


class MerchantNoticeSourceError(MerchantNoticeError):
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


def get_trigger_guard_seconds():
    return max(parse_int(getattr(settings, 'MERCHANT_NOTIFY_TRIGGER_GUARD_SECONDS', 1800), 1800), 0)


def get_special_keywords():
    raw = str(getattr(settings, 'MERCHANT_NOTIFY_SPECIAL_KEYWORDS', '') or '').strip()
    if not raw:
        return list(DEFAULT_SPECIAL_KEYWORDS)

    keywords = []
    for item in raw.replace('，', ',').split(','):
        cleaned = normalize_text(item, 32)
        if cleaned and cleaned not in keywords:
            keywords.append(cleaned)
    return keywords or list(DEFAULT_SPECIAL_KEYWORDS)


def has_special_keyword(name):
    item_name = normalize_text(name, 64)
    return any(keyword in item_name for keyword in get_special_keywords())


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


def compute_payload_fingerprint(payload):
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def normalize_source_item(item):
    name = normalize_text(item.get('name'), 64)
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


def normalize_source_payload(raw_payload):
    slot_date = parse_slot_date(raw_payload.get('slot_date'))
    round_number = max(parse_int(raw_payload.get('round'), 1), 1)
    total_rounds = max(parse_int(raw_payload.get('total_rounds'), 4), 1)
    items = [
        normalize_source_item(item)
        for item in (raw_payload.get('items') or [])
        if isinstance(item, dict) and normalize_text(item.get('name'), 64)
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


def build_source_headers():
    return {
        'Accept': 'application/json,text/plain,*/*',
        'Referer': str(getattr(settings, 'MERCHANT_SOURCE_REFERER', '') or '').strip(),
        'User-Agent': str(getattr(settings, 'MERCHANT_SOURCE_USER_AGENT', '') or '').strip(),
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
    }


def fetch_source_payload(force=False, use_cache=True):
    cache_ttl = max(int(getattr(settings, 'MERCHANT_NOTICE_CACHE_TTL_SECONDS', 30) or 30), 0)
    if use_cache and not force and _CURRENT_PAYLOAD_CACHE['payload'] and _CURRENT_PAYLOAD_CACHE['expires_at'] > time.monotonic():
        return copy.deepcopy(_CURRENT_PAYLOAD_CACHE['payload'])

    try:
        response = requests.get(
            settings.MERCHANT_SOURCE_URL,
            headers=build_source_headers(),
            timeout=float(getattr(settings, 'MERCHANT_NOTIFY_FETCH_TIMEOUT_SECONDS', 8)),
        )
    except requests.RequestException as error:
        raise MerchantNoticeSourceError(f'远行商人源站请求失败: {error}') from error

    if response.status_code < 200 or response.status_code >= 300:
        raise MerchantNoticeSourceError(f'远行商人源站返回异常状态码: {response.status_code}')

    try:
        raw_payload = response.json()
    except ValueError as error:
        raise MerchantNoticeSourceError('远行商人源站返回了非 JSON 数据') from error

    normalized = normalize_source_payload(raw_payload if isinstance(raw_payload, dict) else {})
    _CURRENT_PAYLOAD_CACHE['payload'] = copy.deepcopy(normalized)
    _CURRENT_PAYLOAD_CACHE['expires_at'] = time.monotonic() + cache_ttl
    return normalized


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
        'specialItemNames': [
            name for name in str(snapshot.special_item_names or '').split('、')
            if name
        ],
        'fingerprint': snapshot.fingerprint,
    }


def get_latest_snapshot(include_manual=False):
    queryset = MerchantSnapshot.objects.order_by('-slot_date', '-round', '-id')
    if not include_manual:
        queryset = queryset.exclude(fingerprint__startswith=MANUAL_SNAPSHOT_FINGERPRINT_PREFIX)
    return queryset.first()


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
        current_payload = fetch_source_payload(force=False, use_cache=True)
        return current_payload, False
    except MerchantNoticeSourceError:
        if latest_snapshot:
            return serialize_snapshot(latest_snapshot), True
        raise


def build_subscription_state(record):
    if not record:
        return {
            'status': MerchantNoticeSubscription.STATUS_IDLE,
            'isActive': False,
            'buttonText': '开启下一次提醒',
            'helperText': '完成授权后，命中珍贵商品时会提醒您一次。',
            'subscribedAt': '',
            'consumedAt': '',
        }

    if record.status == MerchantNoticeSubscription.STATUS_ACTIVE:
        button_text = '已开启下一次提醒'
        helper_text = '已记录本次授权，命中珍贵商品时会提醒您一次。'
    elif record.status == MerchantNoticeSubscription.STATUS_CONSUMED:
        button_text = '重新开启下一次提醒'
        helper_text = '上一次提醒已发送，回到蛋查查后可手动开启下一次提醒。'
    elif record.status == MerchantNoticeSubscription.STATUS_INVALID:
        button_text = '重新开启下一次提醒'
        helper_text = '当前提醒已失效，请重新授权订阅消息。'
    else:
        button_text = '开启下一次提醒'
        helper_text = '完成授权后，命中珍贵商品时会提醒您一次。'

    return {
        'status': record.status,
        'isActive': record.status == MerchantNoticeSubscription.STATUS_ACTIVE,
        'buttonText': button_text,
        'helperText': helper_text,
        'subscribedAt': format_iso_datetime(record.subscribed_at),
        'consumedAt': format_iso_datetime(record.consumed_at),
    }


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


def sanitize_current_payload_for_client(payload):
    sanitized_payload = copy.deepcopy(payload or {})
    sanitized_items = []

    for item in sanitized_payload.get('items') or []:
        if not isinstance(item, dict):
            continue
        sanitized_item = dict(item)
        sanitized_item.pop('image', None)
        sanitized_items.append(sanitized_item)

    sanitized_payload['items'] = sanitized_items
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
        'current': payload,
        'subscription': build_subscription_state(subscription),
    }


def get_or_create_subscription(openid, appid):
    now = make_naive_local(get_local_now())
    defaults = {
        'openid_hash': hashlib.sha256(f'{settings.SECRET_KEY}:{openid}'.encode('utf-8')).hexdigest(),
        'appid': normalize_text(appid, 64),
        'template_id': normalize_text(getattr(settings, 'MERCHANT_NOTIFY_TEMPLATE_ID', ''), 128),
        'status': MerchantNoticeSubscription.STATUS_ACTIVE,
        'subscribed_at': now,
        'consumed_at': None,
        'last_error_code': '',
        'last_error_message': '',
        'notify_count': 0,
        'created_at': now,
        'updated_at': now,
    }
    subscription, created = MerchantNoticeSubscription.objects.get_or_create(openid=openid, defaults=defaults)
    if created:
        return subscription, True

    subscription.openid_hash = defaults['openid_hash']
    subscription.appid = defaults['appid']
    subscription.template_id = defaults['template_id']
    subscription.status = MerchantNoticeSubscription.STATUS_ACTIVE
    subscription.subscribed_at = now
    subscription.consumed_at = None
    subscription.last_error_code = ''
    subscription.last_error_message = ''
    subscription.updated_at = now
    subscription.save(update_fields=[
        'openid_hash',
        'appid',
        'template_id',
        'status',
        'subscribed_at',
        'consumed_at',
        'last_error_code',
        'last_error_message',
        'updated_at',
    ])
    return subscription, False


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
        'subscription': build_subscription_state(subscription),
    }


def build_wechat_access_token():
    appid = normalize_text(getattr(settings, 'WECHAT_APP_ID', ''), 128)
    secret = normalize_text(getattr(settings, 'WECHAT_APP_SECRET', ''), 128)
    if not appid or not secret:
        raise MerchantNoticeConfigurationError('未配置 WECHAT_APP_ID 或 WECHAT_APP_SECRET')

    if _ACCESS_TOKEN_CACHE['token'] and _ACCESS_TOKEN_CACHE['expires_at'] > time.monotonic():
        return _ACCESS_TOKEN_CACHE['token']

    try:
        response = requests.get(
            WECHAT_TOKEN_URL,
            params={
                'grant_type': 'client_credential',
                'appid': appid,
                'secret': secret,
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

    expires_in = max(parse_int(data.get('expires_in'), 7200), 120)
    _ACCESS_TOKEN_CACHE['token'] = token
    _ACCESS_TOKEN_CACHE['expires_at'] = time.monotonic() + max(expires_in - 120, 60)
    return token


def truncate_text(value, max_length):
    text = normalize_text(value, max_length * 4)
    if len(text) <= max_length:
        return text
    return text[:max_length]


def build_special_item_summary(names):
    normalized_names = [normalize_text(name, 20) for name in names if normalize_text(name, 20)]
    if not normalized_names:
        return '检测到珍贵货物'
    joined = '、'.join(normalized_names)
    if len(joined) <= 20:
        return joined
    first_name = normalized_names[0]
    if len(normalized_names) == 1:
        return truncate_text(first_name, 20)
    return truncate_text(f'{first_name}等{len(normalized_names)}件', 20)


def build_subscribe_message_body(subscription, snapshot):
    special_names = [
        name for name in str(snapshot.special_item_names or '').split('、')
        if name
    ]
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
                'value': truncate_text(DEFAULT_NOTICE_ADVICE, 20),
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

    request_payload = message_body or build_subscribe_message_body(subscription, snapshot)
    access_token = build_wechat_access_token()
    try:
        response = requests.post(
            WECHAT_SUBSCRIBE_SEND_URL,
            params={'access_token': access_token},
            json=request_payload,
            timeout=float(getattr(settings, 'MERCHANT_NOTIFY_FETCH_TIMEOUT_SECONDS', 8)),
            verify=False,
        )
    except requests.RequestException as error:
        raise MerchantNoticeSourceError(f'微信订阅消息发送失败: {error}') from error

    try:
        data = response.json()
    except ValueError as error:
        raise MerchantNoticeSourceError('微信订阅消息发送接口返回了非 JSON 数据') from error

    errcode = str(data.get('errcode', ''))
    errmsg = normalize_text(data.get('errmsg'), 255)
    msg_id = normalize_text(data.get('msgid') or data.get('msg_id'), 64)
    special_item_names = normalize_text(special_item_names or snapshot.special_item_names, 255)
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

        subscription.status = MerchantNoticeSubscription.STATUS_CONSUMED
        subscription.consumed_at = now
        subscription.last_notified_snapshot = snapshot
        subscription.notify_count = max(parse_int(subscription.notify_count, 0), 0) + 1
        subscription.last_error_code = ''
        subscription.last_error_message = ''
        subscription.updated_at = now
        subscription.save(update_fields=[
            'status',
            'consumed_at',
            'last_notified_snapshot',
            'notify_count',
            'last_error_code',
            'last_error_message',
            'updated_at',
        ])
        return {
            'status': 'success',
            'msgId': msg_id,
        }

    existing_log.status = MerchantNoticeSendLog.STATUS_FAILED
    existing_log.save()

    if errcode in get_invalid_subscription_error_codes():
        subscription.status = MerchantNoticeSubscription.STATUS_INVALID
        subscription.consumed_at = now
    subscription.last_error_code = errcode
    subscription.last_error_message = errmsg
    subscription.updated_at = now
    subscription.save(update_fields=[
        'status',
        'consumed_at',
        'last_error_code',
        'last_error_message',
        'updated_at',
    ])
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
            'special_item_names': '、'.join(payload['specialItemNames']),
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


def broadcast_manual_message(payload):
    service_status = get_notice_service_status()
    if not service_status['ready']:
        raise MerchantNoticeConfigurationError(f'远行提醒未完成通知配置：{service_status["message"]}')

    active_subscriptions = list(
        MerchantNoticeSubscription.objects.filter(
            status=MerchantNoticeSubscription.STATUS_ACTIVE,
        ).order_by('id')
    )

    preview = build_manual_subscribe_message_body(type('PreviewSubscription', (), {'openid': 'preview'})(), payload)
    preview.pop('touser', None)
    campaign_key = resolve_manual_campaign_key(payload.get('campaignKey'))
    snapshot_fingerprint = build_manual_snapshot_fingerprint(campaign_key)
    target_count = len(active_subscriptions)

    if payload.get('dryRun'):
        return {
            'dryRun': True,
            'campaignKey': campaign_key,
            'snapshotFingerprint': snapshot_fingerprint,
            'targetCount': target_count,
            'messagePreview': preview,
        }

    if target_count <= 0:
        return {
            'dryRun': False,
            'created': False,
            'campaignKey': campaign_key,
            'snapshotFingerprint': snapshot_fingerprint,
            'targetCount': 0,
            'successCount': 0,
            'failedCount': 0,
            'skippedCount': 0,
            'lastError': '',
            'messagePreview': preview,
        }

    snapshot, created = get_or_create_manual_snapshot(payload, campaign_key=campaign_key)
    success_count = 0
    failed_count = 0
    skipped_count = 0
    last_error = ''

    for subscription in active_subscriptions:
        message_body = build_manual_subscribe_message_body(subscription, payload)
        result = send_subscribe_message(
            subscription,
            snapshot,
            message_body=message_body,
            special_item_names=payload.get('thing7'),
        )
        if result['status'] == 'success':
            success_count += 1
        elif result['status'] == 'skipped':
            skipped_count += 1
        else:
            failed_count += 1
            last_error = result.get('errorMessage') or result.get('errorCode') or last_error

    snapshot.notification_target_count = len(active_subscriptions)
    snapshot.notification_success_count = success_count
    if failed_count == 0:
        snapshot.notification_dispatched_at = make_naive_local(get_local_now())
    snapshot.save(update_fields=[
        'notification_target_count',
        'notification_success_count',
        'notification_dispatched_at',
    ])

    return {
        'dryRun': False,
        'created': created,
        'campaignKey': campaign_key,
        'snapshotFingerprint': snapshot.fingerprint,
        'targetCount': target_count,
        'successCount': success_count,
        'failedCount': failed_count,
        'skippedCount': skipped_count,
        'lastError': last_error,
        'messagePreview': preview,
    }


def dispatch_snapshot_notifications(snapshot):
    if not snapshot.has_special_hit:
        return {
            'status': 'no_special_hit',
            'targetCount': 0,
            'successCount': 0,
        }
    service_status = get_notice_service_status()
    if not service_status['ready']:
        raise MerchantNoticeConfigurationError(f'远行提醒未完成通知配置：{service_status["message"]}')

    active_subscriptions = list(
        MerchantNoticeSubscription.objects.filter(
            status=MerchantNoticeSubscription.STATUS_ACTIVE,
        ).order_by('id')
    )

    success_count = 0
    failed_count = 0
    skipped_count = 0
    last_error = ''
    for subscription in active_subscriptions:
        result = send_subscribe_message(subscription, snapshot)
        if result['status'] == 'success':
            success_count += 1
        elif result['status'] == 'skipped':
            skipped_count += 1
        else:
            failed_count += 1
            last_error = result.get('errorMessage') or result.get('errorCode') or last_error

    snapshot.notification_target_count = len(active_subscriptions)
    snapshot.notification_success_count = success_count
    if failed_count == 0:
        snapshot.notification_dispatched_at = make_naive_local(get_local_now())
    snapshot.save(update_fields=[
        'notification_target_count',
        'notification_success_count',
        'notification_dispatched_at',
    ])

    return {
        'status': 'sent' if failed_count == 0 else 'partial_failed',
        'targetCount': len(active_subscriptions),
        'successCount': success_count,
        'failedCount': failed_count,
        'skippedCount': skipped_count,
        'lastError': last_error,
    }


def watch_current_merchant(timeout_seconds=None, poll_interval_seconds=None):
    timeout_seconds = float(timeout_seconds or getattr(settings, 'MERCHANT_NOTIFY_POLL_TIMEOUT_SECONDS', 600))
    poll_interval_seconds = float(poll_interval_seconds or getattr(settings, 'MERCHANT_NOTIFY_POLL_INTERVAL_SECONDS', 30))
    started_at = time.monotonic()
    attempt_count = 0
    latest_snapshot = get_latest_snapshot()

    while True:
        attempt_count += 1
        payload = fetch_source_payload(force=True, use_cache=False)
        if latest_snapshot and latest_snapshot.fingerprint == payload['fingerprint']:
            if time.monotonic() - started_at >= timeout_seconds:
                return {
                    'status': 'timeout',
                    'attemptCount': attempt_count,
                    'latestFingerprint': latest_snapshot.fingerprint,
                }
            time.sleep(max(poll_interval_seconds, 1))
            continue

        with transaction.atomic():
            snapshot, created = create_snapshot_from_payload(payload)
        if not latest_snapshot:
            return {
                'status': 'baseline_created',
                'attemptCount': attempt_count,
                'created': created,
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
