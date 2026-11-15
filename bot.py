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
IMG_DIR = BASE / "images"                         # images/дыня.jpg ...

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
  user_id bigint primary key, name text,
  money bigint not null default 500, xp bigint not null default 0,
  light text not null default 'окно', can text not null default 'жестяная',
  farm text not null default 'подоконник',
  acc text, acc_dur int not null default 0,
  since_treasure int not null default 99, harvests int not null default 0);
create table if not exists plots(
  user_id bigint, farm text, slot int, pot text not null default 'малый',
  strain text, planted_at double precision, ready_at double precision,
  last_water double precision, fert text,
  primary key(user_id, farm, slot));
create table if not exists inventory(
  user_id bigint, item text, qty double precision not null,
  primary key(user_id, item));
create table if not exists journal(
  user_id bigint, strain text, cnt int not null default 0,
  best_g double precision not null default 0, best_q int not null default 0,
  primary key(user_id, strain));
create table if not exists missions(
  user_id bigint, day text, idx int, kind text, goal int,
  progress double precision not null default 0, reward int,
  claimed boolean not null default false, text text,
  primary key(user_id, day, idx));
alter table players add column if not exists vip_until double precision not null default 0;
alter table players add column if not exists last_bonus text;
alter table players add column if not exists username text;
alter table players add column if not exists banned boolean not null default false;
create table if not exists admin_log(
  id bigserial primary key, created double precision, admin_id bigint,
  target bigint, action text);
create table if not exists payments(
  charge_id text primary key, user_id bigint, payload text, stars int,
  created double precision, delivered boolean not null default false,
  refunded boolean not null default false);
