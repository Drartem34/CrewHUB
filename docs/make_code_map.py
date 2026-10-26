"""Генерує docs/11-code-map.md — карту коду: кожен файл, кожна функція / маршрут / метод / вкладка з номером рядка
і першим рядком опису (докстрінг або коментар над нею). Запуск з кореня репозиторію:

    python docs/make_code_map.py

Номери рядків — на момент запуску: після змін у коді перезапустіть, щоб карта була точною."""
import ast
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "11-code-map.md")

# Що робить кожен файл (коротко); подробиці — у списку функцій під ним
FILES = {
    "test_app/crewhub_core.py": "Усе ядро сервера: схема й міграції бази, налаштування, логіка змін і Wi-Fi, пристрої, зарплата, "
                                "графік, дей-оффи, бекапи, фонові потоки, усі веб-маршрути Flask і фільтри шаблонів.",
    "test_app/start.py": "Запуск сервера одним файлом: перенесення даних, перевірка бази, settings.ini, фонові потоки, тунель, Waitress.",
    "test_app/profiles.py": "Особові папки працівників (people/<Ім'я>/): запис з бази, ручні правки profile.json, відновлення, імпорт.",
    "test_app/settings_file.py": "settings.ini у папці даних ↔ «Правила» й точки в базі: запис, читання, трьохстороннє злиття.",
    "test_app/lan.py": "Пристрої в мережі сервера: ARP, NetBIOS, mDNS, DNS роутера, виробник, модель; present() — хто щойно відповів.",
    "test_app/tui.py": "Жива панель сервера в терміналі (бібліотека rich).",
    "test_app/recover_admin.py": "Скидання пароля адміна з самого сервера (термінал).",
    "test_app/test_crewhub.py": "Основні автотести: вхід, зміни, Wi-Fi, гроші, графік, папки, бекапи.",
    "test_app/test_hardening.py": "Тест на кожну виправлену дірку й баг версії 16 і фаз-тест форм.",
    "test_app/test_features.py": "Тести функцій версії 17+: settings.ini, пристрої, MAC, заряд, світло, плашка дей-оффу.",
    "test_app/test_scenarios.py": "Сценарії з життя: кілька телефонів в одній Wi-Fi, робочий день, світло, усі сторінки для всіх ролей.",
    "test_app/static/app.js": "Спільна поведінка сторінок: тема, тости, діалоги, CSRF, гортання таблиць, відбиток пристрою.",
    "test_app/static/clock.js": "Екран «Зміна»: кнопки, кругова шкала, лічильник, фонові пінги (з відбитком і зарядом).",
    "test_app/static/live.js": "«Огляд» керівника: список «хто де» оновлюється кожні 10 с.",
    "test_app/static/nav.js": "Переходи між сторінками без перезавантаження.",
    "test_app/static/motion.js": "Анімації: натискання, поява, супер-анімації іконок.",
    "test_app/static/qrcode.js": "Генерація QR-коду підключення (бібліотека qrcode-generator, MIT).",
    "test_app/templates/base.html": "Каркас сторінки: меню, шапка, тема, підключення скриптів.",
    "test_app/templates/_ui.html": "Макроси: іконки (SVG-спрайт), аватари, таблиця місяця, плашки станів.",
    "test_app/templates/me.html": "Кабінет працівника: зміна, табель, графік, запити, колеги, дей-оффи (тімлід), профіль, налаштування.",
    "test_app/templates/admin.html": "Пульт керування: огляд, працівники, запити, графік, точки й Wi-Fi, підключення, журнал, зарплата, правила.",
    "test_app/templates/employee.html": "Картка працівника для керівника.",
    "test_app/templates/login.html": "Сторінка входу.",
    "test_app/templates/register.html": "Самостійна реєстрація.",
    "test_app/templates/password.html": "Зміна пароля.",
    "test_app/templates/error.html": "Сторінка помилки.",
    "android/src/ua/crewhub/app/MainActivity.java": "Головне вікно застосунку (WebView): підключення, QR, перевірка сервера, вхід після зміни IP.",
    "android/src/ua/crewhub/app/WatchService.java": "Фонова служба: Wi-Fi, пінги, вихід з Wi-Fi, сповіщення, пошук сервера, заряд.",
    "android/src/ua/crewhub/app/ScanActivity.java": "Сканер QR (ZXing).",
    "android/src/ua/crewhub/app/BootReceiver.java": "Запуск служби після перезавантаження телефона.",
    "android/demo/build_demo.py": "Збирає знімки сторінок сервера для демо-застосунку.",
    "android/demo/demo.js": "Підміна запитів у демо-застосунку.",
    "android/demo/src/ua/crewhub/demo/DemoActivity.java": "Демо-застосунок: віддає знімки сторінок з APK.",
    "android/build.sh": "Збірка CrewHub.apk без Android Studio.",
    "android/demo/build.sh": "Збірка CrewHub_Demo.apk.",
}


