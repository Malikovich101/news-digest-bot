# Настройка news-digest-bot

## 1. Файлы в репозитории
Загрузи всю эту структуру в свой GitHub-репозиторий, сохраняя папки:

```
digest.py
manage_channels.py
get_session.py
requirements.txt
channels.txt
bot_offset.json
.github/workflows/digest.yml
.github/workflows/manage.yml
```

## 2. Секреты (Settings → Secrets and variables → Actions → New repository secret)

| Имя секрета        | Значение                                  |
|---------------------|--------------------------------------------|
| TG_API_ID           | твой api_id с my.telegram.org              |
| TG_API_HASH         | твой api_hash с my.telegram.org             |
| TG_SESSION_STRING   | получишь через get_session.py (см. ниже)    |
| TG_BOT_TOKEN        | токен от @BotFather                         |
| TG_CHAT_ID          | твой chat_id                                |
| GEMINI_API_KEY      | ключ из aistudio.google.com                 |

## 3. Получение TG_SESSION_STRING (один раз, локально)

1. Установи Python на своём компьютере, если его нет
2. В терминале: `pip install telethon`
3. Скачай `get_session.py` и запусти: `python get_session.py`
4. Введи api_id, api_hash
5. Введи номер телефона ЗАПАСНОГО аккаунта (с +, например +79991234567)
6. Введи код, который придёт в Telegram на этот аккаунт
7. Скопируй выведенную session string в секрет TG_SESSION_STRING

## 4. Управление каналами

- Отредактировать `channels.txt` вручную на GitHub (по одному @username на строку)
- Или прямо в боте: `/add @channel`, `/remove @channel`, `/list`
  (изменения через бота применяются в течение ~30 минут)

## 5. Проверка работы

Actions → выбрать workflow "News Digest" → Run workflow (запустить вручную,
не дожидаясь расписания) - если всё настроено верно, придёт сообщение от бота.
