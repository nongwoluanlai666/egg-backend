import hashlib
import json
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone

from wxcloudrun.models import Counters, EggFeedback


logger = logging.getLogger('log')

MAX_SNAPSHOT_COUNT = 10
MAX_SPECIES_LENGTH = 64
MAX_REPORTS_PER_HOUR = 20
MAX_REPORTS_PER_HOUR_PER_IP = 60


class ValidationError(Exception):
    pass


def json_response(code=0, error_msg='', data=None, status=200):
    payload = {
        'code': code,
        'errorMsg': error_msg,
    }
    if data is not None:
        payload['data'] = data
    return JsonResponse(payload, status=status, json_dumps_params={'ensure_ascii': False})


def index(request, _):
    return render(request, 'index.html')


def counter(request, _):
    try:
        if request.method == 'GET':
            rsp = get_count()
        elif request.method == 'POST':
            rsp = update_count(request)
        else:
            rsp = json_response(-1, '请求方式错误', status=405)
    except ValidationError as error:
        rsp = json_response(40001, str(error), status=400)
    logger.info('response result: %s', rsp.content.decode('utf-8'))
    return rsp


def egg_feedback(request, _):
    if request.method != 'POST':
        rsp = json_response(-1, '请求方式错误', status=405)
        logger.info('response result: %s', rsp.content.decode('utf-8'))
        return rsp

    try:
        payload = parse_feedback_payload(request)
        existing = find_existing_feedback(payload)
        if existing:
            rsp = json_response(
                0,
                '',
                {
                    'recordId': existing.id,
                    'duplicated': True,
                    'qualityStatus': existing.quality_status,
                    'qualityScore': existing.quality_score,
                }
            )
            logger.info('response result: %s', rsp.content.decode('utf-8'))
            return rsp

        rate_limit_error = get_rate_limit_error(payload)
        if rate_limit_error:
            rsp = json_response(42901, rate_limit_error, status=429)
            logger.info('response result: %s', rsp.content.decode('utf-8'))
            return rsp

        quality_status, quality_score, review_note = build_quality_result(payload)
        record = EggFeedback.objects.create(
            request_id=payload['request_id'],
            prediction_session_id=payload['prediction_session_id'],
            source=payload['source'],
            size=payload['size'],
            weight=payload['weight'],
            rideable_only=payload['rideable_only'],
            confirmed_species=payload['confirmed_species'],
            predicted_species=payload['predicted_species'],
            predicted_rank=payload['predicted_rank'],
            predicted_probability=payload['predicted_probability'],
            prediction_version=payload['prediction_version'],
            prediction_snapshot=json.dumps(payload['prediction_snapshot'], ensure_ascii=False),
            candidate_count=len(payload['prediction_snapshot']),
            is_custom_species=payload['is_custom_species'],
            species_in_snapshot=payload['species_in_snapshot'],
            quality_status=quality_status,
            quality_score=quality_score,
            review_note=review_note,
            appid=payload['appid'],
            openid_hash=payload['openid_hash'],
            ip_hash=payload['ip_hash'],
            user_agent=payload['user_agent'],
        )
        rsp = json_response(
            0,
            '',
            {
                'recordId': record.id,
                'duplicated': False,
                'qualityStatus': quality_status,
                'qualityScore': quality_score,
            }
        )
    except ValidationError as error:
        rsp = json_response(40001, str(error), status=400)
    except Exception:
        logger.exception('egg feedback unexpected error')
        rsp = json_response(50001, '提交失败，请稍后重试', status=500)

    logger.info('response result: %s', rsp.content.decode('utf-8'))
    return rsp


def get_count():
    try:
        data = Counters.objects.get(id=1)
    except Counters.DoesNotExist:
        return json_response(0, '', 0)
    return json_response(0, '', data.count)


def update_count(request):
    logger.info('update_count req: %s', request.body)

    body = parse_json_body(request)
    action = body.get('action')
    if not action:
        return json_response(-1, '缺少 action 参数', status=400)

    if action == 'inc':
        try:
            data = Counters.objects.get(id=1)
        except Counters.DoesNotExist:
            data = Counters(id=1)
        data.count += 1
        data.save()
        return json_response(0, '', data.count)

    if action == 'clear':
        try:
            data = Counters.objects.get(id=1)
            data.delete()
        except Counters.DoesNotExist:
            logger.info('record not exist')
        return json_response(0, '', 0)

    return json_response(-1, 'action 参数错误', status=400)