def first_line(text):
    text = (text or "").strip()
    return re.sub(r"\s+", " ", text.split("\n\n")[0]).strip() if text else ""


def comment_above(lines, i, marks=("#", "//", "*", "/*")):
    """Коментар безпосередньо над рядком i (0-базовий): кілька рядків # / // / javadoc."""
    out = []
    j = i - 1
    while j >= 0:
        t = lines[j].strip()
        if t.startswith("@") and not t.startswith("@param"):  # декоратор / анотація
            j -= 1
            continue
        if t.startswith(marks) and not t.startswith(("*/",)) or t.startswith("/**"):
            out.insert(0, re.sub(r"^(#|//|/\*\*|/\*|\*)\s?", "", t).rstrip("*/ ").strip())
            j -= 1
            continue
        if t == "*/":
            j -= 1
            continue
        break
    return " ".join(x for x in out if x)


def comment_inside(lines, node):
    """Перший коментар у тілі функції (якщо немає докстрінга й коментаря над нею)."""
    for i in range(node.lineno, min(getattr(node, "end_lineno", node.lineno), node.lineno + 25)):
        t = lines[i].strip()
        if t.startswith("#"):
            return t.lstrip("# ").strip()
        if "  # " in lines[i]:
            return lines[i].split("  # ", 1)[1].strip()
    return ""


def python_items(path):
    src = open(path, encoding="utf-8").read()
    lines = src.splitlines()
    tree = ast.parse(src)
    items = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            routes = []
            for d in node.decorator_list:
                if isinstance(d, ast.Call) and getattr(d.func, "attr", "") in ("route", "get", "post") and d.args \
                        and isinstance(d.args[0], ast.Constant):
                    meth = d.func.attr.upper() if d.func.attr != "route" else "/".join(
                        k.value.elts[i].value for k in d.keywords if k.arg == "methods" for i in range(len(k.value.elts))) or "GET"
                    routes.append(f"{meth} {d.args[0].value}")
                elif isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "template_filter" and d.args:
                    routes.append(f"фільтр |{d.args[0].value}")
            doc = first_line(ast.get_docstring(node)) or comment_above(lines, node.lineno - 1 - len(node.decorator_list)) \
                or comment_inside(lines, node)
            kind = "клас" if isinstance(node, ast.ClassDef) else "функція"
            if not doc and node.name.startswith("test_"):
                doc = "тест: " + node.name[5:].replace("_", " ")
            items.append((node.lineno, node.name, kind, ", ".join(routes), doc))
            if isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        d = first_line(ast.get_docstring(sub)) or comment_above(lines, sub.lineno - 1) or comment_inside(lines, sub)
                        if not d and sub.name.startswith("test_"):
                            d = "тест: " + sub.name[5:].replace("_", " ")
                        items.append((sub.lineno, f"{node.name}.{sub.name}", "метод", "", d))
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id.isupper() and node.end_lineno - node.lineno < 400:
            tail = lines[node.lineno - 1].split("  # ", 1)[1].strip() if "  # " in lines[node.lineno - 1] else ""
            items.append((node.lineno, node.targets[0].id, "константа", "", comment_above(lines, node.lineno - 1) or tail))
    return items


