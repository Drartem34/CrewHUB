# Для розробника

## Принципи («Nokia 3310»)

- **Мінімум залежностей:** Flask, Waitress, psutil, rich. Без ORM, без фреймворків на фронтенді, без збірки JS/CSS.
  Нові можливості — стандартною бібліотекою Python (див. `lan.py`: ARP, NetBIOS, mDNS без сторонніх пакетів).
- **Один процес** (Waitress з потоками): фонові потоки не дублюються. Другий запуск на тому ж порту не стартує.
- **SQLite у режимі WAL**, усі записи серіалізує глобальний `_lock` (RLock). Читання паралельні.
- **Нічого з інтернету:** іконки — SVG-спрайт у `_ui.html`, шрифти системні, CSP `default-src 'self'`.
- **Будь-яке сміття на вході не дає 500:** дати, числа, налаштування перевіряються (`parse_*`, `clean_setting`),
  фаз-тест у `test_hardening.py`.
- **Фонові потоки не падають:** `run_worker()` ловить винятки, пише в журнал і показує адміну банер.
- Коментарі й тексти — українською; коментар пояснює «чому», а не «що».

Детально по кожному файлу — [карта коду](11-code-map.md): кожна функція, маршрут, метод Java, вкладка шаблону
з номером рядка й описом. Оновити після змін у коді: `python docs/make_code_map.py`.

## Структура

```
test_app/
├── crewhub_core.py    ядро: шляхи й папка даних, схема БД і міграції, налаштування, логіка змін,
│                      контроль Wi-Fi (sweep), зарплата, дей-оффи, бекапи, усі маршрути Flask
├── settings_file.py   settings.ini ⇄ база (правила, точки, мережі, порт)
├── profiles.py        особові папки people/ ⇄ база; CLI імпорту папок
├── lan.py             «Пристрої в мережі»: ARP, NetBIOS, mDNS, DNS роутера, User-Agent, OUI; present() — хто щойно відповів на ARP
├── start.py           запуск: перенесення даних, перевірка бази, фонові потоки, тунель, Waitress
├── tui.py             панель сервера в терміналі (rich)
├── recover_admin.py   скидання пароля адміна з консолі сервера
├── install_service.sh служба systemd;  start.sh / start.bat — запуск з автоперезапуском
├── templates/         Jinja2: base.html (каркас, меню), _ui.html (іконки-спрайт, макроси таблиць),
│                      me.html (кабінет працівника), admin.html (керування), employee.html (картка людини),
│                      login/register/password/logout/error.html
├── static/            style.css, motion.css (анімації), app.js (тема, діалоги, широкі таблиці, CSRF),
│                      motion.js (рух іконок, супер-анімації), nav.js (переходи без перезавантаження),
│                      clock.js (екран «Зміна»), live.js («Огляд»), qrcode.js, manifest.json, icon.svg
└── test_crewhub.py, test_hardening.py, test_features.py   автотести
android/               застосунок (див. 07-android.md)
docs/                  ця документація
```

## Запуск для розробки

```bash
cd test_app
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
CREWHUB_DATA=/tmp/crewhub-dev CREWHUB_DEMO=1 CREWHUB_PLAIN=1 python start.py
```

`CREWHUB_DEMO=1` — тестові акаунти (`0500000001/admin123`, `0500000002/moder123`, `0671234567/1234`,
`0937654321/1234`) й історія змін за два тижні. Окрема `CREWHUB_DATA` — щоб не чіпати робочі дані.

## Тести

```bash
cd test_app
python -m unittest test_crewhub test_hardening test_features      # ~130 тестів, ~4 хв
python -m unittest test_features.LanTest -v                       # окремий клас
```

- `test_crewhub.py` — основа: `CrewHubBase` (чиста база й папки на кожен тест, `login()`, `clock()`, `sql()`,
  `sync()`) і функціональні тести змін, запитів, дей-оффів, папок, бекапів.
- `test_hardening.py` — тест на кожну закриту дірку й баг (IP, ролі, ліміти, CSRF, CSP, CSV, часовий пояс…)
  і фаз-тест: сміття в кожну форму не дає 500.
