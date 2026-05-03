import io
import json
import re
import zipfile
from datetime import datetime
from uuid import uuid4

import oss2
import requests
from django.conf import settings


MAX_NICKNAME_LENGTH = 32
MAX_CONTENT_LENGTH = 20000
MAX_IMAGE_COUNT = 5
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 6 * 1024 * 1024
TEMP_FILE_URL_TIMEOUT_SECONDS = 20


class GuideSubmissionError(Exception):
    pass


class GuideSubmissionConfigurationError(GuideSubmissionError):
    pass


def normalize_text(value, max_length):
    if value is None:
        return ''
    normalized = str(value).replace('\r\n', '\n').replace('\r', '\n').strip()
    return normalized[:max_length]


def sanitize_file_name(value, default_name):
    text = str(value or '').strip().lower()
    text = re.sub(r'[^a-z0-9._-]+', '-', text)
    text = text.strip('.-')
    return text or default_name


def ensure_oss_configured():
    required_values = {
        'OSS_ACCESS_KEY_ID': settings.OSS_ACCESS_KEY_ID,
        'OSS_ACCESS_KEY_SECRET': settings.OSS_ACCESS_KEY_SECRET,
        'OSS_BUCKET': settings.OSS_BUCKET,
        'OSS_REGION': settings.OSS_REGION,
        'OSS_UPLOAD_PREFIX': settings.OSS_UPLOAD_PREFIX,
    }
    missing = [key for key, value in required_values.items() if not str(value or '').strip()]
    if missing:
        raise GuideSubmissionConfigurationError(
            f'攻略投稿未配置 OSS 参数: {", ".join(missing)}'
        )


def get_oss_bucket():
    ensure_oss_configured()
    endpoint = f'https://oss-{settings.OSS_REGION}.aliyuncs.com'
    auth = oss2.Auth(settings.OSS_ACCESS_KEY_ID, settings.OSS_ACCESS_KEY_SECRET)
    return oss2.Bucket(auth, endpoint, settings.OSS_BUCKET)


def parse_submission_payload(payload):
    nickname = normalize_text(payload.get('nickname'), MAX_NICKNAME_LENGTH)
    content = normalize_text(payload.get('content'), MAX_CONTENT_LENGTH)
    images = payload.get('images')

    if not nickname:
        raise GuideSubmissionError('请填写昵称')
    if not content:
        raise GuideSubmissionError('请填写攻略正文')
    if not isinstance(images, list):
        images = []
    if len(images) > MAX_IMAGE_COUNT:
        raise GuideSubmissionError(f'最多上传 {MAX_IMAGE_COUNT} 张图片')

    normalized_images = []
    for index, item in enumerate(images):
      if not isinstance(item, dict):
          raise GuideSubmissionError(f'第 {index + 1} 张图片数据格式错误')

      file_id = str(item.get('fileID') or item.get('fileId') or '').strip()
      temp_file_url = str(item.get('tempFileURL') or item.get('tempFileUrl') or '').strip()
      file_name = sanitize_file_name(item.get('fileName'), f'image-{index + 1}.jpg')

      if not file_id:
          raise GuideSubmissionError(f'第 {index + 1} 张图片缺少 fileID')
      if not temp_file_url:
          raise GuideSubmissionError(f'第 {index + 1} 张图片缺少临时链接')

      normalized_images.append({
          'file_id': file_id,
          'temp_file_url': temp_file_url,
          'file_name': file_name,
      })

    return {
        'nickname': nickname,
        'content': content,
        'images': normalized_images,
    }


def download_image_bytes(image, index):
    try:
        response = requests.get(
            image['temp_file_url'],
            timeout=TEMP_FILE_URL_TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        raise GuideSubmissionError(f'第 {index + 1} 张图片下载失败') from error

    if response.status_code < 200 or response.status_code >= 300:
        raise GuideSubmissionError(f'第 {index + 1} 张图片下载失败')

    content = response.content or b''
    size = len(content)
    if size <= 0:
        raise GuideSubmissionError(f'第 {index + 1} 张图片内容为空')
    if size > MAX_IMAGE_BYTES:
        raise GuideSubmissionError(f'第 {index + 1} 张图片过大')

    return {
        'file_name': image['file_name'],
        'file_id': image['file_id'],
        'temp_file_url': image['temp_file_url'],
        'bytes': content,
        'size': size,
        'content_type': response.headers.get('Content-Type', ''),
    }


def download_submission_images(images):
    downloaded = []
    total_size = 0

    for index, image in enumerate(images):
        downloaded_image = download_image_bytes(image, index)
        total_size += downloaded_image['size']
        if total_size > MAX_TOTAL_IMAGE_BYTES:
            raise GuideSubmissionError('图片总大小过大，请减少图片数量或更换更小的图片')
        downloaded.append(downloaded_image)

    return downloaded


def build_submission_archive(payload, meta):
    archive_buffer = io.BytesIO()
    archive_timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')

    downloaded_images = download_submission_images(payload['images'])
    manifest = {
        'submittedAt': meta['submitted_at'],
        'nickname': payload['nickname'],
        'contentLength': len(payload['content']),
        'imageCount': len(downloaded_images),
        'images': [
            {
                'fileID': image['file_id'],
                'fileName': image['file_name'],
                'contentType': image['content_type'],
                'size': image['size'],
            }
            for image in downloaded_images
        ],
        'client': {
            'openid': meta['openid'],
            'appid': meta['appid'],
            'userAgent': meta['user_agent'],
            'ip': meta['ip'],
        },
    }

    with zipfile.ZipFile(archive_buffer, mode='w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            'meta.json',
            json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'),
        )
        archive.writestr('nickname.txt', payload['nickname'].encode('utf-8'))
        archive.writestr('content.md', payload['content'].encode('utf-8'))

        for image in downloaded_images:
            archive.writestr(f'images/{image["file_name"]}', image['bytes'])

    archive_buffer.seek(0)
    archive_name = f'guide-submission-{archive_timestamp}-{uuid4().hex[:8]}.zip'
    return archive_name, archive_buffer.getvalue()


def upload_submission_archive(payload, meta):
    archive_name, archive_bytes = build_submission_archive(payload, meta)
    prefix = str(settings.OSS_UPLOAD_PREFIX or 'uploads/').strip().strip('/')
    object_key = f'{prefix}/{archive_name}' if prefix else archive_name

    bucket = get_oss_bucket()
    bucket.put_object(
        object_key,
        archive_bytes,
        headers={'Content-Type': 'application/zip'},
    )

    public_base_url = str(settings.OSS_PUBLIC_BASE_URL or '').strip().rstrip('/')
    public_url = f'{public_base_url}/{object_key}' if public_base_url else ''

    return {
        'archiveName': archive_name,
        'objectKey': object_key,
        'url': public_url,
        'size': len(archive_bytes),
        'submittedAt': meta['submitted_at'],
    }
