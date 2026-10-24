# Сервер CrewHub

Код сервера: Flask + SQLite, запуск одним файлом.

```bash
pip install -r requirements.txt
python start.py          # у Windows: двічі клацнути start.bat
```

Перший вхід: `050 000 00 01` / `admin123` (система одразу попросить свій пароль).
Дані (база, особові папки, копії, `settings.ini`) лежать окремо від цієї папки — у `~/CrewHub-data`.

Повна документація — у папці [`docs/`](../docs/README.md):
[встановлення](../docs/01-install.md) ·
[адміністратор](../docs/02-admin-guide.md) ·
[працівник](../docs/03-employee-guide.md) ·
[правила і settings.ini](../docs/04-settings.md) ·
[зміни, Wi-Fi і зарплата](../docs/05-shifts-and-pay.md) ·
[дані й бекапи](../docs/06-data-and-backups.md) ·
[безпека](../docs/08-security.md) ·
[для розробника](../docs/09-developer.md) ·
[FAQ](../docs/10-faq.md) ·
[історія версій](../docs/CHANGELOG.md).

Тести: `python -m unittest test_crewhub test_hardening test_features`.