- `test_features.py` — версія 17: папка даних і перенесення, `settings.ini`, пристрої в мережі (без реального
  сканування — лише `127.0.0.0/30`), сторінки без зовнішніх ресурсів, супер-анімації кожної іконки.

Тести не потребують мережі й не чіпають `~/CrewHub-data` (оточення задає `test_crewhub.py`).

## База даних

| Таблиця | Що зберігає |
|---|---|
| `settings` | правила (ключ → значення рядком) |
| `meta` | службове: `server_id`, `secret_key`, `alive_at`, `backup_mirror`, позначки міграцій, `dayoff_open_until` |
| `locations` | точки: назва, адреса, `is_active`, `wifi_ssid`, `wifi_password` |
| `networks` | робочі мережі точок (`cidr`, підпис) |
| `users` | люди: логін, хеш пароля, ПІБ, роль, посада, відділ, точка, телефон, контакти, ставка, статус, `folder`, `must_change_password` |
| `devices` | телефони людей (`device_key`: `app-…` застосунок, `web-…` браузер), перший і останній IP, `mac`, `fingerprint` (відбиток зі сторінки) |
| `remember_tokens` | «запам'ятати мене»: selector + хеш validator, термін, пристрій, `mac`, `fingerprint` |
| `shifts` | зміни: початок, кінець, статус (`active`/`paused`/`closed`), причина закриття, ставка на момент старту, останній пінг і IP, пристрій, `is_edited`, `bg_ok`, `mac` (телефон зміни в мережі точки), `battery`, `charging`, `battery_at`, `power_lost_at` (коли розрядився) |
| `breaks` | перерви: `lunch`/`rest`/`wifi_loss`, оплачувана, % ставки, причина (`left`/`silence`), `notified` |
| `schedule` | графік: заплановані зміни |
| `requests` | запити: тип, статус, дати, пропонований час, обмін, коментарі, `grant_until` |
| `adjustments` | бонуси (+) і штрафи (−) з датою і причиною |
| `conn_log` | журнал підключень (180 днів) |
| `audit` | журнал редагувань: хто, що, «було → стало» (JSON) |
| `notifications` | сповіщення людям |
| `avatars` | фото (blob) |
| `dirty_users` | черга людей, чиї папки треба переписати (заповнюють тригери) |
| `lan_devices` | «Пристрої в мережі»: MAC, IP, ім'я, модель, виробник, працівник, коли бачили |
| `outages` | коли сервер не працював (зникло світло тощо): `started_at` — останній `alive_at`, `ended_at` — запуск; див. `restore_after_outage` |

Міграції — у `init_db()`: нові колонки додаються `alter table`, одноразові перетворення позначаються в `meta`.
Унікальні індекси: одна відкрита зміна на людину (`shifts_one_open`), один акаунт на телефон (`users_phone_uq`).

## Фонові потоки

| Потік | Як часто | Що робить |
|---|---|---|
| `sweep` (Контроль Wi-Fi) | 15 с | паузи при тиші, сповіщення, автозакриття, ліміт тривалості, облік простою сервера |
| `people` (Особові папки) | 3 с | папки ⇄ база, відновлення зниклих папок |
| `config` (settings.ini) | 10 с | файл ⇄ база |
| `lan` (Пристрої в мережі) | 5 хв | сканування мережі (якщо `lan_scan = 1`) |
| `presence` (Телефони в Wi-Fi) | 30 с | `presence_step`: змінам, що мовчать понад 40 с, — пінг від імені телефона, якщо його MAC щойно відповів у мережі точки; людям у робочий час без зміни (`presence_starters`) — автостарт, якщо відповів MAC їхнього телефона (`lan_presence = 1`) |
| `backup` (Автобекап) | 1 год | копія, якщо сьогоднішньої немає; обслуговування бази |

Стан потоків — `WORKERS`, перевірка — `workers_problem()`: `/health` → 503 і банер для адміна, якщо потік
падає або мовчить довше за 4 своїх інтервали (мінімум 3 хв).

## Маршрути

