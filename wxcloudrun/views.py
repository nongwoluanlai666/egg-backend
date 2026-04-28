import hashlib
import hmac
import json
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone

from wxcloudrun.merchant_notice import (
    MerchantNoticeConfigurationError,
    MerchantNoticePermissionError,
    MerchantNoticeSourceError,
    MerchantNoticeValidationError,
    broadcast_manual_message as broadcast_manual_merchant_notice,
    build_current_response as build_merchant_notice_current_response,
    get_subscription_preferences as get_merchant_notice_preferences,
    prepare_subscription_next as prepare_next_merchant_notice_subscription,
    run_guarded_watch_current_merchant,
    send_dev_self_test_message as send_dev_self_test_merchant_notice,
    subscribe_next as subscribe_next_merchant_notice,
    unlock_subscription_rewarded_gate as unlock_merchant_notice_rewarded_gate,
    update_subscription_preferences as update_merchant_notice_preferences,
    verify_job_request,
)
from wxcloudrun.local_model_predict import (
    LocalModelPredictError,
    get_local_model_runtime_summary,
    predict_with_local_model,
    reset_local_model_cache,
)
from wxcloudrun.models import Counters, EggFeedback, EggPredictorConfig
from wxcloudrun.upstream_predict import UpstreamPredictError, fetch_upstream_prediction


logger = logging.getLogger('log')

MAX_SNAPSHOT_COUNT = 10
MAX_SPECIES_LENGTH = 64
MAX_REPORTS_PER_HOUR = 20
MAX_REPORTS_PER_HOUR_PER_IP = 60
DEFAULT_DEV_EXPORT_PAGE_SIZE = 50
DEFAULT_DEV_HISTORY_LIMIT = 8


class ValidationError(Exception):
    pass


class PermissionDeniedError(Exception):
    pass


class ServiceUnavailableError(Exception):
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


def build_feedback_response_data(record, duplicated=False):
    return {
        'recordId': record.id,
        'duplicated': duplicated,
        'qualityStatus': record.quality_status,
        'qualityScore': record.quality_score,
        'upstreamVerificationStatus': getattr(
            record,
            'upstream_verification_status',
            EggFeedback.VERIFICATION_UNKNOWN,
        ),
        'upstreamTop1Species': getattr(record, 'upstream_top1_species', ''),
        'upstreamConfirmedRank': getattr(record, 'upstream_confirmed_rank', None),
    }


def build_feedback_export_row(record):
    try:
        prediction_snapshot = json.loads(record.prediction_snapshot or '[]')
    except json.JSONDecodeError:
        prediction_snapshot = []

    return {
        'id': record.id,
        'request_id': record.request_id,
        'prediction_session_id': record.prediction_session_id,
        'source': record.source,
        'size': float(record.size),
        'weight': float(record.weight),
        'rideable_only': bool(record.rideable_only),
        'confirmed_species': record.confirmed_species,
        'predicted_species': record.predicted_species,
        'predicted_rank': record.predicted_rank,
        'predicted_probability': (
            float(record.predicted_probability)
            if record.predicted_probability is not None
            else None
        ),
        'prediction_version': record.prediction_version,
        'prediction_snapshot': prediction_snapshot,
        'candidate_count': record.candidate_count,
        'is_custom_species': bool(record.is_custom_species),
        'species_in_snapshot': bool(record.species_in_snapshot),
        'quality_status': record.quality_status,
        'quality_score': record.quality_score,
        'review_note': record.review_note,
        'upstream_verification_status': record.upstream_verification_status,
        'upstream_top1_species': record.upstream_top1_species,
        'upstream_top1_probability': (
            float(record.upstream_top1_probability)
            if record.upstream_top1_probability is not None
            else None
        ),
        'upstream_confirmed_rank': record.upstream_confirmed_rank,
        'upstream_confirmed_probability': (
            float(record.upstream_confirmed_probability)
            if record.upstream_confirmed_probability is not None
            else None
        ),
        'upstream_checked_at': format_datetime(record.upstream_checked_at),
        'appid': record.appid,
        'openid_hash': record.openid_hash,
        'ip_hash': record.ip_hash,
        'user_agent': record.user_agent,
        'created_at': format_datetime(record.created_at),
        'updated_at': format_datetime(record.updated_at),
    }


