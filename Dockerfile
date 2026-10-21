FROM python:3.12.11-slim-bookworm AS build

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV SET_CONTAINER_TIMEZONE=true
ENV CONTAINER_TIMEZONE=Asia/Shanghai
ENV TZ=Asia/Shanghai 


ARG TARGETARCH
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
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && dpkg-reconfigure --frontend noninteractive tzdata \
    && if [ "$TARGETARCH" = "amd64" ]; then \
           CHROME_VER="120.0.6099.109" \
           && wget -q "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VER}/linux64/chrome-linux64.zip" -O /tmp/chrome.zip \
           && wget -q "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VER}/linux64/chromedriver-linux64.zip" -O /tmp/chromedriver.zip \
           && unzip -q /tmp/chrome.zip -d /opt/ \
           && unzip -q /tmp/chromedriver.zip -d /opt/ \
           && rm -f /usr/bin/chromium /usr/bin/chromedriver \
           && ln -sf /opt/chrome-linux64/chrome /usr/bin/chromium \
           && ln -sf /opt/chromedriver-linux64/chromedriver /usr/bin/chromedriver \
           && rm -f /tmp/chrome.zip /tmp/chromedriver.zip; \
       fi \
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
