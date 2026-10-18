#!/usr/bin/env bash
# Установка / обновление PrintBot на Ubuntu Server.
# Запуск: sudo bash install.sh
set -euo pipefail

APP_DIR=/opt/printbot
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "$EUID" -ne 0 ]; then
  echo "Запусти через sudo: sudo bash install.sh"
  exit 1
fi

echo "==> Устанавливаю пакеты (CUPS, драйверы HP, LibreOffice для Word, шрифты, Python)"
apt-get update -q
apt-get install -y -q cups hplip python3-venv usb-modeswitch
apt-get install -y -q --no-install-recommends libreoffice-writer \
  fonts-dejavu fonts-liberation fonts-crosextra-carlito fonts-crosextra-caladea
systemctl enable --now cups

echo "==> Правило для HP M1132: переключать из режима «Smart Install» в режим принтера"
install -m 644 "$SRC_DIR/printer/hp-m1132.modeswitch" /etc/usb_modeswitch.d/03f0:042a
install -m 644 "$SRC_DIR/printer/41-hp-m1132-modeswitch.rules" /etc/udev/rules.d/
udevadm control --reload

echo "==> Создаю пользователя printbot и папку $APP_DIR"
id printbot &>/dev/null || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin printbot
mkdir -p "$APP_DIR/data"
cp "$SRC_DIR/bot.py" "$SRC_DIR/requirements.txt" "$APP_DIR/"
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$SRC_DIR/.env.example" "$APP_DIR/.env"
fi

echo "==> Ставлю зависимости Python"
[ -d "$APP_DIR/venv" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

chown -R printbot:printbot "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo "==> Регистрирую службу printbot"
cp "$SRC_DIR/printbot.service" /etc/systemd/system/printbot.service
systemctl daemon-reload
systemctl enable printbot

if grep -qE '^BOT_TOKEN=.+' "$APP_DIR/.env"; then
  systemctl restart printbot
  echo
  echo "Готово, бот запущен. Логи: journalctl -u printbot -f"
else
  echo
  echo "Почти готово. Впиши токен бота:"
  echo "  sudo nano $APP_DIR/.env"
  echo "и запусти:"
  echo "  sudo systemctl restart printbot"
fi
