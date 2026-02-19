"""
Flow EVM Balance Watcher Bot
Совместим с Google Colab, Jupyter и обычным Python.

Установка:
    pip install "web3>=6.0" python-telegram-bot aiohttp nest_asyncio

Конфигурация:
    TELEGRAM_TOKEN   — токен от @BotFather
    TELEGRAM_CHAT_ID — ID чата (необязательно)
    FLOW_RPC         — HTTP RPC Flow EVM
    POLL_INTERVAL    — интервал проверки блоков в секундах (default: 5)
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

# Патч для Colab/Jupyter — разрешает запуск asyncio внутри уже работающего loop
try:
    import nest_asyncio
    nest_asyncio.apply()
except ImportError:
    pass

from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ─── Конфигурация ────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
FLOW_RPC         = os.getenv("FLOW_RPC", "https://mainnet.evm.nodes.onflow.org")
POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL", "5"))
STATE_FILE       = Path("state.json")

# ─── Логирование ─────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("flow_bot")

# ─── Хранилище состояния ─────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"addresses": {}, "chat_ids": []}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))

state = load_state()

# ─── Web3 ────────────────────────────────────────────────────────────────────

w3 = AsyncWeb3(AsyncHTTPProvider(FLOW_RPC))

async def get_balance(address: str) -> int:
    checksum = AsyncWeb3.to_checksum_address(address)
    return await w3.eth.get_balance(checksum)

async def get_block_number() -> int:
    return await w3.eth.block_number

# ─── Уведомления ─────────────────────────────────────────────────────────────

def wei_to_flow(wei: int) -> str:
    return f"{wei / 10**18:.8f}"

async def notify(app: Application, text: str, chat_id: Optional[int] = None):
    targets = []
    if chat_id:
        targets = [chat_id]
    elif TELEGRAM_CHAT_ID:
        targets = [int(TELEGRAM_CHAT_ID)]
    else:
        targets = list(state.get("chat_ids", []))

    for cid in targets:
        try:
            await app.bot.send_message(chat_id=cid, text=text, parse_mode="HTML")
        except Exception as e:
            log.error("Не удалось отправить сообщение в %s: %s", cid, e)

# ─── Мониторинг ──────────────────────────────────────────────────────────────

async def check_balances(app: Application):
    addresses = state.get("addresses", {})
    if not addresses:
        return

    for addr, info in list(addresses.items()):
        try:
            new_balance = await get_balance(addr)
            old_balance = int(info.get("balance", -1))

            if old_balance == -1:
                state["addresses"][addr]["balance"] = str(new_balance)
                save_state(state)
                continue

            if new_balance != old_balance:
                diff = new_balance - old_balance
                sign = "+" if diff > 0 else ""
                msg = (
                    f"💰 <b>Изменение баланса</b>\n"
                    f"📍 Адрес: <code>{addr}</code>\n"
                    f"🔗 Сеть: Flow EVM\n\n"
                    f"До:    <code>{wei_to_flow(old_balance)} FLOW</code>\n"
                    f"После: <code>{wei_to_flow(new_balance)} FLOW</code>\n"
                    f"Δ:     <code>{sign}{wei_to_flow(diff)} FLOW</code>"
                )
                await notify(app, msg, info.get("chat_id"))
                state["addresses"][addr]["balance"] = str(new_balance)
                save_state(state)

        except Exception as e:
            log.error("Ошибка при проверке %s: %s", addr, e)

async def monitor_loop(app: Application):
    log.info("Мониторинг запущен. RPC: %s, интервал: %s сек.", FLOW_RPC, POLL_INTERVAL)
    last_block = -1

    while True:
        try:
            block = await get_block_number()
            if block != last_block:
                last_block = block
                log.debug("Новый блок #%s", block)
                await check_balances(app)
        except Exception as e:
            log.error("Ошибка monitor_loop: %s", e)

        await asyncio.sleep(POLL_INTERVAL)

# ─── Команды бота ────────────────────────────────────────────────────────────

def is_valid_address(addr: str) -> bool:
    return bool(re.match(r"^0x[0-9a-fA-F]{40}$", addr))

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in state["chat_ids"]:
        state["chat_ids"].append(chat_id)
        save_state(state)

    await update.message.reply_text(
        "👋 <b>Flow EVM Balance Watcher</b>\n\n"
        "Слежу за балансами адресов в сети Flow EVM.\n\n"
        "/add <code>0xАДРЕС</code> — добавить адрес\n"
        "/remove <code>0xАДРЕС</code> — удалить адрес\n"
        "/list — список отслеживаемых адресов\n"
        "/balance <code>0xАДРЕС</code> — текущий баланс\n"
        "/status — статус подключения",
        parse_mode="HTML",
    )

async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Использование: /add <code>0xАДРЕС</code>", parse_mode="HTML")
        return

    addr = ctx.args[0].strip()
    if not is_valid_address(addr):
        await update.message.reply_text("❌ Неверный формат адреса (ожидается 0x + 40 hex символов)")
        return

    addr_lower = addr.lower()
    if addr_lower in state["addresses"]:
        await update.message.reply_text(f"ℹ️ Адрес <code>{addr}</code> уже отслеживается.", parse_mode="HTML")
        return

    try:
        balance = await get_balance(addr)
        state["addresses"][addr_lower] = {
            "balance": str(balance),
            "chat_id": update.effective_chat.id,
        }
        save_state(state)
        await update.message.reply_text(
            f"✅ Адрес добавлен!\n"
            f"📍 <code>{addr}</code>\n"
            f"💰 Текущий баланс: <code>{wei_to_flow(balance)} FLOW</code>",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Использование: /remove <code>0xАДРЕС</code>", parse_mode="HTML")
        return

    addr = ctx.args[0].strip().lower()
    if addr not in state["addresses"]:
        await update.message.reply_text("❌ Адрес не найден в списке.")
        return

    del state["addresses"][addr]
    save_state(state)
    await update.message.reply_text(f"🗑 Адрес <code>{addr}</code> удалён.", parse_mode="HTML")

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    addresses = state.get("addresses", {})
    if not addresses:
        await update.message.reply_text("📭 Нет отслеживаемых адресов.")
        return

    lines = ["📋 <b>Отслеживаемые адреса:</b>\n"]
    for addr, info in addresses.items():
        bal = wei_to_flow(int(info.get("balance", 0)))
        lines.append(f"• <code>{addr}</code>\n  💰 {bal} FLOW")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Использование: /balance <code>0xАДРЕС</code>", parse_mode="HTML")
        return

    addr = ctx.args[0].strip()
    if not is_valid_address(addr):
        await update.message.reply_text("❌ Неверный адрес.")
        return

    try:
        balance = await get_balance(addr)
        await update.message.reply_text(
            f"💰 Баланс <code>{addr}</code>:\n<code>{wei_to_flow(balance)} FLOW</code>",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        block = await get_block_number()
        status = f"✅ Подключён | Блок: #{block}"
    except Exception as e:
        status = f"❌ Нет подключения: {e}"

    await update.message.reply_text(
        f"🔗 RPC: {FLOW_RPC}\n"
        f"Статус: {status}\n"
        f"👀 Адресов: {len(state.get('addresses', {}))}\n"
        f"⏱ Интервал: {POLL_INTERVAL} сек.",
    )

# ─── Запуск ──────────────────────────────────────────────────────────────────

async def run_bot():
    """Асинхронная точка входа — работает в Colab, Jupyter и обычном Python."""
    if TELEGRAM_TOKEN == "YOUR_BOT_TOKEN":
        log.error("Установите переменную TELEGRAM_TOKEN!")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("status", cmd_status))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    log.info("Бот запущен! Нажмите Ctrl+C для остановки.")

    # Запускаем мониторинг параллельно с ботом
    try:
        await monitor_loop(app)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Остановка...")
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

def main():
    """Обычный запуск (не Colab)."""
    asyncio.run(run_bot())

if __name__ == "__main__":
    main()
