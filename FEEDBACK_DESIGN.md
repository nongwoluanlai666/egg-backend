# Egg Feedback / Local Training Design

## Current Position

- Short term prediction accuracy: `POST /api/egg-predict` is now `upstream first -> local model fallback`.
- Mini program fallback: if cloud call fails, the client falls back to the existing local predictor.
- Feedback collection: keep `POST /api/egg-feedback` lightweight. It only validates, de-duplicates, rate-limits, scores, and stores feedback.
- Source-site verification: do **not** run during feedback save. Do it later offline after exporting records to local.
- Model training: do **not** train on cloud. Export data locally, clean locally, verify locally, train locally.

## Can Existing Cloud Data Be Used

Yes.

Existing feedback rows in cloud MySQL are usable as the raw pool for later training, but they should be split into confidence tiers locally instead of being fed directly into a model.

Recommended local confidence tiers:

1. High confidence
   - `quality_status = accepted`
   - not custom, or custom but later normalized to canonical species
   - passes offline source-site verification (`top1` match is best)
2. Medium confidence
   - `quality_status = accepted`
   - source-site offline verification hits Top 10 but not Top 1
3. Low confidence / review pool
   - `pending`
   - `suspicious`
   - custom names not yet normalized
   - clear mismatch after offline verification

## Online Interfaces

### Stable user-facing interfaces

- `POST /api/egg-predict`
  - first tries the upstream source-site predictor
  - if upstream fails, falls back to the cloud container's packaged or configured local model
  - response path stays unchanged for the mini program
- `POST /api/egg-feedback`
  - unchanged path
  - no synchronous source-site verification anymore
  - remains lightweight and low-latency

### Developer-only interfaces

These require the request header `X-DEV-ADMIN-TOKEN`, which must match the backend env var `EGG_DEV_ADMIN_TOKEN`.

- `POST /api/dev/feedback-export`
  - paginated export for local cleaning/training
  - supports `pageSize`, `cursor`, `minQualityScore`, `qualityStatuses`, `includeCustom`
- `GET /api/dev/model-config`
  - read the current active predictor config plus recent history
- `POST /api/dev/model-config`
  - register and activate a new predictor config
  - stores version, strategy, model type, artifact URI, notes, and config JSON

## Mini Program Developer Entry

The mini program now contains a hidden page:

- route: `pages/dev/index`
- access pattern:
  - only intended for `develop` environment
  - hidden on the home page brand area
  - long-press or tap multiple times to open

Capabilities:

1. Save the developer token locally in DevTools storage
2. Page through cloud feedback rows
3. Copy the current batch as JSONL text
4. Read the active cloud predictor config
5. Update and activate a new predictor config record

## Export Workflow

Because `wx.cloud.callContainer` is most convenient inside WeChat DevTools, the current pragmatic export flow is:

1. Open the hidden developer page in local `develop` mode
2. Fill and save the developer token
3. Export feedback data page by page
4. Copy each batch of JSONL text
5. Append the copied batches into a local `.jsonl` file
6. Run local cleaning, offline source-site verification, and model training

This avoids making the feedback API slower and avoids adding extra pressure to the live cloud container.

## Local Verification / Training Pipeline

Recommended offline pipeline:

1. Export raw rows from cloud
2. Normalize custom species names into canonical names where possible
3. Run offline source-site verification on the exported rows
4. Split rows into confidence tiers
5. Merge with the bootstrap sample set from `species_points`
6. Train and evaluate locally
7. Upload model artifact to OSS or another accessible location
8. Register the new model version through the developer page

## Backend Runtime Behavior

### `POST /api/egg-predict`

Runtime order:

1. Validate `size`, `weight`, `rideableOnly`
2. Call the upstream predictor
3. If upstream succeeds:
   - return upstream results directly
4. If upstream fails:
   - load the local packaged/configured model
   - run local prediction
   - return the local prediction with fallback metadata
5. If both upstream and local model fail:
   - return `502`

Returned fallback payload includes:

- `source = cloud_model_fallback`
- `predictionVersion = <local model version>`
- `fallbackApplied = true`
- `fallbackReason = upstream_unavailable`
- `upstreamError = <upstream error message>`

### Local model loading

- default packaged model path:
  - `egg_backend/egg-backend/model_artifacts/egg_model_v2.joblib.gz`
- the container preloads the model on app startup by default
- if the active `EggPredictorConfig` record contains `artifact_uri`, runtime uses that artifact first
- if `artifact_uri` is an `http(s)` URL, the container downloads it once into the runtime cache and then loads it

