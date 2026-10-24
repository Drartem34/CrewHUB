#!/usr/bin/env bash
# Збирає CrewHub_Demo.apk: інтерфейс CrewHub без сервера (знімки сторінок з тестовими даними).
# Потрібно те саме, що для основного застосунку (див. ../build.sh), плюс Python з Flask (як для сервера).
#   ANDROID_JAR=шлях/до/android.jar ./build.sh      → build/CrewHub_Demo.apk
set -euo pipefail
cd "$(dirname "$0")"
ANDROID_JAR="${ANDROID_JAR:-$HOME/android-sdk/platforms/android-34/android.jar}"
[ -f "$ANDROID_JAR" ] || { echo "Не знайдено $ANDROID_JAR — вкажіть ANDROID_JAR=шлях/до/android.jar (API 34)"; exit 1; }
PY="${PYTHON:-python3}"
OUT=build
rm -rf "$OUT" && mkdir -p "$OUT/gen" "$OUT/classes"

"$PY" build_demo.py   # → assets/www

# Ресурси (іконка, тема) — спільні з основним застосунком
aapt package -f -m -J "$OUT/gen" -M AndroidManifest.xml -S ../res -A assets -I "$ANDROID_JAR" -F "$OUT/unsigned.apk"
javac --release 8 -nowarn -encoding UTF-8 -cp "$ANDROID_JAR" -d "$OUT/classes" $(find src "$OUT/gen" -name '*.java')
dalvik-exchange --dex --min-sdk-version=23 --output="$OUT/classes.dex" "$OUT/classes"
(cd "$OUT" && aapt add -f unsigned.apk classes.dex >/dev/null)
zipalign -f -p 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"

# Підпис — тим самим ключем, що й основний застосунок (../crewhub.keystore, створює ../build.sh)
KEYSTORE="${CREWHUB_KEYSTORE:-../crewhub.keystore}"
PASS_FILE="${CREWHUB_KS_PASS_FILE:-../keystore.pass}"
[ -f "$KEYSTORE" ] || { echo "Немає ключа $KEYSTORE: спершу зберіть основний застосунок (../build.sh), він створить ключ"; exit 1; }
export KS_PASS="${CREWHUB_KS_PASS:-$( [ -f "$PASS_FILE" ] && cat "$PASS_FILE" )}"
apksigner sign --ks "$KEYSTORE" --ks-pass env:KS_PASS --key-pass env:KS_PASS --out "$OUT/CrewHub_Demo.apk" "$OUT/aligned.apk"
apksigner verify "$OUT/CrewHub_Demo.apk" && echo "Готово: $OUT/CrewHub_Demo.apk"
