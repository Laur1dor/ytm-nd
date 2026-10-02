#!/usr/bin/env bash
# Запускает периодическую синхронизацию плейлиста YouTube Music в библиотеку Navidrome.
set -uo pipefail

INTERVAL="${INTERVAL_SECONDS:-3600}"   # полная сверка раз в час
PROBE_INTERVAL="${LIKES_PROBE_SECONDS:-60}"

upgrade_ytdlp() {
  echo "[ytm-sync] обновляю yt-dlp..."
  if pip install --no-cache-dir --timeout 10 --retries 1 -U \
      "yt-dlp[default]" bgutil-ytdlp-pot-provider >/tmp/pip.log 2>&1; then
    tail -1 /tmp/pip.log
  else
    echo "[ytm-sync] обновление yt-dlp не удалось, использую установленную версию"
  fi
}

# Ждём появления сетевой шары (после ребута хоста она может подняться позже).
# Так контейнер сам подхватит NAS без ручного ребута (нужен также rslave-проброс).
wait_for_share() {
  [ "${REQUIRE_SHARE:-true}" = "true" ] || return 0
  local root="${MUSIC_ROOT:-/music}" tries=0 fstype
  while true; do
    fstype=$(stat -f -c %T "$root" 2>/dev/null || echo "?")
    if [ -f "$root/.ytm_share_ok" ] && echo "$fstype" | grep -qiE 'smb|cifs|nfs'; then
      [ $tries -gt 0 ] && echo "[ytm-sync] шара появилась (fstype=$fstype)"
      return 0
    fi
    tries=$((tries + 1))
    echo "[ytm-sync] шара не готова (fstype=$fstype), жду 30с (попытка $tries)"
    sleep 30
  done
}

upgrade_ytdlp
echo "[ytm-sync] версия yt-dlp: $(yt-dlp --version 2>/dev/null || echo '?')"
last_upgrade=$(date +%s)

while true; do
  now=$(date +%s)
  # обновлять yt-dlp не чаще раза в сутки
  if (( now - last_upgrade > 86400 )); then
    upgrade_ytdlp
    last_upgrade=$now
  fi

  wait_for_share
  echo "[ytm-sync] $(date -Is) === запуск синхронизации ==="
  if [ "${LIKES_TO_MAIN:-false}" = "true" ]; then
    python3 /likes_to_main.py || echo "[ytm-sync] перенос лайков не удался; продолжаю синхронизацию Main"
  fi
  python3 /sync.py || echo "[ytm-sync] sync завершился с ошибкой (продолжаю по расписанию)"

  next_full=$(($(date +%s) + INTERVAL))
  echo "[ytm-sync] $(date -Is) полная сверка через ${INTERVAL}s; проверка лайков каждые ${PROBE_INTERVAL}s"
  while [ "$(date +%s)" -lt "$next_full" ]; do
    sleep "$PROBE_INTERVAL"
    [ "$(date +%s)" -ge "$next_full" ] && break
    [ "${LIKES_TO_MAIN:-false}" = "true" ] || continue

    if python3 /likes_to_main.py --probe; then
      continue
    else
      probe_rc=$?
    fi
    if [ "$probe_rc" -ne 10 ]; then
      echo "[ytm-sync] проверка лайков не удалась (код $probe_rc)"
      continue
    fi

    if python3 /likes_to_main.py --signal-added; then
      continue
    else
      transfer_rc=$?
    fi
    if [ "$transfer_rc" -eq 10 ]; then
      wait_for_share
      FAST_SYNC=true python3 /sync.py || echo "[ytm-sync] загрузка нового трека завершилась с ошибкой; повторю при полной сверке"
    else
      echo "[ytm-sync] перенос лайков не удался (код $transfer_rc); повторю на следующей проверке"
    fi
  done
done
