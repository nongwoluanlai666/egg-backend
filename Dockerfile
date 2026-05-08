FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i \
            -e 's|http://deb.debian.org/debian|https://mirrors.tencent.com/debian|g' \
            -e 's|https://deb.debian.org/debian|https://mirrors.tencent.com/debian|g' \
            -e 's|http://security.debian.org/debian-security|https://mirrors.tencent.com/debian-security|g' \
            -e 's|https://security.debian.org/debian-security|https://mirrors.tencent.com/debian-security|g' \
            /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i \
            -e 's|http://deb.debian.org/debian|https://mirrors.tencent.com/debian|g' \
            -e 's|https://deb.debian.org/debian|https://mirrors.tencent.com/debian|g' \
            -e 's|http://security.debian.org/debian-security|https://mirrors.tencent.com/debian-security|g' \
            -e 's|https://security.debian.org/debian-security|https://mirrors.tencent.com/debian-security|g' \
            /etc/apt/sources.list; \
    fi; \
    apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates cron curl libgomp1 tzdata \
    && ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime \
    && echo "${TZ}" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt

RUN pip install --upgrade pip \
    && pip install -r /app/requirements.txt

COPY . /app

RUN chmod +x /app/start.sh /app/task.sh \
    && crontab /app/task.txt

EXPOSE 80

ENTRYPOINT ["sh", "/app/start.sh"]
