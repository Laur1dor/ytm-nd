#!/usr/bin/env bash
# Запускает периодическую синхронизацию плейлиста YouTube Music в библиотеку Navidrome.
set -uo pipefail

INTERVAL="${INTERVAL_SECONDS:-14400}"   # по умолчанию каждые 4 часа

upgrade_ytdlp() {
  echo "[ytm-sync] обновляю yt-dlp..."
  if pip install --no-cache-dir -U "yt-dlp[default]" >/tmp/pip.log 2>&1; then
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
  python3 /sync.py || echo "[ytm-sync] sync завершился с ошибкой (продолжаю по расписанию)"

  echo "[ytm-sync] $(date -Is) сплю ${INTERVAL}s до следующей проверки"
  sleep "${INTERVAL}"
done