def serialize_predictor_config(record, include_config_json=True):
    if not record:
        return build_default_predictor_config()

    config_data = parse_stored_json_object(record.config_json)
    payload = {
        'id': record.id,
        'version': record.version,
        'strategy': record.strategy,
        'modelType': record.model_type,
        'artifactUri': record.artifact_uri,
        'notes': record.notes,
        'isActive': bool(record.is_active),
        'createdAt': format_datetime(record.created_at),
        'updatedAt': format_datetime(record.updated_at),
        'configKeyCount': len(config_data),
    }
    if include_config_json:
        payload['configJson'] = config_data
    return payload


def build_default_predictor_config():
    runtime = get_local_model_runtime_summary()
    return {
        'id': None,
        'version': runtime.get('version') or 'hybrid-default',
        'strategy': EggPredictorConfig.STRATEGY_HYBRID,
        'modelType': runtime.get('modelType') or 'sklearn_random_forest_joblib',
        'artifactUri': runtime.get('artifactUri') or runtime.get('artifactPath') or '',
        'notes': '当前线上预测链路为上游优先，失败时回退到云托管本地模型。',
        'isActive': True,
        'createdAt': '',
        'updatedAt': '',
        'configKeyCount': 0,
        'configJson': {
            'runtimeAvailable': runtime.get('available', False),
            'runtimeSource': runtime.get('source', ''),
            'runtimeError': runtime.get('error', ''),
            'classCount': runtime.get('classCount', 0),
        },
    }


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


def egg_predict(request, _):
    if request.method != 'POST':
        rsp = json_response(-1, '请求方式错误', status=405)
        logger.info('response result: %s', rsp.content.decode('utf-8'))
        return rsp

    try:
        payload = parse_predict_payload(request)
        upstream_data = fetch_upstream_prediction(
            payload['size'],
            payload['weight'],
            payload['rideable_only'],
        )
        rsp = json_response(0, '', upstream_data)
    except ValidationError as error:
        rsp = json_response(40001, str(error), status=400)
    except UpstreamPredictError as error:
        logger.warning('upstream predict failed: %s', error)
        try:
            fallback_data = predict_with_local_model(
                payload['size'],
                payload['weight'],
                payload['rideable_only'],
            )
            fallback_data['fallbackApplied'] = True
            fallback_data['fallbackReason'] = 'upstream_unavailable'
            fallback_data['upstreamError'] = str(error)
            rsp = json_response(0, '', fallback_data)
        except LocalModelPredictError as model_error:
            logger.warning('local model fallback failed: %s', model_error)
            rsp = json_response(50201, f'{error}; local model fallback failed: {model_error}', status=502)
    except Exception:
        logger.exception('egg predict unexpected error')
        rsp = json_response(50002, '预测服务暂时不可用，请稍后再试', status=500)

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
            rsp = json_response(0, '', build_feedback_response_data(existing, duplicated=True))
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
        rsp = json_response(0, '', build_feedback_response_data(record, duplicated=False))
    except ValidationError as error:
        rsp = json_response(40001, str(error), status=400)
    except Exception:
        logger.exception('egg feedback unexpected error')
        rsp = json_response(50001, '提交失败，请稍后重试', status=500)

    logger.info('response result: %s', rsp.content.decode('utf-8'))
    return rsp


