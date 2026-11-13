import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.types import FSInputFile, Message
from aiohttp import ClientSession, web

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["BOT_TOKEN"]
DB_URL = os.environ["DATABASE_URL"]          # строка подключения Neon/Supabase
PORT = int(os.environ.get("PORT", 10000))
SELF_URL = os.environ.get("RENDER_EXTERNAL_URL")  # Render подставляет сам

BASE = Path(__file__).parent
CFG = json.loads((BASE / "game.json").read_text(encoding="utf-8"))
IMG_DIR = BASE / "images"                    # images/дыня.jpg, images/harvest.jpg ...

SCHEMA = """
create table if not exists players(
  user_id bigint primary key, name text,
  money bigint not null default 500, xp int not null default 0,
  light text not null default 'окно', can text not null default 'жестяная');
create table if not exists plots(
  user_id bigint, slot int, pot text not null default 'малый',
  strain text, planted_at double precision, ready_at double precision,
  last_water double precision, fert text,
  primary key(user_id, slot));
create table if not exists inventory(
  user_id bigint, item text, qty double precision not null,
  primary key(user_id, item));
"""

pool: asyncpg.Pool
LOCK = asyncio.Lock()
now = time.time


# ---------- утилиты ----------
def level_of(xp: int) -> int:
    return xp // 100 + 1


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


def state(pl) -> str:
    if not pl["strain"]:
        return "empty"
    st = CFG["strains"][pl["strain"]]
    die_after = pl["last_water"] + st["water_every_min"] * 120
    if min(now(), pl["ready_at"]) > die_after:
        return "dead"
    return "ready" if now() >= pl["ready_at"] else "grow"


async def get_player(conn, m: Message):
    uid = m.from_user.id
    p = await conn.fetchrow("select * from players where user_id=$1", uid)
    if not p:
        await conn.execute(
            "insert into players(user_id,name,money) values($1,$2,$3)",
            uid, m.from_user.full_name, CFG["start_money"])
        for s in range(1, CFG["start_slots"] + 1):
            await conn.execute("insert into plots(user_id,slot) values($1,$2)", uid, s)
        await add_item(conn, uid, "seed:дыня", 2)
        p = await conn.fetchrow("select * from players where user_id=$1", uid)
    return p


async def get_plots(conn, uid):
    return await conn.fetch("select * from plots where user_id=$1 order by slot", uid)


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
    return item


# ---------- команды ----------
async def cmd_help(conn, p, a):
    return [(
        "🌿 ГРОУФЕРМА\n\n"
        "Ферма — что растёт\n"
        "Посадить <сорт> [слот]\n"
        "Полить [слот]\n"
        "Удобрить <удобрение> [слот]\n"
        "Собрать [слот]\n"
        "Убрать <слот> — убрать засохшее\n"
        "Склад — всё в наличии\n"
        "Магазин / Купить <название> [кол-во]\n"
        "Горшок <слот> <название> — сменить горшок\n"
        "Продать [сорт]\n"
        "Профиль", None)]


async def cmd_profile(conn, p, a):
    lvl = level_of(p["xp"])
    return [(
        f"👤 {p['name']}\n⭐ Уровень: {lvl} (XP {p['xp']}, до след.: {100 - p['xp'] % 100})\n"
        f"💰 Баланс: {p['money']}\n"
        f"💡 Свет: {CFG['lights'][p['light']]['name']} (×{CFG['lights'][p['light']]['mult']})\n"
        f"🚿 Лейка: {CFG['cans'][p['can']]['name']} (слотов за раз: {CFG['cans'][p['can']]['slots']})",
        None)]


async def cmd_farm(conn, p, a):
    plots = await get_plots(conn, p["user_id"])
    L = [f"🏡 Ферма: {p['name']}", f"💡 {CFG['lights'][p['light']]['name']}", ""]
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
            L.append(f"💀 {head}: {st['name']} засох. Напиши «Убрать {pl['slot']}»")
        elif s == "ready":
            L.append(f"✅ {head}: {st['name']} готов! «Собрать»")
        else:
            water_in = pl["last_water"] + st["water_every_min"] * 60 - t
            w = f"💧 пора поливать!" if water_in <= 0 else f"💧 через {fmt_t(water_in)}"
            L.append(f"🌱 {head}: {st['name']}\n    ⏱ {fmt_t(pl['ready_at'] - t)} | {w}"
                     + (f" | 🧪 {pl['fert']}" if pl["fert"] else ""))
    return [("\n".join(L), None)]


