FROM python:3.12.11-slim-bookworm AS build

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV SET_CONTAINER_TIMEZONE=true
ENV CONTAINER_TIMEZONE=Asia/Shanghai
ENV TZ=Asia/Shanghai 


ARG TARGETARCH
ARG CHROME_VER=156.0.8078.47
ARG VERSION
ENV VERSION=${VERSION}
ENV PYTHON_IN_DOCKER='PYTHON_IN_DOCKER'

COPY scripts/* /app/
WORKDIR /app

RUN apt-get --allow-releaseinfo-change update \
    && apt-get install -y --no-install-recommends \
        jq fonts-noto-cjk tzdata \
        wget unzip ca-certificates \
        chromium chromium-driver \
        libgl1 libglib2.0-0 \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && dpkg-reconfigure --frontend noninteractive tzdata \
    && case "$TARGETARCH" in \
         amd64) CHROME_PLATFORM=linux64 ;; \
         arm64) CHROME_PLATFORM=linux-arm64 ;; \
         *) echo "不支持的浏览器架构: $TARGETARCH" >&2; exit 1 ;; \
       esac \
    && wget -q "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VER}/${CHROME_PLATFORM}/chrome-${CHROME_PLATFORM}.zip" -O /tmp/chrome.zip \
    && wget -q "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VER}/${CHROME_PLATFORM}/chromedriver-${CHROME_PLATFORM}.zip" -O /tmp/chromedriver.zip \
    && unzip -q /tmp/chrome.zip -d /opt/ \
    && unzip -q /tmp/chromedriver.zip -d /opt/ \
    && rm -f /usr/bin/chromium /usr/bin/chromedriver \
    && ln -sf "/opt/chrome-${CHROME_PLATFORM}/chrome" /usr/bin/chromium \
    && ln -sf "/opt/chromedriver-${CHROME_PLATFORM}/chromedriver" /usr/bin/chromedriver \
    && rm -f /tmp/chrome.zip /tmp/chromedriver.zip \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

COPY ./requirements.txt /tmp/requirements.txt

RUN mkdir /data \
    && cd /tmp \
    && python3 -m pip install --upgrade pip \
    && PIP_ROOT_USER_ACTION=ignore pip install \
    --disable-pip-version-check \
    --no-cache-dir \
    -r requirements.txt \
    && rm -rf /tmp/* \
    && pip cache purge \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /var/log/*

ENV LANG=C.UTF-8

CMD ["python3", "main.py"]
