"""Достаёт custom_emoji_id из наборов эмодзи и собирает emoji_map.json для бота.

Запуск (нужен интернет и токен бота):
    BOT_TOKEN=123:abc python emoji_pack_ids.py RestrictedEmoji GameEmoji
На Windows (PowerShell):
    $env:BOT_TOKEN="123:abc"; python emoji_pack_ids.py RestrictedEmoji GameEmoji

Результат:
  emoji_map.json  — {"🌿": "id", ...} для бота; при повторе одного эмодзи побеждает
                    набор, указанный раньше (порядок аргументов = приоритет)
  emoji_all.json  — все найденные ID по каждому эмодзи, чтобы вручную заменить любой
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    sys.exit("Задай переменную BOT_TOKEN")
packs = sys.argv[1:] or ["RestrictedEmoji", "GameEmoji"]
packs = [p.rsplit("/", 1)[-1] for p in packs]   # можно передавать и ссылки

emoji_map, emoji_all = {}, {}
for pack in packs:
    url = f"https://api.telegram.org/bot{TOKEN}/getStickerSet?" + urllib.parse.urlencode({"name": pack})
    try:
        data = json.load(urllib.request.urlopen(url, timeout=30))
    except Exception as e:
        print(f"[!] {pack}: не удалось получить набор ({e})")
        continue
    stickers = data["result"]["stickers"]
    print(f"{pack}: {len(stickers)} эмодзи")
    for st in stickers:
        em, cid = st.get("emoji"), st.get("custom_emoji_id")
        if not em or not cid:
            continue
        emoji_all.setdefault(em, [])
        if cid not in [x["id"] for x in emoji_all[em]]:
            emoji_all[em].append({"id": cid, "pack": pack})
        emoji_map.setdefault(em, cid)

base = Path(__file__).parent
(base / "emoji_map.json").write_text(json.dumps(emoji_map, ensure_ascii=False, indent=1), encoding="utf-8")
(base / "emoji_all.json").write_text(json.dumps(emoji_all, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"Готово: {len(emoji_map)} эмодзи в emoji_map.json")
print("Эмодзи из игры, которых нет в наборах (останутся обычными):")
used = "⛏🎯✨⚖️💰⭐🗺📡🔧🎁📍🎒🛒👤💸🎉🔒💎📖👑⏳🏺🚜⛽🛠📦⚙🛡"
print(" ".join(c for c in used if c not in emoji_map and c + "\ufe0f" not in emoji_map))
