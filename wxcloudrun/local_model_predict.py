import hashlib
import json
import logging
import os
import tempfile
import threading
import warnings
from pathlib import Path

import joblib
import numpy as np
import requests
from django.conf import settings
from django.db import DatabaseError

from wxcloudrun.models import EggPredictorConfig


logger = logging.getLogger('log')
warnings.filterwarnings('ignore', message='X does not have valid feature names.*')

DEFAULT_MODEL_FILENAME = 'egg_model_v2.joblib.gz'
DEFAULT_MODEL_TYPE = 'lightgbm_multiclass_joblib'
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
        'model_type': DEFAULT_MODEL_TYPE,
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


def _build_knn_feature_vector(size, weight, transform):
    mode = str((transform or {}).get('mode') or '').strip()
    size_value = float(size)
    weight_value = float(weight)
    log_weight_value = np.log1p(max(weight_value, 0.0))
    safe_size = max(size_value, 0.02)

    if mode == 'standard_size_log_weight':
        raw = np.asarray([[size_value, log_weight_value]], dtype=np.float32)
    elif mode == 'standard_size_weight_ratio':
        raw = np.asarray([[size_value, weight_value, weight_value / safe_size]], dtype=np.float32)
    elif mode == 'bucket_size_log_weight':
        raw = np.asarray([[size_value / 0.01, log_weight_value / 0.08]], dtype=np.float32)
    elif mode == 'bucket_size_weight':
        raw = np.asarray([[size_value / 0.01, weight_value / 1.0]], dtype=np.float32)
    else:
        raise LocalModelPredictError(f'unsupported fusion feature mode: {mode}')

    if transform.get('mean') is not None:
        mean = np.asarray(transform.get('mean'), dtype=np.float32)
        std = np.asarray(transform.get('std'), dtype=np.float32)
        std[std < 1e-6] = 1.0
        raw = (raw - mean) / std
    return raw.astype(np.float32)


def _make_scoped_rank(probabilities, classes, rideable_species, rideable_only, limit):
    scoped = probabilities
    if rideable_only:
        scoped = np.asarray(probabilities, dtype=np.float64).copy()
        rideable_mask = np.asarray([species in rideable_species for species in classes], dtype=bool)
        scoped[~rideable_mask] = -1.0
    return np.argsort(scoped)[::-1][:limit]


