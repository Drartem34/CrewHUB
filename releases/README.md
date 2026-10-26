# Збірки застосунку (APK) усіх версій

Код кожної версії — коміти в git (посилання «код» і «zip»), хронологія — [docs/VERSIONS.md](../docs/VERSIONS.md). Тут — зібрані APK, яких
у git інакше не було б (`test_app/static/*.apk` — у `.gitignore`). Усі підписані одним ключем, тож нова версія
встановлюється поверх старої без видалення. Сам ключ (`android/crewhub.keystore`, `android/keystore.pass`) сюди
не потрапляє — зберігайте його окремо.

## Застосунок CrewHub (`android/`)

| Файл | Версія | Сервер | Звідки |
|---|---|---|---|
| [CrewHub-2.0.apk](android/CrewHub-2.0.apk) | 2.0 (versionCode 12) | v17.0 | з архіву `CrewHub_v17_full.zip`, як роздавався |
| [CrewHub-2.1.apk](android/CrewHub-2.1.apk) | 2.1 (13) | v17.1 | зібрано з коміту `a45c67e` тим самим ключем (окремої збірки 2.1 не збереглось) |
| [CrewHub-2.2.apk](android/CrewHub-2.2.apk) | 2.2 (14) | v17.2 | зібрано з коміту `5d17b0a` тим самим ключем (окремої збірки 2.2 не збереглось) |
| [CrewHub-2.3.apk](android/CrewHub-2.3.apk) | 2.3 (15) | v17.3 – v17.9 | з архівів `CrewHub_v17_5_full.zip` … `CrewHub_v17_9_full.zip` (той самий файл) |

Версія 1.9 (сервер v15–v16) підписувалась старим тестовим ключем, який з версії 16 замінено (див. коміт
`022c18b`); її збірки не збереглось, а нова іншим ключем не встановилась би поверх. Код 1.9 — у версіях v15 і v16 (див. [docs/VERSIONS.md](../docs/VERSIONS.md)).

Найновіший застосунок сервер роздає сам: сторінка входу → «Завантажити застосунок для Android».

## Демо-застосунок без сервера (`android/demo/`)

Знімки сторінок відповідної версії сервера з тестовими даними. Однаковий `versionName` («17-demo»), різниться вмістом.

| Файл | Сервер | Звідки |
|---|---|---|
| [CrewHub_Demo-17.0.apk](demo/CrewHub_Demo-17.0.apk) | v17.0 | з архіву `CrewHub_v17_full.zip` |
| [CrewHub_Demo-17.5.apk](demo/CrewHub_Demo-17.5.apk) | v17.5 | з архіву `CrewHub_v17_5_full.zip` |
| [CrewHub_Demo-17.6.apk](demo/CrewHub_Demo-17.6.apk) | v17.6 | з архіву `CrewHub_v17_6_full.zip` |
| [CrewHub_Demo-17.7.apk](demo/CrewHub_Demo-17.7.apk) | v17.7 | з архіву `CrewHub_v17_7_full.zip` |
| [CrewHub_Demo-17.8.apk](demo/CrewHub_Demo-17.8.apk) | v17.8 | з архіву `CrewHub_v17_8_full.zip` |
| [CrewHub_Demo-17.9.apk](demo/CrewHub_Demo-17.9.apk) | v17.9 | з архіву `CrewHub_v17_9_full.zip` |