def parse_feedback_payload(request):
    body = parse_json_body(request)
    size = parse_decimal_field(body.get('size'), '尺寸')
    weight = parse_decimal_field(body.get('weight'), '重量')
    if size <= 0 or size > Decimal('999.9999'):
        raise ValidationError('尺寸参数超出允许范围')
    if weight <= 0 or weight > Decimal('9999.9999'):
        raise ValidationError('重量参数超出允许范围')

    prediction_session_id = normalize_token(body.get('predictionSessionId'))
    if not prediction_session_id:
        raise ValidationError('缺少 predictionSessionId')

    source = normalize_token(body.get('source'))
    if source not in {
        EggFeedback.SOURCE_TOP1,
        EggFeedback.SOURCE_TOP10,
        EggFeedback.SOURCE_CUSTOM,
    }:
        raise ValidationError('source 参数错误')

    confirmed_species = normalize_species_name(body.get('confirmedSpecies'))
    if not confirmed_species:
        raise ValidationError('请填写孵化出的精灵名称')

    prediction_snapshot = sanitize_prediction_snapshot(body.get('predictionSnapshot'))
    if not prediction_snapshot:
        raise ValidationError('缺少 predictionSnapshot')

    predicted_species = normalize_species_name(body.get('predictedSpecies'))
    if not predicted_species:
        predicted_species = prediction_snapshot[0]['species']

    matched_snapshot = next(
        (item for item in prediction_snapshot if item['species'] == confirmed_species),
        None
    )

    if source == EggFeedback.SOURCE_TOP1:
        top1_item = prediction_snapshot[0]
        if confirmed_species != top1_item['species']:
            raise ValidationError('Top 1 上报必须与第一候选一致')
        predicted_rank = top1_item['rank']
        predicted_probability = Decimal(str(top1_item['prob'])).quantize(Decimal('0.0001'))
        is_custom_species = False
        species_in_snapshot = True
    elif source == EggFeedback.SOURCE_TOP10:
        if not matched_snapshot:
            raise ValidationError('Top 10 上报必须来自当前候选列表')
        predicted_rank = matched_snapshot['rank']
        predicted_probability = Decimal(str(matched_snapshot['prob'])).quantize(Decimal('0.0001'))
        is_custom_species = False
        species_in_snapshot = True
    else:
        if matched_snapshot:
            raise ValidationError('该精灵已在候选列表中，请直接点击“孵化了它”')
        predicted_rank = None
        predicted_probability = None
        is_custom_species = True
        species_in_snapshot = False

    openid = get_header(request, 'X-WX-OPENID')
    appid = get_header(request, 'X-WX-APPID')
    user_agent = request.META.get('HTTP_USER_AGENT', '')[:255]
    ip = get_request_ip(request)
    openid_hash = build_secure_hash(openid)
    ip_hash = build_secure_hash(ip)
    request_id = build_secure_hash(
        '|'.join([
            prediction_session_id,
            source,
            confirmed_species,
            openid_hash or ip_hash or 'anonymous',
        ])
    )

    return {
        'request_id': request_id,
        'prediction_session_id': prediction_session_id,
        'source': source,
        'size': size,
        'weight': weight,
        'rideable_only': parse_boolean(body.get('rideableOnly')),
        'confirmed_species': confirmed_species,
        'predicted_species': predicted_species,
        'predicted_rank': predicted_rank,
        'predicted_probability': predicted_probability,
        'prediction_version': normalize_token(body.get('predictionVersion') or 'local-v1', 32),
        'prediction_snapshot': prediction_snapshot,
        'is_custom_species': is_custom_species,
        'species_in_snapshot': species_in_snapshot,
        'appid': appid[:64],
        'openid_hash': openid_hash,
        'ip_hash': ip_hash,
        'user_agent': user_agent,
    }


def parse_json_body(request):
    try:
        raw_body = request.body.decode('utf-8') if request.body else '{}'
        return json.loads(raw_body or '{}')
    except json.JSONDecodeError as error:
        raise ValidationError('请求体不是合法 JSON') from error


def parse_decimal_field(value, label):
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValidationError(f'{label}参数格式错误') from error
    return decimal_value.quantize(Decimal('0.0001'))


