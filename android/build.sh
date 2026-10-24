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

# Ключ підпису: той самий файл потрібен для оновлень поверх встановленого застосунку
if [ ! -f crewhub.keystore ]; then
  keytool -genkeypair -keystore crewhub.keystore -alias crewhub -keyalg RSA -keysize 2048 -validity 10000 \
    -storepass crewhub123 -keypass crewhub123 -dname "CN=CrewHub, O=CrewHub, C=UA" >/dev/null 2>&1
fi
apksigner sign --ks crewhub.keystore --ks-pass pass:crewhub123 --key-pass pass:crewhub123 \
  --out "$OUT/CrewHub.apk" "$OUT/aligned.apk"
apksigner verify "$OUT/CrewHub.apk" && echo "Готово: $OUT/CrewHub.apk"
