import threading
import time
from copy import deepcopy
from decimal import Decimal

import requests
from django.conf import settings


DEFAULT_BASE_URL = 'https://roco-eggs.tsuki-world.com'
DEFAULT_TIMEOUT_SECONDS = 8
DEFAULT_CACHE_TTL_SECONDS = 300

_CACHE_LOCK = threading.Lock()
_PREDICT_CACHE = {}


class UpstreamPredictError(Exception):
    pass


def _get_base_url():
    return str(getattr(settings, 'ROCO_UPSTREAM_BASE_URL', DEFAULT_BASE_URL)).rstrip('/')


def _get_timeout_seconds():
    try:
        value = float(getattr(settings, 'ROCO_UPSTREAM_TIMEOUT_SECONDS', DEFAULT_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT_SECONDS
    return max(value, 1.0)


def _get_cache_ttl_seconds():
    try:
        value = int(getattr(settings, 'ROCO_UPSTREAM_CACHE_TTL_SECONDS', DEFAULT_CACHE_TTL_SECONDS))
    except (TypeError, ValueError):
        value = DEFAULT_CACHE_TTL_SECONDS
    return max(value, 0)


def _quantize_number(value):
    return str(Decimal(str(value)).quantize(Decimal('0.0001')))


def _build_cache_key(size, weight, rideable_only):
    return (
        _quantize_number(size),
        _quantize_number(weight),
        bool(rideable_only),
    )


def _get_cached_prediction(cache_key):
    ttl_seconds = _get_cache_ttl_seconds()
    if ttl_seconds <= 0:
        return None

    now = time.time()
    with _CACHE_LOCK:
        cached = _PREDICT_CACHE.get(cache_key)
        if not cached:
            return None
        if now - cached['saved_at'] > ttl_seconds:
            _PREDICT_CACHE.pop(cache_key, None)
            return None
        return deepcopy(cached['data'])


def _set_cached_prediction(cache_key, data):
    ttl_seconds = _get_cache_ttl_seconds()
    if ttl_seconds <= 0:
        return

    with _CACHE_LOCK:
        _PREDICT_CACHE[cache_key] = {
            'saved_at': time.time(),
            'data': deepcopy(data),
        }


def _build_headers():
    base_url = _get_base_url()
    return {
        'User-Agent': 'Mozilla/5.0',
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json',
        'Referer': f'{base_url}/',
        'Origin': base_url,
        'X-Requested-With': 'XMLHttpRequest',
    }


def _normalize_results(results):
    normalized = []
    for index, raw_item in enumerate(results or [], start=1):
        if not isinstance(raw_item, dict):
            continue
        species = str(raw_item.get('species') or '').strip()
        if not species:
            continue
        prob = raw_item.get('prob')
        try:
            prob = float(prob)
        except (TypeError, ValueError):
            prob = 0.0
        normalized.append({
            'species': species,
            'prob': prob,
            'rank': index,
        })
    return normalized


def fetch_upstream_prediction(size, weight, rideable_only=False, use_cache=True):
    cache_key = _build_cache_key(size, weight, rideable_only)
    if use_cache:
        cached = _get_cached_prediction(cache_key)
        if cached is not None:
            return cached

    payload = {
        'size': float(size),
        'weight': float(weight),
        'rideable_only': bool(rideable_only),
    }
    request_url = f'{_get_base_url()}/api/predict'

    try:
        response = requests.post(
            request_url,
            headers=_build_headers(),
            json=payload,
            timeout=_get_timeout_seconds(),
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as error:
        raise UpstreamPredictError(f'上游预测接口请求失败: {error}') from error
    except ValueError as error:
        raise UpstreamPredictError('上游预测接口返回了非法 JSON') from error

    if not isinstance(data, dict):
        raise UpstreamPredictError('上游预测接口返回格式错误')

    normalized = dict(data)
    normalized['results'] = _normalize_results(data.get('results'))
    normalized['source'] = normalized.get('source') or 'roco_upstream_proxy'
    normalized['predictionVersion'] = 'upstream-proxy-v1'
    normalized['upstreamUrl'] = request_url

    if use_cache:
        _set_cached_prediction(cache_key, normalized)
    return normalized


def summarize_prediction_for_species(prediction_data, confirmed_species=''):
    normalized_results = _normalize_results((prediction_data or {}).get('results'))
    top1 = normalized_results[0] if normalized_results else None
    confirmed_rank = None
    confirmed_probability = None
    for item in normalized_results:
        if item['species'] == confirmed_species:
            confirmed_rank = item['rank']
            confirmed_probability = item['prob']
            break

    status = 'unknown'
    if confirmed_species:
        if top1 and top1['species'] == confirmed_species:
            status = 'matched'
        elif confirmed_rank is not None and confirmed_rank <= 10:
            status = 'top10'
        elif confirmed_rank is not None:
            status = 'present'
        else:
            status = 'mismatch'

    return {
        'status': status,
        'top1_species': top1['species'] if top1 else '',
        'top1_probability': top1['prob'] if top1 else None,
        'confirmed_rank': confirmed_rank,
        'confirmed_probability': confirmed_probability,
        'result_count': len(normalized_results),
    }