async def cmd_plant(conn, p, a):
    keys = [x for x in a if x in CFG["strains"]]
    if not keys:
        return [("Напиши: Посадить <сорт> [слот]. Сорта смотри в «Магазин».", None)]
    key = keys[0]
    st = CFG["strains"][key]
    if level_of(p["xp"]) < st["min_level"]:
        return [(f"🔒 {st['name']} доступен с {st['min_level']} уровня.", None)]
    plots = await get_plots(conn, p["user_id"])
    slot = slot_arg(a)
    if slot:
        pl = next((x for x in plots if x["slot"] == slot), None)
        if not pl:
            return [("Такого слота нет.", None)]
        if pl["strain"]:
            return [("Слот занят. Если там мёртвое растение — «Убрать N».", None)]
    else:
        pl = next((x for x in plots if not x["strain"]), None)
        if not pl:
            return [("Свободных слотов нет.", None)]
    if not await take_item(conn, p["user_id"], f"seed:{key}", 1):
        return [(f"Нет семян {st['name']}. Купи: Купить {key}", None)]
    t = now()
    ready = t + st["grow_min"] * 60 / CFG["lights"][p["light"]]["mult"]
    await conn.execute(
        "update plots set strain=$3, planted_at=$4, ready_at=$5, last_water=$4, fert=null "
        "where user_id=$1 and slot=$2", p["user_id"], pl["slot"], key, t, ready)
    return [(f"🌱 Посажено: {st['name']} в слот {pl['slot']}\n"
             f"⏱ Созреет через {fmt_t(ready - t)}\n"
             f"💧 Поливай каждые {st['water_every_min']} мин (иначе засохнет через ×2)", key)]


async def cmd_water(conn, p, a):
    can = CFG["cans"][p["can"]]
    slot = slot_arg(a)
    plots = await get_plots(conn, p["user_id"])
    targets = [x for x in plots if state(x) == "grow" and (slot is None or x["slot"] == slot)]
    targets.sort(key=lambda x: x["last_water"])
    targets = targets[:can["slots"]]
    if not targets:
        return [("Поливать нечего.", None)]
    for pl in targets:
        await conn.execute("update plots set last_water=$3 where user_id=$1 and slot=$2",
                           p["user_id"], pl["slot"], now())
    slots = ", ".join(str(x["slot"]) for x in targets)
    return [(f"💧 Полито: слоты {slots} ({can['name']})", None)]


async def cmd_fert(conn, p, a):
    keys = [x for x in a if x in CFG["ferts"]]
    if not keys:
        return [("Напиши: Удобрить <удобрение> [слот]", None)]
    key = keys[0]
    slot = slot_arg(a)
    plots = await get_plots(conn, p["user_id"])
    cand = [x for x in plots if state(x) == "grow" and not x["fert"]
            and (slot is None or x["slot"] == slot)]
    if not cand:
        return [("Нет подходящего растения (нужно растущее и без удобрения).", None)]
    if not await take_item(conn, p["user_id"], f"fert:{key}", 1):
        return [(f"У тебя нет «{CFG['ferts'][key]['name']}». Купи: Купить {key}", None)]
    await conn.execute("update plots set fert=$3 where user_id=$1 and slot=$2",
                       p["user_id"], cand[0]["slot"], key)
    return [(f"🧪 Слот {cand[0]['slot']} удобрен: {CFG['ferts'][key]['name']} "
             f"(+{int(CFG['ferts'][key]['bonus'] * 100)}% к урожаю)", None)]


