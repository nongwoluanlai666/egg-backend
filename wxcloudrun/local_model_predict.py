import hashlib
import json
import logging
import os
import tempfile
import threading
from pathlib import Path

import joblib
import numpy as np
import requests
from django.conf import settings
from django.db import DatabaseError

from wxcloudrun.models import EggPredictorConfig


logger = logging.getLogger('log')

DEFAULT_MODEL_FILENAME = 'egg_model_v2.joblib.gz'
DEFAULT_TOP_K = 10
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 20

_MODEL_LOCK = threading.Lock()
_MODEL_STATE = {
    'artifact_key': '',
    'artifact_uri': '',
    'artifact_path': '',
    'bundle': None,
    'loaded': False,
    'load_error': '',
    'source': '',
}


class LocalModelPredictError(Exception):
    pass


def _parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def _get_default_model_path():
    configured = str(getattr(settings, 'EGG_MODEL_DEFAULT_RELATIVE_PATH', '') or '').strip()
    relative_path = configured or f'model_artifacts/{DEFAULT_MODEL_FILENAME}'
    return (Path(settings.BASE_DIR) / relative_path).resolve()


def _get_default_top_k():
    try:
        value = int(getattr(settings, 'EGG_MODEL_TOP_K', DEFAULT_TOP_K))
    except (TypeError, ValueError):
        value = DEFAULT_TOP_K
    return max(1, min(value, 20))