def js_items(path):
    lines = open(path, encoding="utf-8").read().splitlines()
    rx = re.compile(r"^\s*(?:async\s+)?function\s+([\w$]+)|^\s*(?:const|let|var)\s+([\w$]+)\s*=\s*(?:async\s*)?(?:function|\([^)]*\)\s*=>|[\w$]+\s*=>)"
                    r"|^\s*window\.([\w$]+)\s*=|^\s*document\.addEventListener\(\"([\w:]+)\"|^\s*addEventListener\(\"([\w:]+)\"")
    items = []
    for i, l in enumerate(lines):
        m = rx.match(l)
        if m:
            name = next(g for g in m.groups() if g)
            kind = "подія" if (m.group(4) or m.group(5)) else ("глобальна" if m.group(3) else "функція")
            items.append((i + 1, name, kind, "", comment_above(lines, i) or (l.split("//", 1)[1].strip() if "//" in l else "")))
    return items


JAVA_STD = {"onCreate": "Android: екран / служба створюється — побудова інтерфейсу, підписки", "onResume": "Android: екран знову видно",
            "onPause": "Android: екран сховано", "onDestroy": "Android: екран / служба знищується — відписки, звільнення ресурсів",
            "onNewIntent": "Android: застосунок відкрили посиланням / QR, коли він уже запущений",
            "onSaveInstanceState": "Android: зберегти стан перед поворотом екрана чи вбивством процесу",
            "onBackPressed": "Android: кнопка «Назад»", "onActivityResult": "Android: результат іншого екрана (сканер QR, вибір файлу, Wi-Fi)",
            "onRequestPermissionsResult": "Android: людина відповіла на запит дозволу", "onReceive": "Android: прийшла системна подія (broadcast)",
            "onStartCommand": "Android: службу запущено / розбуджено будильником", "onBind": "Android: прив'язка до служби (не використовується)",
            "onClick": "натискання кнопки", "onEditorAction": "Enter у полі вводу", "run": "тіло Runnable / потоку (див. коментар над ним)",
            "call": "завдання для пулу потоків", "onAvailable": "Wi-Fi з'явився", "onLost": "Wi-Fi зник",
            "shouldOverrideUrlLoading": "WebView: куди відкривати посилання (свій сайт — у застосунку, решта — назовні)",
            "shouldInterceptRequest": "WebView: віддати файл замість запиту в мережу", "onPageStarted": "WebView: сторінка почала вантажитись",
            "onPageFinished": "WebView: сторінка завантажилась — зберегти cookie, назву компанії", "onReceivedError": "WebView: помилка завантаження",
            "onReceiveValue": "результат JavaScript зі сторінки", "onShowFileChooser": "WebView: вибір фото / файлу",
            "onJsConfirm": "WebView: діалог confirm() сторінки", "onDownloadStart": "WebView: завантаження файлу (CSV)",
            "surfaceCreated": "камера: поверхня готова", "surfaceChanged": "камера: розмір змінився", "surfaceDestroyed": "камера: поверхня зникла",
            "onPreviewFrame": "камера: кадр для розпізнавання QR", "prefs": "налаштування застосунку (SharedPreferences)",
            "dp": "переведення dp у пікселі", "enc": "URL-кодування", "hex": "байти → hex", "read": "прочитати відповідь сервера",
            "time": "час ГГ:ХХ", "update": "оновити текст постійного сповіщення", "notification": "побудувати постійне сповіщення служби"}


def java_items(path):
    lines = open(path, encoding="utf-8").read().splitlines()
    rx = re.compile(r"^\s*(?:@\w+\s+)*(?:public|private|protected|static|final|synchronized|\s)+[\w<>\[\],. ]+\s+(\w+)\s*\([^;]*\)\s*(?:throws [\w., ]+)?\s*\{?\s*$")
    fld = re.compile(r"^\s*(?:private|public|static|final|volatile|\s)+[\w<>\[\],. ]+\s+(\w+)\s*(=.*)?;\s*(//.*)?$")
    items = []
    for i, l in enumerate(lines):
        m = rx.match(l)
        if m and m.group(1) not in ("if", "for", "while", "switch", "catch", "return", "new", "Thread", "runOnUiThread", "Runnable"):
            items.append((i + 1, m.group(1), "метод", "", comment_above(lines, i) or JAVA_STD.get(m.group(1), "")))
        elif "Runnable" in l and "new Runnable" in l and "=" in l and ("private" in l or "final" in l):
            name = re.search(r"(\w+)\s*=\s*new Runnable", l)
            if name:
                items.append((i + 1, name.group(1), "Runnable", "", comment_above(lines, i)))
        elif fld.match(l) and l.startswith("    ") and not l.startswith("        "):
            f = fld.match(l)
            items.append((i + 1, f.group(1), "поле", "", (f.group(3) or "").lstrip("/ ").strip() or comment_above(lines, i)))
    return items