def dev_feedback_export(request, _):
    if request.method not in {'GET', 'POST'}:
        rsp = json_response(-1, '请求方式错误', status=405)
        logger.info('response result: %s', rsp.content.decode('utf-8'))
        return rsp

    try:
        request_data = request.GET if request.method == 'GET' else parse_json_body(request)
        require_dev_admin(request)
        payload = parse_feedback_export_payload(request_data)

        queryset = EggFeedback.objects.all().order_by('-id')
        if payload['cursor']:
            queryset = queryset.filter(id__lt=payload['cursor'])
        if payload['quality_statuses']:
            queryset = queryset.filter(quality_status__in=payload['quality_statuses'])
        if payload['min_quality_score'] > 0:
            queryset = queryset.filter(quality_score__gte=payload['min_quality_score'])
        if not payload['include_custom']:
            queryset = queryset.filter(is_custom_species=False)

        rows = list(queryset[:payload['page_size'] + 1])
        has_more = len(rows) > payload['page_size']
        rows = rows[:payload['page_size']]
        next_cursor = rows[-1].id if has_more and rows else 0

        rsp = json_response(
            0,
            '',
            {
                'rows': [build_feedback_export_row(item) for item in rows],
                'returnedCount': len(rows),
                'pageSize': payload['page_size'],
                'hasMore': has_more,
                'nextCursor': next_cursor,
                'filters': {
                    'cursor': payload['cursor'],
                    'qualityStatuses': payload['quality_statuses'],
                    'minQualityScore': payload['min_quality_score'],
                    'includeCustom': payload['include_custom'],
                },
                'exportedAt': format_datetime(timezone.now()),
            },
        )
    except PermissionDeniedError as error:
        rsp = json_response(40301, str(error), status=403)
    except ServiceUnavailableError as error:
        rsp = json_response(50301, str(error), status=503)
    except ValidationError as error:
        rsp = json_response(40001, str(error), status=400)
    except Exception:
        logger.exception('dev feedback export unexpected error')
        rsp = json_response(50003, '导出失败，请稍后重试', status=500)

    logger.info('response result: %s', rsp.content.decode('utf-8'))
    return rsp


def dev_model_config(request, _):
    if request.method not in {'GET', 'POST'}:
        rsp = json_response(-1, '请求方式错误', status=405)
        logger.info('response result: %s', rsp.content.decode('utf-8'))
        return rsp

    try:
        require_dev_admin(request)

        if request.method == 'GET':
            active_record = EggPredictorConfig.objects.filter(is_active=True).order_by('-updated_at', '-id').first()
            history_records = EggPredictorConfig.objects.order_by('-updated_at', '-id')[:DEFAULT_DEV_HISTORY_LIMIT]
            rsp = json_response(
                0,
                '',
                {
                    'active': serialize_predictor_config(active_record, include_config_json=True),
                    'history': [
                        serialize_predictor_config(item, include_config_json=False)
                        for item in history_records
                    ],
                },
            )
        else:
            body = parse_json_body(request)
            payload = parse_predictor_config_payload(body)
            current_active = EggPredictorConfig.objects.filter(is_active=True).order_by('-updated_at', '-id').first()

            duplicated = bool(
                current_active and
                current_active.version == payload['version'] and
                current_active.strategy == payload['strategy'] and
                current_active.model_type == payload['model_type'] and
                current_active.artifact_uri == payload['artifact_uri'] and
                current_active.config_json == payload['config_json'] and
                current_active.notes == payload['notes']
            )

            if duplicated:
                record = current_active
            else:
                with transaction.atomic():
                    EggPredictorConfig.objects.filter(is_active=True).update(is_active=False)
                    record = EggPredictorConfig.objects.create(
                        version=payload['version'],
                        strategy=payload['strategy'],
                        model_type=payload['model_type'],
                        artifact_uri=payload['artifact_uri'],
                        config_json=payload['config_json'],
                        notes=payload['notes'],
                        is_active=True,
                    )
                reset_local_model_cache()

            history_records = EggPredictorConfig.objects.order_by('-updated_at', '-id')[:DEFAULT_DEV_HISTORY_LIMIT]
            rsp = json_response(
                0,
                '',
                {
                    'duplicated': duplicated,
                    'active': serialize_predictor_config(record, include_config_json=True),
                    'history': [
                        serialize_predictor_config(item, include_config_json=False)
                        for item in history_records
                    ],
                },
            )
    except PermissionDeniedError as error:
        rsp = json_response(40301, str(error), status=403)
    except ServiceUnavailableError as error:
        rsp = json_response(50301, str(error), status=503)
    except ValidationError as error:
        rsp = json_response(40001, str(error), status=400)
    except Exception:
        logger.exception('dev model config unexpected error')
        rsp = json_response(50004, '配置保存失败，请稍后重试', status=500)

    logger.info('response result: %s', rsp.content.decode('utf-8'))
    return rsp