Recommended memory planning:

- current packaged model is roughly `43 MB` on disk
- loaded process memory is roughly `1.4 GB` private memory
- cloud container should use at least `2 GB`
- `4 GB` is safer if the container will handle more concurrent work or multiple heavy operations

## Train To Deploy Workflow

This is the current recommended chain for updating the backend model.

### Step 1: export data

Export feedback rows from cloud MySQL or via the developer export interface.

### Step 2: prepare training rows locally

If the upstream site is available:

```powershell
.\.venv\Scripts\python.exe update\model\dataClean.py --feedback-csv "update\数据库导出\<your-file>.csv"
```

If the upstream site is unstable:

```powershell
.\.venv\Scripts\python.exe update\model\dataClean.py --skip-upstream --feedback-csv "update\数据库导出\<your-file>.csv"
```

Before overwriting a known-good cleaned dataset, back it up.

### Step 3: train locally

```powershell
.\.venv\Scripts\python.exe update\model\dataTrain.py
```

Important outputs:

- `update/model/model/egg_model_v2.joblib.gz`
- `update/model/model/egg_model_v2_report.json`
- `update/model/model/egg_model_v2_meta.json`

### Step 4: package the model for backend deployment

Copy the trained model into the backend artifact directory:

```powershell
Copy-Item update\model\model\egg_model_v2.joblib.gz egg_backend\egg-backend\model_artifacts\egg_model_v2.joblib.gz -Force
```

This makes the model travel with the Docker image and gives the backend an immediate fallback model even without OSS.

### Step 5: optionally switch to remote artifact management

If you upload the model to OSS, set either:

- backend env var `EGG_MODEL_ARTIFACT_URI`
- or active `EggPredictorConfig.artifact_uri`

Runtime will try that artifact first, then fall back to the packaged local artifact if available.

### Step 6: rebuild and deploy cloud container

Because the model artifact is inside the backend project, rebuilding the Docker image is enough to deploy the new fallback model.

### Step 7: verify after deployment

Recommended checks:

1. Call `POST /api/egg-predict` with a normal sample while upstream is healthy
2. Temporarily point `ROCO_UPSTREAM_BASE_URL` to an invalid address in a test environment
3. Confirm the same interface now returns `source = cloud_model_fallback`
4. Confirm the mini program still works without frontend changes

## Current Baseline Result

The current lightweight Gaussian baseline was already trained offline once.

Reference output:

- `roco/snapshots/2026-04-18/analysis/generated/baseline_training/baseline_report.json`

Observed metrics on the held-out set:

- `top1_accuracy = 0.1241`
- `top3_accuracy = 0.2263`
- `top5_accuracy = 0.2555`

Conclusion:

- the baseline is useful as a technical scaffold
- it is not strong enough to replace the source-site predictor yet
- short term should continue to rely on the upstream proxy

## Training Plan

### Stage 1: data accumulation

- Continue collecting feedback through the current mini program
- Keep `egg-feedback` lightweight
- Export and verify offline in batches

### Stage 2: first trainable local model

Recommended direction:

- features:
  - `size`
  - `weight`
  - `rideable_only`
  - optionally local predictor rank/probability features from snapshot
- model candidates:
  - gradient boosting classifier
  - LightGBM / XGBoost style tabular classifier
  - top-k re-ranker over a candidate shortlist

### Stage 3: cloud rollout

- package the local fallback model in `model_artifacts/`
- or upload model artifact to OSS
- register new version through `/api/dev/model-config`
- backend runtime now supports upstream-first with local-model fallback
- future work can still add pure `cloud_model` or more advanced routing strategies

## Operational Notes

- backend env vars to set in cloud:
  - `EGG_DEV_ADMIN_TOKEN`
  - optional: `EGG_DEV_EXPORT_MAX_PAGE_SIZE`
- model/runtime env vars:
  - `EGG_MODEL_ARTIFACT_URI`
  - `EGG_MODEL_DEFAULT_RELATIVE_PATH`
  - `EGG_MODEL_PRELOAD_ON_START`
  - `EGG_MODEL_DOWNLOAD_CACHE_DIR`
  - `EGG_MODEL_DOWNLOAD_TIMEOUT_SECONDS`
  - `EGG_MODEL_TOP_K`
- current predictor config records are for version management and future switching preparation
- current live prediction accuracy is protected by the upstream-first, local-fallback chain