TABS = {"clock": "вкладка «Зміна»", "timesheet": "вкладка «Табель»", "schedule": "вкладка «Графік»", "requests": "вкладка «Запити»",
        "team": "вкладка «Колеги»", "dayoffs": "вкладка «Дей-оффи й обміни» (тімлід)", "profile": "вкладка «Профіль»",
        "app": "вкладка «Налаштування додатку»", "live": "вкладка «Огляд»", "employees": "вкладка «Працівники»",
        "zones": "вкладка «Точки і Wi-Fi»", "logs": "вкладка «Підключення»", "audit": "вкладка «Журнал редагувань»",
        "payroll": "вкладка «Зарплата»", "settings": "вкладка «Правила»"}


def template_items(path):
    lines = open(path, encoding="utf-8").read().splitlines()
    rx = re.compile(r"\{%-?\s*(macro\s+\w+|block\s+\w+|(?:el)?if\s+tab\s*==\s*'\w+'|include\s+[^%]+)")
    items = []
    for i, l in enumerate(lines):
        for m in rx.finditer(l):
            txt = m.group(1)
            kind = txt.split()[0].replace("elif", "вкладка").replace("if", "вкладка")
            name = re.sub(r"^(macro|block|elif|if|include)\s+", "", txt).replace("tab == ", "").strip("' ")
            items.append((i + 1, name, kind, "", TABS.get(name, "")))
        for m in re.finditer(r"<(?:div|section|dialog|form)[^>]*\bid=\"([\w-]+)\"", l):
            items.append((i + 1, "#" + m.group(1), "блок", "", ""))
    return items


def shell_items(path):
    lines = open(path, encoding="utf-8").read().splitlines()
    return [(i + 1, l.strip("# ").strip()[:100], "крок", "", "") for i, l in enumerate(lines) if l.startswith("# ") and i > 0][:15]


def esc(t):
    return (t or "").replace("|", "\\|").replace("\n", " ")


def main():
    files = [f for f in FILES if os.path.exists(os.path.join(ROOT, f))]
    rev = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    out = ["# Карта коду", "",
           f"Кожен файл, кожна функція / маршрут / метод / вкладка — з номером рядка (стан коду: коміт `{rev}`, плюс незакомічені зміни,"
           " якщо були). Згенеровано `python docs/make_code_map.py`: після змін у коді перезапустіть, щоб рядки були точними.",
           "Огляд того, як усе пов'язано, — [09-developer.md](09-developer.md) і розділ «Сервер: що робить кожен файл» у `readme.html`.", "",
           "## Файли", "", "| Файл | Рядків | Що робить |", "|---|---:|---|"]
    for f in files:
        n = sum(1 for _ in open(os.path.join(ROOT, f), encoding="utf-8"))
        out.append(f"| [`{f}`](#{re.sub(r'[^a-z0-9а-яіїєґ_-]', '', f.lower().replace('/', '').replace('.', ''))}) | {n} | {esc(FILES[f])} |")
    for f in files:
        p = os.path.join(ROOT, f)
        if f.endswith(".py"):
            items = python_items(p)
        elif f.endswith(".js"):
            items = js_items(p)
        elif f.endswith(".java"):
            items = java_items(p)
        elif f.endswith(".html"):
            items = template_items(p)
        else:
            items = shell_items(p)
        out += ["", f"## `{f}`", "", FILES[f], ""]
        if not items:
            out.append("_(без окремих функцій)_")
            continue
        out += ["| Рядок | Назва | Що це | Маршрут / подія | Опис |", "|---:|---|---|---|---|"]
        for line, name, kind, route, doc in items:
            out.append(f"| {line} | `{esc(name)}` | {kind} | {esc(route)} | {esc(doc)[:300]} |")
    open(OUT, "w", encoding="utf-8").write("\n".join(out) + "\n")
    print(f"{OUT}: {len(out)} рядків, {len(files)} файлів")


if __name__ == "__main__":
    main()