def merchant_notice_current(request, _):
    if request.method != 'GET':
        rsp = json_response(-1, '请求方式错误', status=405)
        logger.info('response result: %s', rsp.content.decode('utf-8'))
        return rsp

    try:
        openid = get_header(request, 'X-WX-OPENID')
        rsp = json_response(0, '', build_merchant_notice_current_response(openid=openid))
    except MerchantNoticeConfigurationError as error:
        rsp = json_response(50311, str(error), status=503)
    except MerchantNoticeSourceError as error:
        rsp = json_response(50211, str(error), status=502)
    except Exception:
        logger.exception('merchant notice current unexpected error')
        rsp = json_response(50011, '远行提醒数据暂时不可用，请稍后再试', status=500)

    logger.info('response result: %s', rsp.content.decode('utf-8'))
    return rsp


def merchant_notice_subscribe_next(request, _):
    if request.method != 'POST':
        rsp = json_response(-1, '请求方式错误', status=405)
        logger.info('response result: %s', rsp.content.decode('utf-8'))
        return rsp

    try:
        parse_json_body(request)
        openid = get_header(request, 'X-WX-OPENID')
        appid = get_header(request, 'X-WX-APPID')
        rsp = json_response(0, '', subscribe_next_merchant_notice(openid=openid, appid=appid))
    except ValidationError as error:
        rsp = json_response(40001, str(error), status=400)
    except MerchantNoticeValidationError as error:
        rsp = json_response(40011, str(error), status=400)
    except MerchantNoticePermissionError as error:
        rsp = json_response(40111, str(error), status=401)
    except MerchantNoticeConfigurationError as error:
        rsp = json_response(50311, str(error), status=503)
    except Exception:
        logger.exception('merchant notice subscribe unexpected error')
        rsp = json_response(50012, '开启远行提醒失败，请稍后再试', status=500)

    logger.info('response result: %s', rsp.content.decode('utf-8'))
    return rsp


def merchant_notice_subscribe_status(request, _):
    if request.method != 'GET':
        rsp = json_response(-1, '请求方式错误', status=405)
        logger.info('response result: %s', rsp.content.decode('utf-8'))
        return rsp

    try:
        openid = get_header(request, 'X-WX-OPENID')
        appid = get_header(request, 'X-WX-APPID')
        rsp = json_response(
            0,
            '',
            prepare_next_merchant_notice_subscription(openid=openid, appid=appid),
        )
    except MerchantNoticePermissionError as error:
        rsp = json_response(40111, str(error), status=401)
    except MerchantNoticeConfigurationError as error:
        rsp = json_response(50311, str(error), status=503)
    except Exception:
        logger.exception('merchant notice subscribe status unexpected error')
        rsp = json_response(50016, '远行提醒状态检查失败，请稍后再试', status=500)

    logger.info('response result: %s', rsp.content.decode('utf-8'))
    return rsp


