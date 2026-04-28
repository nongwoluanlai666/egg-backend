import os
from pathlib import Path
import time

CUR_PATH = os.path.dirname(os.path.realpath(__file__))  
LOG_PATH = os.path.join(os.path.dirname(CUR_PATH), 'logs') # LOG_PATH是存放日志的路径
if not os.path.exists(LOG_PATH): os.mkdir(LOG_PATH)  # 如果不存在这个logs文件夹，就自动创建一个

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/3.2/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = 'django-insecure-_&03zc)d*3)w-(0grs-+t-0jjxktn7k%$3y6$9=x_n_ibg4js6'

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

ALLOWED_HOSTS = ['*']

# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'wxcloudrun'
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    # 'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'wxcloudrun.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'wxcloudrun.wsgi.application'

# Database
# https://docs.djangoproject.com/en/3.2/ref/settings/#databases

MYSQL_ADDRESS = os.environ.get('MYSQL_ADDRESS', '127.0.0.1:3306')
if ':' in MYSQL_ADDRESS:
    MYSQL_HOST, MYSQL_PORT = MYSQL_ADDRESS.split(':', 1)
else:
    MYSQL_HOST, MYSQL_PORT = MYSQL_ADDRESS, '3306'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': os.environ.get('MYSQL_DATABASE', 'django_demo'),
        'USER': os.environ.get('MYSQL_USERNAME', 'root'),
        'HOST': MYSQL_HOST,
        'PORT': MYSQL_PORT,
        'PASSWORD': os.environ.get('MYSQL_PASSWORD', ''),
        'OPTIONS': {'charset': 'utf8mb4'},
    }
}

# Password validation
# https://docs.djangoproject.com/en/3.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

LOGGING = {
    'version': 1,
    'disable_existing_loggers': True,
    'formatters': {
        # 日志格式
        'standard': {
            'format': '[%(asctime)s] [%(filename)s:%(lineno)d] [%(module)s:%(funcName)s] '
                      '[%(levelname)s]- %(message)s'},
        'simple': {  # 简单格式
            'format': '%(levelname)s %(message)s'
        },
    },
    # 过滤
    'filters': {
    },
    # 定义具体处理日志的方式
    'handlers': {
        # 默认记录所有日志
        'default': {
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': os.path.join(LOG_PATH, 'all-{}.log'.format(time.strftime('%Y-%m-%d'))),
            'maxBytes': 1024 * 1024 * 5,  # 文件大小
            'backupCount': 5,  # 备份数
            'formatter': 'standard',  # 输出格式
            'encoding': 'utf-8',  # 设置默认编码，否则打印出来汉字乱码
        },
        # 输出错误日志
        'error': {
            'level': 'ERROR',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': os.path.join(LOG_PATH, 'error-{}.log'.format(time.strftime('%Y-%m-%d'))),
            'maxBytes': 1024 * 1024 * 5,  # 文件大小
            'backupCount': 5,  # 备份数
            'formatter': 'standard',  # 输出格式
            'encoding': 'utf-8',  # 设置默认编码
        },
        # 控制台输出
        'console': {
            'level': 'DEBUG',
            'class': 'logging.StreamHandler',
            'formatter': 'standard'
        },
        # 输出info日志
        'info': {
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': os.path.join(LOG_PATH, 'info-{}.log'.format(time.strftime('%Y-%m-%d'))),
            'maxBytes': 1024 * 1024 * 5,
            'backupCount': 5,
            'formatter': 'standard',
            'encoding': 'utf-8',  # 设置默认编码
        },
    },
    # 配置用哪几种 handlers 来处理日志
    'loggers': {
        # 类型 为 django 处理所有类型的日志， 默认调用
        'django': {
            'handlers': ['default', 'console'],
            'level': 'INFO',
            'propagate': False
        },
        # log 调用时需要当作参数传入
        'log': {
            'handlers': ['error', 'info', 'console', 'default'],
            'level': 'INFO',
            'propagate': True
        },
    }
}

# Internationalization
# https://docs.djangoproject.com/en/3.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_L10N = True

USE_TZ = False

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/3.2/howto/static-files/

STATIC_URL = '/static/'

# Default primary key field type
# https://docs.djangoproject.com/en/3.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGS_DIR = '/data/logs/'

ROCO_UPSTREAM_BASE_URL = os.environ.get('ROCO_UPSTREAM_BASE_URL', 'https://roco-eggs.tsuki-world.com').rstrip('/')
ROCO_UPSTREAM_TIMEOUT_SECONDS = float(os.environ.get('ROCO_UPSTREAM_TIMEOUT_SECONDS', '5'))
ROCO_UPSTREAM_CACHE_TTL_SECONDS = int(os.environ.get('ROCO_UPSTREAM_CACHE_TTL_SECONDS', '300'))
EGG_DEV_ADMIN_TOKEN = os.environ.get('EGG_DEV_ADMIN_TOKEN', '')
try:
    EGG_DEV_EXPORT_MAX_PAGE_SIZE = int(os.environ.get('EGG_DEV_EXPORT_MAX_PAGE_SIZE', '100'))
except (TypeError, ValueError):
    EGG_DEV_EXPORT_MAX_PAGE_SIZE = 100


def parse_env_bool(name, default='false'):
    return str(os.environ.get(name, default)).strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def parse_env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def parse_env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


