"""
ВАЖНО: этот скрипт запускается ОДИН РАЗ на своём компьютере, локально.
Не запускай его нигде, кроме своего компьютера, и не показывай никому
результат, который он выведет в конце - это как пароль от аккаунта.

Логинься тем аккаунтом, который подписан на все нужные каналы
(в нашем случае - запасной аккаунт).

Перед запуском установи библиотеку:
    pip install telethon

Запуск:
    python get_session.py
"""

from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("Введи свой api_id: ").strip())
api_hash = input("Введи свой api_hash: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:
    session_string = client.session.save()
    print("\n" + "=" * 60)
    print("Готово! Вот твоя session string:")
    print("=" * 60)
    print(session_string)
    print("=" * 60)
    print("\nСкопируй строку выше целиком и сохрани её как секрет")
    print("TG_SESSION_STRING в настройках GitHub-репозитория.")
    print("Никому её не показывай - это полный доступ к аккаунту.")