def merchant_notice_reward_unlock(request, _):
    if request.method != 'POST':
        rsp = json_response(-1, '请求方式错误', status=405)
        logger.info('response result: %s', rsp.content.decode('utf-8'))
        return rsp

    try:
        parse_json_body(request)
        openid = get_header(request, 'X-WX-OPENID')
        appid = get_header(request, 'X-WX-APPID')
        rsp = json_response(
            0,
            '',
            unlock_merchant_notice_rewarded_gate(openid=openid, appid=appid),
        )
    except ValidationError as error:
        rsp = json_response(40001, str(error), status=400)
    except MerchantNoticeValidationError as error:
        rsp = json_response(40011, str(error), status=400)
    except MerchantNoticePermissionError as error:
        rsp = json_response(40111, str(error), status=401)
    except MerchantNoticeConfigurationError as error:
        rsp = json_response(50311, str(error), status=503)
    except Exception:
        logger.exception('merchant notice reward unlock unexpected error')
        rsp = json_response(50017, '激励校验失败，请稍后再试', status=500)

    logger.info('response result: %s', rsp.content.decode('utf-8'))
    return rsp


def merchant_notice_dev_self_test(request, _):
    if request.method != 'POST':
        rsp = json_response(-1, '请求方式错误', status=405)
        logger.info('response result: %s', rsp.content.decode('utf-8'))
        return rsp

    try:
        body = parse_json_body(request)
        openid = get_header(request, 'X-WX-OPENID')
        appid = get_header(request, 'X-WX-APPID')
        rsp = json_response(
            0,
            '',
            send_dev_self_test_merchant_notice(
                openid=openid,
                appid=appid,
                env_version=body.get('envVersion'),
            ),
        )
    except ValidationError as error:
        rsp = json_response(40001, str(error), status=400)
    except MerchantNoticeValidationError as error:
        rsp = json_response(40011, str(error), status=400)
    except MerchantNoticePermissionError as error:
        rsp = json_response(40111, str(error), status=401)
    except MerchantNoticeConfigurationError as error:
        rsp = json_response(50311, str(error), status=503)
    except MerchantNoticeSourceError as error:
        rsp = json_response(50211, str(error), status=502)
    except Exception:
        logger.exception('merchant notice dev self test unexpected error')
        rsp = json_response(50018, '开发模式测试通知发送失败，请稍后再试', status=500)

    logger.info('response result: %s', rsp.content.decode('utf-8'))
    return rsp


def merchant_notice_preferences(request, _):
    if request.method not in {'GET', 'POST'}:
        rsp = json_response(-1, '请求方式错误', status=405)
        logger.info('response result: %s', rsp.content.decode('utf-8'))
        return rsp

    try:
        openid = get_header(request, 'X-WX-OPENID')
        appid = get_header(request, 'X-WX-APPID')
        if request.method == 'GET':
            rsp = json_response(0, '', get_merchant_notice_preferences(openid=openid))
        else:
            body = parse_json_body(request)
            rsp = json_response(
                0,
                '',
                update_merchant_notice_preferences(
                    openid=openid,
                    appid=appid,
                    selected_goods=body.get('selectedGoods') if isinstance(body, dict) else None,
                ),
            )
    except ValidationError as error:
        rsp = json_response(40001, str(error), status=400)
    except MerchantNoticeValidationError as error:
        rsp = json_response(40011, str(error), status=400)
    except MerchantNoticePermissionError as error:
        rsp = json_response(40111, str(error), status=401)
    except MerchantNoticeConfigurationError as error:
        rsp = json_response(50311, str(error), status=503)
    except Exception:
        logger.exception('merchant notice preferences unexpected error')
        rsp = json_response(50015, '通知商品配置保存失败，请稍后再试', status=500)

    logger.info('response result: %s', rsp.content.decode('utf-8'))
    return rsp