async def cmd_harvest(conn, p, a):
    slot = slot_arg(a)
    plots = await get_plots(conn, p["user_id"])
    out, total_xp = [], 0
    quals = CFG["qualities"]
    qsum = sum(q["chance"] for q in quals)
    for pl in plots:
        if slot and pl["slot"] != slot:
            continue
        s = state(pl)
        if s == "grow":
            if slot:
                out.append(("Ещё не созрело.", None))
            continue
        if s not in ("ready",):
            continue
        st = CFG["strains"][pl["strain"]]
        pot = CFG["pots"][pl["pot"]]
        q = random.choices(quals, weights=[x["chance"] for x in quals])[0]
        bonus = 1 + (CFG["ferts"][pl["fert"]]["bonus"] if pl["fert"] else 0)
        grams = round(st["yield_g"] * pot["yield_mult"] * random.uniform(0.85, 1.15)
                      * q["mult"] * bonus, 1)
        value = round(grams * st["price_g"])
        await add_item(conn, p["user_id"], f"crop:{pl['strain']}", grams)
        await conn.execute(
            "update plots set strain=null, planted_at=null, ready_at=null, last_water=null, "
            "fert=null where user_id=$1 and slot=$2", p["user_id"], pl["slot"])
        total_xp += st["xp"]
        text = (f"🌿 Сбор урожая! (слот {pl['slot']})\n"
                f"🪴 Горшок: {pot['liters']} л | 💡 {CFG['lights'][p['light']]['name']}\n"
                f"🌱 Сорт: {st['name']}\n"
                f"✨ Качество: {q['name']}\n"
                f"🎯 Шанс этого качества: {q['chance'] / qsum * 100:.1f}%\n"
                f"⚖️ Вес: {fmt_n(grams)} г\n"
                f"💰 Цена при продаже: {value}\n"
                f"⭐ XP: +{st['xp']}")
        if random.random() < CFG["event_chance"]:
            ev = random.choices(CFG["events"], weights=[e["w"] for e in CFG["events"]])[0]
            text += f"\n\n🎲 Необычное событие!\n{ev['text']}"
            if "item" in ev:
                await add_item(conn, p["user_id"], ev["item"], ev["qty"])
            if "money" in ev:
                await conn.execute("update players set money=money+$2 where user_id=$1",
                                   p["user_id"], ev["money"])
        out.append((text, pl["strain"]))
    if not out:
        return [("Собирать пока нечего.", None)]
    old = level_of(p["xp"])
    await conn.execute("update players set xp=xp+$2 where user_id=$1", p["user_id"], total_xp)
    new = level_of(p["xp"] + total_xp)
    if new > old:
        out.append((f"🎉 Новый уровень: {new}! Открылись новые сорта.", None))
    return out


async def cmd_clear(conn, p, a):
    slot = slot_arg(a)
    plots = await get_plots(conn, p["user_id"])
    pl = next((x for x in plots if x["slot"] == slot), None)
    if not pl or state(pl) != "dead":
        return [("Убрать можно только засохшее растение: Убрать <слот>", None)]
    await conn.execute(
        "update plots set strain=null, planted_at=null, ready_at=null, last_water=null, "
        "fert=null where user_id=$1 and slot=$2", p["user_id"], slot)
    return [(f"🧹 Слот {slot} очищен.", None)]


async def cmd_stock(conn, p, a):
    rows = await conn.fetch(
        "select item, qty from inventory where user_id=$1 order by item", p["user_id"])
    if not rows:
        return [("📦 Склад пуст.", None)]
    L = ["📦 Склад:"] + [f"• {item_name(r['item'])} × {fmt_n(r['qty'])}" for r in rows]
    return [("\n".join(L), None)]


async def cmd_shop(conn, p, a):
    L = ["🛒 МАГАЗИН  (Купить <название> [кол-во])", "", "🌱 Семена:"]
    for k, s in CFG["strains"].items():
        L.append(f"• {k} — {s['name']}: {s['seed_price']} 💰 | с {s['min_level']} ур. | {s['grow_min']} мин")
    L += ["", "🧪 Удобрения:"]
    for k, s in CFG["ferts"].items():
        L.append(f"• {k}: {s['price']} 💰 | +{int(s['bonus'] * 100)}% урожая")
    L += ["", "💡 Свет (ускоряет рост):"]
    for k, s in CFG["lights"].items():
        if s["price"]:
            L.append(f"• {k} — {s['name']}: {s['price']} 💰 | ×{s['mult']}")
    L += ["", "🚿 Лейки:"]
    for k, s in CFG["cans"].items():
        if s["price"]:
            L.append(f"• {k} — {s['name']}: {s['price']} 💰 | {s['slots']} слота за раз")
    L += ["", "🪴 Горшки (Горшок <слот> <название>, слот должен быть пуст):"]
    for k, s in CFG["pots"].items():
        if s["price"]:
            L.append(f"• {k}: {s['price']} 💰 | {s['liters']} л | урожай ×{s['yield_mult']}")
    return [("\n".join(L), None)]