def _get_download_timeout_seconds():
    try:
        value = float(getattr(settings, 'EGG_MODEL_DOWNLOAD_TIMEOUT_SECONDS', DEFAULT_DOWNLOAD_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        value = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS
    return max(value, 1.0)


def _get_download_cache_dir():
    configured = str(getattr(settings, 'EGG_MODEL_DOWNLOAD_CACHE_DIR', '') or '').strip()
    if configured:
        path = Path(configured)
    else:
        path = Path(tempfile.gettempdir()) / 'egg_model_cache'
    path.mkdir(parents=True, exist_ok=True)
    return path


def _parse_json_object(raw_value):
    if not raw_value:
        return {}
    try:
        value = json.loads(raw_value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _get_active_predictor_config():
    try:
        record = EggPredictorConfig.objects.filter(is_active=True).order_by('-updated_at', '-id').first()
    except DatabaseError as error:
        logger.warning('load active predictor config failed: %s', error)
        return None
    return record


def _build_runtime_config():
    env_artifact_uri = str(getattr(settings, 'EGG_MODEL_ARTIFACT_URI', '') or '').strip()
    default_path = _get_default_model_path()

    config = {
        'strategy': EggPredictorConfig.STRATEGY_HYBRID,
        'version': '',
        'model_type': 'sklearn_random_forest_joblib',
        'artifact_uri': env_artifact_uri,
        'artifact_path': default_path if default_path.exists() else None,
        'config_json': {},
        'source': 'default_packaged',
    }

    record = _get_active_predictor_config()
    if record:
        config.update({
            'strategy': record.strategy or config['strategy'],
            'version': record.version or config['version'],
            'model_type': record.model_type or config['model_type'],
            'config_json': _parse_json_object(record.config_json),
            'source': 'db_active_config',
        })
        if record.artifact_uri:
            config['artifact_uri'] = record.artifact_uri.strip()

    if not config['artifact_uri'] and config['artifact_path'] is not None:
        config['artifact_uri'] = str(config['artifact_path'])

    return config


def _artifact_cache_path_for_uri(artifact_uri):
    suffix = ''.join(Path(artifact_uri).suffixes) or '.bin'
    digest = hashlib.sha256(artifact_uri.encode('utf-8')).hexdigest()
    return _get_download_cache_dir() / f'{digest}{suffix}'


def _download_artifact(artifact_uri):
    target_path = _artifact_cache_path_for_uri(artifact_uri)
    if target_path.exists():
        return target_path

    response = requests.get(artifact_uri, timeout=_get_download_timeout_seconds(), stream=True)
    response.raise_for_status()

    temp_path = target_path.with_suffix(target_path.suffix + '.download')
    with temp_path.open('wb') as handle:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                handle.write(chunk)
    os.replace(temp_path, target_path)
    return target_path


def _resolve_artifact_path(config):
    artifact_uri = str(config.get('artifact_uri') or '').strip()
    default_path = config.get('artifact_path')

    candidates = []
    if artifact_uri:
        candidates.append(artifact_uri)
    if default_path:
        candidates.append(str(default_path))

    last_error = None
    for candidate in candidates:
        try:
            if candidate.startswith('http://') or candidate.startswith('https://'):
                return _download_artifact(candidate), candidate

            candidate_path = Path(candidate)
            if not candidate_path.is_absolute():
                candidate_path = (Path(settings.BASE_DIR) / candidate_path).resolve()
            if candidate_path.exists():
                return candidate_path, candidate
            last_error = f'model artifact not found: {candidate_path}'
        except Exception as error:  # pragma: no cover - network/file edge cases
            last_error = str(error)

    raise LocalModelPredictError(last_error or 'no model artifact configured')


def _build_feature_vector(size, weight):
    size_value = float(size)
    weight_value = float(weight)
    log_weight_value = np.log1p(max(weight_value, 0.0))
    safe_size = max(size_value, 0.02)
    return np.asarray([[
        round(size_value, 6),
        round(weight_value, 6),
        round(log_weight_value, 6),
        round(size_value * weight_value, 6),
        round(size_value * log_weight_value, 6),
        round(size_value * size_value, 6),
        round(log_weight_value * log_weight_value, 6),
        round(weight_value / safe_size, 6),
    ]], dtype=np.float32)


def _load_model_bundle(force_reload=False):
    with _MODEL_LOCK:
        if not force_reload and _MODEL_STATE['loaded'] and _MODEL_STATE['bundle'] is not None:
            return _MODEL_STATE['bundle']

        runtime_config = _build_runtime_config()
        artifact_path, artifact_key = _resolve_artifact_path(runtime_config)

        if (
            not force_reload and
            _MODEL_STATE['loaded'] and
            _MODEL_STATE['bundle'] is not None and
            _MODEL_STATE['artifact_key'] == artifact_key
        ):
            return _MODEL_STATE['bundle']

        try:
            bundle = joblib.load(artifact_path)
        except Exception as error:
            _MODEL_STATE.update({
                'artifact_key': artifact_key,
                'artifact_uri': runtime_config.get('artifact_uri') or '',
                'artifact_path': str(artifact_path),
                'bundle': None,
                'loaded': False,
                'load_error': str(error),
                'source': runtime_config.get('source') or 'unknown',
            })
            raise LocalModelPredictError(f'load model failed: {error}') from error

        if not isinstance(bundle, dict) or 'model' not in bundle or 'classes' not in bundle:
            raise LocalModelPredictError('invalid model bundle format')

        bundle['rideableSpecies'] = set(bundle.get('rideableSpecies', []))
        bundle['_runtime'] = {
            'version': runtime_config.get('version') or bundle.get('version') or 'egg_model_v2',
            'modelType': runtime_config.get('model_type') or bundle.get('modelFamily') or '',
            'strategy': runtime_config.get('strategy') or EggPredictorConfig.STRATEGY_HYBRID,
            'artifactKey': artifact_key,
            'artifactPath': str(artifact_path),
            'artifactUri': runtime_config.get('artifact_uri') or '',
            'source': runtime_config.get('source') or 'unknown',
            'configJson': runtime_config.get('config_json') or {},
        }

        _MODEL_STATE.update({
            'artifact_key': artifact_key,
            'artifact_uri': runtime_config.get('artifact_uri') or '',
            'artifact_path': str(artifact_path),
            'bundle': bundle,
            'loaded': True,
            'load_error': '',
            'source': runtime_config.get('source') or 'unknown',
        })
        logger.info('local predictor loaded: %s', bundle['_runtime'])
        return bundle


def get_local_model_runtime_summary():
    try:
        bundle = _load_model_bundle(force_reload=False)
    except LocalModelPredictError as error:
        return {
            'available': False,
            'error': str(error),
            'artifactUri': _MODEL_STATE['artifact_uri'],
            'artifactPath': _MODEL_STATE['artifact_path'],
        }

    runtime = bundle.get('_runtime', {})
    return {
        'available': True,
        'version': runtime.get('version') or bundle.get('version') or 'egg_model_v2',
        'strategy': runtime.get('strategy') or EggPredictorConfig.STRATEGY_HYBRID,
        'modelType': runtime.get('modelType') or bundle.get('modelFamily') or '',
        'artifactUri': runtime.get('artifactUri') or '',
        'artifactPath': runtime.get('artifactPath') or '',
        'source': runtime.get('source') or '',
        'classCount': len(bundle.get('classes') or []),
    }


def preload_local_model_if_configured():
    if not _parse_bool(getattr(settings, 'EGG_MODEL_PRELOAD_ON_START', True), default=True):
        return
    _load_model_bundle(force_reload=False)


def reset_local_model_cache():
    with _MODEL_LOCK:
        _MODEL_STATE.update({
            'artifact_key': '',
            'artifact_uri': '',
            'artifact_path': '',
            'bundle': None,
            'loaded': False,
            'load_error': '',
            'source': '',
        })


def predict_with_local_model(size, weight, rideable_only=False, top_k=None):
    bundle = _load_model_bundle(force_reload=False)
    features = _build_feature_vector(size, weight)
    probabilities = bundle['model'].predict_proba(features)[0]
    classes = bundle['classes']
    candidates = list(zip(classes, probabilities))

    if rideable_only:
        candidates = [
            (species, prob)
            for species, prob in candidates
            if species in bundle['rideableSpecies']
        ]

    candidates.sort(key=lambda item: item[1], reverse=True)
    result_limit = top_k or _get_default_top_k()
    normalized_results = []
    for index, (species, prob) in enumerate(candidates[:result_limit], start=1):
        normalized_results.append({
            'species': species,
            'prob': float(prob),
            'rank': index,
            'rideable': species in bundle['rideableSpecies'],
        })

    runtime = bundle.get('_runtime', {})
    return {
        'results': normalized_results,
        'total': len(normalized_results),
        'source': 'cloud_model_fallback',
        'predictionVersion': runtime.get('version') or bundle.get('version') or 'egg_model_v2',
        'modelType': runtime.get('modelType') or bundle.get('modelFamily') or '',
        'strategy': runtime.get('strategy') or EggPredictorConfig.STRATEGY_HYBRID,
        'artifactUri': runtime.get('artifactUri') or '',
        'artifactPath': runtime.get('artifactPath') or '',
    }