def internal_merchant_watch(request, _):
    if request.method not in {'GET', 'POST'}:
        rsp = json_response(-1, '请求方式错误', status=405)
        logger.info('response result: %s', rsp.content.decode('utf-8'))
        return rsp

    try:
        request_data = request.GET if request.method == 'GET' else parse_json_body(request)
        verify_job_request(
            request_data.get('token') or get_header(request, 'X-MERCHANT-JOB-TOKEN'),
            get_request_ip(request),
        )
        timeout_seconds = parse_float_field(
            request_data.get('timeoutSeconds', getattr(settings, 'MERCHANT_NOTIFY_POLL_TIMEOUT_SECONDS', 900)),
            'timeoutSeconds',
            minimum=10,
            maximum=3600,
        )
        poll_interval_seconds = parse_float_field(
            request_data.get('pollIntervalSeconds', getattr(settings, 'MERCHANT_NOTIFY_POLL_INTERVAL_SECONDS', 30)),
            'pollIntervalSeconds',
            minimum=1,
            maximum=300,
        )
        force = parse_boolean(request_data.get('force')) if 'force' in request_data else False
        rsp = json_response(
            0,
            '',
            run_guarded_watch_current_merchant(
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                force=force,
            ),
        )
    except MerchantNoticePermissionError as error:
        rsp = json_response(40311, str(error), status=403)
    except MerchantNoticeConfigurationError as error:
        rsp = json_response(50311, str(error), status=503)
    except MerchantNoticeSourceError as error:
        rsp = json_response(50211, str(error), status=502)
    except ValidationError as error:
        rsp = json_response(40001, str(error), status=400)
    except Exception:
        logger.exception('internal merchant watch unexpected error')
        rsp = json_response(50013, '远行提醒任务执行失败', status=500)

    logger.info('response result: %s', rsp.content.decode('utf-8'))
    return rsp


def dev_merchant_notice_broadcast(request, _):
    if request.method != 'POST':
        rsp = json_response(-1, '请求方式错误', status=405)
        logger.info('response result: %s', rsp.content.decode('utf-8'))
        return rsp

    try:
        require_dev_admin(request)
        body = parse_json_body(request)
        payload = parse_merchant_notice_broadcast_payload(body)
        rsp = json_response(0, '', broadcast_manual_merchant_notice(payload))
    except PermissionDeniedError as error:
        rsp = json_response(40301, str(error), status=403)
    except ServiceUnavailableError as error:
        rsp = json_response(50301, str(error), status=503)
    except MerchantNoticeConfigurationError as error:
        rsp = json_response(50311, str(error), status=503)
    except MerchantNoticeSourceError as error:
        rsp = json_response(50211, str(error), status=502)
    except ValidationError as error:
        rsp = json_response(40001, str(error), status=400)
    except Exception:
        logger.exception('dev merchant notice broadcast unexpected error')
        rsp = json_response(50014, '手动远行提醒发送失败，请稍后再试', status=500)

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


