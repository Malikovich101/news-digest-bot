import os
import json
from datetime import datetime, timedelta, timezone

import requests
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from google import genai

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION_STRING = os.environ["TG_SESSION_STRING"]
BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

STATE_FILE = "state.json"
CHANNELS_FILE = "channels.txt"
MESSAGES_PER_CHANNEL_LIMIT = 100
FIRST_RUN_LOOKBACK_HOURS = 9


def load_channels():
    if not os.path.exists(CHANNELS_FILE):
        return []
    with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
        return [
            line.strip()
            for line in f
            if line.strip() and not line.strip().startswith("#")
        ]


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_run": None}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    # Telegram ограничивает сообщение 4096 символами - режем на части
    for i in range(0, len(text), 3800):
        chunk = text[i:i + 3800]
        resp = requests.post(url, data={"chat_id": CHAT_ID, "text": chunk})
        if not resp.ok:
            print("Ошибка отправки в Telegram:", resp.text)


def build_prompt(collected):
    items_text = "\n\n".join(
        f"[Канал: {c['channel']}]\n{c['text']}" for c in collected
    )
    return f"""Ты помощник, который составляет сводку новостей из Telegram-каналов.
Ниже — посты из нескольких каналов за последний период. Разные каналы часто
пишут об одном и том же событии разными словами.

Твоя задача:
1. Сгруппируй посты, которые рассказывают об одном и том же событии/новости.
2. Для каждой уникальной новости напиши краткую сводку (2-4 предложения) своими словами.
3. После каждой сводки в скобках укажи каналы-источники, например: (@channel1, @channel2).
4. Не повторяй одну и ту же новость дважды.
5. Начинай каждый пункт с эмодзи 📌, не используй Markdown-разметку со звёздочками или решётками.
6. Отвечай на русском языке.

Посты:
{items_text}
"""


def main():
    state = load_state()
    last_run_str = state.get("last_run")
    if last_run_str:
        last_run = datetime.fromisoformat(last_run_str)
    else:
        last_run = datetime.now(timezone.utc) - timedelta(hours=FIRST_RUN_LOOKBACK_HOURS)

    channels = load_channels()
    now = datetime.now(timezone.utc)

    if not channels:
        print("channels.txt пуст - нечего собирать")
        state["last_run"] = now.isoformat()
        save_state(state)
        return

    collected = []

    with TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) as client:
        for ch in channels:
            try:
                for message in client.iter_messages(ch, limit=MESSAGES_PER_CHANNEL_LIMIT):
                    if message.date <= last_run:
                        break
                    if message.text:
                        collected.append({
                            "channel": ch,
                            "text": message.text,
                        })
            except Exception as e:
                print(f"Ошибка при чтении {ch}: {e}")

    if not collected:
        send_telegram_message("За этот период новых постов не было.")
        state["last_run"] = now.isoformat()
        save_state(state)
        return

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=build_prompt(collected),
    )
    digest_text = response.text or "Не удалось сформировать сводку."

    send_telegram_message(digest_text)

    state["last_run"] = now.isoformat()
    save_state(state)


if __name__ == "__main__":
    main()