def parse_boolean(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {'1', 'true', 'yes', 'y', 'on'}
    return bool(value)


def sanitize_prediction_snapshot(snapshot):
    if not isinstance(snapshot, list):
        raise ValidationError('predictionSnapshot 参数格式错误')

    cleaned = []
    for raw_item in snapshot[:MAX_SNAPSHOT_COUNT]:
        if not isinstance(raw_item, dict):
            continue
        species = normalize_species_name(raw_item.get('species'))
        if not species:
            continue
        rank = raw_item.get('rank')
        try:
            rank = int(rank)
        except (TypeError, ValueError):
            raise ValidationError('候选 rank 参数格式错误')
        if rank <= 0 or rank > MAX_SNAPSHOT_COUNT:
            raise ValidationError('候选 rank 参数超出范围')

        prob_value = raw_item.get('prob')
        try:
            prob = Decimal(str(prob_value)).quantize(Decimal('0.0001'))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValidationError('候选概率参数格式错误') from error
        if prob < 0 or prob > 1:
            raise ValidationError('候选概率参数超出范围')

        cleaned.append({
            'species': species,
            'rank': rank,
            'prob': float(prob),
            'rideable': bool(raw_item.get('rideable')),
        })

    cleaned.sort(key=lambda item: item['rank'])
    if len(cleaned) != len({item['rank'] for item in cleaned}):
        raise ValidationError('候选 rank 不能重复')
    return cleaned


def normalize_species_name(value):
    if value is None:
        return ''
    normalized = ' '.join(str(value).replace('\u3000', ' ').split())
    if len(normalized) > MAX_SPECIES_LENGTH:
        raise ValidationError('精灵名称过长')
    return normalized


def normalize_token(value, max_length=64):
    if value is None:
        return ''
    normalized = ''.join(str(value).strip().split())
    return normalized[:max_length]


def get_header(request, header_name):
    meta_key = f'HTTP_{header_name.upper().replace("-", "_")}'
    return request.META.get(meta_key, '')


def get_request_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '')


def build_secure_hash(value):
    if not value:
        return ''
    raw = f'{settings.SECRET_KEY}:{value}'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def find_existing_feedback(payload):
    session_filters = {'prediction_session_id': payload['prediction_session_id']}
    if payload['openid_hash']:
        session_filters['openid_hash'] = payload['openid_hash']
    elif payload['ip_hash']:
        session_filters['ip_hash'] = payload['ip_hash']

    existing = EggFeedback.objects.filter(**session_filters).order_by('-created_at').first()
    if existing:
        return existing

    return EggFeedback.objects.filter(request_id=payload['request_id']).first()


def get_rate_limit_error(payload):
    now = timezone.now()
    one_hour_ago = now - timedelta(hours=1)

    if payload['openid_hash']:
        recent_count = EggFeedback.objects.filter(
            openid_hash=payload['openid_hash'],
            created_at__gte=one_hour_ago,
        ).count()
        if recent_count >= MAX_REPORTS_PER_HOUR:
            return '提交过于频繁，请稍后再试'

    if payload['ip_hash']:
        recent_ip_count = EggFeedback.objects.filter(
            ip_hash=payload['ip_hash'],
            created_at__gte=one_hour_ago,
        ).count()
        if recent_ip_count >= MAX_REPORTS_PER_HOUR_PER_IP:
            return '当前网络提交过于频繁，请稍后再试'

    return ''


def build_quality_result(payload):
    score = 55
    review_note = ''
    status = EggFeedback.STATUS_ACCEPTED

    if payload['openid_hash']:
        score += 10
    if payload['prediction_snapshot']:
        score += 8
    if payload['source'] == EggFeedback.SOURCE_TOP1:
        score += 20
    elif payload['source'] == EggFeedback.SOURCE_TOP10:
        score += 14
    else:
        score -= 8
        status = EggFeedback.STATUS_PENDING
        review_note = '自定义精灵待人工归并'

    if payload['species_in_snapshot']:
        score += 6

    recent_window = timezone.now() - timedelta(minutes=20)
    recent_openid_count = 0
    recent_ip_count = 0

    if payload['openid_hash']:
        recent_openid_count = EggFeedback.objects.filter(
            openid_hash=payload['openid_hash'],
            created_at__gte=recent_window,
        ).count()

    if payload['ip_hash']:
        recent_ip_count = EggFeedback.objects.filter(
            ip_hash=payload['ip_hash'],
            created_at__gte=recent_window,
        ).count()

    if recent_openid_count >= 6 or recent_ip_count >= 12:
        score = max(score - 25, 0)
        status = EggFeedback.STATUS_SUSPICIOUS
        review_note = '短时间内高频提交，建议复核'

    return status, min(max(score, 0), 100), review_note