def parse_predict_payload(request):
    body = parse_json_body(request)
    size = parse_decimal_field(body.get('size'), '尺寸')
    weight = parse_decimal_field(body.get('weight'), '重量')
    if size <= 0 or size > Decimal('999.9999'):
        raise ValidationError('尺寸参数超出允许范围')
    if weight <= 0 or weight > Decimal('9999.9999'):
        raise ValidationError('重量参数超出允许范围')

    rideable_value = body.get('rideableOnly') if 'rideableOnly' in body else body.get('rideable_only')
    return {
        'size': size,
        'weight': weight,
        'rideable_only': parse_boolean(rideable_value),
    }


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
        None,
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

    rideable_value = body.get('rideableOnly') if 'rideableOnly' in body else body.get('rideable_only')

    return {
        'request_id': request_id,
        'prediction_session_id': prediction_session_id,
        'source': source,
        'size': size,
        'weight': weight,
        'rideable_only': parse_boolean(rideable_value),
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


def parse_feedback_export_payload(data):
    page_size = parse_int_field(
        data.get('pageSize', DEFAULT_DEV_EXPORT_PAGE_SIZE),
        'pageSize',
        minimum=1,
        maximum=get_dev_export_max_page_size(),
    )
    cursor = parse_int_field(data.get('cursor', 0), 'cursor', minimum=0)
    min_quality_score = parse_int_field(
        data.get('minQualityScore', 0),
        'minQualityScore',
        minimum=0,
        maximum=100,
    )
    quality_statuses = parse_choice_list(
        data.get('qualityStatuses'),
        {choice[0] for choice in EggFeedback.STATUS_CHOICES},
        'qualityStatuses',
    )
    include_custom = True
    if 'includeCustom' in data:
        include_custom = parse_boolean(data.get('includeCustom'))

    return {
        'page_size': page_size,
        'cursor': cursor,
        'min_quality_score': min_quality_score,
        'quality_statuses': quality_statuses,
        'include_custom': include_custom,
    }


def parse_predictor_config_payload(data):
    version = normalize_token(data.get('version'), 64)
    if not version:
        raise ValidationError('version 不能为空')

    strategy = normalize_token(data.get('strategy'), 32)
    allowed_strategies = {choice[0] for choice in EggPredictorConfig.STRATEGY_CHOICES}
    if strategy not in allowed_strategies:
        raise ValidationError('strategy 参数错误')

    model_type = normalize_token(data.get('modelType'), 64)
    artifact_uri = normalize_text(data.get('artifactUri'), 512)
    notes = normalize_text(data.get('notes'), 255)
    config_data = parse_json_object_field(data.get('configJson'), 'configJson')

    return {
        'version': version,
        'strategy': strategy,
        'model_type': model_type,
        'artifact_uri': artifact_uri,
        'notes': notes,
        'config_json': json.dumps(config_data, ensure_ascii=False, sort_keys=True),
    }


def parse_merchant_notice_broadcast_payload(data):
    message_data = data.get('data') if isinstance(data.get('data'), dict) else {}
    date_text = normalize_text(data.get('date2') or message_data.get('date2'), 32)
    thing_text = normalize_text(data.get('thing7') or message_data.get('thing7'), 64)
    advice_text = normalize_text(data.get('thing10') or message_data.get('thing10'), 64)
    page = normalize_text(data.get('page'), 255)
    miniprogram_state = normalize_text(
        data.get('miniprogramState') or data.get('miniprogram_state'),
        16,
    )
    campaign_key = normalize_text(data.get('campaignKey') or data.get('campaign_key'), 64)
    template_id = normalize_text(data.get('templateId') or data.get('template_id'), 128)

    dry_run = False
    if 'dryRun' in data:
        dry_run = parse_boolean(data.get('dryRun'))
    elif 'dry_run' in data:
        dry_run = parse_boolean(data.get('dry_run'))

    if not date_text:
        raise ValidationError('缺少 date2')
    if not thing_text:
        raise ValidationError('缺少 thing7')
    if not advice_text:
        raise ValidationError('缺少 thing10')
    if miniprogram_state and miniprogram_state not in {'developer', 'trial', 'formal'}:
        raise ValidationError('miniprogramState 参数错误')

    return {
        'date2': date_text,
        'thing7': thing_text,
        'thing10': advice_text,
        'page': page,
        'miniprogramState': miniprogram_state,
        'campaignKey': campaign_key,
        'templateId': template_id,
        'dryRun': dry_run,
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


def parse_int_field(value, label, minimum=None, maximum=None):
    try:
        int_value = int(value)
    except (TypeError, ValueError) as error:
        raise ValidationError(f'{label} 参数格式错误') from error

    if minimum is not None and int_value < minimum:
        raise ValidationError(f'{label} 参数超出允许范围')
    if maximum is not None and int_value > maximum:
        raise ValidationError(f'{label} 参数超出允许范围')
    return int_value


def parse_float_field(value, label, minimum=None, maximum=None):
    try:
        float_value = float(value)
    except (TypeError, ValueError) as error:
        raise ValidationError(f'{label} 参数格式错误') from error

    if minimum is not None and float_value < minimum:
        raise ValidationError(f'{label} 参数超出允许范围')
    if maximum is not None and float_value > maximum:
        raise ValidationError(f'{label} 参数超出允许范围')
    return float_value


def parse_boolean(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {'1', 'true', 'yes', 'y', 'on'}
    return bool(value)


def parse_choice_list(value, allowed_values, label):
    items = parse_text_list(value)
    invalid_items = [item for item in items if item not in allowed_values]
    if invalid_items:
        raise ValidationError(f'{label} 包含不支持的值')
    return items


def parse_text_list(value):
    if value is None or value == '':
        return []
    if isinstance(value, (list, tuple, set)):
        raw_items = value
    else:
        raw_items = str(value).split(',')

    cleaned = []
    seen = set()
    for item in raw_items:
        normalized = str(item).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(normalized)
    return cleaned


def parse_json_object_field(value, label):
    if value in (None, ''):
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValidationError(f'{label} 不是合法 JSON') from error
        if not isinstance(parsed, dict):
            raise ValidationError(f'{label} 需要是 JSON 对象')
        return parsed
    raise ValidationError(f'{label} 参数格式错误')


def parse_stored_json_object(value):
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


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

        try:
            rank = int(raw_item.get('rank'))
        except (TypeError, ValueError) as error:
            raise ValidationError('候选 rank 参数格式错误') from error
        if rank <= 0 or rank > MAX_SNAPSHOT_COUNT:
            raise ValidationError('候选 rank 参数超出范围')

        try:
            prob = Decimal(str(raw_item.get('prob'))).quantize(Decimal('0.0001'))
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


def normalize_text(value, max_length=255):
    if value is None:
        return ''
    normalized = ' '.join(str(value).strip().split())
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


def require_dev_admin(request):
    configured_token = str(getattr(settings, 'EGG_DEV_ADMIN_TOKEN', '') or '').strip()
    if not configured_token:
        raise ServiceUnavailableError('开发者接口未配置 EGG_DEV_ADMIN_TOKEN')

    provided_token = str(get_header(request, 'X-DEV-ADMIN-TOKEN') or '').strip()
    if not provided_token:
        raise PermissionDeniedError('缺少开发者令牌')

    if not hmac.compare_digest(provided_token, configured_token):
        raise PermissionDeniedError('开发者令牌无效')


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
    one_hour_ago = timezone.now() - timedelta(hours=1)

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
    score = 48
    notes = []
    status = EggFeedback.STATUS_ACCEPTED

    if payload['openid_hash']:
        score += 12
    else:
        notes.append('匿名样本')

    if payload['prediction_snapshot']:
        score += 8

    if payload['source'] == EggFeedback.SOURCE_TOP1:
        score += 22
        notes.append('Top1 直接确认')
    elif payload['source'] == EggFeedback.SOURCE_TOP10:
        score += 14
        notes.append('Top10 候选确认')
    else:
        score -= 6
        status = EggFeedback.STATUS_PENDING
        notes.append('自定义精灵待离线清洗归并')

    if payload['species_in_snapshot']:
        score += 8

    if payload['predicted_species'] == payload['confirmed_species']:
        score += 4

    if str(payload['prediction_version']).startswith('upstream'):
        score += 4
        notes.append('预测来自云端代理')

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
        notes = ['短时间内高频提交，建议复核']

    review_note = '；'.join(dict.fromkeys(note for note in notes if note))
    return status, min(max(score, 0), 100), review_note


def format_datetime(value):
    if not value:
        return ''
    return value.isoformat()


def get_dev_export_max_page_size():
    try:
        value = int(getattr(settings, 'EGG_DEV_EXPORT_MAX_PAGE_SIZE', '100'))
    except (TypeError, ValueError):
        value = 100
    return max(1, min(value, 200))