Сторінки (HTML): `/login`, `/register`, `/password` (обов'язкова зміна), `/logout` (GET — сторінка,
POST — вихід), `/` (перенаправлення за роллю), `/me?tab=clock|timesheet|schedule|requests|team|profile|app|dayoffs`,
`/admin?tab=live|employees|requests|schedule|zones|logs|audit|payroll|settings`, `/admin/employee/<id>`.

API застосунку й сторінки «Зміна»: `POST /api/clock/<action>` з JSON, де action:
`check` (стан і автостарт), `auto` (фоновий пінг застосунку), `heartbeat`, `start`, `stop`, `break_start`
(`kind`: `lunch`/`rest`), `break_end`, `left` (вийшов з Wi-Fi, `away_since`). Відповідь — стан зміни
(`on_work_network`, `shift`, `why`, `next_check`, …). `GET /api/live` — дані «Огляду».
`GET /api/hello` — `server_id` і назва компанії (без входу); з `?n=<hex>&s=<selector>` ще й `proof` — доказ, що сервер знає токен «запам'ятати мене» (застосунок шукає сервер на новому IP, див. [07-android.md](07-android.md#доказ-сервера)). `GET /health` — стан сервера (без входу).

Дії (POST, форми з CSRF-токеном): `/me/request`, `/me/request/<id>/<cancel|accept|decline>`, `/me/dayoff/<id>`,
`/me/profile`, `/me/password`, `/photo/<uid>`, `/notifications/read`; тімлід — `/team/dayoff`, `/team/swap`,
`/dayoffs/campaign`, `/schedule/toggle`, `/schedule/dayoff`, `/schedule/fill`; керування — `/admin/employee/new`,
`/admin/employee/<id>` (+ `/role`, `/reset_password`, `/delete`, `/logout_all`, `/device/<did>/delete`),
`/admin/registration/<id>/<approve|reject>`, `/admin/rates`, `/admin/shift/save`, `/admin/adjustment`
(+ `/<id>/delete`), `/admin/request/<id>/<approve|reject>`, `/admin/absence`, `/admin/schedule`, `/admin/zones`,
`/admin/lan/scan`, `/admin/settings`, `/admin/backup_mirror`.

Файли: `/admin/payroll.csv?month=YYYY-MM`, `/admin/employee/<id>/timesheet.csv`, `/admin/employee/<id>/folder.zip`,
`/admin/backup.zip`, `/avatar/<id>`, `/app` (APK).

Доступ — декоратор `login_required(staff=…, admin=…)`; «свої записи» — `own_record_blocked()`.

## Як зробити типові зміни

**Нове правило:**
1. `DEFAULT_SETTINGS` — значення за замовчуванням (рядок) і коментар;
2. `SETTING_RULES` — тип (`int`, `float`, `bool`, `time`, `choice`, `text`), межі, назва;
3. якщо це галочка — додати ключ у `CHECKBOX_SETTINGS`;
4. поле у вкладці «Правила» (`admin.html`);
5. читати через `setting()` / `setting_int()` / `setting_float()` — значення вже перевірене;
6. `settings.ini` і документація ([04-settings.md](04-settings.md)) підхоплять ключ самі (підказка — з `SETTING_RULES`),
   опис у документації допишіть.

**Нова іконка:** `<symbol id="i-назва">` у `_ui.html`, частини з класами `p-*`, і супер-анімація
`.icon.sup.anim[data-anim="s-назва"] …` у `motion.css` (тест перевіряє, що вона є). У шаблоні — `{{ icon('назва') }}`.

**Нова вкладка:** гілка `{% elif tab == '…' %}` у `me.html`/`admin.html`, дані — у `me()`/`admin()`, пункт меню
в `base.html`.

**Версії статики:** після зміни CSS/JS збільште `?v=` у `base.html` (телефони кешують статику на добу).

## Відомі обмеження

- Масштаб — сотні людей на одному сервері (стрес-тест: 200 одночасних стартів зміни за ~8 с без помилок).
  Для тисяч потрібні Postgres і кілька процесів.
- Сервер бачить IP лише в одній мережі з телефонами або через тунель з `X-Forwarded-For`.
- HTTPS у локальній мережі не налаштовано (див. [08-security.md](08-security.md#чого-немає-і-що-з-цим-робити)).