EGG_MODEL_ARTIFACT_URI = os.environ.get('EGG_MODEL_ARTIFACT_URI', '').strip()
EGG_MODEL_DEFAULT_RELATIVE_PATH = os.environ.get(
    'EGG_MODEL_DEFAULT_RELATIVE_PATH',
    'model_artifacts/egg_model_v2.joblib.gz',
).strip()
EGG_MODEL_PRELOAD_ON_START = parse_env_bool('EGG_MODEL_PRELOAD_ON_START', 'true')
EGG_MODEL_DOWNLOAD_CACHE_DIR = os.environ.get('EGG_MODEL_DOWNLOAD_CACHE_DIR', '/tmp/egg_model_cache').strip()
try:
    EGG_MODEL_DOWNLOAD_TIMEOUT_SECONDS = float(os.environ.get('EGG_MODEL_DOWNLOAD_TIMEOUT_SECONDS', '20'))
except (TypeError, ValueError):
    EGG_MODEL_DOWNLOAD_TIMEOUT_SECONDS = 20.0
try:
    EGG_MODEL_TOP_K = int(os.environ.get('EGG_MODEL_TOP_K', '10'))
except (TypeError, ValueError):
    EGG_MODEL_TOP_K = 10

WECHAT_APP_ID = os.environ.get('WECHAT_APP_ID', os.environ.get('WXA_APPID', '')).strip()
WECHAT_APP_SECRET = os.environ.get('WECHAT_APP_SECRET', os.environ.get('WXA_APPSECRET', '')).strip()

MERCHANT_NOTICE_TIMEZONE = os.environ.get('MERCHANT_NOTICE_TIMEZONE', 'Asia/Shanghai').strip() or 'Asia/Shanghai'
MERCHANT_SOURCE_URL = os.environ.get(
    'MERCHANT_SOURCE_URL',
    'https://roco-eggs.tsuki-world.com/api/merchant/current',
).strip()
MERCHANT_SOURCE_REFERER = os.environ.get(
    'MERCHANT_SOURCE_REFERER',
    'https://roco-eggs.tsuki-world.com/forum',
).strip()
MERCHANT_SOURCE_USER_AGENT = os.environ.get(
    'MERCHANT_SOURCE_USER_AGENT',
    (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36'
    ),
).strip()
MERCHANT_SOURCE_PRIORITY = os.environ.get(
    'MERCHANT_SOURCE_PRIORITY',
    'primary,backup',
).strip()
MERCHANT_BACKUP_SOURCE_URL = os.environ.get(
    'MERCHANT_BACKUP_SOURCE_URL',
    'https://wegame.shallow.ink/api/v1/games/rocom/merchant/info?refresh=true',
).strip()
MERCHANT_BACKUP_SOURCE_REFERER = os.environ.get(
    'MERCHANT_BACKUP_SOURCE_REFERER',
    'https://wegame.shallow.ink/api/v1/games/rocom/merchant/info?refresh=true',
).strip()
MERCHANT_BACKUP_SOURCE_API_KEY = os.environ.get(
    'MERCHANT_BACKUP_SOURCE_API_KEY',
    'sk-f9a97f99fed455ae910d028edc172078',
).strip()
MERCHANT_NOTIFY_TEMPLATE_ID = os.environ.get(
    'MERCHANT_NOTIFY_TEMPLATE_ID',
    'NA9mVDvFObzNcV9QbXJbUfyoRw_XAw0fLYd8TvIKNpo',
).strip()
MERCHANT_NOTIFY_PAGE = os.environ.get(
    'MERCHANT_NOTIFY_PAGE',
    'pages/merchant-notice/index?from=notify',
).strip()
MERCHANT_NOTIFY_SPECIAL_KEYWORDS = os.environ.get(
    'MERCHANT_NOTIFY_SPECIAL_KEYWORDS',
    '炫彩,棱镜球,同乘,祝福项坠',
).strip()
MERCHANT_NOTIFY_DEFAULT_SELECTED_GOODS = os.environ.get(
    'MERCHANT_NOTIFY_DEFAULT_SELECTED_GOODS',
    '炫彩蛋,棱镜球,祝福项坠,黑白炫彩蛋,赛季炫彩蛋',
).strip()
MERCHANT_NOTIFY_JOB_TOKEN = os.environ.get('MERCHANT_NOTIFY_JOB_TOKEN', '').strip()
MERCHANT_NOTIFY_MINIPROGRAM_STATE = os.environ.get('MERCHANT_NOTIFY_MINIPROGRAM_STATE', '').strip()
MERCHANT_NOTIFY_FETCH_TIMEOUT_SECONDS = parse_env_float('MERCHANT_NOTIFY_FETCH_TIMEOUT_SECONDS', '8')
MERCHANT_NOTIFY_POLL_INTERVAL_SECONDS = parse_env_float('MERCHANT_NOTIFY_POLL_INTERVAL_SECONDS', '30')
MERCHANT_NOTIFY_POLL_TIMEOUT_SECONDS = parse_env_float('MERCHANT_NOTIFY_POLL_TIMEOUT_SECONDS', '900')
MERCHANT_NOTIFY_TRIGGER_GUARD_SECONDS = parse_env_int('MERCHANT_NOTIFY_TRIGGER_GUARD_SECONDS', '1800')
MERCHANT_NOTICE_DAILY_REWARDED_STEP = parse_env_int('MERCHANT_NOTICE_DAILY_REWARDED_STEP', '30')
MERCHANT_NOTICE_CACHE_TTL_SECONDS = parse_env_int('MERCHANT_NOTICE_CACHE_TTL_SECONDS', '30')
MERCHANT_NOTICE_DISPATCH_WORKER_ENABLED = parse_env_int('MERCHANT_NOTICE_DISPATCH_WORKER_ENABLED', '1')
MERCHANT_NOTICE_DISPATCH_WORKER_IDLE_SECONDS = parse_env_float('MERCHANT_NOTICE_DISPATCH_WORKER_IDLE_SECONDS', '2')
MERCHANT_NOTICE_DISPATCH_WORKER_STALE_SECONDS = parse_env_int('MERCHANT_NOTICE_DISPATCH_WORKER_STALE_SECONDS', '600')