async def cmd_buy(conn, p, a):
    if not a:
        return [("Напиши: Купить <название> [кол-во]", None)]
    key = a[0]
    qty = int(a[1]) if len(a) > 1 and a[1].isdigit() else 1
    qty = max(1, min(qty, 100))
    uid = p["user_id"]
    if key in CFG["strains"]:
        price, item = CFG["strains"][key]["seed_price"] * qty, f"seed:{key}"
        label = f"семена {CFG['strains'][key]['name']} × {qty}"
    elif key in CFG["ferts"]:
        price, item = CFG["ferts"][key]["price"] * qty, f"fert:{key}"
        label = f"{CFG['ferts'][key]['name']} × {qty}"
    elif key in CFG["lights"] and CFG["lights"][key]["price"]:
        price, item, label = CFG["lights"][key]["price"], None, CFG["lights"][key]["name"]
    elif key in CFG["cans"] and CFG["cans"][key]["price"]:
        price, item, label = CFG["cans"][key]["price"], None, CFG["cans"][key]["name"]
    else:
        return [("Такого товара нет. Смотри «Магазин».", None)]
    if p["money"] < price:
        return [(f"Не хватает денег: нужно {price}, у тебя {p['money']}.", None)]
    await conn.execute("update players set money=money-$2 where user_id=$1", uid, price)
    if item:
        await add_item(conn, uid, item, qty)
    elif key in CFG["lights"]:
        await conn.execute("update players set light=$2 where user_id=$1", uid, key)
    else:
        await conn.execute("update players set can=$2 where user_id=$1", uid, key)
    return [(f"✅ Куплено: {label} за {price} 💰", None)]


async def cmd_pot(conn, p, a):
    slot = slot_arg(a)
    keys = [x for x in a if x in CFG["pots"]]
    if not slot or not keys:
        return [("Напиши: Горшок <слот> <название>", None)]
    key = keys[0]
    plots = await get_plots(conn, p["user_id"])
    pl = next((x for x in plots if x["slot"] == slot), None)
    if not pl:
        return [("Такого слота нет.", None)]
    if pl["strain"]:
        return [("Сначала освободи слот.", None)]
    price = CFG["pots"][key]["price"]
    if p["money"] < price:
        return [(f"Не хватает денег: нужно {price}.", None)]
    await conn.execute("update players set money=money-$2 where user_id=$1", p["user_id"], price)
    await conn.execute("update plots set pot=$3 where user_id=$1 and slot=$2",
                       p["user_id"], slot, key)
    return [(f"🪴 В слоте {slot} теперь горшок «{key}» ({CFG['pots'][key]['liters']} л).", None)]


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
        gain = round(r["qty"] * st["price_g"])
        total += gain
        await take_item(conn, p["user_id"], r["item"], r["qty"])
        L.append(f"• {st['name']} {fmt_n(r['qty'])} г — {gain} 💰")
    await conn.execute("update players set money=money+$2 where user_id=$1", p["user_id"], total)
    L.append(f"\nИтого: +{total} 💰 (баланс {p['money'] + total})")
    return [("\n".join(L), None)]


COMMANDS = {
    "помощь": cmd_help, "профиль": cmd_profile, "ферма": cmd_farm,
    "посадить": cmd_plant, "полить": cmd_water, "удобрить": cmd_fert,
    "собрать": cmd_harvest, "убрать": cmd_clear, "склад": cmd_stock,
    "магазин": cmd_shop, "купить": cmd_buy, "горшок": cmd_pot, "продать": cmd_sell,
}


# ---------- Telegram ----------
async def send(m: Message, text: str, img: str | None):
    path = None
    if img:
        for ext in ("jpg", "png", "webp"):
            f = IMG_DIR / f"{img}.{ext}"
            if f.exists():
                path = f
                break
    if path:
        await m.reply_photo(FSInputFile(path), caption=text[:1024])
    else:
        await m.reply(text)


async def on_text(m: Message):
    if not m.from_user or m.from_user.is_bot or not m.text:
        return
    parts = m.text.strip().lower().split()
    if not parts:
        return
    handler = COMMANDS.get(parts[0])
    if not handler:
        return
    async with LOCK, pool.acquire() as conn:
        async with conn.transaction():
            p = await get_player(conn, m)
            out = await handler(conn, p, parts[1:])
    for text, img in out:
        await send(m, text, img)


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
        await c.execute(SCHEMA)

    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    bot = Bot(TOKEN)
    dp = Dispatcher()
    dp.message.register(on_text, F.text)
    asyncio.create_task(self_ping())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
