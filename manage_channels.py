import os
import json
import urllib.request
import urllib.parse

BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
OWNER_CHAT_ID = str(os.environ["TG_CHAT_ID"])

OFFSET_FILE = "bot_offset.json"
CHANNELS_FILE = "channels.txt"


def api_call(method, params=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    if params:
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data)
    else:
        req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def load_offset():
    if os.path.exists(OFFSET_FILE):
        with open(OFFSET_FILE, "r") as f:
            return json.load(f).get("offset", 0)
    return 0


def save_offset(offset):
    with open(OFFSET_FILE, "w") as f:
        json.dump({"offset": offset}, f)


def load_channels():
    if not os.path.exists(CHANNELS_FILE):
        return []
    with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
        return [
            line.strip()
            for line in f
            if line.strip() and not line.strip().startswith("#")
        ]


def save_channels(channels):
    with open(CHANNELS_FILE, "w", encoding="utf-8") as f:
        for ch in channels:
            f.write(ch + "\n")


def send_message(text):
    api_call("sendMessage", {"chat_id": OWNER_CHAT_ID, "text": text})


def normalize(name):
    name = name.strip()
    if not name.startswith("@"):
        name = "@" + name
    return name


def main():
    offset = load_offset()
    result = api_call("getUpdates", {"offset": offset, "timeout": 0})
    updates = result.get("result", [])

    if not updates:
        return

    channels = load_channels()
    changed = False

    for upd in updates:
        offset = upd["update_id"] + 1
        msg = upd.get("message")
        if not msg or "text" not in msg:
            continue
        if str(msg["chat"]["id"]) != OWNER_CHAT_ID:
            continue  # игнорируем сообщения не от владельца

        text = msg["text"].strip()

        if text.startswith("/add"):
            parts = text.split(maxsplit=1)
            if len(parts) == 2:
                ch = normalize(parts[1])
                if ch not in channels:
                    channels.append(ch)
                    changed = True
                    send_message(f"Добавил {ch}. Всего каналов: {len(channels)}")
                else:
                    send_message(f"{ch} уже есть в списке")
            else:
                send_message("Формат: /add @channelname")

        elif text.startswith("/remove"):
            parts = text.split(maxsplit=1)
            if len(parts) == 2:
                ch = normalize(parts[1])
                if ch in channels:
                    channels.remove(ch)
                    changed = True
                    send_message(f"Убрал {ch}. Всего каналов: {len(channels)}")
                else:
                    send_message(f"{ch} не найден в списке")
            else:
                send_message("Формат: /remove @channelname")

        elif text.startswith("/list"):
            if channels:
                send_message("Текущие каналы (" + str(len(channels)) + "):\n" + "\n".join(channels))
            else:
                send_message("Список каналов пуст")

    save_offset(offset)
    if changed:
        save_channels(channels)


if __name__ == "__main__":
    main()