"""
RESET = ("drop table if exists missions, journal, inventory, plots, players cascade;")

VIP = CFG["vip"]
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


def slot_arg(args):
    for a in args:
        if a.isdigit():
            return int(a)
    return None


def farm_label(key: str) -> str:
    f = CFG["farms"][key]
    return f"{f['emoji']} {f['name']}"


def state(pl) -> str:
    if not pl["strain"]:
        return "empty"
    st = CFG["strains"][pl["strain"]]
    die_after = pl["last_water"] + st["water_every_min"] * 120
    if min(now(), pl["ready_at"]) > die_after:
        return "dead"
    return "ready" if now() >= pl["ready_at"] else "grow"


def is_vip(p) -> bool:
    return p["vip_until"] > now()


def vip_line(p) -> str:
    if not is_vip(p):
        return ""
    return "\n👑 VIP до " + time.strftime("%d.%m.%Y", time.gmtime(p["vip_until"]))


def today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


async def get_player(conn, m):
    uid = m.from_user.id
    uname = m.from_user.username
    p = await conn.fetchrow("select * from players where user_id=$1", uid)
    if not p:
        await conn.execute(
            "insert into players(user_id,name,username,money) values($1,$2,$3,$4)",
            uid, m.from_user.full_name, uname, CFG["start_money"])
        await add_item(conn, uid, "seed:дыня", 2)
        p = await conn.fetchrow("select * from players where user_id=$1", uid)
    elif p["username"] != uname or p["name"] != m.from_user.full_name:
        await conn.execute("update players set name=$2, username=$3 where user_id=$1",
                           uid, m.from_user.full_name, uname)
    return p


async def get_plots(conn, p, farm=None):
    farm = farm or p["farm"]
    await conn.execute(
        "insert into plots(user_id,farm,slot) select $1,$2,g from generate_series(1,$3) g "
        "on conflict do nothing", p["user_id"], farm, CFG["farms"][farm]["slots"])
    return await conn.fetch(
        "select * from plots where user_id=$1 and farm=$2 order by slot", p["user_id"], farm)


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


def item_name(item: str) -> str:
    kind, key = item.split(":", 1)
    if kind == "seed":
        return f"Семена: {CFG['strains'][key]['name']}"
    if kind == "fert":
        return CFG["ferts"][key]["name"]
    if kind == "crop":
        return f"Урожай: {CFG['strains'][key]['name']} (г)"
    if kind == "acc":
        return f"Запасной аксессуар: {CFG['accs'][key]['name']}"
    if kind == "pot":
        return f"Горшок: {key} ({CFG['pots'][key]['liters']} л)"
    return item


# ---------- миссии ----------
async def ensure_missions(conn, p):
    uid, day = p["user_id"], today()
    rows = await conn.fetch(
        "select * from missions where user_id=$1 and day=$2 order by idx", uid, day)
    if rows:
        return rows
    lvl = level_of(p["xp"])
    for idx, pool_ in enumerate(CFG["missions"]):
        t = random.choice(pool_)
        reward = round(t["reward"] * (1 + 0.03 * lvl))
        await conn.execute(
            "insert into missions(user_id,day,idx,kind,goal,reward,text) values($1,$2,$3,$4,$5,$6,$7) "
            "on conflict do nothing", uid, day, idx, t["kind"], t["goal"], reward,
            t["text"].format(goal=t["goal"]))
    return await conn.fetch(
        "select * from missions where user_id=$1 and day=$2 order by idx", uid, day)


async def bump(conn, p, kind: str, amount: float):
    await ensure_missions(conn, p)
    await conn.execute(
        "update missions set progress=least(goal::double precision, progress+$4::double precision) "
        "where user_id=$1 and day=$2 and kind=$3 and not claimed",
        p["user_id"], today(), kind, float(amount))


# ---------- команды ----------
async def cmd_help(conn, p, a):
    return [(
        "🌿 ГРОУФЕРМА — как играть\n\n"
        "Вся игра работает кнопками. Напиши в чат:\n"
        "• Ферма — твоя ферма: полив, сбор, посадка, удобрения, горшки\n"
        "• Магазин — товары по разделам\n"
        "• Меню — главное меню (склад, миссии, профиль, VIP и т.д.)\n\n"
        "Растения нужно поливать вовремя: если не поливать дольше двух интервалов, они засохнут. "
        "Чем выше уровень, тем больше ферм открывается.", None)]


async def cmd_profile(conn, p, a):
    lvl = level_of(p["xp"])
    acc = "нет"
    if p["acc"]:
        acc = f"{CFG['accs'][p['acc']]['name']} (прочность {p['acc_dur']}/{CFG['accs'][p['acc']]['durability']})"
    return [(
        f"👤 {p['name']}\n⭐ Уровень: {lvl} (XP {p['xp']}, до след.: {xp_left(p['xp'])})\n"
        f"💰 Баланс: {p['money']}{vip_line(p)}\n"
        f"📍 Ферма: {farm_label(p['farm'])}\n"
        f"💡 Свет: {CFG['lights'][p['light']]['name']} (×{CFG['lights'][p['light']]['mult']})\n"
        f"🚿 Полив: {CFG['cans'][p['can']]['name']} (слотов за раз: {CFG['cans'][p['can']]['slots']})\n"
        f"🔧 Аксессуар: {acc}\n"
        f"🌾 Всего собрано растений: {p['harvests']}", None)]


async def cmd_farms(conn, p, a):
    lvl = level_of(p["xp"])
    L = ["🏘 ФЕРМЫ (Ферма <название>)", ""]
    for k, f in CFG["farms"].items():
        mark = "📍" if k == p["farm"] else ("🔓" if lvl >= f["min_level"] else "🔒")
        L.append(f"{mark} {k} — {f['emoji']} {f['name']} · Lv.{f['min_level']} · "
                 f"{f['slots']} слотов · урожай ×{f['yield_mult']}")
    return [("\n".join(L), None)]


async def cmd_farm(conn, p, a):
    if a and a[0] in CFG["farms"]:
        f = CFG["farms"][a[0]]
        if level_of(p["xp"]) < f["min_level"]:
            return [(f"🔒 {f['name']} открывается с {f['min_level']} уровня.", None)]
        await conn.execute("update players set farm=$2 where user_id=$1", p["user_id"], a[0])
        p = await conn.fetchrow("select * from players where user_id=$1", p["user_id"])
    farm = CFG["farms"][p["farm"]]
    plots = await get_plots(conn, p)
    L = [f"🏡 {farm['emoji']} {farm['name']} · {'👑 ' if is_vip(p) else ''}{p['name']}",
         f"💡 {CFG['lights'][p['light']]['name']} · урожай фермы ×{farm['yield_mult']}", ""]
    t = now()
    for pl in plots:
        pot = CFG["pots"][pl["pot"]]
        head = f"Слот {pl['slot']} ({pot['liters']} л)"
        s = state(pl)
        if s == "empty":
            L.append(f"🕳 {head}: пусто")
            continue
        st = CFG["strains"][pl["strain"]]
        if s == "dead":
            L.append(f"💀 {head}: {st['name']} засох")
        elif s == "ready":
            L.append(f"✅ {head}: {st['name']} готов!")
        else:
            water_in = pl["last_water"] + st["water_every_min"] * 60 - t
            w = "💧 пора поливать!" if water_in <= 0 else f"💧 через {fmt_t(water_in)}"
            L.append(f"🌱 {head}: {st['name']}\n    ⏱ {fmt_t(pl['ready_at'] - t)} | {w}"
                     + (f" | 🧪 {pl['fert']}" if pl["fert"] else ""))
    return [("\n".join(L), None)]


async def cmd_plant(conn, p, a):
    keys = [x for x in a if x in CFG["strains"]]
    if not keys:
        return [("Выбери семена кнопкой в меню фермы.", None)]
    key = keys[0]
    st = CFG["strains"][key]
    if p["farm"] not in st["farms"]:
        where = ", ".join(farm_label(f) for f in st["farms"])
        return [(f"🌱 {st['name']} не растёт на ферме «{CFG['farms'][p['farm']]['name']}».\n"
                 f"Подходит: {where}", None)]
    plots = await get_plots(conn, p)
    slot = slot_arg(a)
    if slot:
        pl = next((x for x in plots if x["slot"] == slot), None)
        if not pl:
            return [("Такого слота нет.", None)]
        if pl["strain"]:
            return [("Слот занят. Засохшее растение убирается кнопкой «Убрать засохшие» в меню фермы.", None)]
    else:
        pl = next((x for x in plots if not x["strain"]), None)
        if not pl:
            return [("Свободных слотов нет.", None)]
    if not await take_item(conn, p["user_id"], f"seed:{key}", 1):
        return [(f"Нет семян {st['name']}. Купи в магазине, раздел «Семена».", None)]
    t = now()
    speed = CFG["lights"][p["light"]]["mult"] * (1 + VIP["speed_bonus"] if is_vip(p) else 1)
    ready = t + st["grow_min"] * 60 / speed
    await conn.execute(
        "update plots set strain=$4, planted_at=$5, ready_at=$6, last_water=$5, fert=null "
        "where user_id=$1 and farm=$2 and slot=$3", p["user_id"], p["farm"], pl["slot"], key, t, ready)
    return [(f"🌱 Посажено: {st['name']} в слот {pl['slot']}\n"
             f"⏱ Созреет через {fmt_t(ready - t)}\n"
             f"💧 Поливай каждые {st['water_every_min']} мин (иначе засохнет через ×2)", key)]


async def cmd_water(conn, p, a):
    can = CFG["cans"][p["can"]]
    slot = slot_arg(a)
    plots = await get_plots(conn, p)
    targets = [x for x in plots if state(x) == "grow" and (slot is None or x["slot"] == slot)]
    targets.sort(key=lambda x: x["last_water"])
    targets = targets[:can["slots"]]
    if not targets:
        return [("Поливать нечего.", None)]
    for pl in targets:
        await conn.execute(
            "update plots set last_water=$4 where user_id=$1 and farm=$2 and slot=$3",
            p["user_id"], p["farm"], pl["slot"], now())
    await bump(conn, p, "water", len(targets))
    slots = ", ".join(str(x["slot"]) for x in targets)
    return [(f"💧 Полито: слоты {slots} ({can['name']})", None)]


async def cmd_fert(conn, p, a):
    keys = [x for x in a if x in CFG["ferts"]]
    if not keys:
        return [("Выбери удобрение кнопкой в меню фермы.", None)]
    key = keys[0]
    slot = slot_arg(a)
    plots = await get_plots(conn, p)
    cand = [x for x in plots if state(x) == "grow" and not x["fert"]
            and (slot is None or x["slot"] == slot)]
    if not cand:
        return [("Нет подходящего растения (нужно растущее и без удобрения).", None)]
    if not await take_item(conn, p["user_id"], f"fert:{key}", 1):
        return [(f"У тебя нет «{CFG['ferts'][key]['name']}». Купи в магазине, раздел «Удобрения».", None)]
    await conn.execute("update plots set fert=$4 where user_id=$1 and farm=$2 and slot=$3",
                       p["user_id"], p["farm"], cand[0]["slot"], key)
    return [(f"🧪 Слот {cand[0]['slot']} удобрен: {CFG['ferts'][key]['name']} "
             f"(+{int(CFG['ferts'][key]['bonus'] * 100)}% к урожаю)", None)]


async def cmd_harvest(conn, p, a):
    uid = p["user_id"]
    slot = slot_arg(a)
    plots = await get_plots(conn, p)
    farm = CFG["farms"][p["farm"]]
    quals = CFG["qualities"]
    qsum = sum(q["chance"] for q in quals)
    acc, dur, since = p["acc"], p["acc_dur"], p["since_treasure"]
    vip = is_vip(p)
    vip_b = 1 + VIP["yield_bonus"] if vip else 1
    out, total_xp, count, grams_total, rare_cnt = [], 0, 0, 0.0, 0
    for pl in plots:
        if slot and pl["slot"] != slot:
            continue
        s = state(pl)
        if s == "grow":
            if slot:
                out.append(("Ещё не созрело.", None))
            continue
        if s != "ready":
            continue
        st = CFG["strains"][pl["strain"]]
        pot = CFG["pots"][pl["pot"]]
        qi = random.choices(range(len(quals)), weights=[x["chance"] for x in quals])[0]
        q = quals[qi]
        fert_b = 1 + (CFG["ferts"][pl["fert"]]["bonus"] if pl["fert"] else 0)
        acc_b = 1 + (CFG["accs"][acc]["bonus"] if acc else 0)
        grams = round(st["yield_g"] * pot["yield_mult"] * farm["yield_mult"]
                      * random.uniform(0.85, 1.15) * q["mult"] * fert_b * acc_b * vip_b, 1)
        value = round(grams * st["price_g"])
        await add_item(conn, uid, f"crop:{pl['strain']}", grams)
        await conn.execute(
            "update plots set strain=null, planted_at=null, ready_at=null, last_water=null, "
            "fert=null where user_id=$1 and farm=$2 and slot=$3", uid, p["farm"], pl["slot"])
        await conn.execute(
            "insert into journal(user_id,strain,cnt,best_g,best_q) values($1,$2,1,$3,$4) "
            "on conflict (user_id,strain) do update set cnt=journal.cnt+1, "
            "best_g=greatest(journal.best_g,$3), best_q=greatest(journal.best_q,$4)",
            uid, pl["strain"], grams, qi)
        xp_gain = round(st["xp"] * (1 + VIP["xp_bonus"] if vip else 1))
        total_xp += xp_gain
        count += 1
        grams_total += grams
        rare_cnt += 1 if qi >= 2 else 0

        text = (f"🌿 Сбор урожая! (слот {pl['slot']})\n"
                f"📍 Ферма: {farm_label(p['farm'])}\n"
                f"🪴 Горшок: {pot['liters']} л | 💡 {CFG['lights'][p['light']]['name']}\n"
                f"🌱 Сорт: {st['name']}\n"
                f"✨ Качество: {q['name']}\n"
                f"🎯 Шанс этого качества: {q['chance'] / qsum * 100:.1f}%\n"
                f"⚖️ Вес: {fmt_n(grams)} г\n"
                f"💰 Цена при продаже: {value}\n"
                f"⭐ XP: +{xp_gain}")

        # износ аксессуара
        if acc:
            dur -= 1
            if dur <= 0:
                broken = CFG["accs"][acc]["name"]
                spare = await conn.fetchval(
                    "select item from inventory where user_id=$1 and item like 'acc:%' "
                    "order by (item=$2) desc limit 1", uid, f"acc:{acc}")
                if spare:
                    await take_item(conn, uid, spare, 1)
                    acc = spare.split(":", 1)[1]
                    dur = CFG["accs"][acc]["durability"]
                    text += f"\n\n🔧 {broken} сломался. Установлен запасной: {CFG['accs'][acc]['name']}."
                else:
                    acc, dur = None, 0
                    text += f"\n\n🔧 {broken} сломался. Купи новый в магазине."

        # сокровище (два подряд невозможны без нужного числа сборов)
        since += 1
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
            text += ("\n\n🎁 Необычная находка!\nПока ты срезал куст, под корнями "
                     f"нашёлся старый ящик.\n\n🎁 Внутри:\n{label}")
        out.append((text, pl["strain"]))

    if not count:
        return out or [("Собирать пока нечего.", None)]
    old = level_of(p["xp"])
    await conn.execute(
        "update players set xp=xp+$2, acc=$3, acc_dur=$4, since_treasure=$5, harvests=harvests+$6 "
        "where user_id=$1", uid, total_xp, acc, dur, since, count)
    await bump(conn, p, "harvest", count)
    await bump(conn, p, "grams", grams_total)
    if rare_cnt:
        await bump(conn, p, "rare", rare_cnt)
    new = level_of(p["xp"] + total_xp)
    if new > old:
        unlocked = [f"{v['emoji']} {v['name']}" for v in CFG["farms"].values()
                    if old < v["min_level"] <= new]
        extra = f"\nОткрыта ферма: {', '.join(unlocked)}" if unlocked else ""
        out.append((f"🎉 Новый уровень: {new}!{extra}", None))
    return out


async def cmd_clear(conn, p, a):
    slot = slot_arg(a)
    plots = await get_plots(conn, p)
    pl = next((x for x in plots if x["slot"] == slot), None)
    if not pl or state(pl) != "dead":
        return [("Убрать можно только засохшее растение: Убрать <слот>", None)]
    await conn.execute(
        "update plots set strain=null, planted_at=null, ready_at=null, last_water=null, "
        "fert=null where user_id=$1 and farm=$2 and slot=$3", p["user_id"], p["farm"], slot)
    return [(f"🧹 Слот {slot} очищен.", None)]


async def cmd_stock(conn, p, a):
    rows = await conn.fetch(
        "select item, qty from inventory where user_id=$1 order by item", p["user_id"])
    if not rows:
        return [("📦 Склад пуст.", None)]
    L = ["📦 Склад:"] + [f"• {item_name(r['item'])} × {fmt_n(r['qty'])}" for r in rows]
    return [("\n".join(L), None)]


async def cmd_journal(conn, p, a):
    rows = {r["strain"]: r for r in await conn.fetch(
        "select * from journal where user_id=$1", p["user_id"])}
    L = ["📖 ЖУРНАЛ ФЕРМЕРА", ""]
    for k, st in CFG["strains"].items():
        r = rows.get(k)
        if r:
            L.append(f"🌿 {st['name']}: собрано {r['cnt']} · рекорд {fmt_n(r['best_g'])} г · "
                     f"лучшее качество: {CFG['qualities'][r['best_q']]['name']}")
        else:
            L.append(f"❔ {st['name']}: ещё не собирал")
    L.append(f"\n🌾 Всего растений: {p['harvests']}")
    return [("\n".join(L), None)]


async def cmd_missions(conn, p, a):
    rows = await ensure_missions(conn, p)
    L = ["🎯 МИССИИ НА СЕГОДНЯ", ""]
    for r, tag in zip(rows, ["Лёгкая", "Средняя", "Сложная"]):
        if r["claimed"]:
            st = "✅ получено"
        elif r["progress"] >= r["goal"]:
            st = "🎁 готово, жми «Забрать награды»"
        else:
            st = f"{fmt_n(r['progress'])}/{r['goal']}"
        L.append(f"{tag}: {r['text']}\n    {st} · награда {r['reward']} 💰")
    L.append("\nОбновление каждый день (по UTC).")
    return [("\n".join(L), None)]


async def cmd_claim(conn, p, a):
    rows = await ensure_missions(conn, p)
    ready = [r for r in rows if r["progress"] >= r["goal"] and not r["claimed"]]
    if not ready:
        return [("Готовых наград пока нет.", None)]
    total = sum(r["reward"] for r in ready)
    for r in ready:
        await conn.execute(
            "update missions set claimed=true where user_id=$1 and day=$2 and idx=$3",
            p["user_id"], today(), r["idx"])
    await conn.execute("update players set money=money+$2 where user_id=$1", p["user_id"], total)
    return [(f"🎁 Награды за миссии: +{total} 💰 (баланс {p['money'] + total})", None)]


STAR_WORDS = {"звёзды", "звезды", "звезд", "⭐", "stars", "star"}
CATS = [("strains", "seed", "seed_price"), ("ferts", "fert", "price"), ("lights", "light", "price"),
        ("accs", "acc", "price"), ("cans", "can", "price"), ("pots", "pot", "price")]


def unit_stars(unit_price: int, cfg_item: dict) -> int:
    if cfg_item.get("stars"):
        return cfg_item["stars"]
    return max(1, math.ceil(unit_price / CFG["stars_rate"]))


def resolve_item(key: str, qty: int = 1):
    for cat, kind, pf in CATS:
        s_ = CFG[cat].get(key)
        if not s_ or not s_[pf]:
            continue                         # бесплатные стартовые предметы не продаются
        q = max(1, min(int(qty), 100)) if kind in ("seed", "fert") else 1
        unit = s_[pf]
        if kind == "seed":
            label = f"семена {s_['name']} × {q}"
        elif kind == "fert":
            label = f"{s_['name']} × {q}"
        elif kind == "pot":
            label = f"горшок «{key}» ({s_['liters']} л)"
        else:
            label = s_["name"]
        return dict(kind=kind, key=key, qty=q, price=unit * q,
                    stars=unit_stars(unit, s_) * q, need=s_.get("min_level", 1), label=label)
    return None


async def deliver_item(conn, uid, it) -> str:
    kind, key, qty = it["kind"], it["key"], it["qty"]
    if kind in ("seed", "fert"):
        await add_item(conn, uid, f"{kind}:{key}", qty)
    elif kind == "light":
        await conn.execute("update players set light=$2 where user_id=$1", uid, key)
    elif kind == "can":
        await conn.execute("update players set can=$2 where user_id=$1", uid, key)
    elif kind == "pot":
        await add_item(conn, uid, f"pot:{key}", 1)
        return "\n📦 Горшок на складе. Установить: Ферма → 🪴 Горшки."
    elif kind == "acc":
        cur = await conn.fetchval("select acc from players where user_id=$1", uid)
        if cur is None:
            await conn.execute("update players set acc=$2, acc_dur=$3 where user_id=$1",
                               uid, key, CFG["accs"][key]["durability"])
        else:
            await add_item(conn, uid, f"acc:{key}", 1)
            return "\n📦 Положен на склад как запасной (заменит сломанный)."
    return ""


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
                return "Такого товара нет. Загляни в магазин."
            if level_of(p["xp"]) < it["need"]:
                return f"🔒 {it['label']} доступно с {it['need']} уровня."
            title, desc, stars = it["label"], f"Покупка в игре: {it['label']}", it["stars"]
        elif parts[0] == "prod":
            pr = CFG["stars_products"].get(parts[1])
            if not pr:
                return "Такого товара нет. Список: «Звёзды»."
            title, desc, stars = pr["title"], pr["desc"], pr["stars"]
        else:
            return "Неизвестный товар."
    except (IndexError, ValueError):
        return "Не понял, что покупаем."
    return {"title": title[:32], "desc": desc[:255], "stars": stars,
            "payload": f"{p['user_id']}|{spec}", "spec": spec}


async def cmd_buy(conn, p, a):
    if not a:
        return [("Выбери товар в магазине.", None)]
    key, rest = a[0], a[1:]
    stars_mode = any(x in STAR_WORDS for x in rest)
    qty = next((int(x) for x in rest if x.isdigit()), 1)
    it = resolve_item(key, qty)
    if not it:
        return [("Такого товара нет. Загляни в магазин.", None)]
    if level_of(p["xp"]) < it["need"]:
        return [(f"🔒 {it['label']} доступно с {it['need']} уровня.", None)]
    if stars_mode:
        inv = build_invoice(p, f"buy|{key}|{it['qty']}")
        return [inv] if isinstance(inv, dict) else [(inv, None)]
    if p["money"] < it["price"]:
        return [(f"Не хватает денег: нужно {it['price']}, у тебя {p['money']}.\nМожно оплатить ⭐ Stars.", None)]
    await conn.execute("update players set money=money-$2 where user_id=$1",
                       p["user_id"], it["price"])
    note = await deliver_item(conn, p["user_id"], it)
    return [(f"✅ Куплено: {it['label']} за {it['price']} 💰{note}", None)]


async def cmd_pot(conn, p, a):
    slot = slot_arg(a)
    keys = [x for x in a if x in CFG["pots"]]
    if not slot or not keys:
        return [("Выбери слот и горшок кнопками в меню фермы.", None)]
    key = keys[0]
    plots = await get_plots(conn, p)
    pl = next((x for x in plots if x["slot"] == slot), None)
    if not pl:
        return [("Такого слота нет.", None)]
    if pl["strain"]:
        return [("Сначала освободи слот.", None)]
    paid = ""
    if await take_item(conn, p["user_id"], f"pot:{key}", 1):
        paid = " (со склада)"
    else:
        price = CFG["pots"][key]["price"]
        if p["money"] < price:
            return [(f"Не хватает денег: нужно {price} 💰. Горшок можно купить в магазине за ⭐.", None)]
        await conn.execute("update players set money=money-$2 where user_id=$1", p["user_id"], price)
    await conn.execute("update plots set pot=$4 where user_id=$1 and farm=$2 and slot=$3",
                       p["user_id"], p["farm"], slot, key)
    return [(f"🪴 В слоте {slot} теперь горшок «{key}» ({CFG['pots'][key]['liters']} л){paid}.", None)]


# ---------- VIP, бонус, Stars-магазин ----------
def vip_perks() -> str:
    v = VIP
    return (f"+{int(v['yield_bonus'] * 100)}% к урожаю · +{int(v['xp_bonus'] * 100)}% к XP · "
            f"+{int(v['sell_bonus'] * 100)}% к цене продажи · рост быстрее на {int(v['speed_bonus'] * 100)}% · "
            f"ежедневный бонус ×{v['daily_mult']}")


async def cmd_vip(conn, p, a):
    head = ("👑 VIP активен" + vip_line(p).replace("\n👑 VIP", "")) if is_vip(p) else "👑 VIP не активен"
    L = [head, "", "Бонусы: " + vip_perks(), "", "Купить:"]
    for k, pr in CFG["stars_products"].items():
        if "vip_days" in pr:
            L.append(f"• {k} — {pr['title']}: ⭐{pr['stars']}")
    L.append("\nВыбери срок кнопкой ниже. Повторная покупка продлевает срок.")
    return [("\n".join(L), None)]


async def cmd_stars(conn, p, a):
    if a and a[0] in CFG["stars_products"]:
        inv = build_invoice(p, f"prod|{a[0]}")
        return [inv] if isinstance(inv, dict) else [(inv, None)]
    L = ["⭐ ЗВЁЗДЫ — VIP и донат", ""]
    for k, pr in CFG["stars_products"].items():
        L.append(f"• {k} — {pr['title']}: ⭐{pr['stars']}")
    L += ["", "Счёт придёт в личку бота. Вопросы по платежам: /paysupport"]
    return [("\n".join(L), None)]


async def cmd_bonus(conn, p, a):
    if p["last_bonus"] == today():
        return [("🎁 Сегодняшний бонус уже получен. Приходи завтра (обновление по UTC).", None)]
    amount = 150 + 10 * level_of(p["xp"])
    if is_vip(p):
        amount *= VIP["daily_mult"]
    await conn.execute("update players set money=money+$2, last_bonus=$3 where user_id=$1",
                       p["user_id"], amount, today())
    return [(f"🎁 Ежедневный бонус: +{amount} 💰" + (" (VIP ×" + str(VIP["daily_mult"]) + ")" if is_vip(p) else ""), None)]


async def fulfil(conn, p, spec: str) -> str:
    parts = spec.split("|")
    uid = p["user_id"]
    if parts[0] == "buy":
        it = resolve_item(parts[1], int(parts[2]))
        note = await deliver_item(conn, uid, it)
        return f"✅ Оплата получена: {it['label']}{note}"
    pr = CFG["stars_products"][parts[1]]
    L = [f"✅ Оплата получена: {pr['title']}"]
    if "vip_days" in pr:
        cur = max(now(), p["vip_until"])
        new = cur + pr["vip_days"] * 86400
        await conn.execute("update players set vip_until=$2 where user_id=$1", uid, new)
        L.append("👑 VIP до " + time.strftime("%d.%m.%Y", time.gmtime(new)))
    if "money" in pr:
        await conn.execute("update players set money=money+$2 where user_id=$1", uid, pr["money"])
        L.append(f"💰 +{pr['money']}")
    if "item" in pr:
        await add_item(conn, uid, pr["item"], pr["qty"])
        L.append(f"📦 {item_name(pr['item'])} × {pr['qty']}")
    return "\n".join(L)


async def cmd_sell(conn, p, a):
    rows = await conn.fetch(
        "select item, qty from inventory where user_id=$1 and item like 'crop:%'", p["user_id"])
    if a:
        rows = [r for r in rows if r["item"] == f"crop:{a[0]}"]
    if not rows:
        return [("Продавать нечего.", None)]
    total, L = 0, ["💸 Продано:"]
    for r in rows:
        key = r["item"].split(":", 1)[1]
        st = CFG["strains"][key]
        gain = round(r["qty"] * st["price_g"] * (1 + VIP["sell_bonus"] if is_vip(p) else 1))
        total += gain
        await take_item(conn, p["user_id"], r["item"], r["qty"])
        L.append(f"• {st['name']} {fmt_n(r['qty'])} г — {gain} 💰")
    await conn.execute("update players set money=money+$2 where user_id=$1", p["user_id"], total)
    await bump(conn, p, "sell_value", total)
    L.append(f"\nИтого: +{total} 💰 (баланс {p['money'] + total})")
    return [("\n".join(L), None)]





# ---------- Telegram ----------
FLOOD: dict[int, list[float]] = {}


def flood_count(uid: int) -> int:
    t = now()
    q = [x for x in FLOOD.get(uid, []) if t - x < 30]
    q.append(t)
    FLOOD[uid] = q
    return len(q)


async def send(m: Message, text: str, img: str | None, markup=None):
    path = None
    if img:
        for ext in ("jpg", "png", "webp"):
            f = IMG_DIR / f"{img}.{ext}"
            if f.exists():
                path = f
                break
    raw = text[:1024] if path else text
    for html_on in ([True, False] if EMOJI_RE else [False]):
        body = decorate(raw) if html_on else raw
        pm = "HTML" if html_on else None
        try:
            if path:
                await m.reply_photo(FSInputFile(path), caption=body, parse_mode=pm, reply_markup=markup)
            else:
                await m.reply(body, parse_mode=pm, reply_markup=markup)
            return
        except TelegramBadRequest as e:
            if not html_on:
                raise
            logging.warning("отправка с премиум-эмодзи не удалась, шлю обычным текстом: %s", e)


SCREENS = {
    "меню": "home", "menu": "home", "помощь": "help",
    "ферма": "farm", "фермы": "farmsm", "магазин": "shop", "склад": "stock",
    "профиль": "profile", "миссии": "missions", "бонус": "bonus", "журнал": "journal",
    "вип": "vip", "донат": "donat", "звёзды": "donat", "звезды": "donat", "⭐": "donat",
}


async def on_text(m: Message):
    """Текстом открываются только экраны с кнопками. Все действия делаются кнопками."""
    if not m.from_user or m.from_user.is_bot or not m.text:
        return
    parts = m.text.strip().lower().split()
    if not parts:
        return
    screen = SCREENS.get(parts[0])
    if not screen:
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
            text, markup, invs, extras = await menu_screen(conn, p, screen, [])
    await send(m, text, None, markup)


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
                  "не имеют денежной стоимости и не обмениваются на деньги. Игра вымышленная. "
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
    await m.reply("✅ ⭐ возвращены. Выданное в игре (VIP, монеты, предметы) при необходимости отними вручную.")


# ---------- кнопочное меню игрока ----------
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


CAT_TITLES = {"strains": "🌱 Семена", "ferts": "🧪 Удобрения", "lights": "💡 Свет",
              "accs": "🔧 Аксессуары", "cans": "🚿 Полив", "pots": "🪴 Горшки"}
CAT_OF = {key: cat for cat, _, _ in CATS for key in CFG[cat]}


def home_text(p) -> str:
    return (f"🌿 ГРОУФЕРМА{' 👑' if is_vip(p) else ''}\n"
            f"👤 {p['name']} · ⭐ Lv.{level_of(p['xp'])} · 💰 {p['money']}\n"
            f"📍 {farm_label(p['farm'])}\n\nВыбери раздел:")


def kb_home(uid: int):
    rows = [
        [("🏡 Ферма", mcd(uid, "farm")), ("🛒 Магазин", mcd(uid, "shop"))],
        [("📦 Склад", mcd(uid, "stock")), ("🎯 Миссии", mcd(uid, "missions"))],
        [("👤 Профиль", mcd(uid, "profile")), ("📖 Журнал", mcd(uid, "journal"))],
        [("🎁 Бонус", mcd(uid, "bonus")), ("👑 VIP", mcd(uid, "vip"))],
        [("⭐ Донат", mcd(uid, "donat")), ("❓ Помощь", mcd(uid, "help"))],
    ]
    if is_admin(uid):
        rows.append([("🛠 Админ-панель", acd(uid, "home"))])
    return kb(rows)


def kb_back(uid: int):
    return kb([[("🏠 Меню", mcd(uid, "home"))]])


def kb_farm(uid: int, has_dead: bool = False):
    rows = [
        [("💧 Полить", mcd(uid, "water")), ("✂️ Собрать", mcd(uid, "harvest"))],
        [("🌱 Посадить", mcd(uid, "plantm")), ("🧪 Удобрить", mcd(uid, "fertm"))],
        [("🪴 Горшки", mcd(uid, "potm")), ("🏘 Фермы", mcd(uid, "farmsm"))],
    ]
    if has_dead:
        rows.append([("🧹 Убрать засохшие", mcd(uid, "clearall"))])
    rows.append([("🔄 Обновить", mcd(uid, "farm")), ("🏠 Меню", mcd(uid, "home"))])
    return kb(rows)


def out_text(out) -> str:
    texts = [x[0] for x in out if isinstance(x, tuple)]
    if not texts and any(isinstance(x, dict) for x in out):
        return "💳 Счёт отправлен в личные сообщения с ботом."
    return ("\n\n".join(texts) or "Готово.")[:3900]


async def farm_screen(conn, p, note=""):
    out = await cmd_farm(conn, p, [])
    plots = await get_plots(conn, p)
    dead = any(state(x) == "dead" for x in plots)
    text = (note + "\n\n" if note else "") + out[0][0]
    return text, kb_farm(p["user_id"], dead)


async def plant_all(conn, p, key):
    done, last = 0, ""
    for _ in range(CFG["farms"][p["farm"]]["slots"]):
        t = (await cmd_plant(conn, p, [key]))[0][0]
        if t.startswith("🌱 Посажено"):
            done += 1
        else:
            last = t
            break
    if done:
        return f"🌱 Посажено: {CFG['strains'][key]['name']} × {done}"
    return last


async def fert_all(conn, p, key):
    done = 0
    for _ in range(CFG["farms"][p["farm"]]["slots"]):
        t = (await cmd_fert(conn, p, [key]))[0][0]
        if t.startswith("🧪"):
            done += 1
        else:
            break
    return (f"🧪 Удобрено растений: {done}" if done
            else "Нет подходящих растений или закончились удобрения.")


def item_lines(cat, s_):
    if cat == "strains":
        where = ", ".join(farm_label(f) for f in s_["farms"])
        return [f"⏱ Растёт {s_['grow_min']} мин · полив каждые {s_['water_every_min']} мин",
                f"⚖️ Урожай ~{s_['yield_g']} г · продажа {s_['price_g']} 💰/г",
                f"📍 Растёт на: {where}"]
    if cat == "ferts":
        return [f"+{int(s_['bonus'] * 100)}% к урожаю растения"]
    if cat == "lights":
        return [f"Рост быстрее в ×{s_['mult']} · нужен Lv.{s_['min_level']}"]
    if cat == "accs":
        return [f"+{int(s_['bonus'] * 100)}% к урожаю · прочность {s_['durability']} сборов · Lv.{s_['min_level']}"]
    if cat == "cans":
        return [f"Поливает слотов за раз: {s_['slots']}"]
    return [f"Объём {s_['liters']} л · урожай ×{s_['yield_mult']}"]


async def cat_screen(conn, uid, cat):
    p = await conn.fetchrow("select * from players where user_id=$1", uid)
    lvl = level_of(p["xp"])
    rows = []
    for key, s_ in CFG[cat].items():
        it = resolve_item(key)
        if not it:
            continue
        name = s_.get("name", f"Горшок «{key}»")
        lock = "🔒 " if lvl < it["need"] else ""
        rows.append([(f"{lock}{name} · {it['price']}💰 · ⭐{it['stars']}", mcd(uid, "item", key))])
    rows.append([("⬅ Разделы", mcd(uid, "shop")), ("🏠 Меню", mcd(uid, "home"))])
    return f"{CAT_TITLES[cat]}\n💰 Баланс: {p['money']}\nВыбери товар:", kb(rows)


async def item_screen(conn, uid, key, note=""):
    cat = CAT_OF[key]
    s_ = CFG[cat][key]
    it = resolve_item(key)
    p = await conn.fetchrow("select * from players where user_id=$1", uid)
    name = s_.get("name", f"Горшок «{key}»")
    unit_price = it["price"] // it["qty"]
    unit_stars_ = it["stars"] // it["qty"]
    L = ([note, ""] if note else []) + [f"{CAT_TITLES[cat]} · {name}", *item_lines(cat, s_), "",
         f"Цена: {unit_price} 💰 или ⭐{unit_stars_}", f"💰 Твой баланс: {p['money']}"]
    back = [("⬅ Раздел", mcd(uid, "cat", cat)), ("🏠 Меню", mcd(uid, "home"))]
    if level_of(p["xp"]) < it["need"]:
        L.append(f"\n🔒 Доступно с {it['need']} уровня")
        return "\n".join(L), kb([back])
    if it["kind"] in ("seed", "fert"):
        rows = [[(f"💰 ×{q}", mcd(uid, "buy", key, "m", q)) for q in (1, 5, 10)],
                [(f"⭐ ×{q}", mcd(uid, "buy", key, "s", q)) for q in (1, 5, 10)]]
    else:
        rows = [[("💰 Купить", mcd(uid, "buy", key, "m", 1)), ("⭐ Купить", mcd(uid, "buy", key, "s", 1))]]
    return "\n".join(L), kb(rows + [back])


async def stock_screen(conn, p, note=""):
    uid = p["user_id"]
    out = await cmd_stock(conn, p, [])
    crops = await conn.fetch(
        "select item, qty from inventory where user_id=$1 and item like 'crop:%' order by item", uid)
    btns = [(f"💸 {CFG['strains'][r['item'].split(':', 1)[1]]['name']} {fmt_n(r['qty'])} г",
             mcd(uid, "sell", r["item"].split(":", 1)[1])) for r in crops]
    rows = chunk(btns)
    if crops:
        rows.append([("💸 Продать всё", mcd(uid, "sell", "all"))])
    rows.append([("🏠 Меню", mcd(uid, "home"))])
    return (note + "\n\n" if note else "") + out[0][0], kb(rows)


async def missions_screen(conn, p, note=""):
    uid = p["user_id"]
    out = await cmd_missions(conn, p, [])
    rows_db = await ensure_missions(conn, p)
    rows = []
    if any(r["progress"] >= r["goal"] and not r["claimed"] for r in rows_db):
        rows.append([("🎁 Забрать награды", mcd(uid, "claim"))])
    rows.append([("🏠 Меню", mcd(uid, "home"))])
    return (note + "\n\n" if note else "") + out[0][0], kb(rows)


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

    # ----- ферма -----
    if action == "farm":
        return ret(*await farm_screen(conn, p))
    if action == "water":
        return ret(*await farm_screen(conn, p, await run(cmd_water)))
    if action == "harvest":
        out = await cmd_harvest(conn, p, [])
        got = [x for x in out if x[0].startswith("🌿 Сбор урожая")]
        if got:
            for t, img in out:
                extras.append((f"👤 {p['name']}\n{t}" if t.startswith("🌿") else t, img))
            note = f"✂️ Собрано растений: {len(got)}"
        else:
            note = out_text(out)
        return ret(*await farm_screen(conn, p, note))
    if action == "clearall":
        plots = await get_plots(conn, p)
        n = 0
        for pl in plots:
            if state(pl) == "dead":
                await cmd_clear(conn, p, [str(pl["slot"])])
                n += 1
        return ret(*await farm_screen(conn, p, f"🧹 Убрано засохших: {n}" if n else "Засохших растений нет."))
    if action == "farmsm":
        btns = [(f"{'🔒' if lvl < f['min_level'] else f['emoji']} {f['name']} · Lv.{f['min_level']}",
                 mcd(uid, "farmgo", k)) for k, f in CFG["farms"].items()]
        rows = chunk(btns) + [[("⬅ Ферма", mcd(uid, "farm"))]]
        return ret(f"🏘 Выбери ферму (сейчас: {farm_label(p['farm'])}):", kb(rows))
    if action == "farmgo":
        f = CFG["farms"][args[0]]
        if lvl < f["min_level"]:
            return ret(f"🔒 {f['name']} открывается с {f['min_level']} уровня.",
                       kb([[("⬅ Фермы", mcd(uid, "farmsm"))]]))
        await conn.execute("update players set farm=$2 where user_id=$1", uid, args[0])
        p2 = await conn.fetchrow("select * from players where user_id=$1", uid)
        return ret(*await farm_screen(conn, p2, f"📍 Ты на ферме: {farm_label(args[0])}"))

    # ----- посадка -----
    if action == "plantm":
        rows_db = await conn.fetch(
            "select item, qty from inventory where user_id=$1 and item like 'seed:%' order by item", uid)
        btns = []
        for r in rows_db:
            key = r["item"].split(":", 1)[1]
            st = CFG["strains"][key]
            ok = p["farm"] in st["farms"]
            btns.append((f"{'🌱' if ok else '🚫'} {st['name']} ×{fmt_n(r['qty'])}", mcd(uid, "plantk", key)))
        if not btns:
            return ret("🌱 ПОСАДКА\nУ тебя нет семян. Купи их в магазине.",
                       kb([[("🛒 Купить семена", mcd(uid, "cat", "strains"))], [("⬅ Ферма", mcd(uid, "farm"))]]))
        return ret(f"🌱 ПОСАДКА · {farm_label(p['farm'])}\nВыбери семена (🚫 — не растут на этой ферме):",
                   kb(chunk(btns) + [[("⬅ Ферма", mcd(uid, "farm"))]]))
    if action == "plantk":
        key = args[0]
        st = CFG["strains"][key]
        have = await conn.fetchval("select qty from inventory where user_id=$1 and item=$2", uid, f"seed:{key}")
        back = [("⬅ Семена", mcd(uid, "plantm"))]
        if not have:
            return ret("Семян не осталось.", kb([back]))
        if p["farm"] not in st["farms"]:
            where = ", ".join(farm_label(f) for f in st["farms"])
            return ret(f"🚫 {st['name']} не растёт на ферме «{CFG['farms'][p['farm']]['name']}».\nПодходит: {where}",
                       kb([back]))
        plots = await get_plots(conn, p)
        free = [x for x in plots if not x["strain"]]
        if not free:
            return ret("Свободных слотов нет. Собери урожай или убери засохшее.", kb([back]))
        speed = CFG["lights"][p["light"]]["mult"] * (1 + VIP["speed_bonus"] if is_vip(p) else 1)
        btns = [(f"Слот {x['slot']} · {CFG['pots'][x['pot']]['liters']} л", mcd(uid, "plants", key, x["slot"]))
                for x in free]
        rows = chunk(btns, 3) + [[("🌱 Во все свободные", mcd(uid, "plants", key, "all"))], back]
        return ret(f"🌱 {st['name']} · семян: {fmt_n(have)}\n⏱ Созреет за {fmt_t(st['grow_min'] * 60 / speed)}\n"
                   f"💧 Полив каждые {st['water_every_min']} мин\nКуда сажаем?", kb(rows))
    if action == "plants":
        key, slot = args[0], args[1]
        note = await plant_all(conn, p, key) if slot == "all" else await run(cmd_plant, [key, slot])
        return ret(*await farm_screen(conn, p, note))

    # ----- удобрения -----
    if action == "fertm":
        rows_db = await conn.fetch(
            "select item, qty from inventory where user_id=$1 and item like 'fert:%' order by item", uid)
        btns = [(f"🧪 {CFG['ferts'][r['item'].split(':', 1)[1]]['name']} ×{fmt_n(r['qty'])}",
                 mcd(uid, "fertk", r["item"].split(":", 1)[1])) for r in rows_db]
        if not btns:
            return ret("🧪 УДОБРЕНИЯ\nУдобрений нет. Купи их в магазине.",
                       kb([[("🛒 Купить удобрения", mcd(uid, "cat", "ferts"))], [("⬅ Ферма", mcd(uid, "farm"))]]))
        return ret("🧪 УДОБРЕНИЯ\nВыбери удобрение:", kb(chunk(btns) + [[("⬅ Ферма", mcd(uid, "farm"))]]))
    if action == "fertk":
        key = args[0]
        plots = await get_plots(conn, p)
        cand = [x for x in plots if state(x) == "grow" and not x["fert"]]
        back = [("⬅ Удобрения", mcd(uid, "fertm"))]
        if not cand:
            return ret("Нет растущих растений без удобрения.", kb([back]))
        btns = [(f"Слот {x['slot']} · {CFG['strains'][x['strain']]['name']}", mcd(uid, "ferts", key, x["slot"]))
                for x in cand]
        rows = chunk(btns) + [[("🧪 Всем подходящим", mcd(uid, "ferts", key, "all"))], back]
        f = CFG["ferts"][key]
        return ret(f"🧪 {f['name']} (+{int(f['bonus'] * 100)}% урожая)\nКакое растение удобряем?", kb(rows))
    if action == "ferts":
        key, slot = args[0], args[1]
        note = await fert_all(conn, p, key) if slot == "all" else await run(cmd_fert, [key, slot])
        return ret(*await farm_screen(conn, p, note))

    # ----- горшки -----
    if action == "potm":
        plots = await get_plots(conn, p)
        free = [x for x in plots if not x["strain"]]
        if not free:
            return ret("Горшок можно менять только в пустом слоте.", kb([[("⬅ Ферма", mcd(uid, "farm"))]]))
        btns = [(f"Слот {x['slot']} · {x['pot']} ({CFG['pots'][x['pot']]['liters']} л)", mcd(uid, "potk", x["slot"]))
                for x in free]
        return ret("🪴 ГОРШКИ\nВ каком слоте меняем горшок?", kb(chunk(btns) + [[("⬅ Ферма", mcd(uid, "farm"))]]))
    if action == "potk":
        slot = args[0]
        inv = {r["item"]: r["qty"] for r in await conn.fetch(
            "select item, qty from inventory where user_id=$1 and item like 'pot:%'", uid)}
        btns = []
        for k, pt in CFG["pots"].items():
            have = inv.get(f"pot:{k}")
            price = "со склада" if have else ("бесплатно" if not pt["price"] else f"{pt['price']}💰")
            btns.append((f"{k} · {pt['liters']} л · ×{pt['yield_mult']} · {price}", mcd(uid, "potset", slot, k)))
        rows = [[b] for b in btns] + [[("⬅ Горшки", mcd(uid, "potm"))]]
        return ret(f"🪴 Слот {slot}: выбери горшок.\n💰 Баланс: {p['money']}\n"
                   "Купить горшок за ⭐ можно в магазине (раздел «Горшки»).", kb(rows))
    if action == "potset":
        return ret(*await farm_screen(conn, p, await run(cmd_pot, [args[0], args[1]])))

    # ----- магазин -----
    if action == "shop":
        btns = [(t, mcd(uid, "cat", c)) for c, t in CAT_TITLES.items()]
        return ret(f"🛒 МАГАЗИН\n💰 Баланс: {p['money']}\nВыбери раздел:",
                   kb(chunk(btns) + [[("🏠 Меню", mcd(uid, "home"))]]))
    if action == "cat":
        return ret(*await cat_screen(conn, uid, args[0]))
    if action == "item":
        return ret(*await item_screen(conn, uid, args[0]))
    if action == "buy":
        key, mode, qty = args[0], args[1], args[2]
        note = await run(cmd_buy, [key, qty] + (["звёзды"] if mode == "s" else []))
        return ret(*await item_screen(conn, uid, key, note))

    # ----- остальное -----
    if action == "stock":
        return ret(*await stock_screen(conn, p))
    if action == "sell":
        note = await run(cmd_sell, [] if args[0] == "all" else [args[0]])
        return ret(*await stock_screen(conn, p, note))
    if action == "missions":
        return ret(*await missions_screen(conn, p))
    if action == "claim":
        return ret(*await missions_screen(conn, p, await run(cmd_claim)))
    if action == "profile":
        return ret(await run(cmd_profile), kb_back(uid))
    if action == "journal":
        return ret(await run(cmd_journal), kb_back(uid))
    if action == "bonus":
        return ret(await run(cmd_bonus), kb_back(uid))
    if action == "vip":
        text = await run(cmd_vip)
        rows = [[(f"⭐ {pr['title']} — {pr['stars']}", mcd(uid, "prod", k))]
                for k, pr in CFG["stars_products"].items() if "vip_days" in pr]
        return ret(text, kb(rows + [[("🏠 Меню", mcd(uid, "home"))]]))
    if action == "donat":
        btns = [(f"{pr['title']} — ⭐{pr['stars']}", mcd(uid, "prod", k))
                for k, pr in CFG["stars_products"].items()]
        return ret("⭐ ДОНАТ — выбери товар. Счёт придёт в личку бота.\nВопросы по платежам: /paysupport",
                   kb(chunk(btns) + [[("🏠 Меню", mcd(uid, "home"))]]))
    if action == "prod":
        return ret(await run(cmd_stars, [args[0]]), kb_back(uid))
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
        await cb.answer("Это меню другого игрока. Напиши «Ферма» или «Меню», чтобы открыть своё.", show_alert=True)
        return
    if flood_count(owner) > 14:
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
    for cat, kind, pf in CATS:
        s_ = CFG[cat].get(key)
        if s_:
            q = max(1, min(qty, 1000)) if kind in ("seed", "fert") else 1
            name = s_.get("name", f"горшок «{key}»")
            return dict(kind=kind, key=key, qty=q, label=f"{name} × {q}")
    return None


async def admin_apply(conn, admin, tgt, action, args) -> str:
    if action == "money":
        n = int(args[0])
        await conn.execute("update players set money=greatest(0, money+$2) where user_id=$1", tgt, n)
        note = f"💰 {n:+d}"
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
            return "❌ Такого предмета нет (ключи: дыня, золото, вентилятор, кадка, led …)"
        await deliver_item(conn, tgt, it)
        note = f"📦 Выдано: {it['label']}"
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

    if action in ("moneyc", "lvl", "item", "gift", "card", "money", "vip", "vipoff", "xp", "giftdo", "ban", "unban"):
        if not tgt:
            return "Сначала выбери игрока.", kb([[("👤 Выбрать игрока", acd(admin, "pick"))], back])
    if action == "moneyc":
        st["await"] = "money"
        return "💰 Пришли число монет (можно отрицательное, чтобы списать).", kb([[("✖ Отмена", acd(admin, "card"))]])
    if action == "lvl" and not args:
        st["await"] = "lvl"
        return "🎚 Пришли номер уровня (1–500). Опыт будет установлен под этот уровень.", kb([[("✖ Отмена", acd(admin, "card"))]])
    if action == "item" and not args:
        st["await"] = "item"
        return ("📦 Пришли: <ключ> [кол-во], например «золото 5» или «кадка».\n"
                "Ключи: сорта, удобрения, свет, аксессуары, лейки, горшки (как в магазине)."), kb([[("✖ Отмена", acd(admin, "card"))]])
    if action == "gift":
        btns = [(f"{pr['title']}", acd(admin, "giftdo", k)) for k, pr in CFG["stars_products"].items()]
        return ("🎁 Выдать игроку донат-пакет бесплатно (как после оплаты):",
                kb(chunk(btns) + [[("⬅ Карточка", acd(admin, "card"))]]))
    if action in ("money", "vip", "vipoff", "xp", "giftdo", "ban", "unban", "lvl", "item"):
        note = await admin_apply(conn, admin, tgt, action, args)

    # карточка игрока
    st["await"] = None
    r = await conn.fetchrow("select * from players where user_id=$1", tgt)
    if not r:
        return "Игрок не найден.", kb([back])
    vip = ("до " + time.strftime("%d.%m.%Y", time.gmtime(r["vip_until"]))) if r["vip_until"] > now() else "нет"
    text = ((note + "\n\n") if note else "") + (
        f"👤 {r['name']}" + (f" (@{r['username']})" if r["username"] else "") + f"\nID: {tgt}\n"
        f"⭐ Lv.{level_of(r['xp'])} (XP {r['xp']}) · 💰 {r['money']}\n"
        f"👑 VIP: {vip} · 📍 {farm_label(r['farm'])} · 🌾 {r['harvests']}"
        + ("\n🚫 ЗАБЛОКИРОВАН" if r["banned"] else ""))
    rows = [
        [("💰 +1 000", acd(admin, "money", 1000)), ("💰 +10 000", acd(admin, "money", 10000)),
         ("💰 +100 000", acd(admin, "money", 100000))],
        [("💰 Своё число", acd(admin, "moneyc"))],
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
            note = ""
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
        if os.environ.get("RESET_DB") == "1":     # ОСТОРОЖНО: стирает все данные
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
    dp.message.register(on_menu_cmd, F.text.lower().in_({"меню", "menu"}))
    dp.message.register(on_admin_cmd, Command("admin"))
    dp.message.register(on_admin_input, F.text, admin_waiting)
    dp.callback_query.register(on_menu_cb, F.data.startswith("m|"))
    dp.callback_query.register(on_admin_cb, F.data.startswith("a|"))
    dp.message.register(on_text, F.text)
    asyncio.create_task(self_ping())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
