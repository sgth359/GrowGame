import asyncio
import base64
import html
import json
import logging
import math
import os
import random
import re
import time
from pathlib import Path

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           LabeledPrice, Message, PreCheckoutQuery)
from aiohttp import ClientSession, web

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["BOT_TOKEN"]
DB_URL = os.environ["DATABASE_URL"]               # строка подключения Neon/Supabase
PORT = int(os.environ.get("PORT", 10000))
SELF_URL = os.environ.get("RENDER_EXTERNAL_URL")  # Render подставляет сам
_ids = [int(x) for x in os.environ.get("ADMIN_ID", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()]
ADMIN_IDS = set(_ids)                              # ADMIN_ID=123,456 — несколько админов через запятую
ADMIN_ID = _ids[0] if _ids else 0                 # ему приходят уведомления о сбоях выдачи
SUPPORT = os.environ.get("SUPPORT_CONTACT", "администратору игрового чата")

BASE = Path(__file__).parent
CFG = json.loads((BASE / "game.json").read_text(encoding="utf-8"))
IMG_DIR = BASE / "images"                         # images/пляж.jpg ... (картинка локации)
VIP = CFG["vip"]
RARITY = CFG["rarity"]

# все находки: ключ = «локация + номер», напр. пляж3
FINDS = {}
for _lk, _loc in CFG["locations"].items():
    for _i, _f in enumerate(_loc["finds"]):
        FINDS[f"{_lk}{_i}"] = dict(_f, key=f"{_lk}{_i}", loc=_lk)

# коллекционные находки: ключ = «локация + r + номер», напр. пляжr2
RELICS = {}
for _lk, _loc in CFG["locations"].items():
    for _i, _r in enumerate(_loc.get("relics", [])):
        RELICS[f"{_lk}r{_i}"] = dict(_r, key=f"{_lk}r{_i}", loc=_lk)
COLL = CFG["collection"]
CH = CFG["chests"]
CHC = CFG["chest_cfg"]
KEYS = CFG["keys"]
EXPC = CFG["expedition"]
ROUTES = CFG["routes"]
MODS = CFG["modules"]

# Премиум-эмодзи: emoji_map.json делает emoji_pack_ids.py ({"🌿": "id", ...})
EMOJI_RE = None
EMOJI_IDS = {}
_map_file = BASE / "emoji_map.json"
if _map_file.exists():
    _raw = json.loads(_map_file.read_text(encoding="utf-8"))
    EMOJI_IDS = {k.replace("\ufe0f", ""): v for k, v in _raw.items() if k}
    if EMOJI_IDS:
        _keys = sorted(EMOJI_IDS, key=len, reverse=True)
        EMOJI_RE = re.compile("(" + "|".join(re.escape(k) for k in _keys) + ")\ufe0f?")


def decorate(text: str) -> str:
    """Экранирует HTML и заменяет обычные эмодзи на премиум (с обычным как запасным)."""
    text = html.escape(text, quote=False)
    if not EMOJI_RE:
        return text
    return EMOJI_RE.sub(
        lambda mo: f'<tg-emoji emoji-id="{EMOJI_IDS[mo.group(1)]}">{mo.group(0)}</tg-emoji>', text)


SCHEMA = """
create table if not exists players(
  user_id bigint primary key, name text, username text,
  money bigint not null default 300, xp bigint not null default 0,
  loc text not null default 'пляж', tool text not null default 'ржавая',
  det text, det_dur int not null default 0, bait text,
  last_dig double precision not null default 0,
  since_treasure int not null default 99, digs int not null default 0,
  vip_until double precision not null default 0, last_bonus text,
  banned boolean not null default false);
alter table players add column if not exists username text;
alter table players add column if not exists loc text not null default 'пляж';
alter table players add column if not exists tool text not null default 'ржавая';
alter table players add column if not exists det text;
alter table players add column if not exists det_dur int not null default 0;
alter table players add column if not exists bait text;
alter table players add column if not exists last_dig double precision not null default 0;
alter table players add column if not exists since_treasure int not null default 99;
alter table players add column if not exists digs int not null default 0;
alter table players add column if not exists vip_until double precision not null default 0;
alter table players add column if not exists last_bonus text;
alter table players add column if not exists banned boolean not null default false;
create table if not exists inventory(
  user_id bigint, item text, qty double precision not null,
  primary key(user_id, item));
create table if not exists dig_journal(
  user_id bigint, item text, cnt int not null default 0,
  best_kg double precision not null default 0, best_r int not null default 0,
  primary key(user_id, item));
create table if not exists dig_missions(
  user_id bigint, day text, idx int, kind text, goal int,
  progress double precision not null default 0, reward int,
  claimed boolean not null default false, text text,
  primary key(user_id, day, idx));
create table if not exists payments(
  charge_id text primary key, user_id bigint, payload text, stars int,
  created double precision, delivered boolean not null default false,
  refunded boolean not null default false);
create table if not exists admin_log(
  id bigserial primary key, created double precision, admin_id bigint,
  target bigint, action text);
alter table players add column if not exists vehicle text;
alter table players add column if not exists stars bigint not null default 0;
create table if not exists star_log(
  id bigserial primary key, created double precision, user_id bigint, delta bigint, reason text);
create table if not exists relic_journal(
  user_id bigint, key text, cnt int not null default 0,
  primary key(user_id, key));
create table if not exists vehicles(
  user_id bigint, key text, engine int not null default 0, radar int not null default 0,
  cargo int not null default 0, hull int not null default 0,
  primary key(user_id, key));
create table if not exists expeditions(
  user_id bigint primary key, vehicle text, route text, loc text,
  started double precision, ready double precision);
"""
RESET = "drop table if exists star_log, expeditions, vehicles, relic_journal, dig_missions, dig_journal, inventory, players cascade;"

pool: asyncpg.Pool
LOCK = asyncio.Lock()
now = time.time


# ---------- утилиты ----------
def level_of(xp: int) -> int:
    return int((xp / 50) ** 0.5) + 1


def xp_left(xp: int) -> int:
    return 50 * level_of(xp) ** 2 - xp


def fmt_t(sec: float) -> str:
    sec = int(max(sec, 0))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    out = (f"{h} ч " if h else "") + (f"{m} мин " if (m or h) else "")
    return (out + f"{s} сек").strip()


def fmt_n(x: float) -> str:
    return f"{x:g}" if abs(x - round(x)) < 1e-9 else f"{x:.1f}"


def today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def is_vip(p) -> bool:
    return p["vip_until"] > now()


def vip_line(p) -> str:
    if not is_vip(p):
        return ""
    return "\n👑 VIP до " + time.strftime("%d.%m.%Y", time.gmtime(p["vip_until"]))


def loc_label(key: str) -> str:
    loc = CFG["locations"][key]
    return f"{loc['emoji']} {loc['name']}"


def find_name(key: str) -> str:
    f = FINDS[key]
    return f"{f['emoji']} {f['name']}"


def item_name(item: str) -> str:
    kind, key = item.split(":", 1)
    if kind == "bait":
        return f"Карта: {CFG['baits'][key]['name']}"
    if kind == "det":
        return f"Запасной детектор: {CFG['dets'][key]['name']}"
    if kind == "find":
        return find_name(key)
    if kind == "fuel":
        return f"⛽ {CFG['fuels'][key]['name']}"
    if kind == "relic":
        return f"{RELICS[key]['emoji']} {RELICS[key]['name']}"
    if kind == "key":
        return f"{KEYS[key]['emoji']} {KEYS[key]['name']}"
    if kind == "chest":
        return f"{CH[key]['emoji']} {CH[key]['name']}"
    return item


async def get_player(conn, m):
    uid = m.from_user.id
    uname = m.from_user.username
    p = await conn.fetchrow("select * from players where user_id=$1", uid)
    if not p:
        await conn.execute(
            "insert into players(user_id,name,username,money,bait) values($1,$2,$3,$4,$5)",
            uid, m.from_user.full_name, uname, CFG["start_money"], "карта1")
        await add_item(conn, uid, "bait:карта1", 3)
        p = await conn.fetchrow("select * from players where user_id=$1", uid)
    elif p["username"] != uname or p["name"] != m.from_user.full_name:
        await conn.execute("update players set name=$2, username=$3 where user_id=$1",
                           uid, m.from_user.full_name, uname)
    return p


async def add_item(conn, uid, item, qty):
    await conn.execute(
        "insert into inventory(user_id,item,qty) values($1,$2,$3) "
        "on conflict (user_id,item) do update set qty=inventory.qty+$3", uid, item, qty)


async def take_item(conn, uid, item, qty) -> bool:
    have = await conn.fetchval(
        "select qty from inventory where user_id=$1 and item=$2", uid, item)
    if not have or have < qty - 1e-9:
        return False
    if have - qty < 1e-9:
        await conn.execute("delete from inventory where user_id=$1 and item=$2", uid, item)
    else:
        await conn.execute(
            "update inventory set qty=qty-$3 where user_id=$1 and item=$2", uid, item, qty)
    return True


# ---------- миссии ----------
async def ensure_missions(conn, p):
    uid, day = p["user_id"], today()
    rows = await conn.fetch(
        "select * from dig_missions where user_id=$1 and day=$2 order by idx", uid, day)
    if rows:
        return rows
    lvl = level_of(p["xp"])
    for idx, pool_ in enumerate(CFG["missions"]):
        t = random.choice(pool_)
        reward = round(t["reward"] * (1 + 0.03 * lvl))
        await conn.execute(
            "insert into dig_missions(user_id,day,idx,kind,goal,reward,text) values($1,$2,$3,$4,$5,$6,$7) "
            "on conflict do nothing", uid, day, idx, t["kind"], t["goal"], reward,
            t["text"].format(goal=t["goal"]))
    return await conn.fetch(
        "select * from dig_missions where user_id=$1 and day=$2 order by idx", uid, day)


async def bump(conn, p, kind: str, amount: float):
    await ensure_missions(conn, p)
    await conn.execute(
        "update dig_missions set progress=least(goal::double precision, progress+$4::double precision) "
        "where user_id=$1 and day=$2 and kind=$3 and not claimed",
        p["user_id"], today(), kind, float(amount))


# ---------- раскопки ----------
def cooldown_sec(p) -> float:
    cd = CFG["tools"][p["tool"]]["cooldown_min"] * 60
    return cd * (1 - VIP["cooldown_cut"]) if is_vip(p) else cd


def loadout_text(p, bait_left=None) -> str:
    tool = CFG["tools"][p["tool"]]
    L = [f"📍 Локация: {loc_label(p['loc'])}", f"⛏ Лопата: {tool['name']}"]
    if p["det"]:
        d = CFG["dets"][p["det"]]
        L.append(f"📡 Детектор: {d['name']} ({p['det_dur']}/{d['durability']})")
    if p["bait"]:
        left = bait_left if bait_left is not None else None
        L.append(f"🗺 Карта: {CFG['baits'][p['bait']]['name']}" + (f" (осталось {fmt_n(left)})" if left is not None else ""))
    return "\n".join(L)


async def do_dig(conn, p):
    uid, t, vip = p["user_id"], now(), is_vip(p)
    loc_key = p["loc"]
    loc = CFG["locations"][loc_key]
    wait = p["last_dig"] + cooldown_sec(p) - t
    if wait > 0:
        return [("⏳ Ты ещё не отдохнул после прошлой раскопки.\n"
                 f"До следующего копания: {fmt_t(wait)}\n" + loadout_text(p), None)]

    # карта (расходник)
    bait, bait_note = None, ""
    if p["bait"]:
        bkey = p["bait"]
        if await take_item(conn, uid, f"bait:{bkey}", 1):
            bait = CFG["baits"][bkey]
            left = await conn.fetchval("select qty from inventory where user_id=$1 and item=$2",
                                       uid, f"bait:{bkey}") or 0
            bait_note = f"🗺 Использована карта: {bait['name']} (осталось {fmt_n(left)})\n"
            if not left:
                await conn.execute("update players set bait=null where user_id=$1", uid)
                bait_note += "⚠️ Карты этого типа закончились.\n"
        else:
            await conn.execute("update players set bait=null where user_id=$1", uid)

    # удача
    det, dur = p["det"], p["det_dur"]
    luck = CFG["tools"][p["tool"]]["luck"]
    luck *= 1 + VIP["luck_bonus"] if vip else 1
    luck *= 1 + CFG["dets"][det]["luck"] if det else 1
    luck *= bait["luck"] if bait else 1
    luck *= 1 + COLL["luck_per_set"] * await collector_sets(conn, uid)

    finds = loc["finds"]
    weights = [f["w"] * (luck if f["r"] >= 2 else 1.0) for f in finds]
    i = random.choices(range(len(finds)), weights=weights)[0]
    f = finds[i]
    fkey = f"{loc_key}{i}"
    chance = weights[i] / sum(weights) * 100
    kg = round(random.uniform(f["wmin"], f["wmax"]), 3)
    value = round(kg * f["ppk"] * (bait["value"] if bait else 1))
    xp_gain = round(f["xp"] * (1 + VIP["xp_bonus"] if vip else 1))

    await add_item(conn, uid, f"find:{fkey}", kg)
    await conn.execute(
        "insert into dig_journal(user_id,item,cnt,best_kg,best_r) values($1,$2,1,$3,$4) "
        "on conflict (user_id,item) do update set cnt=dig_journal.cnt+1, "
        "best_kg=greatest(dig_journal.best_kg,$3), best_r=greatest(dig_journal.best_r,$4)",
        uid, fkey, kg, f["r"])

    text = ("⛏ Раскопка!\n" + bait_note +
            f"📍 Локация: {loc_key and loc_label(loc_key)}\n"
            f"🎯 Ты нашёл: {f['emoji']} {f['name']}\n"
            f"✨ Редкость: {RARITY[f['r']]}\n"
            f"🎯 Шанс найти именно это: {chance:.1f}%\n"
            f"⚖️ Вес: {kg:.3f} кг\n"
            f"💰 Цена при продаже: {value}\n"
            f"⭐ XP: +{xp_gain}")

    rr = await roll_relic(conn, uid, loc_key, luck)
    if rr:
        text += await relic_note(conn, uid, rr[0], rr[1])
    ct = await roll_chest(conn, uid, loc_key, luck)
    if ct:
        text += await chest_note(conn, uid, ct)

    # износ детектора
    if det:
        dur -= 1
        if dur <= 0:
            broken = CFG["dets"][det]["name"]
            spare = await conn.fetchval(
                "select item from inventory where user_id=$1 and item like 'det:%' "
                "order by (item=$2) desc limit 1", uid, f"det:{det}")
            if spare:
                await take_item(conn, uid, spare, 1)
                det = spare.split(":", 1)[1]
                dur = CFG["dets"][det]["durability"]
                text += f"\n\n🔧 {broken} сломался. Установлен запасной: {CFG['dets'][det]['name']}."
            else:
                det, dur = None, 0
                text += f"\n\n🔧 {broken} сломался. Новый можно купить в магазине."

    # необычная находка (два подряд невозможны без нужного числа раскопок)
    since = p["since_treasure"] + 1
    if since > CFG["treasure_gap"] and random.random() < CFG["treasure_chance"]:
        since = 0
        tr = random.choices(CFG["treasures"], weights=[x["w"] for x in CFG["treasures"]])[0]
        if "money" in tr:
            amount = random.randint(*tr["money"])
            await conn.execute("update players set money=money+$2 where user_id=$1", uid, amount)
            label = f"💰 {amount}"
        else:
            await add_item(conn, uid, tr["item"], tr["qty"])
            label = f"✨ {item_name(tr['item'])} × {tr['qty']}"
        text += ("\n\n🎁 Необычная находка!\nПока ты вытаскивал находку, "
                 f"под землёй нашёлся старый сундук.\n\n🎁 Внутри:\n{label}")

    await conn.execute(
        "update players set xp=xp+$2, det=$3, det_dur=$4, since_treasure=$5, digs=digs+1, last_dig=$6 "
        "where user_id=$1", uid, xp_gain, det, dur, since, t)
    await bump(conn, p, "dig", 1)
    await bump(conn, p, "kg", kg)
    if f["r"] >= 2:
        await bump(conn, p, "rare", 1)
    out = [(text, loc_key)]
    old, new = level_of(p["xp"]), level_of(p["xp"] + xp_gain)
    if new > old:
        unlocked = [f"{v['emoji']} {v['name']}" for v in CFG["locations"].values()
                    if old < v["min_level"] <= new]
        extra = f"\nОткрыта локация: {', '.join(unlocked)}" if unlocked else ""
        out.append((f"🎉 Новый уровень: {new}!{extra}", None))
    return out


async def sell_items(conn, p, mode: str) -> str:
    uid = p["user_id"]
    rows = await conn.fetch(
        "select item, qty from inventory where user_id=$1 and item like 'find:%'", uid)
    total, cnt = 0, 0
    for r in rows:
        key = r["item"].split(":", 1)[1]
        f = FINDS[key]
        if mode == "low" and f["r"] > 1:
            continue
        if mode == "nolegend" and f["r"] >= 4:
            continue
        if mode not in ("all", "low", "nolegend") and mode != key:
            continue
        gain = round(r["qty"] * f["ppk"] * (1 + VIP["sell_bonus"] if is_vip(p) else 1))
        total += gain
        cnt += 1
        await take_item(conn, uid, r["item"], r["qty"])
    if not cnt:
        return "Продавать нечего."
    await conn.execute("update players set money=money+$2 where user_id=$1", uid, total)
    await bump(conn, p, "sell_value", total)
    return f"💸 Продано видов находок: {cnt}\nИтого: +{total} 💰 (баланс {p['money'] + total})"


# ---------- магазин, предметы ----------
STAR_WORDS = {"звёзды", "звезды", "звезд", "⭐", "stars", "star"}
CATS = [("tools", "tool", "price"), ("dets", "det", "price"), ("baits", "bait", "price"),
        ("vehicles", "vehicle", "price"), ("fuels", "fuel", "price"), ("keys", "key", "price")]
CAT_TITLES = {"tools": "⛏ Лопаты", "dets": "📡 Детекторы", "baits": "🗺 Карты",
              "vehicles": "🚜 Транспорт", "fuels": "⛽ Топливо", "keys": "🗝 Ключи"}
CAT_OF = {key: cat for cat, _, _ in CATS for key in CFG[cat]}


def unit_stars(unit_price: int, cfg_item: dict) -> int:
    if cfg_item.get("stars"):
        return cfg_item["stars"]
    return max(1, math.ceil(unit_price / CFG["stars_rate"]))


def resolve_item(key: str, qty: int = 1):
    for cat, kind, pf in CATS:
        s_ = CFG[cat].get(key)
        if not s_ or not s_[pf]:
            continue                         # бесплатная стартовая лопата не продаётся
        q = max(1, min(int(qty), 100)) if kind in ("bait", "fuel", "key") else 1
        unit = s_[pf]
        label = f"{s_['name']} × {q}" if kind in ("bait", "fuel", "key") else s_["name"]
        return dict(kind=kind, key=key, qty=q, price=unit * q,
                    stars=unit_stars(unit, s_) * q, need=s_.get("min_level", 1), label=label)
    return None


def check_buy_basic(p, it):
    """Проверки без базы: уровень и лопата. Текст ошибки или None."""
    if level_of(p["xp"]) < it["need"]:
        return f"🔒 {it['label']} доступно с {it['need']} уровня."
    if it["kind"] == "tool":
        order = list(CFG["tools"])
        if order.index(it["key"]) <= order.index(p["tool"]):
            return "У тебя уже есть лопата не хуже этой."
    return None


async def check_buy(conn, p, it):
    """Полная проверка перед покупкой (с базой). Текст ошибки или None."""
    err = check_buy_basic(p, it)
    if err:
        return err
    if it["kind"] == "vehicle":
        if await conn.fetchval("select 1 from vehicles where user_id=$1 and key=$2", p["user_id"], it["key"]):
            return "Этот транспорт у тебя уже есть."
    return None


async def deliver_item(conn, uid, it) -> str:
    kind, key, qty = it["kind"], it["key"], it["qty"]
    if kind == "tool":
        await conn.execute("update players set tool=$2 where user_id=$1", uid, key)
    elif kind == "det":
        cur = await conn.fetchval("select det from players where user_id=$1", uid)
        if cur is None:
            await conn.execute("update players set det=$2, det_dur=$3 where user_id=$1",
                               uid, key, CFG["dets"][key]["durability"])
        else:
            await add_item(conn, uid, f"det:{key}", 1)
            return "\n📦 Положен на склад как запасной (заменит сломанный)."
    elif kind == "bait":
        await add_item(conn, uid, f"bait:{key}", qty)
        cur = await conn.fetchval("select bait from players where user_id=$1", uid)
        if cur is None:
            await conn.execute("update players set bait=$2 where user_id=$1", uid, key)
            return "\n🗺 Карта выбрана для раскопок."
    elif kind == "fuel":
        await add_item(conn, uid, f"fuel:{key}", qty)
    elif kind == "vehicle":
        await conn.execute("insert into vehicles(user_id,key) values($1,$2) on conflict do nothing", uid, key)
        await conn.execute("update players set vehicle=$2 where user_id=$1", uid, key)
        return "\n🚜 Транспорт куплен и выбран. Открой раздел «Экспедиции»."
    elif kind == "relic":
        await add_item(conn, uid, f"relic:{key}", 1)
        await conn.execute(
            "insert into relic_journal(user_id,key,cnt) values($1,$2,1) "
            "on conflict (user_id,key) do update set cnt=relic_journal.cnt+1", uid, key)
    elif kind == "key":
        await add_item(conn, uid, f"key:{key}", qty)
    elif kind == "chest":
        await add_item(conn, uid, f"chest:{key}", qty)
    return ""


async def cmd_buy(conn, p, a):
    key, rest = a[0], a[1:]
    stars_mode = any(x in STAR_WORDS for x in rest)
    qty = next((int(x) for x in rest if x.isdigit()), 1)
    it = resolve_item(key, qty)
    if not it:
        return [("Такого товара нет.", None)]
    err = await check_buy(conn, p, it)
    if err:
        return [(err, None)]
    if stars_mode:
        left = await spend_stars(conn, p["user_id"], it["stars"], f"покупка: {it['label']}")
        if left is None:
            bal = int(await conn.fetchval("select stars from players where user_id=$1", p["user_id"]) or 0)
            return [(f"⭐ Не хватает звёзд: нужно {it['stars']}, у тебя {bal}.\nПополни баланс кнопкой «➕ Пополнить ⭐».", None)]
        note = await deliver_item(conn, p["user_id"], it)
        return [(f"✅ Куплено за ⭐: {it['label']} — {it['stars']} ⭐ (остаток {left}){note}", None)]
    if p["money"] < it["price"]:
        return [(f"Не хватает денег: нужно {it['price']}, у тебя {p['money']}.\nМожно купить за ⭐ (кнопка ⭐ в карточке товара).", None)]
    await conn.execute("update players set money=money-$2 where user_id=$1", p["user_id"], it["price"])
    note = await deliver_item(conn, p["user_id"], it)
    return [(f"✅ Куплено: {it['label']} за {it['price']} 💰{note}", None)]


def b64e(x: str) -> str:
    return base64.urlsafe_b64encode(x.encode()).decode().rstrip("=")


def b64d(x: str) -> str:
    return base64.urlsafe_b64decode(x + "=" * (-len(x) % 4)).decode()


def build_invoice(p, spec: str):
    """spec: 'buy|ключ|кол-во' или 'prod|код'. Вернёт dict счёта или текст ошибки."""
    parts = spec.split("|")
    try:
        if parts[0] == "buy":
            it = resolve_item(parts[1], int(parts[2]))
            if not it:
                return "Такого товара нет."
            err = check_buy_basic(p, it)
            if err:
                return err
            title, desc, stars = it["label"], f"Покупка в игре: {it['label']}", it["stars"]
        elif parts[0] == "prod":
            pr = CFG["stars_products"].get(parts[1])
            if not pr:
                return "Такого товара нет."
            title, desc, stars = pr["title"], pr["desc"], pr["stars"]
        else:
            return "Неизвестный товар."
    except (IndexError, ValueError):
        return "Не понял, что покупаем."
    return {"title": title[:32], "desc": desc[:255], "stars": stars,
            "payload": f"{p['user_id']}|{spec}", "spec": spec}


# ---------- сундуки и ключи ----------
def key_label(k: str) -> str:
    return f"{KEYS[k]['emoji']} {KEYS[k]['name']}"


async def roll_chest(conn, uid, loc_key, luck, mult=1.0):
    if random.random() >= CHC["chance"] * mult:
        return None
    idx = list(CFG["locations"]).index(loc_key)
    lk = min(max(luck, 1.0), CHC["luck_cap"])
    ws = [w * (1 + idx * CHC["loc_boost"] * t) * (lk if t >= 2 else 1.0) for t, w in enumerate(CHC["tier_w"])]
    tier = f"c{random.choices(range(len(ws)), weights=ws)[0] + 1}"
    await add_item(conn, uid, f"chest:{tier}", 1)
    return tier


async def chest_note(conn, uid, tier) -> str:
    c = CH[tier]
    have = int(await conn.fetchval("select qty from inventory where user_id=$1 and item=$2",
                                   uid, f"key:{c['key']}") or 0)
    return (f"\n\n📦 Найден сундук: {c['emoji']} {c['name']}\n"
            f"🔒 Нужен ключ: {key_label(c['key'])} (у тебя: {have})\nОткрыть: Меню → 📦 Сундуки")


async def grant_finds(conn, uid, loc_key, n, luck):
    finds = CFG["locations"][loc_key]["finds"]
    weights = [f["w"] * (luck if f["r"] >= 2 else 1.0) for f in finds]
    got = {}
    for _ in range(n):
        i = random.choices(range(len(finds)), weights=weights)[0]
        kg = round(random.uniform(finds[i]["wmin"], finds[i]["wmax"]), 3)
        g = got.setdefault(i, [0, 0.0, 0.0])
        g[0] += 1
        g[1] += kg
        g[2] = max(g[2], kg)
    for i, (cnt, kg, best) in got.items():
        await add_item(conn, uid, f"find:{loc_key}{i}", round(kg, 3))
        await conn.execute(
            "insert into dig_journal(user_id,item,cnt,best_kg,best_r) values($1,$2,$3,$4,$5) "
            "on conflict (user_id,item) do update set cnt=dig_journal.cnt+$3, "
            "best_kg=greatest(dig_journal.best_kg,$4), best_r=greatest(dig_journal.best_r,$5)",
            uid, f"{loc_key}{i}", cnt, best, finds[i]["r"])
    return got


async def chest_open(conn, p, tier) -> str:
    uid = p["user_id"]
    c = CH[tier]
    kitem = f"key:{c['key']}"
    chests = int(await conn.fetchval("select qty from inventory where user_id=$1 and item=$2", uid, f"chest:{tier}") or 0)
    keys = int(await conn.fetchval("select qty from inventory where user_id=$1 and item=$2", uid, kitem) or 0)
    if chests < 1:
        return "У тебя нет такого сундука."
    if keys < 1:
        return f"🔒 Нужен ключ: {key_label(c['key'])}. Купи его в магазине, раздел «Ключи»."
    await take_item(conn, uid, f"chest:{tier}", 1)
    await take_item(conn, uid, kitem, 1)
    order = list(CFG["locations"])
    loc_key = order[min(order.index(p["loc"]), c["max_loc"])]     # ценность находок ограничена рангом сундука
    lvl = level_of(p["xp"])
    unlocked = [k for k in order[:c["max_loc"] + 1] if CFG["locations"][k]["min_level"] <= lvl] or [order[0]]
    L = [f"{c['emoji']} {c['name']} открыт!", f"🗝 Потрачен ключ: {KEYS[c['key']]['name']}", "", "🎁 Внутри:"]
    money = 0
    for _ in range(c["rolls"]):
        e = random.choices(c["loot"], weights=[x["w"] for x in c["loot"]])[0]
        t = e["t"]
        if t == "coins":
            amt = random.randint(e["min"], e["max"])
            money += amt
            L.append(f"💰 {amt}")
        elif t == "finds":
            n = random.randint(e["min"], e["max"])
            got = await grant_finds(conn, uid, loc_key, n, c["luck"])
            finds = CFG["locations"][loc_key]["finds"]
            kg = sum(g[1] for g in got.values())
            val = sum(g[1] * finds[i]["ppk"] for i, g in got.items())
            top = sorted(got.items(), key=lambda kv: (-finds[kv[0]]["r"], finds[kv[0]]["name"]))[:4]
            L.append(f"💎 Находки ({loc_label(loc_key)}): {n} шт. · {kg:.2f} кг · ≈{round(val)} 💰")
            L += [f"   • {finds[i]['emoji']} {finds[i]['name']} × {g[0]} · {RARITY[finds[i]['r']].split(' ')[0]}" for i, g in top]
        elif t == "item":
            q = random.randint(e["min"], e["max"])
            kind, key = e["item"].split(":", 1)
            await deliver_item(conn, uid, dict(kind=kind, key=key, qty=q))
            L.append(f"✨ {item_name(e['item'])} × {q}")
        elif t == "relic":
            lk = random.choices(unlocked, weights=[order.index(k) + 1 for k in unlocked])[0]
            key, new = await give_relic(conn, uid, lk)
            r = RELICS[key]
            L.append(f"🏺 Коллекционная находка: {r['emoji']} {r['name']} · ценность {r['value']}" + (" 🆕" if new else ""))
    if money:
        await conn.execute("update players set money=money+$2 where user_id=$1", uid, money)
    L.append("\nВсё добавлено на склад.")
    return "\n".join(L)


def loot_lines(c):
    tot = sum(e["w"] for e in c["loot"])
    out = []
    for e in sorted(c["loot"], key=lambda x: -x["w"]):
        pct = e["w"] / tot * 100
        if e["t"] == "coins":
            what = f"💰 Монеты {e['min']}–{e['max']}"
        elif e["t"] == "finds":
            what = (f"💎 Находки × {e['min']}–{e['max']} (твоя локация, но не выше "
                    f"«{CFG['locations'][list(CFG['locations'])[c['max_loc']]]['name']}», удача ×{c['luck']})")
        elif e["t"] == "relic":
            what = "🏺 Коллекционная находка"
        else:
            what = f"✨ {item_name(e['item'])} × {e['min']}" + (f"–{e['max']}" if e["max"] != e["min"] else "")
        out.append(f"{pct:.1f}% · {what}")
    return out


async def chests_screen(conn, p, note=""):
    uid = p["user_id"]
    inv = {r["item"]: int(r["qty"]) for r in await conn.fetch(
        "select item, qty from inventory where user_id=$1 and (item like 'chest:%' or item like 'key:%')", uid)}
    L = ([note, ""] if note else []) + [
        "📦 СУНДУКИ", "Сундуки выпадают при раскопках и экспедициях. Для каждого нужен свой ключ "
        "(магазин, раздел «Ключи»; иногда ключи находятся в сундуках).", ""]
    rows = []
    for tier, c in CH.items():
        n, k = inv.get(f"chest:{tier}", 0), inv.get(f"key:{c['key']}", 0)
        L.append(f"{c['emoji']} {c['name']} ×{n} · {key_label(c['key'])} ×{k}")
        row = []
        if n:
            row.append((f"{'🔓' if k else '🔒'} Открыть {c['emoji']} ×{n}", mcd(uid, "chopen", tier)))
        row.append((f"ℹ️ {c['name']}", mcd(uid, "chinfo", tier)))
        rows.append(row)
    rows += [[("🗝 Купить ключи", mcd(uid, "cat", "keys"))], [("🏠 Меню", mcd(uid, "home"))]]
    return "\n".join(L), kb(rows)


async def chest_info_screen(conn, p, tier):
    uid = p["user_id"]
    c = CH[tier]
    L = [f"{c['emoji']} {c['name']}", f"Ключ: {key_label(c['key'])} · цена {KEYS[c['key']]['price']} 💰 "
         f"или ⭐{unit_stars(KEYS[c['key']]['price'], KEYS[c['key']])}",
         f"Бросков награды: {c['rolls']}", "", "Шансы награды (за один бросок):", *loot_lines(c),
         "", "Содержимое определяется случайно. Всё выдаётся только внутри игры."]
    return "\n".join(L), kb([[("🗝 Купить ключ", mcd(uid, "item", c["key"]))],
                             [("⬅ Сундуки", mcd(uid, "chests")), ("🏠 Меню", mcd(uid, "home"))]])


async def stars_screen(conn, p, note=""):
    uid = p["user_id"]
    bal = int(await conn.fetchval("select stars from players where user_id=$1", uid) or 0)
    tops = [(k, pr) for k, pr in CFG["stars_products"].items() if pr.get("credit")]
    spend = [(k, pr) for k, pr in CFG["stars_products"].items() if not pr.get("credit")]
    L = ([note, ""] if note else []) + [
        "⭐ ЗВЁЗДЫ — игровая валюта", f"Баланс: {bal} ⭐", "",
        "Звёздами платишь в магазине (кнопка ⭐), за ключи, VIP и пакеты. Пополнение идёт через Telegram Stars "
        "(1 к 1), счёт приходит в личку бота. Звёзды нельзя вывести. Вопросы по оплате: /paysupport.", "",
        "Пополнить:"]
    rows = chunk([(f"➕ {pr['credit']} ⭐", mcd(uid, "prod", k)) for k, pr in tops], 3)
    L.append("Потратить на VIP и пакеты:")
    rows += chunk([(f"{pr['title']} · {pr['stars']}⭐", mcd(uid, "prod", k)) for k, pr in spend], 2)
    rows.append([("🏠 Меню", mcd(uid, "home"))])
    return "\n".join(L), kb(rows)


# ---------- коллекционные находки ----------
async def collector_sets(conn, uid) -> int:
    rows = await conn.fetch("select key from relic_journal where user_id=$1", uid)
    have = {r["key"] for r in rows}
    return sum(1 for lk, loc in CFG["locations"].items()
               if loc.get("relics") and all(f"{lk}r{i}" in have for i in range(len(loc["relics"]))))


async def give_relic(conn, uid, loc_key):
    rel = CFG["locations"][loc_key]["relics"]
    i = random.choices(range(len(rel)), weights=[r["w"] for r in rel])[0]
    key = f"{loc_key}r{i}"
    prev = await conn.fetchval("select cnt from relic_journal where user_id=$1 and key=$2", uid, key)
    await add_item(conn, uid, f"relic:{key}", 1)
    await conn.execute(
        "insert into relic_journal(user_id,key,cnt) values($1,$2,1) "
        "on conflict (user_id,key) do update set cnt=relic_journal.cnt+1", uid, key)
    return key, prev is None


async def roll_relic(conn, uid, loc_key, luck, mult=1.0):
    loc = CFG["locations"][loc_key]
    if not loc.get("relics"):
        return None
    chance = loc["relic_chance"] * mult * min(max(luck, 1.0) ** 0.5, 2.0)
    if random.random() >= chance:
        return None
    return await give_relic(conn, uid, loc_key)


async def relic_found_count(conn, uid, loc_key) -> int:
    return int(await conn.fetchval(
        "select count(*) from relic_journal where user_id=$1 and key like $2", uid, f"{loc_key}r%") or 0)


async def relic_note(conn, uid, key, new) -> str:
    r = RELICS[key]
    loc = CFG["locations"][r["loc"]]
    got = await relic_found_count(conn, uid, r["loc"])
    L = ["", "", "🏺 Коллекционная находка!", f"{r['emoji']} {r['name']}", f"💰 Ценность: {r['value']}",
         ("🆕 Новая запись в коллекции" if new else "Дубликат: можно продать")
         + f" · {loc['emoji']} {loc['name']}: {got}/{len(loc['relics'])}"]
    if new and got == len(loc["relics"]):
        L.append("🎉 Коллекция локации собрана! Награда ждёт в разделе «Коллекция».")
    return "\n".join(L)


async def relic_counts(conn, uid) -> dict:
    rows = await conn.fetch("select item, qty from inventory where user_id=$1 and item like 'relic:%'", uid)
    return {r["item"].split(":", 1)[1]: int(r["qty"]) for r in rows}


async def rel_screen(conn, p, note=""):
    uid = p["user_id"]
    found = {r["key"] for r in await conn.fetch("select key from relic_journal where user_id=$1", uid)}
    L = ["🏺 КОЛЛЕКЦИОННЫЕ НАХОДКИ", ""]
    btns = []
    for lk, loc in CFG["locations"].items():
        got = sum(1 for i in range(len(loc["relics"])) if f"{lk}r{i}" in found)
        full = " 🎉" if got == len(loc["relics"]) else ""
        L.append(f"{loc['emoji']} {loc['name']}: {got}/{len(loc['relics'])}{full}")
        btns.append((f"{loc['emoji']} {loc['name']} {got}/{len(loc['relics'])}", mcd(uid, "reloc", lk)))
    L += ["", f"Всего собрано: {len(found)}/{len(RELICS)}",
          f"Каждая собранная коллекция даёт награду и +{int(COLL['luck_per_set'] * 100)}% к удаче.",
          "Коллекционные находки выпадают при раскопках и экспедициях, не заменяя обычную находку."]
    return (note + "\n\n" if note else "") + "\n".join(L), kb(chunk(btns) + [[("🏠 Меню", mcd(uid, "home"))]])


async def reloc_screen(conn, p, lk, note=""):
    uid = p["user_id"]
    loc = CFG["locations"][lk]
    found = {r["key"] for r in await conn.fetch("select key from relic_journal where user_id=$1", uid)}
    have = await relic_counts(conn, uid)
    L = [f"🏺 {loc['emoji']} {loc['name']}", ""]
    btns = []
    for i, r in enumerate(loc["relics"]):
        key = f"{lk}r{i}"
        if key in found:
            q = have.get(key, 0)
            L.append(f"{r['emoji']} {r['name']} · ценность {r['value']} · у тебя ×{q}")
            if q:
                btns.append((f"{r['emoji']} {r['name']} ×{q}", mcd(uid, "relic", key)))
        else:
            L.append("❔ ??? · ещё не найдено")
    complete = all(f"{lk}r{i}" in found for i in range(len(loc["relics"])))
    rows = [[b] for b in btns]
    if complete:
        claimed = await conn.fetchval("select 1 from inventory where user_id=$1 and item=$2", uid, f"set:{lk}")
        if claimed:
            L.append("\n✅ Награда за коллекцию получена.")
        else:
            L.append(f"\n🎁 Коллекция собрана! Награда: {loc['set_reward']} 💰")
            rows.append([("🎁 Забрать награду", mcd(uid, "relset", lk))])
    rows.append([("⬅ Коллекция", mcd(uid, "rel")), ("🏠 Меню", mcd(uid, "home"))])
    return (note + "\n\n" if note else "") + "\n".join(L), kb(rows)


async def relic_screen(conn, p, key, note=""):
    uid = p["user_id"]
    r = RELICS[key]
    q = (await relic_counts(conn, uid)).get(key, 0)
    sell_bonus = 1 + VIP["sell_bonus"] if is_vip(p) else 1
    L = ([note, ""] if note else []) + [f"{r['emoji']} {r['name']}", f"💰 Ценность: {round(r['value'] * sell_bonus)}",
                                         f"У тебя: ×{q}"]
    rows = []
    if q > 1:
        rows.append([("💸 Продать 1", mcd(uid, "relsell", key, "1")),
                     ("💸 Дубликаты (оставить 1)", mcd(uid, "relsell", key, "dup"))])
        rows.append([("💸 Продать все", mcd(uid, "relsell", key, "all"))])
    elif q == 1:
        L.append("Единственная копия. Её можно оставить в коллекции или продать.")
        rows.append([("💸 Продать", mcd(uid, "relsell", key, "1"))])
    rows.append([("⬅ Локация", mcd(uid, "reloc", r["loc"])), ("🏠 Меню", mcd(uid, "home"))])
    return "\n".join(L), kb(rows)


async def relic_sell(conn, p, key, mode) -> str:
    uid = p["user_id"]
    r = RELICS[key]
    have = (await relic_counts(conn, uid)).get(key, 0)
    if have <= 0:
        return "У тебя нет такой находки."
    n = {"1": 1, "dup": have - 1, "all": have}[mode]
    if n <= 0:
        return "Дубликатов нет, оставляю единственную копию."
    n = min(n, have)
    gain = round(n * r["value"] * (1 + VIP["sell_bonus"] if is_vip(p) else 1))
    await take_item(conn, uid, f"relic:{key}", n)
    await conn.execute("update players set money=money+$2 where user_id=$1", uid, gain)
    await bump(conn, p, "sell_value", gain)
    return f"💸 Продано: {r['emoji']} {r['name']} × {n}\n+{gain} 💰"


async def relic_set_claim(conn, p, lk) -> str:
    uid = p["user_id"]
    loc = CFG["locations"][lk]
    found = {r["key"] for r in await conn.fetch("select key from relic_journal where user_id=$1", uid)}
    if not all(f"{lk}r{i}" in found for i in range(len(loc["relics"]))):
        return "Коллекция ещё не собрана."
    if await conn.fetchval("select 1 from inventory where user_id=$1 and item=$2", uid, f"set:{lk}"):
        return "Награда уже получена."
    await add_item(conn, uid, f"set:{lk}", 1)
    await conn.execute("update players set money=money+$2 where user_id=$1", uid, loc["set_reward"])
    return f"🎁 Коллекция «{loc['name']}» завершена!\n+{loc['set_reward']} 💰 и +{int(COLL['luck_per_set'] * 100)}% к удаче навсегда."


# ---------- экспедиции ----------
async def get_vehicle(conn, p):
    if not p["vehicle"]:
        return None
    return await conn.fetchrow("select * from vehicles where user_id=$1 and key=$2", p["user_id"], p["vehicle"])


def exp_duration(p, veh, route) -> float:
    v = CFG["vehicles"][veh["key"]]
    t = ROUTES[route]["minutes"] * 60 * v["time_mult"] * (1 - MODS["engine"]["per"] * veh["engine"])
    return t * (1 - VIP["cooldown_cut"]) if is_vip(p) else t


def exp_haul_n(veh, route) -> int:
    return max(1, round(CFG["vehicles"][veh["key"]]["haul"] * ROUTES[route]["haul_mult"])) + veh["cargo"] // 2


def mod_price(veh, mk) -> int:
    return round(MODS[mk]["base_price"] * CFG["vehicles"][veh["key"]]["module_mult"] * (veh[mk] + 1))


async def exp_start(conn, p, route) -> str:
    uid = p["user_id"]
    veh = await get_vehicle(conn, p)
    if not veh:
        return "У тебя нет транспорта. Купи его в магазине."
    if await conn.fetchrow("select 1 from expeditions where user_id=$1", uid):
        return "Экспедиция уже в пути."
    r = ROUTES[route]
    if level_of(p["xp"]) < r["min_level"]:
        return f"🔒 {r['name']} доступна с {r['min_level']} уровня."
    if not await take_item(conn, uid, "fuel:топливо", r["fuel"]):
        return f"⛽ Не хватает топлива: нужно {r['fuel']}. Купи его в магазине."
    dur = exp_duration(p, veh, route)
    await conn.execute(
        "insert into expeditions(user_id,vehicle,route,loc,started,ready) values($1,$2,$3,$4,$5,$6)",
        uid, veh["key"], route, p["loc"], now(), now() + dur)
    return (f"🚜 Экспедиция отправилась!\n📍 Локация: {loc_label(p['loc'])}\n"
            f"🚜 {CFG['vehicles'][veh['key']]['name']} · {r['name']}\n"
            f"⏱ Вернётся через {fmt_t(dur)}\n⛽ Потрачено топлива: {r['fuel']}")


async def exp_claim(conn, p):
    uid = p["user_id"]
    ex = await conn.fetchrow("select * from expeditions where user_id=$1", uid)
    if not ex:
        return [("Сейчас нет экспедиции в пути.", None)]
    if ex["ready"] > now():
        return [(f"🚜 Экспедиция ещё в пути. Осталось: {fmt_t(ex['ready'] - now())}", None)]
    veh = await conn.fetchrow("select * from vehicles where user_id=$1 and key=$2", uid, ex["vehicle"])
    loc_key = ex["loc"]
    loc = CFG["locations"][loc_key]
    finds = loc["finds"]
    route = ROUTES[ex["route"]]
    vip = is_vip(p)

    luck = route["luck"] * (1 + MODS["radar"]["per"] * veh["radar"])
    luck *= 1 + COLL["luck_per_set"] * await collector_sets(conn, uid)
    luck *= 1 + VIP["luck_bonus"] if vip else 1
    n = exp_haul_n(veh, ex["route"])
    lost_n = 0
    if random.random() < max(0.0, EXPC["loss_chance"] - MODS["hull"]["per"] * veh["hull"]):
        lost_n = max(1, round(n * EXPC["loss_cut"]))
        n = max(1, n - lost_n)

    weights = [f["w"] * (luck if f["r"] >= 2 else 1.0) for f in finds]
    kgm = 1 + MODS["cargo"]["per"] * veh["cargo"]
    got, rare, xp_total, relics, chests = {}, 0, 0.0, [], {}
    for _ in range(n):
        i = random.choices(range(len(finds)), weights=weights)[0]
        f = finds[i]
        kg = round(random.uniform(f["wmin"], f["wmax"]) * kgm, 3)
        g = got.setdefault(i, [0, 0.0, 0.0])
        g[0] += 1
        g[1] += kg
        g[2] = max(g[2], kg)
        xp_total += f["xp"] * EXPC["xp_mult"]
        rare += 1 if f["r"] >= 2 else 0
        rr = await roll_relic(conn, uid, loc_key, luck, EXPC["relic_mult"])
        if rr:
            relics.append(rr)
        ct = await roll_chest(conn, uid, loc_key, luck, CHC["exp_mult"])
        if ct:
            chests[ct] = chests.get(ct, 0) + 1
    xp_gain = round(xp_total * (1 + VIP["xp_bonus"] if vip else 1))

    total_kg = total_val = 0.0
    for i, (cnt, kg, best) in got.items():
        f = finds[i]
        await add_item(conn, uid, f"find:{loc_key}{i}", round(kg, 3))
        await conn.execute(
            "insert into dig_journal(user_id,item,cnt,best_kg,best_r) values($1,$2,$3,$4,$5) "
            "on conflict (user_id,item) do update set cnt=dig_journal.cnt+$3, "
            "best_kg=greatest(dig_journal.best_kg,$4), best_r=greatest(dig_journal.best_r,$5)",
            uid, f"{loc_key}{i}", cnt, best, f["r"])
        total_kg += kg
        total_val += kg * f["ppk"]
    await conn.execute("delete from expeditions where user_id=$1", uid)
    await conn.execute("update players set xp=xp+$2 where user_id=$1", uid, xp_gain)
    await bump(conn, p, "kg", total_kg)
    if rare:
        await bump(conn, p, "rare", rare)

    top = sorted(got.items(), key=lambda kv: (-finds[kv[0]]["r"], finds[kv[0]]["name"]))
    L = ["🚜 Экспедиция вернулась!", f"📍 Локация: {loc_label(loc_key)}",
         f"🚜 {CFG['vehicles'][veh['key']]['name']} · {route['name']}",
         f"📦 Привезено находок: {n} · {total_kg:.2f} кг"]
    for i, (cnt, kg, best) in top[:8]:
        f = finds[i]
        L.append(f"• {f['emoji']} {f['name']} × {cnt} · {kg:.2f} кг · {RARITY[f['r']].split(' ')[0]}")
    if len(top) > 8:
        L.append(f"…и ещё {len(top) - 8} видов")
    L += [f"💰 Ценность груза ≈ {round(total_val)}", f"⭐ XP: +{xp_gain}"]
    if lost_n:
        L.append(f"🛠 В пути случилась поломка: потеряно находок: {lost_n}")
    new_any = False
    for key, new in relics:
        r = RELICS[key]
        new_any = new_any or new
        L.append(f"🏺 Коллекционная находка: {r['emoji']} {r['name']} · ценность {r['value']}" + (" 🆕" if new else ""))
    if chests:
        L.append("📦 Сундуки: " + ", ".join(f"{CH[t]['emoji']} {CH[t]['name']} ×{n}" for t, n in sorted(chests.items()))
                 + " (открыть: Меню → 📦 Сундуки)")
    if new_any and await relic_found_count(conn, uid, loc_key) == len(loc["relics"]):
        L.append("🎉 Коллекция локации собрана! Награда в разделе «Коллекция».")
    out = [("\n".join(L), None)]
    old, new_lvl = level_of(p["xp"]), level_of(p["xp"] + xp_gain)
    if new_lvl > old:
        unlocked = [f"{v['emoji']} {v['name']}" for v in CFG["locations"].values()
                    if old < v["min_level"] <= new_lvl]
        extra = f"\nОткрыта локация: {', '.join(unlocked)}" if unlocked else ""
        out.append((f"🎉 Новый уровень: {new_lvl}!{extra}", None))
    return out


async def mod_upgrade(conn, p, mk) -> str:
    if mk not in MODS:
        return "Неизвестный модуль."
    veh = await get_vehicle(conn, p)
    if not veh:
        return "Нет транспорта."
    m = MODS[mk]
    if veh[mk] >= m["max"]:
        return "Модуль уже на максимальном уровне."
    price = mod_price(veh, mk)
    money = await conn.fetchval("select money from players where user_id=$1", p["user_id"])
    if money < price:
        return f"Не хватает денег: нужно {price}, у тебя {money}."
    await conn.execute("update players set money=money-$2 where user_id=$1", p["user_id"], price)
    await conn.execute(f"update vehicles set {mk}={mk}+1 where user_id=$1 and key=$2", p["user_id"], veh["key"])
    return (f"✅ {m['name']}: уровень {veh[mk] + 1}/{m['max']}\n"
            f"Списано {price} 💰 · остаток {money - price} 💰")


async def exp_screen(conn, p, note=""):
    uid = p["user_id"]
    pre = (note + "\n\n") if note else ""
    veh = await get_vehicle(conn, p)
    if not veh:
        return (pre + "🚜 ЭКСПЕДИЦИИ\nТранспорт с командой копает за тебя, пока ты занят, и привозит целую "
                "партию находок. Для этого нужен транспорт и топливо.",
                kb([[("🛒 Купить транспорт", mcd(uid, "cat", "vehicles"))], [("🏠 Меню", mcd(uid, "home"))]]))
    fuel = int(await conn.fetchval("select qty from inventory where user_id=$1 and item=$2", uid, "fuel:топливо") or 0)
    ex = await conn.fetchrow("select * from expeditions where user_id=$1", uid)
    v = CFG["vehicles"][veh["key"]]
    L = ["🚜 ЭКСПЕДИЦИИ", f"Транспорт: {v['name']}",
         f"⚙ {veh['engine']} · 📡 {veh['radar']} · 📦 {veh['cargo']} · 🛡 {veh['hull']}",
         f"⛽ Топливо: {fuel}", f"📍 Локация: {loc_label(p['loc'])}", ""]
    rows = []
    if ex:
        r = ROUTES[ex["route"]]
        if ex["ready"] > now():
            L.append(f"🚜 В пути: {r['name']} ({loc_label(ex['loc'])})\n⏱ Осталось: {fmt_t(ex['ready'] - now())}")
            rows.append([("🔄 Обновить", mcd(uid, "exp"))])
        else:
            L.append("✅ Экспедиция вернулась! Забери добычу.")
            rows.append([("📦 Забрать добычу", mcd(uid, "expclaim"))])
    else:
        lvl = level_of(p["xp"])
        for rk, r in ROUTES.items():
            ok = lvl >= r["min_level"]
            L.append(f"{'' if ok else '🔒 '}• {r['name']}: {fmt_t(exp_duration(p, veh, rk))}, ⛽{r['fuel']}, "
                     f"~{exp_haul_n(veh, rk)} находок" + ("" if ok else f" (Lv.{r['min_level']})"))
            if ok:
                rows.append([(f"▶ {r['name']} · ⛽{r['fuel']}", mcd(uid, "expgo", rk))])
        L.append("\nЛокацию экспедиции выбирай в разделе «Локации».")
    rows += [[("🔧 Модули", mcd(uid, "expmod")), ("🚜 Транспорт", mcd(uid, "expveh"))],
             [("⛽ Купить топливо", mcd(uid, "item", "топливо")), ("🏠 Меню", mcd(uid, "home"))]]
    return pre + "\n".join(L), kb(rows)


async def mod_screen(conn, p, note=""):
    uid = p["user_id"]
    veh = await get_vehicle(conn, p)
    back = [("⬅ Экспедиции", mcd(uid, "exp")), ("🏠 Меню", mcd(uid, "home"))]
    if not veh:
        return "Сначала купи транспорт.", kb([back])
    money = await conn.fetchval("select money from players where user_id=$1", uid)
    L = ([note, ""] if note else []) + [f"🔧 МОДУЛИ · {CFG['vehicles'][veh['key']]['name']}", f"💰 Баланс: {money}", ""]
    rows = []
    for mk, m in MODS.items():
        lvl = veh[mk]
        if lvl >= m["max"]:
            L.append(f"{m['name']} · ур. {lvl}/{m['max']} (максимум)\n   {m['desc']}")
        else:
            price = mod_price(veh, mk)
            L.append(f"{m['name']} · ур. {lvl}/{m['max']}\n   {m['desc']}\n   Следующий уровень: {price} 💰")
            rows.append([(f"⬆ {m['name']} → {lvl + 1} · {price}💰", mcd(uid, "modup", mk))])
    return "\n".join(L), kb(rows + [back])


async def veh_screen(conn, p):
    uid = p["user_id"]
    rows_db = await conn.fetch("select key from vehicles where user_id=$1 order by key", uid)
    btns = [(f"{'✅ ' if r['key'] == p['vehicle'] else ''}{CFG['vehicles'][r['key']]['name']}",
             mcd(uid, "expsel", r["key"])) for r in rows_db]
    rows = [[b] for b in btns] + [[("🛒 Купить транспорт", mcd(uid, "cat", "vehicles"))],
                                  [("⬅ Экспедиции", mcd(uid, "exp")), ("🏠 Меню", mcd(uid, "home"))]]
    return "🚜 ТРАНСПОРТ\nВыбери, на чём отправлять экспедиции (модули у каждого свои):", kb(rows)


# ---------- VIP, бонус, донат ----------
def vip_perks() -> str:
    v = VIP
    return (f"перезарядка раскопки −{int(v['cooldown_cut'] * 100)}% · удача +{int(v['luck_bonus'] * 100)}% · "
            f"+{int(v['xp_bonus'] * 100)}% к XP · +{int(v['sell_bonus'] * 100)}% к цене продажи · "
            f"ежедневный бонус ×{v['daily_mult']}")


async def credit_stars(conn, uid, amount, reason) -> int:
    new = await conn.fetchval("update players set stars=stars+$2 where user_id=$1 returning stars", uid, amount)
    await conn.execute("insert into star_log(created,user_id,delta,reason) values($1,$2,$3,$4)",
                       now(), uid, amount, reason)
    return new


async def spend_stars(conn, uid, amount, reason):
    """Списывает ⭐ с игрового баланса. Вернёт остаток или None, если не хватает."""
    new = await conn.fetchval(
        "update players set stars=stars-$2 where user_id=$1 and stars>=$2 returning stars", uid, amount)
    if new is None:
        return None
    await conn.execute("insert into star_log(created,user_id,delta,reason) values($1,$2,$3,$4)",
                       now(), uid, -amount, reason)
    return new


async def cmd_vip(conn, p, a):
    head = ("👑 VIP активен" + vip_line(p).replace("\n👑 VIP", "")) if is_vip(p) else "👑 VIP не активен"
    bal = int(await conn.fetchval("select stars from players where user_id=$1", p["user_id"]) or 0)
    L = [head, "", "Бонусы: " + vip_perks(), "", f"⭐ Твой баланс: {bal}", "Купить за звёзды:"]
    for k, pr in CFG["stars_products"].items():
        if "vip_days" in pr:
            L.append(f"• {pr['title']}: ⭐{pr['stars']}")
    L.append("\nВыбери срок кнопкой ниже. Повторная покупка продлевает срок.")
    return [("\n".join(L), None)]


async def cmd_stars(conn, p, a):
    if not a or a[0] not in CFG["stars_products"]:
        return [("Выбери товар в разделе «Звёзды».", None)]
    code = a[0]
    pr = CFG["stars_products"][code]
    if pr.get("credit"):                              # пополнение баланса: настоящий счёт Telegram Stars
        inv = build_invoice(p, f"prod|{code}")
        return [inv] if isinstance(inv, dict) else [(inv, None)]
    left = await spend_stars(conn, p["user_id"], pr["stars"], f"товар: {pr['title']}")
    if left is None:
        bal = int(await conn.fetchval("select stars from players where user_id=$1", p["user_id"]) or 0)
        return [(f"⭐ Не хватает звёзд: нужно {pr['stars']}, у тебя {bal}. Пополни баланс кнопками выше.", None)]
    row = await conn.fetchrow("select * from players where user_id=$1", p["user_id"])
    text = await apply_product(conn, row, code, "✅ Куплено за ⭐: ")
    return [(f"{text}\n⭐ Остаток: {left}", None)]


async def cmd_bonus(conn, p, a):
    if p["last_bonus"] == today():
        return [("🎁 Сегодняшний бонус уже получен. Приходи завтра (обновление по UTC).", None)]
    amount = 150 + 10 * level_of(p["xp"])
    if is_vip(p):
        amount *= VIP["daily_mult"]
    await conn.execute("update players set money=money+$2, last_bonus=$3 where user_id=$1",
                       p["user_id"], amount, today())
    return [(f"🎁 Ежедневный бонус: +{amount} 💰" + (f" (VIP ×{VIP['daily_mult']})" if is_vip(p) else ""), None)]


async def apply_product(conn, p, code, header) -> str:
    pr = CFG["stars_products"][code]
    uid = p["user_id"]
    L = [f"{header}{pr['title']}"]
    if pr.get("credit"):
        new = await credit_stars(conn, uid, pr["credit"], "пополнение через Telegram Stars")
        L.append(f"⭐ Баланс: {new}")
    if "vip_days" in pr:
        cur = max(now(), p["vip_until"])
        new_v = cur + pr["vip_days"] * 86400
        await conn.execute("update players set vip_until=$2 where user_id=$1", uid, new_v)
        L.append("👑 VIP до " + time.strftime("%d.%m.%Y", time.gmtime(new_v)))
    if "money" in pr:
        await conn.execute("update players set money=money+$2 where user_id=$1", uid, pr["money"])
        L.append(f"💰 +{pr['money']}")
    if "item" in pr:
        kind, key = pr["item"].split(":", 1)
        await deliver_item(conn, uid, dict(kind=kind, key=key, qty=pr["qty"]))
        L.append(f"📦 {item_name(pr['item'])} × {pr['qty']}")
    return "\n".join(L)


async def fulfil(conn, p, spec: str) -> str:
    parts = spec.split("|")
    if parts[0] == "buy":
        it = resolve_item(parts[1], int(parts[2]))
        note = await deliver_item(conn, p["user_id"], it)
        return f"✅ Оплата получена: {it['label']}{note}"
    return await apply_product(conn, p, parts[1], "✅ Оплата получена: ")


# ---------- тексты экранов ----------
async def cmd_help(conn, p, a):
    return [(
        "⛏ КЛАДОИСКАТЕЛИ — как играть\n\n"
        "• Напиши «Рыть» — копаешь на выбранной локации. Находки разной редкости, "
        "перезарядка между раскопками зависит от лопаты.\n"
        "• «Меню» — локации, снаряжение, находки, магазин, миссии и всё остальное кнопками.\n"
        "• Находки продаются за монеты. На монеты покупаются лопаты (меньше перезарядка, больше удачи), "
        "металлоискатели (удача, изнашиваются) и карты (расходник, повышают шанс редких находок).\n"
        "• 🏺 «Коллекция»: редкие коллекционные находки выпадают дополнительно к обычной. Их можно хранить, "
        "продавать, а за собранную коллекцию локации дают награду и +удачу.\n"
        "• 🚜 «Экспедиции»: купи транспорт и топливо, отправь команду копать, пока ты занят, "
        "и забери целую партию находок. Модули улучшают скорость, удачу, груз и защиту от потерь.\n"
        "• 📦 «Сундуки» выпадают при раскопках и экспедициях. Для каждого сундука нужен свой ключ: "
        "ключи покупаются за монеты или ⭐ и иногда находятся в сундуках. Шансы наград видно в карточке сундука.\n"
        "• ⭐ «Звёзды» — игровой баланс. Пополняется через Telegram Stars, тратится на ключи, VIP и предметы.\n"
        "• Чем выше уровень, тем больше локаций открывается и тем ценнее находки.", None)]


async def cmd_profile(conn, p, a):
    lvl = level_of(p["xp"])
    return [(
        f"👤 {p['name']}\n🏅 Уровень: {lvl} (XP {p['xp']}, до след.: {xp_left(p['xp'])})\n"
        f"💰 Баланс: {p['money']} · ⭐ Звёзды: {p['stars']}{vip_line(p)}\n"
        f"⛏ Раскопок всего: {p['digs']}\n"
        + await profile_extra(conn, p) + "\n" + loadout_text(p), None)]


async def profile_extra(conn, p) -> str:
    uid = p["user_id"]
    found = int(await conn.fetchval("select count(*) from relic_journal where user_id=$1", uid) or 0)
    L = f"🏺 Коллекция: {found}/{len(RELICS)}\n"
    veh = await get_vehicle(conn, p)
    if veh:
        fuel = int(await conn.fetchval("select qty from inventory where user_id=$1 and item=$2", uid, "fuel:топливо") or 0)
        L += f"🚜 Транспорт: {CFG['vehicles'][veh['key']]['name']} · ⛽ {fuel}\n"
    return L


# ---------- Telegram ----------
FLOOD: dict[int, list[float]] = {}
FLOOD_CB: dict[int, list[float]] = {}


def flood_count(uid: int, cb: bool = False) -> int:
    """Сколько действий игрок сделал за последние 30 с (команды и кнопки считаются отдельно)."""
    store = FLOOD_CB if cb else FLOOD
    t = now()
    q = [x for x in store.get(uid, []) if t - x < 30]
    q.append(t)
    store[uid] = q
    return len(q)


async def send(m: Message, text: str, img: str | None, markup=None):
    path = None
    if img:
        for ext in ("jpg", "png", "webp"):
            f = IMG_DIR / f"{img}.{ext}"
            if f.exists():
                path = f
                break
    head, tail = text, ""
    if path and len(text) > 1024:                    # подпись к фото ограничена: остаток уйдёт вторым сообщением
        cut_at = text.rfind("\n\n", 0, 1024)
        if cut_at < 200:
            cut_at = 1024
        head, tail = text[:cut_at], text[cut_at:].lstrip("\n")
    for html_on in ([True, False] if EMOJI_RE else [False]):
        body = decorate(head) if html_on else head
        pm = "HTML" if html_on else None
        mk = None if tail else markup
        try:
            if path:
                await m.reply_photo(FSInputFile(path), caption=body, parse_mode=pm, reply_markup=mk)
            else:
                await m.reply(body, parse_mode=pm, reply_markup=mk)
            break
        except TelegramBadRequest as e:
            if not html_on:
                raise
            logging.warning("отправка с премиум-эмодзи не удалась, шлю обычным текстом: %s", e)
    if tail:
        await send(m, tail, None, markup)


async def send_invoice(m: Message, inv: dict, uid: int | None = None):
    uid = uid or m.from_user.id
    try:
        await m.bot.send_invoice(
            chat_id=uid, title=inv["title"], description=inv["desc"],
            payload=inv["payload"], currency="XTR",
            prices=[LabeledPrice(label=inv["title"], amount=inv["stars"])])
        if m.chat.type != "private":
            await m.reply("💳 Счёт отправлен тебе в личные сообщения с ботом.")
    except TelegramForbiddenError:
        username = (await m.bot.me()).username
        link = f"https://t.me/{username}?start=pay_{b64e(inv['spec'])}"
        await m.reply(f"💳 Бот ещё не может написать тебе в личку. Открой бота и нажми Start, "
                      f"счёт придёт сразу:\n{link}")
    except TelegramBadRequest as e:
        logging.warning("invoice failed: %s", e)
        await m.reply("Не удалось создать счёт, попробуй позже.")


SCREENS = {
    "меню": "home", "menu": "home", "помощь": "help",
    "локации": "loc", "локация": "loc", "снаряжение": "gear", "находки": "stock",
    "сумка": "stock", "улов": "stock", "склад": "stock", "магазин": "shop",
    "сундуки": "chests", "сундук": "chests", "ключи": "chests", "коллекция": "rel", "реликвии": "rel", "экспедиции": "exp", "экспедиция": "exp", "рейс": "exp",
    "профиль": "profile", "миссии": "missions", "бонус": "bonus", "журнал": "journal",
    "вип": "vip", "vip": "vip", "донат": "donat", "звёзды": "donat", "звезды": "donat", "⭐": "donat",
}
DIG_WORDS = {"рыть", "копать", "копай", "раскопки", "dig"}


async def on_text(m: Message):
    """«Рыть» копает прямо в чате, остальные слова открывают экраны с кнопками."""
    if not m.from_user or m.from_user.is_bot or not m.text:
        return
    parts = m.text.strip().lower().split()
    if not parts:
        return
    word = parts[0]
    is_dig = word in DIG_WORDS
    screen = SCREENS.get(word)
    if not is_dig and not screen:
        return
    n = flood_count(m.from_user.id)
    if n > 10:
        if n == 11:
            await m.reply("🛑 Слишком быстро! Подожди пару секунд.")
        return
    async with LOCK, pool.acquire() as conn:
        async with conn.transaction():
            p = await get_player(conn, m)
            if p["banned"]:
                return
            if is_dig:
                out = await do_dig(conn, p)
            else:
                text, markup, invs, extras = await menu_screen(conn, p, screen, [])
    if is_dig:
        for t, img in out:
            await send(m, t, img)
    else:
        await send(m, text, None, markup)


# ---------- платежи Telegram Stars ----------
async def on_start(m: Message, command: CommandObject):
    arg = command.args or ""
    if arg.startswith("pay_"):
        try:
            spec = b64d(arg[4:])
        except Exception:
            spec = None
        if spec:
            async with LOCK, pool.acquire() as conn:
                p = await get_player(conn, m)
            inv = build_invoice(p, spec)
            if isinstance(inv, dict):
                await send_invoice(m, inv)
            else:
                await m.reply(inv)
            return
    await on_menu_cmd(m)


async def on_pre_checkout(q: PreCheckoutQuery):
    ok = False
    try:
        uid_s, spec = q.invoice_payload.split("|", 1)
        parts = spec.split("|")
        if int(uid_s) == q.from_user.id and q.currency == "XTR":
            if parts[0] == "buy":
                it = resolve_item(parts[1], int(parts[2]))
                ok = bool(it) and it["stars"] == q.total_amount
            elif parts[0] == "prod":
                pr = CFG["stars_products"].get(parts[1])
                ok = bool(pr) and pr["stars"] == q.total_amount
    except Exception:
        ok = False
    await q.answer(ok=ok, error_message=None if ok else "Товар недоступен или цена изменилась. Запроси счёт заново.")


async def on_paid(m: Message):
    sp = m.successful_payment
    charge, uid = sp.telegram_payment_charge_id, m.from_user.id
    async with pool.acquire() as conn:
        added = await conn.fetchval(
            "insert into payments(charge_id,user_id,payload,stars,created) values($1,$2,$3,$4,$5) "
            "on conflict do nothing returning charge_id",
            charge, uid, sp.invoice_payload, sp.total_amount, now())
    if not added:
        return                                   # уже обработан
    try:
        spec = sp.invoice_payload.split("|", 1)[1]
        async with LOCK, pool.acquire() as conn:
            async with conn.transaction():
                p = await get_player(conn, m)
                text = await fulfil(conn, p, spec)
                await conn.execute("update payments set delivered=true where charge_id=$1", charge)
        await send(m, text, None)
    except Exception:
        logging.exception("выдача после оплаты не удалась: %s", charge)
        await m.answer("⚠️ Оплата получена, но выдача не прошла. Напиши /paysupport, "
                       f"админ всё исправит или вернёт ⭐.\nНомер платежа: {charge}")
        if ADMIN_ID:
            await m.bot.send_message(ADMIN_ID, f"⚠️ Не выдано: user {uid}, {sp.invoice_payload}, "
                                               f"⭐{sp.total_amount}, charge {charge}")


async def on_paysupport(m: Message):
    await m.reply(f"💳 Вопросы по оплате и возвраты ⭐: {SUPPORT}.\n"
                  "Опиши, что купил и когда. Возврат делается по запросу.")


async def on_terms(m: Message):
    await m.reply("📄 Условия: все покупки за ⭐ — виртуальные предметы внутри игры, "
                  "не имеют денежной стоимости и не обмениваются на деньги. ⭐ звёзды в игре — внутренняя валюта: пополняется через Telegram Stars 1 к 1 и не выводится. Содержимое сундуков случайное, шансы показаны в игре. Игра вымышленная. "
                  "Возврат — через /paysupport. Мошенничество и абьюз ведут к блокировке.")


async def on_payments(m: Message):
    if not is_admin(m.from_user.id):
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch("select * from payments order by created desc limit 10")
    if not rows:
        await m.reply("Платежей пока нет.")
        return
    L = [f"{r['charge_id']}\n  user {r['user_id']} · ⭐{r['stars']} · {r['payload'].split('|', 1)[1]} · "
         f"{'выдано' if r['delivered'] else 'НЕ ВЫДАНО'}{' · возврат' if r['refunded'] else ''}"
         for r in rows]
    await m.reply("\n".join(L))


async def on_refund(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return
    charge = (command.args or "").strip()
    async with pool.acquire() as conn:
        r = await conn.fetchrow("select * from payments where charge_id=$1", charge)
        if not r or r["refunded"]:
            await m.reply("Платёж не найден или уже возвращён. Список: /payments")
            return
        try:
            await m.bot.refund_star_payment(user_id=r["user_id"], telegram_payment_charge_id=charge)
        except Exception as e:
            await m.reply(f"Не удалось вернуть ⭐: {e}")
            return
        await conn.execute("update payments set refunded=true where charge_id=$1", charge)
        note = ""
        parts = (r["payload"] or "").split("|")
        if len(parts) == 3 and parts[1] == "prod":
            pr = CFG["stars_products"].get(parts[2])
            if pr and pr.get("credit"):
                new = await conn.fetchval(
                    "update players set stars=stars-$2 where user_id=$1 returning stars", r["user_id"], pr["credit"])
                await conn.execute("insert into star_log(created,user_id,delta,reason) values($1,$2,$3,$4)",
                                   now(), r["user_id"], -pr["credit"], f"возврат платежа {charge}")
                note = f"\nИгровой баланс ⭐ уменьшен на {pr['credit']} (сейчас {new})."
    await m.reply("✅ ⭐ возвращены." + note + "\nДругое выданное в игре (VIP, монеты, предметы) при необходимости отними вручную.")


# ---------- кнопочное меню ----------
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def mcd(uid, *parts) -> str:
    return "|".join(["m", str(uid), *[str(x) for x in parts]])


def acd(uid, *parts) -> str:
    return "|".join(["a", str(uid), *[str(x) for x in parts]])


def chunk(btns, n=2):
    return [btns[i:i + n] for i in range(0, len(btns), n)]


def home_text(p) -> str:
    return (f"⛏ КЛАДОИСКАТЕЛИ{' 👑' if is_vip(p) else ''}\n"
            f"👤 {p['name']} · 🏅 Lv.{level_of(p['xp'])} · 💰 {p['money']} · ⭐ {p['stars']}\n"
            f"📍 {loc_label(p['loc'])}\n\nВыбери раздел (а копать можно командой «Рыть» в чате):")


def kb_home(uid: int):
    rows = [
        [("⛏ Рыть", mcd(uid, "dig"))],
        [("📍 Локации", mcd(uid, "loc")), ("🎒 Снаряжение", mcd(uid, "gear"))],
        [("💎 Находки", mcd(uid, "stock")), ("🏺 Коллекция", mcd(uid, "rel"))],
        [("📦 Сундуки", mcd(uid, "chests")), ("🚜 Экспедиции", mcd(uid, "exp"))],
        [("🛒 Магазин", mcd(uid, "shop")), ("🎯 Миссии", mcd(uid, "missions"))],
        [("📖 Журнал", mcd(uid, "journal")), ("👤 Профиль", mcd(uid, "profile"))],
        [("🎁 Бонус", mcd(uid, "bonus")), ("👑 VIP", mcd(uid, "vip"))],
        [("⭐ Звёзды", mcd(uid, "donat")), ("❓ Помощь", mcd(uid, "help"))],
    ]
    if is_admin(uid):
        rows.append([("🛠 Админ-панель", acd(uid, "home"))])
    return kb(rows)


def kb_back(uid: int):
    return kb([[("🏠 Меню", mcd(uid, "home"))]])


def out_text(out) -> str:
    texts = [x[0] for x in out if isinstance(x, tuple)]
    if not texts and any(isinstance(x, dict) for x in out):
        return "💳 Счёт отправлен в личные сообщения с ботом."
    return ("\n\n".join(texts) or "Готово.")[:3900]


def item_lines(cat, s_):
    if cat == "tools":
        return [f"⏱ Перезарядка раскопки: {s_['cooldown_min']} мин",
                f"🍀 Удача (шанс редких находок): ×{s_['luck']} · нужен Lv.{s_['min_level']}"]
    if cat == "dets":
        return [f"🍀 Удача: +{int(s_['luck'] * 100)}% · прочность {s_['durability']} раскопок · Lv.{s_['min_level']}",
                "Запасной детектор заменит сломанный сам"]
    if cat == "vehicles":
        return [f"Команда копает за тебя, пока ты занят: ~{s_['haul']} находок за обычную экспедицию",
                f"⏱ Скорость ×{round(1 / s_['time_mult'], 2)} · нужен Lv.{s_['min_level']}",
                "У каждого транспорта свои модули"]
    if cat == "fuels":
        return ["Тратится при старте экспедиции: обычная ⛽1, дальняя ⛽3"]
    if cat == "keys":
        c = CH[s_["chest"]]
        return [f"Открывает: {c['emoji']} {c['name']}", "Один ключ тратится на один сундук"]
    return [f"🍀 Шанс редких находок: ×{s_['luck']} · цена находки ×{s_['value']}",
            "Тратится по одной карте за раскопку"]


async def cat_screen(conn, uid, cat):
    p = await conn.fetchrow("select * from players where user_id=$1", uid)
    lvl = level_of(p["xp"])
    rows = []
    for key, s_ in CFG[cat].items():
        it = resolve_item(key)
        if not it:
            continue
        lock = "🔒 " if lvl < it["need"] else ""
        rows.append([(f"{lock}{s_['name']} · {it['price']}💰 · ⭐{it['stars']}", mcd(uid, "item", key))])
    rows.append([("⬅ Разделы", mcd(uid, "shop")), ("🏠 Меню", mcd(uid, "home"))])
    return f"{CAT_TITLES[cat]}\n💰 Баланс: {p['money']}\nВыбери товар:", kb(rows)


async def item_screen(conn, uid, key, note=""):
    cat = CAT_OF[key]
    s_ = CFG[cat][key]
    it = resolve_item(key)
    p = await conn.fetchrow("select * from players where user_id=$1", uid)
    L = ([note, ""] if note else []) + [f"{CAT_TITLES[cat]} · {s_['name']}", *item_lines(cat, s_), "",
         f"Цена: {it['price']} 💰 или ⭐{it['stars']}", f"💰 Твой баланс: {p['money']} · ⭐ {p['stars']}"]
    back = [("⬅ Раздел", mcd(uid, "cat", cat)), ("🏠 Меню", mcd(uid, "home"))]
    err = await check_buy(conn, p, it)
    if err:
        L.append("\n" + err)
        return "\n".join(L), kb([back])
    if it["kind"] in ("bait", "fuel", "key"):
        rows = [[(f"💰 ×{q}", mcd(uid, "buy", key, "m", q)) for q in (1, 5, 10)],
                [(f"⭐ ×{q}", mcd(uid, "buy", key, "s", q)) for q in (1, 5, 10)]]
    else:
        rows = [[("💰 Купить", mcd(uid, "buy", key, "m", 1)), ("⭐ Купить", mcd(uid, "buy", key, "s", 1))]]
    return "\n".join(L), kb(rows + [[("➕ Пополнить ⭐", mcd(uid, "donat"))], back])


async def stock_screen(conn, p, note=""):
    uid = p["user_id"]
    rows_db = await conn.fetch(
        "select item, qty from inventory where user_id=$1 and item like 'find:%'", uid)
    items = sorted(((FINDS[r["item"].split(":", 1)[1]], r["qty"]) for r in rows_db),
                   key=lambda x: (-x[0]["r"], x[0]["name"]))
    L = ["💎 НАХОДКИ"]
    if not items:
        L.append("Пока пусто. Напиши «Рыть» в чате!")
    total = 0
    for i, (f, qty) in enumerate(items):
        val = round(qty * f["ppk"])
        total += val
        if i < 40:
            L.append(f"{f['emoji']} {f['name']} · {RARITY[f['r']].split(' ')[0]} · {qty:.2f} кг · ≈{val} 💰")
    if len(items) > 40:
        L.append(f"…и ещё {len(items) - 40}")
    if items:
        L.append(f"\nВсего ≈ {total} 💰")
    rows = []
    if items:
        rows += [[("💸 Продать всё", mcd(uid, "sell", "all"))],
                 [("💸 Мусор и обычные", mcd(uid, "sell", "low")), ("💸 Всё, кроме 🟠", mcd(uid, "sell", "nolegend"))]]
        top = [x for x in items if x[0]["r"] >= 3][:6]
        rows += chunk([(f"💸 {f['emoji']} {f['name']}", mcd(uid, "sell", f["key"])) for f, _ in top])
    rows.append([("🏠 Меню", mcd(uid, "home"))])
    return (note + "\n\n" if note else "") + "\n".join(L), kb(rows)


async def missions_screen(conn, p, note=""):
    uid = p["user_id"]
    rows_db = await ensure_missions(conn, p)
    L = ["🎯 МИССИИ НА СЕГОДНЯ", ""]
    for r, tag in zip(rows_db, ["Лёгкая", "Средняя", "Сложная"]):
        if r["claimed"]:
            st = "✅ получено"
        elif r["progress"] >= r["goal"]:
            st = "🎁 готово, жми «Забрать награды»"
        else:
            st = f"{fmt_n(r['progress'])}/{r['goal']}"
        L.append(f"{tag}: {r['text']}\n    {st} · награда {r['reward']} 💰")
    L.append("\nОбновление каждый день (по UTC).")
    rows = []
    if any(r["progress"] >= r["goal"] and not r["claimed"] for r in rows_db):
        rows.append([("🎁 Забрать награды", mcd(uid, "claim"))])
    rows.append([("🏠 Меню", mcd(uid, "home"))])
    return (note + "\n\n" if note else "") + "\n".join(L), kb(rows)


async def claim_missions(conn, p) -> str:
    rows_db = await ensure_missions(conn, p)
    ready = [r for r in rows_db if r["progress"] >= r["goal"] and not r["claimed"]]
    if not ready:
        return "Готовых наград пока нет."
    total = sum(r["reward"] for r in ready)
    for r in ready:
        await conn.execute("update dig_missions set claimed=true where user_id=$1 and day=$2 and idx=$3",
                           p["user_id"], today(), r["idx"])
    await conn.execute("update players set money=money+$2 where user_id=$1", p["user_id"], total)
    return f"🎁 Награды за миссии: +{total} 💰"


async def journal_screen(conn, p, loc_key=None):
    uid = p["user_id"]
    jr = {r["item"]: r for r in await conn.fetch("select * from dig_journal where user_id=$1", uid)}
    if loc_key is None:
        L = ["📖 ЖУРНАЛ КЛАДОИСКАТЕЛЯ (коллекция находок)", ""]
        btns = []
        total_f = total_d = 0
        for lk, loc in CFG["locations"].items():
            got = sum(1 for i in range(len(loc["finds"])) if f"{lk}{i}" in jr)
            total_f += len(loc["finds"])
            total_d += got
            L.append(f"{loc['emoji']} {loc['name']}: {got}/{len(loc['finds'])}")
            btns.append((f"{loc['emoji']} {loc['name']}", mcd(uid, "jloc", lk)))
        L.append(f"\nВсего найдено: {total_d}/{total_f} · раскопок: {p['digs']}")
        return "\n".join(L), kb(chunk(btns) + [[("🏠 Меню", mcd(uid, "home"))]])
    loc = CFG["locations"][loc_key]
    L = [f"📖 {loc['emoji']} {loc['name']}", ""]
    for i, f in enumerate(loc["finds"]):
        r = jr.get(f"{loc_key}{i}")
        if r:
            L.append(f"{f['emoji']} {f['name']} · {RARITY[f['r']].split(' ')[0]} · найдено {r['cnt']} · рекорд {r['best_kg']:.3f} кг")
        else:
            L.append(f"❔ ??? · {RARITY[f['r']].split(' ')[0]}")
    return "\n".join(L), kb([[("⬅ Журнал", mcd(uid, "journal")), ("🏠 Меню", mcd(uid, "home"))]])


async def menu_screen(conn, p, action, args):
    """Возвращает (текст, клавиатура, счета Stars, доп. сообщения для чата)."""
    uid = p["user_id"]
    lvl = level_of(p["xp"])
    invs, extras = [], []

    async def run(h, a=()):
        out = await h(conn, p, list(a))
        invs.extend(x for x in out if isinstance(x, dict))
        return out_text(out)

    def ret(text, markup):
        return text, markup, invs, extras

    if action == "home":
        return ret(home_text(p), kb_home(uid))
    if action == "help":
        return ret((await cmd_help(conn, p, []))[0][0], kb_back(uid))
    if action == "dig":
        extras.extend(await do_dig(conn, p))
        extras[:] = [(f"👤 {p['name']}\n{t}" if t.startswith(("⛏", "⏳")) else t, img) for t, img in extras]
        return ret(home_text(p), kb_home(uid))

    # ----- локации и снаряжение -----
    if action == "loc":
        btns = [(f"{'✅' if k == p['loc'] else ('🔒' if lvl < l['min_level'] else l['emoji'])} {l['name']} · Lv.{l['min_level']}",
                 mcd(uid, "locgo", k)) for k, l in CFG["locations"].items()]
        cur = CFG["locations"][p["loc"]]
        return ret(f"📍 ЛОКАЦИИ\nСейчас: {loc_label(p['loc'])}\n{cur['desc']}\nВыбери, где копать:",
                   kb(chunk(btns) + [[("🏠 Меню", mcd(uid, "home"))]]))
    if action == "locgo":
        loc = CFG["locations"][args[0]]
        if lvl < loc["min_level"]:
            return ret(f"🔒 {loc['name']} открывается с {loc['min_level']} уровня.", kb([[("⬅ Локации", mcd(uid, "loc"))]]))
        await conn.execute("update players set loc=$2 where user_id=$1", uid, args[0])
        p2 = await conn.fetchrow("select * from players where user_id=$1", uid)
        return ret(f"📍 Ты на локации: {loc['emoji']} {loc['name']}\n{loc['desc']}\n\nКопай командой «Рыть» в чате.",
                   kb([[("⛏ Рыть", mcd(uid, "dig"))], [("🎒 Снаряжение", mcd(uid, "gear")), ("🏠 Меню", mcd(uid, "home"))]]))
    if action == "gear":
        left = None
        if p["bait"]:
            left = await conn.fetchval("select qty from inventory where user_id=$1 and item=$2", uid, f"bait:{p['bait']}") or 0
        text = "🎒 СНАРЯЖЕНИЕ\n" + loadout_text(p, left) + f"\n⏱ Перезарядка: {fmt_t(cooldown_sec(p))}"
        return ret(text, kb([[("📍 Локация", mcd(uid, "loc")), ("🗺 Выбрать карту", mcd(uid, "baitm"))],
                             [("🛒 Купить снаряжение", mcd(uid, "shop")), ("🏠 Меню", mcd(uid, "home"))]]))
    if action == "baitm":
        rows_db = await conn.fetch(
            "select item, qty from inventory where user_id=$1 and item like 'bait:%' order by item", uid)
        btns = [(f"{'✅ ' if p['bait'] == r['item'][5:] else ''}🗺 {CFG['baits'][r['item'][5:]]['name']} ×{fmt_n(r['qty'])}",
                 mcd(uid, "baitset", r["item"][5:])) for r in rows_db]
        rows = chunk(btns) + [[("❌ Без карты", mcd(uid, "baitset", "none"))],
                              [("🛒 Купить карты", mcd(uid, "cat", "baits")), ("⬅ Снаряжение", mcd(uid, "gear"))]]
        return ret("🗺 Какую карту использовать при раскопках?\nКарта тратится по одной за раскопку и повышает шанс редких находок.", kb(rows))
    if action == "baitset":
        await conn.execute("update players set bait=$2 where user_id=$1", uid, None if args[0] == "none" else args[0])
        p2 = await conn.fetchrow("select * from players where user_id=$1", uid)
        return ret("🗺 Выбрано.\n\n" + loadout_text(p2), kb([[("⛏ Рыть", mcd(uid, "dig"))], [("🎒 Снаряжение", mcd(uid, "gear")), ("🏠 Меню", mcd(uid, "home"))]]))

    # ----- магазин -----
    if action == "shop":
        btns = [(t, mcd(uid, "cat", c)) for c, t in CAT_TITLES.items()]
        return ret(f"🛒 МАГАЗИН\n💰 Баланс: {p['money']}\nВыбери раздел:",
                   kb(chunk(btns, 3) + [[("🏠 Меню", mcd(uid, "home"))]]))
    if action == "cat":
        return ret(*await cat_screen(conn, uid, args[0]))
    if action == "item":
        return ret(*await item_screen(conn, uid, args[0]))
    if action == "buy":
        key, mode, qty = args[0], args[1], args[2]
        note = await run(cmd_buy, [key, qty] + (["звёзды"] if mode == "s" else []))
        return ret(*await item_screen(conn, uid, key, note))

    # ----- сундуки -----
    if action == "chests":
        return ret(*await chests_screen(conn, p))
    if action == "chopen":
        return ret(*await chests_screen(conn, p, await chest_open(conn, p, args[0])))
    if action == "chinfo":
        return ret(*await chest_info_screen(conn, p, args[0]))

    # ----- коллекция -----
    if action == "rel":
        return ret(*await rel_screen(conn, p))
    if action == "reloc":
        return ret(*await reloc_screen(conn, p, args[0]))
    if action == "relic":
        return ret(*await relic_screen(conn, p, args[0]))
    if action == "relsell":
        key, mode = args[0], args[1]
        have = (await relic_counts(conn, uid)).get(key, 0)
        n = {"1": 1, "dup": have - 1, "all": have}[mode]
        if have and n >= have and not (len(args) > 2 and args[2] == "yes"):
            r = RELICS[key]
            return ret(f"⚠️ Продать последнюю копию?\n{r['emoji']} {r['name']} · {r['value']} 💰\n"
                       "Она пропадёт из твоих запасов (запись в коллекции останется).",
                       kb([[("✅ Да, продать", mcd(uid, "relsell", key, mode, "yes")),
                            ("✖ Отмена", mcd(uid, "relic", key))]]))
        note = await relic_sell(conn, p, key, mode)
        return ret(*await reloc_screen(conn, p, RELICS[key]["loc"], note))
    if action == "relset":
        return ret(*await reloc_screen(conn, p, args[0], await relic_set_claim(conn, p, args[0])))

    # ----- экспедиции -----
    if action == "exp":
        return ret(*await exp_screen(conn, p))
    if action == "expgo":
        return ret(*await exp_screen(conn, p, await exp_start(conn, p, args[0])))
    if action == "expclaim":
        out = await exp_claim(conn, p)
        note = ""
        if out[0][0].startswith("🚜 Экспедиция вернулась"):
            extras.extend((f"👤 {p['name']}\n{t}" if t.startswith("🚜") else t, img) for t, img in out)
        else:
            note = out[0][0]
        p2 = await conn.fetchrow("select * from players where user_id=$1", uid)
        return ret(*await exp_screen(conn, p2, note or "📦 Добыча на складе: раздел «Находки»."))
    if action == "expmod":
        return ret(*await mod_screen(conn, p))
    if action == "modup":
        return ret(*await mod_screen(conn, p, await mod_upgrade(conn, p, args[0])))
    if action == "expveh":
        return ret(*await veh_screen(conn, p))
    if action == "expsel":
        await conn.execute("update players set vehicle=$2 where user_id=$1", uid, args[0])
        p2 = await conn.fetchrow("select * from players where user_id=$1", uid)
        return ret(*await exp_screen(conn, p2, "🚜 Транспорт выбран."))

    # ----- остальное -----
    if action == "stock":
        return ret(*await stock_screen(conn, p))
    if action == "sell":
        return ret(*await stock_screen(conn, p, await sell_items(conn, p, args[0])))
    if action == "missions":
        return ret(*await missions_screen(conn, p))
    if action == "claim":
        return ret(*await missions_screen(conn, p, await claim_missions(conn, p)))
    if action == "journal":
        return ret(*await journal_screen(conn, p))
    if action == "jloc":
        return ret(*await journal_screen(conn, p, args[0]))
    if action == "profile":
        return ret(await run(cmd_profile), kb_back(uid))
    if action == "bonus":
        return ret(await run(cmd_bonus), kb_back(uid))
    if action == "vip":
        text = await run(cmd_vip)
        rows = [[(f"⭐ {pr['title']} — {pr['stars']}", mcd(uid, "prod", k))]
                for k, pr in CFG["stars_products"].items() if "vip_days" in pr]
        rows.append([("➕ Пополнить ⭐", mcd(uid, "donat"))])
        return ret(text, kb(rows + [[("🏠 Меню", mcd(uid, "home"))]]))
    if action == "donat":
        return ret(*await stars_screen(conn, p))
    if action == "prod":
        note = await run(cmd_stars, [args[0]])
        return ret(*await stars_screen(conn, p, note))
    return ret(home_text(p), kb_home(uid))


async def edit_menu(cb: CallbackQuery, text: str, markup):
    for html_on in ([True, False] if EMOJI_RE else [False]):
        body = decorate(text) if html_on else text
        try:
            await cb.message.edit_text(body, reply_markup=markup,
                                       parse_mode="HTML" if html_on else None)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
            if not html_on:
                logging.warning("edit_menu: %s", e)
                return


async def on_menu_cmd(m: Message):
    if not m.from_user or m.from_user.is_bot:
        return
    async with LOCK, pool.acquire() as conn:
        p = await get_player(conn, m)
    if p["banned"]:
        return
    await send(m, home_text(p), None, kb_home(m.from_user.id))


async def on_menu_cb(cb: CallbackQuery):
    parts = cb.data.split("|")
    owner = int(parts[1])
    if cb.from_user.id != owner:
        await cb.answer("Это меню другого игрока. Напиши «Меню», чтобы открыть своё.", show_alert=True)
        return
    if flood_count(owner, cb=True) > 45:
        await cb.answer("Не так быстро!")
        return
    async with LOCK, pool.acquire() as conn:
        async with conn.transaction():
            p = await get_player(conn, cb)
            if p["banned"]:
                await cb.answer("Доступ закрыт.", show_alert=True)
                return
            text, markup, invs, extras = await menu_screen(conn, p, parts[2], parts[3:])
    await cb.answer()
    await edit_menu(cb, text, markup)
    for t, img in extras:
        await send(cb.message, t, img)
    for inv in invs:
        await send_invoice(cb.message, inv, uid=owner)


# ---------- админ-панель ----------
ADMIN_STATE: dict[int, dict] = {}


def _st(admin: int) -> dict:
    return ADMIN_STATE.setdefault(admin, {"target": None, "await": None})


def admin_waiting(m: Message) -> bool:
    return bool(m.from_user and is_admin(m.from_user.id) and _st(m.from_user.id)["await"])


async def log_admin(conn, admin, target, action):
    logging.info("ADMIN %s -> %s: %s", admin, target, action)
    await conn.execute("insert into admin_log(created,admin_id,target,action) values($1,$2,$3,$4)",
                       now(), admin, target, action)


def admin_item(key: str, qty: int):
    if key in CH:
        q = max(1, min(qty, 1000))
        return dict(kind="chest", key=key, qty=q, label=f"{CH[key]['emoji']} {CH[key]['name']} × {q}")
    if key in RELICS:
        return dict(kind="relic", key=key, qty=1, label=f"{RELICS[key]['emoji']} {RELICS[key]['name']}")
    for cat, kind, pf in CATS:
        s_ = CFG[cat].get(key)
        if s_:
            q = max(1, min(qty, 1000)) if kind in ("bait", "fuel", "key") else 1
            return dict(kind=kind, key=key, qty=q, label=f"{s_['name']} × {q}")
    return None


async def admin_apply(conn, admin, tgt, action, args) -> str:
    if action == "money":
        n = int(args[0])
        await conn.execute("update players set money=greatest(0, money+$2) where user_id=$1", tgt, n)
        note = f"💰 {n:+d}"
    elif action == "stars":
        n = int(args[0])
        new = await conn.fetchval(
            "update players set stars=greatest(0, stars+$2) where user_id=$1 returning stars", tgt, n)
        await conn.execute("insert into star_log(created,user_id,delta,reason) values($1,$2,$3,$4)",
                           now(), tgt, n, f"админ {admin}")
        note = f"⭐ Звёзды {n:+d} (баланс {new})"
    elif action == "vip":
        days = int(args[0])
        cur = await conn.fetchval("select vip_until from players where user_id=$1", tgt)
        new = max(now(), cur) + days * 86400
        await conn.execute("update players set vip_until=$2 where user_id=$1", tgt, new)
        note = f"👑 VIP +{days} дн. (до {time.strftime('%d.%m.%Y', time.gmtime(new))})"
    elif action == "vipoff":
        await conn.execute("update players set vip_until=0 where user_id=$1", tgt)
        note = "👑 VIP снят"
    elif action == "xp":
        await conn.execute("update players set xp=xp+$2 where user_id=$1", tgt, int(args[0]))
        note = f"⭐ XP {int(args[0]):+d}"
    elif action == "lvl":
        lv = max(1, min(int(args[0]), 500))
        await conn.execute("update players set xp=$2 where user_id=$1", tgt, 50 * (lv - 1) ** 2)
        note = f"⭐ Уровень установлен: {lv}"
    elif action == "item":
        it = admin_item(args[0], int(args[1]))
        if not it:
            return "❌ Такого предмета нет (ключи: карта1–3, k1–k5 ключи, c1–c5 сундуки, топливо, повозка, вездеход, экскаватор, стальная, титановая, алмазная, простой, профи, георадар, реликвии вида пляжr0)"
        await deliver_item(conn, tgt, it)
        note = f"📦 Выдано: {it['label']}"
    elif action == "reset_cd":
        await conn.execute("update players set last_dig=0 where user_id=$1", tgt)
        note = "⏱ Перезарядка раскопки сброшена"
    elif action == "giftdo":
        row = await conn.fetchrow("select * from players where user_id=$1", tgt)
        text = await fulfil(conn, row, f"prod|{args[0]}")
        note = "🎁 " + text.replace("✅ Оплата получена: ", "Подарок: ").replace("\n", " · ")
    elif action == "ban":
        await conn.execute("update players set banned=true where user_id=$1", tgt)
        note = "🚫 Игрок заблокирован"
    else:
        await conn.execute("update players set banned=false where user_id=$1", tgt)
        note = "✅ Игрок разблокирован"
    await log_admin(conn, admin, tgt, note)
    return "✅ " + note


async def admin_screen(conn, admin, action, args, note=""):
    st = _st(admin)
    tgt = st["target"]
    back = [("⬅ Админ", acd(admin, "home"))]

    if action == "home":
        st["await"] = None
        n = await conn.fetchval("select count(*) from players")
        v = await conn.fetchval("select count(*) from players where vip_until > $1", now())
        stars = await conn.fetchval("select coalesce(sum(stars),0) from payments where not refunded")
        text = (f"🛠 АДМИН-ПАНЕЛЬ\n👥 Игроков: {n} · 👑 VIP: {v}\n⭐ Принято Stars: {stars}")
        rows = [[("👤 Выбрать игрока", acd(admin, "pick"))]]
        if tgt:
            row = await conn.fetchrow("select name, username from players where user_id=$1", tgt)
            if row:
                text += f"\n🎯 Цель: {row['name']} (ID {tgt})"
                rows.insert(0, [("🎯 Карточка игрока", acd(admin, "card"))])
        rows += [[("💳 Платежи", acd(admin, "pay")), ("🧾 Журнал", acd(admin, "log"))],
                 [("🏠 Игровое меню", mcd(admin, "home"))]]
        return text, kb(rows)
    if action == "pick":
        st["await"] = "target"
        return ("👤 Пришли ID игрока, его @ник (если он уже играл) или перешли сюда его сообщение.\n"
                "В групповом чате можно ответить на сообщение игрока командой /admin."), kb([[("✖ Отмена", acd(admin, "home"))]])
    if action == "pay":
        rows_db = await conn.fetch("select * from payments order by created desc limit 10")
        L = ["💳 Последние платежи (возврат: /refund <номер>)", ""]
        for r in rows_db:
            L.append(f"{r['charge_id']}\n  user {r['user_id']} · ⭐{r['stars']} · {r['payload'].split('|', 1)[1]} · "
                     f"{'выдано' if r['delivered'] else 'НЕ ВЫДАНО'}{' · возврат' if r['refunded'] else ''}")
        if not rows_db:
            L.append("Платежей пока нет.")
        return "\n".join(L)[:3900], kb([back])
    if action == "log":
        rows_db = await conn.fetch("select * from admin_log order by id desc limit 15")
        L = ["🧾 Последние действия админов", ""]
        for r in rows_db:
            L.append(f"{time.strftime('%d.%m %H:%M', time.gmtime(r['created']))} · админ {r['admin_id']} → {r['target']}: {r['action']}")
        if not rows_db:
            L.append("Пока пусто.")
        return "\n".join(L)[:3900], kb([back])

    if action in ("moneyc", "starsc", "slog", "stars", "lvl", "item", "gift", "card", "money", "vip", "vipoff",
                  "xp", "giftdo", "ban", "unban", "reset_cd"):
        if not tgt:
            return "Сначала выбери игрока.", kb([[("👤 Выбрать игрока", acd(admin, "pick"))], back])
    if action == "moneyc":
        st["await"] = "money"
        return "💰 Пришли число монет (можно отрицательное, чтобы списать).", kb([[("✖ Отмена", acd(admin, "card"))]])
    if action == "starsc":
        st["await"] = "stars"
        return ("⭐ Пришли число звёзд для начисления (можно отрицательное, чтобы списать). "
                "Это игровая валюта, настоящие Telegram Stars так не создаются."), kb([[("✖ Отмена", acd(admin, "card"))]])
    if action == "slog":
        rows_db = await conn.fetch("select * from star_log where user_id=$1 order by id desc limit 12", tgt)
        L = ["🧾 ⭐ ЖУРНАЛ ЗВЁЗД ИГРОКА", ""]
        for r in rows_db:
            L.append(f"{time.strftime('%d.%m %H:%M', time.gmtime(r['created']))} · {r['delta']:+d} · {r['reason']}")
        if not rows_db:
            L.append("Движений нет.")
        return "\n".join(L), kb([[("⬅ Карточка", acd(admin, "card")), ("⬅ Админ", acd(admin, "home"))]])
    if action == "lvl" and not args:
        st["await"] = "lvl"
        return "🎚 Пришли номер уровня (1–500). Опыт будет установлен под этот уровень.", kb([[("✖ Отмена", acd(admin, "card"))]])
    if action == "item" and not args:
        st["await"] = "item"
        return ("📦 Пришли: <ключ> [кол-во], например «карта3 5» или «алмазная».\n"
                "Ключи: карта1–3, k1–k5 (ключи), c1–c5 (сундуки), топливо, стальная, титановая, алмазная, простой, профи, георадар."), kb([[("✖ Отмена", acd(admin, "card"))]])
    if action == "gift":
        btns = [(pr["title"], acd(admin, "giftdo", k)) for k, pr in CFG["stars_products"].items()]
        return ("🎁 Выдать игроку донат-пакет бесплатно (как после оплаты):",
                kb(chunk(btns) + [[("⬅ Карточка", acd(admin, "card"))]]))
    if action in ("money", "stars", "vip", "vipoff", "xp", "giftdo", "ban", "unban", "lvl", "item", "reset_cd"):
        note = await admin_apply(conn, admin, tgt, action, args)

    st["await"] = None
    r = await conn.fetchrow("select * from players where user_id=$1", tgt)
    if not r:
        return "Игрок не найден.", kb([back])
    vip = ("до " + time.strftime("%d.%m.%Y", time.gmtime(r["vip_until"]))) if r["vip_until"] > now() else "нет"
    text = ((note + "\n\n") if note else "") + (
        f"👤 {r['name']}" + (f" (@{r['username']})" if r["username"] else "") + f"\nID: {tgt}\n"
        f"🏅 Lv.{level_of(r['xp'])} (XP {r['xp']}) · 💰 {r['money']} · ⭐ {r['stars']}\n"
        f"👑 VIP: {vip} · 📍 {loc_label(r['loc'])} · ⛏ {r['digs']}"
        + ("\n🚫 ЗАБЛОКИРОВАН" if r["banned"] else ""))
    rows = [
        [("💰 +1 000", acd(admin, "money", 1000)), ("💰 +10 000", acd(admin, "money", 10000)),
         ("💰 +100 000", acd(admin, "money", 100000))],
        [("💰 Своё число", acd(admin, "moneyc")), ("⏱ Сбросить перезарядку", acd(admin, "reset_cd"))],
        [("⭐ +10", acd(admin, "stars", 10)), ("⭐ +50", acd(admin, "stars", 50)),
         ("⭐ +100", acd(admin, "stars", 100)), ("⭐ +500", acd(admin, "stars", 500))],
        [("⭐ Своё число", acd(admin, "starsc")), ("🧾 Журнал ⭐", acd(admin, "slog"))],
        [("👑 +7 дн", acd(admin, "vip", 7)), ("👑 +30 дн", acd(admin, "vip", 30)),
         ("👑 +90 дн", acd(admin, "vip", 90))],
        [("👑 Снять VIP", acd(admin, "vipoff"))],
        [("⭐ +100 XP", acd(admin, "xp", 100)), ("⭐ +1000 XP", acd(admin, "xp", 1000)),
         ("🎚 Уровень…", acd(admin, "lvl"))],
        [("📦 Выдать предмет", acd(admin, "item")), ("🎁 Донат-пакет", acd(admin, "gift"))],
        [("✅ Разбан", acd(admin, "unban")) if r["banned"] else ("🚫 Бан", acd(admin, "ban")),
         ("🔄 Обновить", acd(admin, "card"))],
        back,
    ]
    return text, kb(rows)


async def on_admin_cmd(m: Message):
    if not m.from_user or not is_admin(m.from_user.id):
        return
    admin = m.from_user.id
    st = _st(admin)
    action = "home"
    rep_m = m.reply_to_message
    async with LOCK, pool.acquire() as conn:
        async with conn.transaction():
            if rep_m and rep_m.from_user and not rep_m.from_user.is_bot:
                if await conn.fetchval("select 1 from players where user_id=$1", rep_m.from_user.id):
                    st["target"], action = rep_m.from_user.id, "card"
            text, markup = await admin_screen(conn, admin, action, [])
    await send(m, text, None, markup)


async def on_admin_cb(cb: CallbackQuery):
    parts = cb.data.split("|")
    owner = int(parts[1])
    if cb.from_user.id != owner or not is_admin(owner):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    async with LOCK, pool.acquire() as conn:
        async with conn.transaction():
            text, markup = await admin_screen(conn, owner, parts[2], parts[3:])
    await cb.answer()
    await edit_menu(cb, text, markup)


async def on_admin_input(m: Message):
    admin = m.from_user.id
    st = _st(admin)
    mode = st["await"]
    text = (m.text or "").strip()
    async with LOCK, pool.acquire() as conn:
        async with conn.transaction():
            if mode == "target":
                uid = None
                fo = getattr(m, "forward_origin", None)
                if fo is not None and getattr(fo, "sender_user", None):
                    uid = fo.sender_user.id
                elif text.lstrip("-").isdigit():
                    uid = int(text)
                elif text.startswith("@"):
                    uid = await conn.fetchval(
                        "select user_id from players where lower(username)=lower($1)", text[1:])
                if not uid or not await conn.fetchval("select 1 from players where user_id=$1", uid):
                    await m.reply("Игрок не найден (он должен хоть раз написать боту команду). "
                                  "Пришли ID, @ник или перешли его сообщение. Отмена — /admin")
                    return
                st["target"], st["await"] = uid, None
                scr = await admin_screen(conn, admin, "card", [], "🎯 Игрок выбран.")
            else:
                tgt = st["target"]
                st["await"] = None
                try:
                    if mode == "money":
                        res = await admin_apply(conn, admin, tgt, "money", [int(text)])
                    elif mode == "stars":
                        res = await admin_apply(conn, admin, tgt, "stars", [int(text)])
                    elif mode == "lvl":
                        res = await admin_apply(conn, admin, tgt, "lvl", [int(text)])
                    else:
                        tok = text.lower().split()
                        qty = int(tok[1]) if len(tok) > 1 and tok[1].isdigit() else 1
                        res = await admin_apply(conn, admin, tgt, "item", [tok[0], qty])
                except (ValueError, IndexError):
                    st["await"] = mode
                    await m.reply("Не понял формат, попробуй ещё раз. Отмена — /admin")
                    return
                scr = await admin_screen(conn, admin, "card", [], res)
    await send(m, scr[0], None, scr[1])


async def self_ping():
    if not SELF_URL:
        return
    async with ClientSession() as s:
        while True:
            await asyncio.sleep(600)
            try:
                await s.get(SELF_URL)
            except Exception as e:
                logging.warning("ping failed: %s", e)


async def main():
    global pool
    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=5)
    async with pool.acquire() as c:
        if os.environ.get("RESET_DB") == "1":     # ОСТОРОЖНО: стирает игроков и прогресс
            await c.execute(RESET)
        await c.execute(SCHEMA)

    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    bot = Bot(TOKEN)
    dp = Dispatcher()
    dp.message.register(on_start, CommandStart())
    dp.message.register(on_paysupport, Command("paysupport"))
    dp.message.register(on_terms, Command("terms"))
    dp.message.register(on_payments, Command("payments"))
    dp.message.register(on_refund, Command("refund"))
    dp.message.register(on_paid, F.successful_payment)
    dp.pre_checkout_query.register(on_pre_checkout)
    dp.message.register(on_menu_cmd, Command("menu"))
    dp.message.register(on_admin_cmd, Command("admin"))
    dp.message.register(on_admin_input, F.text, admin_waiting)
    dp.callback_query.register(on_menu_cb, F.data.startswith("m|"))
    dp.callback_query.register(on_admin_cb, F.data.startswith("a|"))
    dp.message.register(on_text, F.text)
    asyncio.create_task(self_ping())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
