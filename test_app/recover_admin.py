#!/usr/bin/env python3
"""Скидання пароля адміністратора CrewHub з сервера.

Запускається В ТЕРМІНАЛІ НА СЕРВЕРІ, де лежить база. Питає новий пароль,
хешує його і записує адміну. Захист простий і достатній: цей скрипт може
запустити лише той, у кого є доступ до самого сервера (SSH/консоль). Ліва
людина без доступу до сервера його не запустить.

   python3 recover_admin.py            # вибрати адміна і задати новий пароль
   python3 recover_admin.py <логін>    # одразу конкретному адміну

Кожне скидання пишеться в журнал редагувань (audit), тож видно, коли і кому
міняли пароль. Після входу з новим паролем система одразу попросить змінити його.
"""
import getpass
import sys

import crewhub_core as core


def pick_admin(conn, wanted=None):
    admins = conn.execute("select id, username, full_name from users where role = 'admin' and status != 'dismissed'"
                          " order by username").fetchall()
    if not admins:
        print("У системі немає активного адміністратора.")
        return None
    if wanted:
        for a in admins:
            if a["username"] == wanted:
                return a
        print(f"Адміна з логіном «{wanted}» не знайдено.")
        return None
    if len(admins) == 1:
        return admins[0]
    print("Адміністратори:")
    for i, a in enumerate(admins, 1):
        print(f"  {i}. {a['full_name']} ({a['username']})")
    try:
        choice = int(input("Кому скинути пароль (номер): ").strip())
        return admins[choice - 1]
    except (ValueError, IndexError):
        print("Невірний вибір.")
        return None


def main(argv):
    core.init_db(seed=False)
    conn = core.connect()
    admin = pick_admin(conn, argv[1] if len(argv) > 1 else None)
    if not admin:
        return 1
    print(f"Скидання пароля для: {admin['full_name']} ({admin['username']})")
    new = getpass.getpass("Новий пароль (мін. 6 символів): ")
    if len(new) < 6:
        print("Пароль надто короткий.")
        return 1
    if getpass.getpass("Повторіть новий пароль: ") != new:
        print("Паролі не збігаються.")
        return 1
    with core._lock:
        conn.execute("update users set password_hash = ?, must_change_password = 1, status = 'active' where id = ?",
                     (core.hash_password(new), admin["id"]))
        # Якщо пароль скидають через злам, старий вхід «запам'ятати мене» на чужих пристроях теж має згаснути
        core.revoke_remember(conn, admin["id"])
        core.audit(conn, None, "PASSWORD_RESET", "users", admin["id"], admin["id"],
                   {"via": [None, "recover_admin.py (сервер)"], "sessions": [None, "вихід на всіх пристроях"]})
        conn.commit()
    print(f"\nГотово. Увійдіть як {admin['username']} з новим паролем — система попросить одразу його змінити.\n"
          "Усі попередні входи цього адміна на інших пристроях скасовано.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