def _compute_fusion_vote(bundle, size, weight, rideable_only):
    fusion = bundle.get('fusion') or {}
    if not fusion.get('enabled'):
        return None

    params = fusion.get('params') or {}
    neighbors = fusion.get('neighbors')
    if neighbors is None:
        return None

    transform = fusion.get('transform') or {}
    query_x = _build_knn_feature_vector(size, weight, transform)
    train_species_indices = np.asarray(fusion.get('trainSpeciesIndices'), dtype=np.int32)
    train_weights = np.asarray(fusion.get('trainWeights'), dtype=np.float32)
    train_rideable = np.asarray(fusion.get('trainRideable'), dtype=bool)
    if train_species_indices.size == 0 or train_weights.size == 0:
        return None

    try:
        requested_neighbors = int(params.get('neighbors') or 20)
    except (TypeError, ValueError):
        requested_neighbors = 20
    requested_neighbors = max(1, min(requested_neighbors, train_species_indices.size))
    distances, indices = neighbors.kneighbors(query_x, n_neighbors=requested_neighbors, return_distance=True)
    neighbor_indices = indices[0].astype(np.int32)
    neighbor_distances = distances[0].astype(np.float32)

    if rideable_only:
        keep_mask = train_rideable[neighbor_indices]
        neighbor_indices = neighbor_indices[keep_mask]
        neighbor_distances = neighbor_distances[keep_mask]
        if len(neighbor_indices) == 0:
            return None

    species_indices = train_species_indices[neighbor_indices]
    base_weights = train_weights[neighbor_indices]
    try:
        bandwidth = max(float(params.get('bandwidth') or 0.2), 1e-6)
    except (TypeError, ValueError):
        bandwidth = 0.2
    distance_weights = np.exp(-0.5 * np.square(neighbor_distances / bandwidth))
    scores = np.bincount(
        species_indices,
        weights=base_weights * distance_weights,
        minlength=len(bundle.get('classes') or []),
    )
    total = float(scores.sum())
    if total <= 0:
        return None

    ranked_indices = np.argsort(scores)[::-1]
    top_index = int(ranked_indices[0])
    top_score = float(scores[top_index])
    second_score = float(scores[ranked_indices[1]]) if len(ranked_indices) > 1 else 0.0
    vote = {
        'speciesIndex': top_index,
        'confidence': top_score / total,
        'margin': top_score / max(second_score, 1e-9),
        'nearestDistance': float(neighbor_distances[0]) if len(neighbor_distances) else None,
        'params': params,
    }

    try:
        accepted = (
            vote['confidence'] >= float(params.get('confidence') or 0.5) and
            vote['margin'] >= float(params.get('margin') or 1.2) and
            vote['nearestDistance'] <= float(params.get('maxDistance') or 0.75)
        )
    except (TypeError, ValueError):
        accepted = False
    vote['accepted'] = accepted
    return vote


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

        model = bundle['model']
        if hasattr(model, 'set_params'):
            try:
                model.set_params(n_jobs=1)
            except Exception:  # pragma: no cover - model-specific runtime quirks
                pass
        elif hasattr(model, 'n_jobs'):
            try:
                model.n_jobs = 1
            except Exception:  # pragma: no cover - defensive
                pass

        bundle['rideableSpecies'] = set(bundle.get('rideableSpecies', []))
        runtime_model_type = bundle.get('modelFamily') or runtime_config.get('model_type') or DEFAULT_MODEL_TYPE
        if bundle.get('modelFamily') == 'lightgbm':
            runtime_model_type = DEFAULT_MODEL_TYPE
        bundle['_runtime'] = {
            'version': runtime_config.get('version') or bundle.get('version') or 'egg_model_v2',
            'modelType': runtime_model_type,
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
        'modelType': runtime.get('modelType') or bundle.get('modelFamily') or DEFAULT_MODEL_TYPE,
        'artifactUri': runtime.get('artifactUri') or '',
        'artifactPath': runtime.get('artifactPath') or '',
        'source': runtime.get('source') or '',
        'classCount': len(bundle.get('classes') or []),
        'fusionEnabled': bool(bundle.get('fusion') and bundle.get('fusion', {}).get('enabled')),
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
    fusion_vote = _compute_fusion_vote(bundle, size, weight, rideable_only)
    use_fusion = bool(fusion_vote and fusion_vote.get('accepted'))
    ranked_indices = _make_scoped_rank(
        probabilities,
        classes,
        bundle['rideableSpecies'],
        rideable_only,
        top_k or _get_default_top_k(),
    )
    if (
        use_fusion and
        fusion_vote.get('params', {}).get('requireBaseTop10', True) and
        int(fusion_vote['speciesIndex']) not in {int(index) for index in ranked_indices}
    ):
        use_fusion = False
    if use_fusion:
        fusion_index = int(fusion_vote['speciesIndex'])
        ranked_indices = np.asarray(
            [fusion_index] + [int(index) for index in ranked_indices if int(index) != fusion_index],
            dtype=np.int32,
        )[:max(1, top_k or _get_default_top_k())]

    result_limit = top_k or _get_default_top_k()
    normalized_results = []
    for index, species_index in enumerate(ranked_indices[:result_limit], start=1):
        species = classes[int(species_index)]
        prob = float(probabilities[int(species_index)])
        if use_fusion and int(species_index) == int(fusion_vote['speciesIndex']):
            prob = max(prob, float(fusion_vote['confidence']))
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
        'modelType': runtime.get('modelType') or bundle.get('modelFamily') or DEFAULT_MODEL_TYPE,
        'strategy': runtime.get('strategy') or EggPredictorConfig.STRATEGY_HYBRID,
        'artifactUri': runtime.get('artifactUri') or '',
        'artifactPath': runtime.get('artifactPath') or '',
        'fusionApplied': use_fusion,
        'fusionSpecies': classes[int(fusion_vote['speciesIndex'])] if use_fusion else '',
        'fusionConfidence': float(fusion_vote['confidence']) if use_fusion else None,
        'fusionMargin': float(fusion_vote['margin']) if use_fusion else None,
        'fusionNearestDistance': float(fusion_vote['nearestDistance']) if use_fusion else None,
    }
