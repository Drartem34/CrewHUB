#!/usr/bin/env bash
# Збирає CrewHub.apk без Android Studio і Gradle.
# Потрібно (Ubuntu/Debian): sudo apt install aapt apksigner zipalign dalvik-exchange openjdk-17-jdk + android.jar API 34 (див. нижче)
set -euo pipefail
cd "$(dirname "$0")"
# Потрібні ресурси й класи Android 14 (API 34): фонова служба, сповіщення, тип служби для Android 14+.
# Підходить android.jar з Android SDK (platforms/android-34) або android-all з Maven Central:
# https://repo1.maven.org/maven2/org/robolectric/android-all/14-robolectric-10818077/android-all-14-robolectric-10818077.jar
ANDROID_JAR="${ANDROID_JAR:-$HOME/android-sdk/platforms/android-34/android.jar}"
[ -f "$ANDROID_JAR" ] || { echo "Не знайдено $ANDROID_JAR — вкажіть ANDROID_JAR=шлях/до/android.jar (API 34)"; exit 1; }
JAVA_ANDROID_JAR="${JAVA_ANDROID_JAR:-$ANDROID_JAR}"
OUT=build
rm -rf "$OUT" && mkdir -p "$OUT/gen" "$OUT/classes"

aapt package -f -m -J "$OUT/gen" -M AndroidManifest.xml -S res -I "$ANDROID_JAR" -F "$OUT/unsigned.apk"
# libs/zxing-core: розпізнавання QR-коду підключення (Apache 2.0, https://github.com/zxing/zxing)
LIBS=$(ls libs/*.jar 2>/dev/null | tr '\n' ':')
javac --release 8 -nowarn -encoding UTF-8 -cp "$JAVA_ANDROID_JAR:$LIBS" -d "$OUT/classes" \
  $(find src "$OUT/gen" -name '*.java')
dalvik-exchange --dex --min-sdk-version=23 --output="$OUT/classes.dex" "$OUT/classes" libs/*.jar
(cd "$OUT" && aapt add -f unsigned.apk classes.dex >/dev/null)
zipalign -f -p 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"

# Ключ підпису: той самий ключ потрібен для оновлень поверх встановленого застосунку.
# Ключ і пароль — секрет: не кладіть їх у git і не пересилайте (див. .gitignore). Зробіть резервну копію обох файлів.
KEYSTORE="${CREWHUB_KEYSTORE:-crewhub.keystore}"
PASS_FILE="${CREWHUB_KS_PASS_FILE:-keystore.pass}"
if [ ! -f "$KEYSTORE" ]; then
  (umask 077; python3 -c "import secrets; print(secrets.token_urlsafe(24))" > "$PASS_FILE")
  keytool -genkeypair -keystore "$KEYSTORE" -alias crewhub -keyalg RSA -keysize 2048 -validity 10000 \
    -storepass "$(cat "$PASS_FILE")" -keypass "$(cat "$PASS_FILE")" -dname "CN=CrewHub, O=CrewHub, C=UA" >/dev/null 2>&1
  echo "Створено новий ключ підпису $KEYSTORE (пароль у $PASS_FILE). Збережіть копію обох файлів у надійному місці."
fi
export KS_PASS="${CREWHUB_KS_PASS:-$( [ -f "$PASS_FILE" ] && cat "$PASS_FILE" )}"
[ -n "$KS_PASS" ] || { echo "Немає пароля ключа: вкажіть CREWHUB_KS_PASS або файл $PASS_FILE"; exit 1; }
apksigner sign --ks "$KEYSTORE" --ks-pass env:KS_PASS --key-pass env:KS_PASS \
  --out "$OUT/CrewHub.apk" "$OUT/aligned.apk"
apksigner verify "$OUT/CrewHub.apk" && echo "Готово: $OUT/CrewHub.apk"
