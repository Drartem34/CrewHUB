#!/usr/bin/env bash
# Перевірка «що працювало — не зламалось»: тести старих версій (з історії git) запускаються на ПОТОЧНОМУ коді.
# Кожне падіння — кандидат у поломку; розбирати вручну (частина падінь — свідомі виправлення, див. docs/CHANGELOG.md).
#   ./check_old_versions.sh                 — версії архівів CrewHub Full (v16, v17, v17.1–17.4)
#   ./check_old_versions.sh 0ebd155 c467fcb — вибрані коміти
# Тести оригіналу (3fd8651) підставляли робочий IP заголовком X-Forwarded-For; з версії 16 він довіряється лише тунелю,
# тому для них вмикається CREWHUB_TRUST_PROXY=1 (як для тунелю). Кожен запуск — з окремою чистою папкою даних.
set -u
cd "$(dirname "$0")/.."
VERSIONS=${*:-"3fd8651 5ae15da 0ebd155 248ae55 da5b3bc 6d8bede c467fcb"}
PY=${PYTHON:-python3}
for V in $VERSIONS; do
  D=$(mktemp -d); cp -r test_app "$D/test_app"; rm -f "$D"/test_app/test_*.py; rm -rf "$D/test_app/__pycache__"
  mods=""
  for f in $(git ls-tree --name-only "$V" test_app/ | grep "test_app/test_.*\.py$"); do
    git show "$V:$f" > "$D/$f"; mods="$mods $(basename "$f" .py)"
  done
  echo "== $V $(git log -1 --format=%s "$V" | cut -c1-70)"
  (cd "$D/test_app" && CREWHUB_DATA="$D/data" CREWHUB_TRUST_PROXY=1 $PY -W ignore -m unittest $mods 2>&1) \
    | grep -E "^(FAIL|ERROR):|^Ran|^OK|^FAILED"
  rm -rf "$D"
done
