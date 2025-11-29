#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
LEMON — развлекательный рулетка-бот для групп.
Включает:
- ставки (батч-окно), отмену ставок
- баланс (LEMON)
- OWNER (админ), выдача/сброс денег
- агенты поддержки (сетап / снятьап)
- репорты и ответ на репорт (репорт, репортотв)
- админ-панель, /help меню с inline-кнопками
- база: aiosqlite (lemon.db)
"""

import os
import re
import random
import asyncio
import aiosqlite
from datetime import datetime

from telegram import (
    Update,
    Chat,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# ------------------ Настройки ------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN") or "8267151632:AAGQ8cBN4zD6l5dXeJh5PVvRIUsxApP2Ioc"
# Владелец: можно указать через env OWNER_ID, иначе остаётся None (назначается при первом /start в приватном чате)
OWNER_ID = int(os.environ.get("OWNER_ID")) if os.environ.get("OWNER_ID") else None
# Чат куда отправлять репорты администраторам (если указан) (например -100123...)
SUPPORT_CHAT_ID = int(os.environ.get("SUPPORT_CHAT_ID")) if os.environ.get("SUPPORT_CHAT_ID") else None

DB_PATH = "lemon.db"
CURRENCY = "LEMON"
BOT_NAME = "LEMON"

# коэффициенты
COLOR_PAYOUT = 2
NUMBER_PAYOUT = 36
NUMBER_COLOR_PAYOUT = 72

# окно приёма ставок (сек)
BET_WINDOW_SECONDS = 5

# красные номера
RED_NUMBERS = {
    1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36
}

# Регекс парсинга ставок
BET_RE = re.compile(r'^\s*(\d+)(?:\s+(\d{1,2})(?:\s*([кКkK]|[чЧ]))?|\s*([кКkK]|[чЧ]))\s*$', re.IGNORECASE)

# ------------------ SQL ------------------
CREATE_USERS_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    balance INTEGER DEFAULT 1000
);
"""
CREATE_LOG_SQL = """
CREATE TABLE IF NOT EXISTS bets_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    user_id INTEGER,
    username TEXT,
    stake INTEGER,
    bet_type TEXT,
    target TEXT,
    result_number INTEGER,
    result_color TEXT,
    payout INTEGER,
    timestamp TEXT
);
"""
CREATE_CONFIG_SQL = """
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""
CREATE_SUPPORT_SQL = """
CREATE TABLE IF NOT EXISTS support_agents (
    user_id INTEGER PRIMARY KEY
);
"""
CREATE_REPORTS_SQL = """
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    text TEXT,
    status TEXT DEFAULT 'open',
    created_at TEXT
);
"""

# ------------------ DB helpers ------------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_USERS_SQL)
        await db.execute(CREATE_LOG_SQL)
        await db.execute(CREATE_CONFIG_SQL)
        await db.execute(CREATE_SUPPORT_SQL)
        await db.execute(CREATE_REPORTS_SQL)
        await db.commit()
    # If OWNER_ID provided via env, save to config
    if OWNER_ID:
        await set_config("owner_id", str(OWNER_ID))

async def set_config(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
        await db.commit()

async def get_config(key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM config WHERE key=?", (key,))
        r = await cur.fetchone()
        return r[0] if r else None

async def get_owner_id():
    v = await get_config("owner_id")
    return int(v) if v else None

async def set_owner_id(uid: int):
    await set_config("owner_id", str(uid))

async def get_balance(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
        r = await cur.fetchone()
        if r:
            return r[0]
        await db.execute("INSERT INTO users (user_id, balance) VALUES (?, ?)", (user_id, 1000))
        await db.commit()
        return 1000

async def set_balance(user_id: int, new_balance: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO users (user_id, balance) VALUES (?, ?)", (user_id, new_balance))
        await db.commit()

async def change_balance(user_id: int, delta: int):
    bal = await get_balance(user_id)
    new = bal + delta
    if new < 0:
        return False, bal
    await set_balance(user_id, new)
    return True, new

async def log_bet_db(chat_id:int, user_id:int, username:str, stake:int, bet_type:str, target:str, result_number:int, result_color:str, payout:int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO bets_log (chat_id, user_id, username, stake, bet_type, target, result_number, result_color, payout, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, user_id, username, stake, bet_type, target, result_number, result_color, payout, datetime.utcnow().isoformat())
        )
        await db.commit()

async def add_support_agent(user_id:int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO support_agents (user_id) VALUES (?)", (user_id,))
        await db.commit()

async def remove_support_agent(user_id:int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM support_agents WHERE user_id=?", (user_id,))
        await db.commit()

async def list_support_agents():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM support_agents")
        rows = await cur.fetchall()
        return [r[0] for r in rows]

async def create_report(user_id:int, text:str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("INSERT INTO reports (user_id, text, status, created_at) VALUES (?, ?, 'open', ?)", (user_id, text, datetime.utcnow().isoformat()))
        await db.commit()
        return cur.lastrowid

async def set_report_status(report_id:int, status:str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE reports SET status=? WHERE id=?", (status, report_id))
        await db.commit()

async def get_report(report_id:int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, user_id, text, status, created_at FROM reports WHERE id=?", (report_id,))
        return await cur.fetchone()

# ------------------ Utils ------------------
def format_user_tag(user):
    return f"@{user.username}" if user.username else user.full_name

def spin_wheel():
    number = random.randint(0, 36)
    if number == 0:
        color = "GREEN"
    else:
        color = "RED" if number in RED_NUMBERS else "BLACK"
    return number, color

# ------------------ Bot Handlers ------------------

# /start: if OWNER not set and user started in private — set owner
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Prohibit use in private? We'll allow for owner setup
    if update.effective_chat.type == Chat.PRIVATE:
        owner = await get_owner_id()
        if owner is None:
            await set_owner_id(update.effective_user.id)
            await update.message.reply_text("Вы назначены владельцем бота ✅")
        else:
            await update.message.reply_text("Бот LEMON — работает. Добавь меня в группу.")
        return

    # in groups
    await update.message.reply_text(f"Привет! Я <b>{BOT_NAME}</b> — рулетка (виртуальные {CURRENCY}). Напиши /help", parse_mode="HTML")

# /help with inline menu
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("📨 Отправить репорт", callback_data="send_report")],
        [InlineKeyboardButton("💼 Баланс", callback_data="my_balance"), InlineKeyboardButton("📡 Пинг", callback_data="ping_btn")],
    ]
    text = (
        "<b>📘 Меню команд LEMON</b>\n\n"
        "👑 Команды владельца:\n"
        "/выдать <id> <сумма>\n/сброс <id>\n/сетап <id>\n/снятьап <id>\n/admin — админ-панель\n\n"
        "💬 Команды поддержки:\n"
        "/репортотв <id> <ответ>\n\n"
        "👤 Пользователи:\n"
        "/репорт <текст>\n/пинг\n\n"
        "Также: в чате ставьте ставки форматом как в инструкции (пример: 100 к, 50 7 ч, б, отмена).\n"
    )
    await update.message.reply_html(text, reply_markup=InlineKeyboardMarkup(kb))

# Callback for inline buttons
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "ping_btn":
        await query.edit_message_text("pong 🟢")
    elif data == "my_balance":
        bal = await get_balance(query.from_user.id)
        await query.edit_message_text(f"💼 Ваш баланс: <b>{bal}</b>", parse_mode="HTML")
    elif data == "send_report":
        await query.edit_message_text("Чтобы отправить репорт, используй команду:\n\n/репорт <текст>")

# ------------------ Admin / Owner helpers ------------------
async def is_owner_async(user_id:int):
    owner = await get_owner_id()
    return owner == user_id

def ensure_owner_sync(user_id:int):
    # helper for sync check where needed
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(is_owner_async(user_id))

# /пинг
async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong 🟢")

# /выдать <id> <amount>  (owner only)
async def give_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner_async(update.effective_user.id):
        return await update.message.reply_text("Команда доступна только владельцу.")
    if len(context.args) < 2:
        return await update.message.reply_text("Использование: /выдать <id> <сумма>")
    try:
        uid = int(context.args[0])
        amount = int(context.args[1])
    except:
        return await update.message.reply_text("Неверный формат. /выдать <id> <сумма>")
    await change_balance(uid, amount)
    await update.message.reply_text(f"Выдано {amount} {CURRENCY} пользователю {uid} ✅")

# /сброс <id> (owner)
async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner_async(update.effective_user.id):
        return await update.message.reply_text("Команда доступна только владельцу.")
    if len(context.args) < 1:
        return await update.message.reply_text("Использование: /сброс <id>")
    try:
        uid = int(context.args[0])
    except:
        return await update.message.reply_text("Неверный id.")
    await set_balance(uid, 0)
    await update.message.reply_text(f"Баланс пользователя {uid} сброшен до 0 ✅")

# /сетап <id> add support agent (owner)
async def setup_agent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner_async(update.effective_user.id):
        return await update.message.reply_text("Только владелец.")
    if len(context.args) < 1:
        return await update.message.reply_text("Использование: /сетап <id>")
    try:
        uid = int(context.args[0])
    except:
        return await update.message.reply_text("Неверный id.")
    await add_support_agent(uid)
    await update.message.reply_text(f"Пользователь {uid} назначен агентом поддержки ✅")

# /снятьап <id>
async def remove_agent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner_async(update.effective_user.id):
        return await update.message.reply_text("Только владелец.")
    if len(context.args) < 1:
        return await update.message.reply_text("Использование: /снятьап <id>")
    try:
        uid = int(context.args[0])
    except:
        return await update.message.reply_text("Неверный id.")
    await remove_support_agent(uid)
    await update.message.reply_text(f"Агент {uid} снят ✅")

# /репорт <text>
async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        return await update.message.reply_text("Напиши: /репорт <текст>")
    text = " ".join(context.args)
    uid = update.effective_user.id
    rid = await create_report(uid, text)

    await update.message.reply_text(f"Репорт отправлен! ID: {rid}")

    # Если задан SUPPORT_CHAT_ID, шлём туда; иначе — шлём всем агентам (DM)
    if SUPPORT_CHAT_ID:
        try:
            await context.bot.send_message(
                SUPPORT_CHAT_ID,
                f"📨 Новый репорт #{rid}\nОт: {uid} ({format_user_tag(update.effective_user)})\n\n{text}\n\n"
                f"Ответ: /репортотв {rid} <ответ>"
            )
        except Exception:
            pass
    else:
        agents = await list_support_agents()
        for a in agents:
            try:
                await context.bot.send_message(
                    a,
                    f"📨 Новый репорт #{rid}\nОт: {uid} ({format_user_tag(update.effective_user)})\n\n{text}\n\n"
                    f"Ответ: /репортотв {rid} <ответ>"
                )
            except:
                pass

# /репортотв <report_id> <ответ> (ап или owner)
async def reply_report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user.id
    agents = await list_support_agents()
    if not (caller in agents or await is_owner_async(caller)):
        return await update.message.reply_text("Только агент поддержки или владелец может отвечать на репорт.")
    if len(context.args) < 2:
        return await update.message.reply_text("Использование: /репортотв <report_id> <ответ>")
    try:
        rid = int(context.args[0])
    except:
        return await update.message.reply_text("Неверный report_id")
    answer = " ".join(context.args[1:])
    row = await get_report(rid)
    if not row:
        return await update.message.reply_text("Репорт не найден.")
    target_uid = row[1]
    try:
        await context.bot.send_message(target_uid, f"📬 Ответ на ваш репорт #{rid}:\n\n{answer}")
        await set_report_status(rid, "answered")
        await update.message.reply_text("Ответ отправлен успешно ✅")
    except Exception:
        await update.message.reply_text("Не удалось отправить сообщение пользователю (возможно, пользователь закрыл чат).")

# ------------------ Betting system (batch) ------------------
# chat_data['pending_bets'] = [ {user_id, username, stake, bet_type, target}, ... ]
# chat_data['spin_task'] = asyncio.Task

async def schedule_spin_if_needed(chat_id:int, context: ContextTypes.DEFAULT_TYPE):
    cd = context.chat_data
    if cd.get("spin_task") and not cd["spin_task"].done():
        return
    cd["spin_task"] = asyncio.create_task(spin_after_window(chat_id, context))

async def spin_after_window(chat_id:int, context: ContextTypes.DEFAULT_TYPE):
    await asyncio.sleep(BET_WINDOW_SECONDS)
    cd = context.chat_data
    pending = cd.pop("pending_bets", [])
    cd.pop("spin_task", None)
    if not pending:
        return

    result_number, result_color = spin_wheel()

    # aggregate per user
    results_by_user = {}
    messages_details = []

    for bet in pending:
        user_id = bet["user_id"]
        username = bet["username"]
        stake = bet["stake"]
        bet_type = bet["bet_type"]
        target = bet["target"]
        payout = 0
        won = False

        if bet_type == "COLOR":
            if result_color == target:
                payout = stake * COLOR_PAYOUT
                won = True
        elif bet_type == "NUMBER":
            num = int(target.split()[0])
            if result_number == num:
                payout = stake * NUMBER_PAYOUT
                won = True
        elif bet_type == "NUMBER_COLOR":
            parts = target.split()
            num = int(parts[0]); col = parts[1]
            if result_number == num and result_color == col:
                payout = stake * NUMBER_COLOR_PAYOUT
                won = True

        if won and payout > 0:
            await change_balance(user_id, payout)

        # log (do not block)
        asyncio.create_task(log_bet_db(chat_id, user_id, username, stake, bet_type, target, result_number, result_color, payout if won else 0))

        entry = {"username": username, "stake": stake, "bet_type": bet_type, "target": target, "won": won, "payout": payout if won else 0}
        messages_details.append(entry)

        ru = results_by_user.get(user_id)
        if not ru:
            ru = {"username": username, "won_total": 0, "lost_total": 0, "details": []}
            results_by_user[user_id] = ru
        if won:
            ru["won_total"] += payout
        else:
            ru["lost_total"] += stake
        ru["details"].append(entry)

    # build summary message in GRAM style
    header = f"{BOT_NAME}\nРУЛЕТКА 🎯\nВыпало: {result_number} {result_color}\n\n"
    lines = [header]
    for uid, info in results_by_user.items():
        uname = info["username"]
        if info["won_total"] > 0:
            lines.append(f"{uname} выиграл {info['won_total']} {CURRENCY}")
        else:
            lines.append(f"{uname} проиграл {info['lost_total']} {CURRENCY}")
        # details
        detail_parts = []
        for d in info["details"]:
            status = "WIN" if d["won"] else "LOSS"
            detail_parts.append(f"{d['stake']}→{d['target']} ({status})")
        lines.append("  ставки: " + ", ".join(detail_parts))
        lines.append("")
    full_msg = "\n".join(lines)
    try:
        await context.bot.send_message(chat_id=chat_id, text=full_msg)
    except:
        pass

# Cancel: removes all pending bets of the user in chat and returns money
async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == Chat.PRIVATE:
        return await update.message.reply_text("Команда работает только в группе.")
    cd = context.chat_data
    pending = cd.get("pending_bets", [])
    if not pending:
        return await update.message.reply_text("Нет активных ставок для отмены.")
    kept = []
    refunded = 0
    uid = update.effective_user.id
    username = format_user_tag(update.effective_user)
    for bet in pending:
        if bet["user_id"] == uid:
            await change_balance(uid, bet["stake"])
            refunded += bet["stake"]
        else:
            kept.append(bet)
    cd["pending_bets"] = kept
    if refunded:
        await update.message.reply_text(f"Отмена: возвращено {refunded} {CURRENCY} тебе, {username}.")
    else:
        await update.message.reply_text("У тебя нет активных ставок в этом окне.")

async def handle_cancel_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip().lower()
    if txt in ("отмена", "/cancel", "/отмена", "cancel"):
        await cancel_cmd(update, context)

# Main message handler for bets and balance check
async def bet_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == Chat.PRIVATE:
        # ignore or return
        return

    text = update.message.text.strip()
    user = update.effective_user
    chat_id = update.effective_chat.id

    # balance short
    if text.lower() == 'б':
        bal = await get_balance(user.id)
        return await update.message.reply_html(f"{user.mention_html()} баланс: {bal} {CURRENCY}")

    # cancellation word handled separately by filter->handle_cancel_word; also check here
    if text.lower() in ("отмена", "/cancel", "/отмена", "cancel"):
        return await cancel_cmd(update, context)

    m = BET_RE.match(text)
    if not m:
        return  # not a bet

    amount_str = m.group(1)
    number_group = m.group(2)
    color_group1 = m.group(3)
    color_group2 = m.group(4)

    try:
        stake = int(amount_str)
        if stake <= 0:
            return await update.message.reply_text("Ставка должна быть положительным числом.")
    except:
        return await update.message.reply_text("Неверный формат суммы.")

    bal = await get_balance(user.id)
    if stake > bal:
        return await update.me
