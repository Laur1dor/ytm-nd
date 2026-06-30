#!/usr/bin/env bash
# Авто-восстановление NAS-шары после ребута (запускается systemd при старте LXC).
#
# Ждёт появления сетевой шары в /mnt/Music (по маркеру .ytm_share_ok) и
# перезапускает контейнеры, которым она нужна — чтобы не заходить вручную в LXC и
# не рестартить navidrome после каждого ребута хоста.
# (ytm-sync ждёт шару сам и в рестарте не нуждается.)
set -u

MARKER="/mnt/Music/.ytm_share_ok"
CONTAINERS="navidrome"
MAX=90          # 90 * 10с = до 15 минут ожидания, пока хост поднимет шару

for i in $(seq 1 "$MAX"); do
  if [ -e "$MARKER" ]; then
    echo "[nas-watch] шара доступна (попытка $i) — рестарт: $CONTAINERS"
    for c in $CONTAINERS; do
      if docker restart "$c" >/dev/null 2>&1; then
        echo "[nas-watch] перезапущен: $c"
      else
        echo "[nas-watch] не удалось перезапустить: $c"
      fi
    done
    exit 0
  fi
  sleep 10
done

echo "[nas-watch] шара не появилась за ~15 минут — выхожу" >&2
exit 1
