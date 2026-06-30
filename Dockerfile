FROM python:3.12-slim

# ffmpeg — извлечение/ремукс аудио; curl/unzip — для установки deno
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates tzdata curl unzip \
    && rm -rf /var/lib/apt/lists/*

# deno — JS-движок для yt-dlp (нужен решателю nsig/возрастных, EJS)
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && deno --version

# rclone — для опционального зеркалирования в S3/удалённое хранилище
RUN curl -fsSL https://rclone.org/install.sh | bash || true

# yt-dlp[default] тянет yt-dlp-ejs (решатель JS-челленджей YouTube, нужен с deno);
# mutagen — встраивание обложек и проставление тегов альбома.
# yt-dlp обновляется при каждом старте контейнера (entrypoint).
RUN pip install --no-cache-dir -U "yt-dlp[default]" mutagen

COPY entrypoint.sh /entrypoint.sh
COPY sync.py /sync.py
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
