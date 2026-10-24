#!/usr/bin/env bash
# Linux: CrewHub як системна служба. Стартує разом з комп'ютером (навіть без входу в систему)
# і перезапускається сама, якщо впаде. Запуск: ./install_service.sh   Видалення: ./install_service.sh remove
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"
UNIT=/etc/systemd/system/crewhub.service
if [ "$1" = "remove" ]; then
  sudo systemctl disable --now crewhub 2>/dev/null || true
  sudo rm -f "$UNIT" && sudo systemctl daemon-reload
  echo "Службу CrewHub видалено"; exit 0
fi
./start.sh --deps-only
PY="$DIR/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
sudo tee "$UNIT" >/dev/null <<UNITEOF
[Unit]
Description=CrewHub (облік змін)
After=network-online.target
Wants=network-online.target

[Service]
User=$(id -un)
WorkingDirectory=$DIR
ExecStart=$PY $DIR/start.py
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1
# Захист від витоку пам'яті: якщо сервер з'їсть більше 1 ГБ, systemd його перезапустить
MemoryMax=1G
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
UNITEOF
sudo systemctl daemon-reload
sudo systemctl enable --now crewhub
sleep 3
systemctl --no-pager status crewhub | head -5
echo
echo "Готово. Сервер працює у фоні і стартуватиме з комп'ютером."
echo "Журнал: journalctl -u crewhub -f    Зупинити: sudo systemctl stop crewhub"
