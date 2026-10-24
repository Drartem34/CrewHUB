#!/usr/bin/env bash
# Запуск сервера CrewHub на Linux / macOS
cd "$(dirname "$0")"
# Бібліотеки ставимо в окреме середовище .venv: на Arch, Fedora, Ubuntu 23+ системний pip заборонений
[ -x .venv/bin/python ] || python3 -m venv .venv 2>/dev/null || true
PY=python3; [ -x .venv/bin/python ] && PY=.venv/bin/python
# Ставимо лише коли бібліотек немає або змінився requirements.txt: звичайний запуск не потребує інтернету
REQ_HASH=$(cksum requirements.txt | cut -d' ' -f1)
MARK=.deps-installed
if [ "$(cat "$MARK" 2>/dev/null)" != "$REQ_HASH" ] || ! $PY -c "import flask, waitress" 2>/dev/null; then
  { $PY -m pip install -q -r requirements.txt 2>/dev/null \
    || $PY -m pip install -q --user -r requirements.txt 2>/dev/null \
    || $PY -m pip install -q --break-system-packages -r requirements.txt; } \
    && echo "$REQ_HASH" > "$MARK" \
    || echo "Не вдалося встановити бібліотеки. Перевірте інтернет."
fi
# Брандмауер: відкриваємо порт 5000 для телефонів у мережі (firewalld або ufw)
if systemctl is-active --quiet firewalld 2>/dev/null; then
  if ! firewall-cmd --query-port=5000/tcp >/dev/null 2>&1; then
    echo "Відкриваю порт 5000 у firewalld (може запитати пароль)..."
    sudo firewall-cmd --permanent --add-port=5000/tcp >/dev/null && sudo firewall-cmd --reload >/dev/null \
      || echo "Не вдалося. Виконайте: sudo firewall-cmd --permanent --add-port=5000/tcp && sudo firewall-cmd --reload"
  fi
elif command -v ufw >/dev/null 2>&1 && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  sudo ufw allow 5000/tcp >/dev/null
fi
[ "$1" = "--deps-only" ] && exit 0
# Якщо сервер впаде, він сам підніметься через 3 секунди. Ctrl+C зупиняє повністю.
while true; do
  $PY start.py
  code=$?
  if [ $code -eq 0 ] || [ $code -eq 130 ] || [ $code -eq 143 ]; then break; fi
  echo "Сервер зупинився з помилкою (код $code). Перезапуск через 3 с... (Ctrl+C, щоб вийти)"
  sleep 3 || break
done
