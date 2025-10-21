import os, time, aiosqlite
from fastapi import FastAPI, Request, HTTPException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

# ========= ENV =========
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@zp_bez_pdr")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "zapbezpdr2025")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
TRUST_QUOTA = int(os.environ.get("TRUST_QUOTA", "0"))

# ========= КАТЕГОРІЇ =========
CATEGORIES = [
    "🚗 Перестроювання без покажчика повороту",
    "↔️ Перестроювання без надання переваги",
    "⛳ Перехрестя: перехід у іншу смугу",
    "🅿️ Неправильне паркування (тротуар/зебра)",
    "⛔ Рух по зустрічній",
    "❗ Інше"
]
PDR_MAP = {
    "🚗 Перестроювання без покажчика повороту": "ПДР: п.9.2, п.9.4",
    "↔️ Перестроювання без надання переваги": "ПДР: п.10.3",
    "⛳ Перехрестя: перехід у іншу смугу": "ПДР: п.10.4 (+п.10.1)",
    "🅿️ Неправильне паркування (тротуар/зебра)": "ПДР: п.15.9–15.10",
    "⛔ Рух по зустрічній": "ПДР: розд.11",
    "❗ Інше": "ПДР: (уточнити)",
}

# ========= FASTAPI + PTB =========
app = FastAPI()
tg_app: Application = Application.builder().token(BOT_TOKEN).build()

# ========= DB =========
async def init_db():
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            trust INT DEFAULT 0,
            last_reset INT DEFAULT 0,
            hourly_count INT DEFAULT 0
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS inbox(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            caption TEXT,
            media_file_id TEXT,
            media_type TEXT,
            category TEXT,
            ts INT
        )""")
        await db.commit()

# ========= HELPERS =========
def category_keyboard():
    rows = [[InlineKeyboardButton(c, callback_data=f"cat|{c}")] for c in CATEGORIES]
    return InlineKeyboardMarkup(rows)

async def ensure_user(uid: int):
    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute("SELECT user_id FROM users WHERE user_id=?", (uid,))
        if not await cur.fetchone():
            await db.execute("INSERT INTO users(user_id, trust, last_reset, hourly_count) VALUES(?,?,?,?)",
                             (uid, 0, int(time.time()), 0))
            await db.commit()

async def publish_to_channel(context: ContextTypes.DEFAULT_TYPE, mtype: str, file_id: str, text: str):
    if mtype == "photo":
        await context.bot.send_photo(chat_id=CHANNEL_ID, photo=file_id, caption=text)
    else:
        await context.bot.send_video(chat_id=CHANNEL_ID, video=file_id, caption=text)

# ========= HANDLERS =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Новий репорт", callback_data="newreport")],
        [InlineKeyboardButton("📨 Звернутись до адміністратора", callback_data="adminmsg")]
    ])
    await update.message.reply_text(
        "👋 Привіт! Оберіть дію нижче.\n"
        "— «📤 Новий репорт» → надішліть фото/відео порушення.\n"
        "— «📨 Звернутись до адміністратора» → текстове звернення (не публікується в канал).",
        reply_markup=kb
    )

async def start_new_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("📸 Надішліть фото або відео порушення. Потім оберіть категорію.")

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user.id)

    async with aiosqlite.connect("bot.db") as db:
        caption = (update.message.caption or "").strip()
        if update.message.photo:
            file_id, mtype = update.message.photo[-1].file_id, "photo"
        elif update.message.video:
            file_id, mtype = update.message.video.file_id, "video"
        else:
            await update.message.reply_text("📎 Надішліть фото або відео, не документ.")
            return
        await db.execute(
            "INSERT INTO inbox(user_id,caption,media_file_id,media_type,category,ts) VALUES(?,?,?,?,?,?)",
            (user.id, caption, file_id, mtype, "", int(time.time()))
        )
        await db.commit()

    await update.message.reply_text("🚦 Оберіть категорію:", reply_markup=category_keyboard())

async def handle_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, category = q.data.split("|", 1)
    uid = q.from_user.id
    uname = q.from_user.username or "без_ніка"

    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute(
            "SELECT id,caption,media_file_id,media_type FROM inbox "
            "WHERE user_id=? AND category='' ORDER BY id DESC LIMIT 1", (uid,)
        )
        row = await cur.fetchone()
        if not row:
            await q.edit_message_text("⚠️ Немає медіа для категоризації. Спробуйте ще раз.")
            return
        rec_id, caption, file_id, mtype = row
        await db.execute("UPDATE inbox SET category=? WHERE id=?", (category, rec_id))
        cur2 = await db.execute("SELECT trust FROM users WHERE user_id=?", (uid,))
        u = await cur2.fetchone()
        trust = u[0] if u else 0
        await db.commit()

    pdr_note = PDR_MAP.get(category, "ПДР: (уточнити)")
    base_text = (
        "🚗 Порушення ПДР | Запоріжжя\n"
        f"🗂 Категорія: {category}\n"
        f"🧾 {pdr_note}\n"
        f"👤 Від: @{uname} (id {uid})\n\n"
        f"{caption or ''}"
    )

    if TRUST_QUOTA > 0 and ADMIN_CHAT_ID and trust < TRUST_QUOTA:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Опублікувати", callback_data=f"mod|ok|{rec_id}"),
            InlineKeyboardButton("❌ Відхилити", callback_data=f"mod|no|{rec_id}")
        ]])
        caption = "📝 На модерацію\n" + base_text
        if mtype == "photo":
            await context.bot.send_photo(chat_id=int(ADMIN_CHAT_ID), photo=file_id, caption=caption, reply_markup=kb)
        else:
            await context.bot.send_video(chat_id=int(ADMIN_CHAT_ID), video=file_id, caption=caption, reply_markup=kb)
        await q.edit_message_text("🔎 Репорт надіслано на модерацію. Дякуємо!")
        return

    await publish_to_channel(context, mtype, file_id, base_text)
    await q.edit_message_text("✅ Опубліковано в канал. Дякуємо!")

async def mod_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, decision, rec_id = q.data.split("|", 2)
    rec_id = int(rec_id)

    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute("SELECT user_id, caption, media_file_id, media_type, category FROM inbox WHERE id=?",
                               (rec_id,))
        row = await cur.fetchone()
        if not row:
            await q.edit_message_text("Запис не знайдено.")
            return
        uid, caption, file_id, mtype, category = row
        cur2 = await db.execute("SELECT trust FROM users WHERE user_id=?", (uid,))
        trust = (await cur2.fetchone() or (0,))[0]

        if decision == "ok":
            pdr_note = PDR_MAP.get(category, "ПДР: (уточнити)")
            text = (
                "🚗 Порушення ПДР | Запоріжжя\n"
                f"🗂 Категорія: {category}\n"
                f"🧾 {pdr_note}\n\n{caption or ''}"
            )
            await publish_to_channel(context, mtype, file_id, text)
            new_trust = min(trust + 1, TRUST_QUOTA)
            await db.execute("UPDATE users SET trust=? WHERE user_id=?", (new_trust, uid))
            await db.commit()
            await q.edit_message_text(f"✅ Опубліковано. Довіра користувача: {new_trust}/{TRUST_QUOTA}")
        else:
            await q.edit_message_text("❌ Відхилено.")

# ===== Звернення до адміністратора =====
ADMIN_MSG = 1001

async def ask_admin_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("✍️ Напишіть текст повідомлення адміністратору. Воно **не публікується** в каналі.")
    return ADMIN_MSG

async def handle_admin_msg_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if ADMIN_CHAT_ID:
        try:
            uname = update.effective_user.username or 'без_ніка'
            msg = (
                "📨 Нове звернення до адміністратора\n"
                f"Від: @{uname} (id {update.effective_user.id})\n\n"
                f"{text}"
            )
            await context.bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=msg)
        except Exception as e:
            print("ADMIN DM ERROR:", e)

    await update.message.reply_text("✅ Повідомлення надіслано адміністратору. Дякуємо!")
    return ConversationHandler.END

# ===== Допоміжні команди =====
async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"chat_id: {update.effective_chat.id}")

# ========= ROUTING =========
tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(CommandHandler("chatid", chatid))
tg_app.add_handler(CallbackQueryHandler(start_new_report, pattern=r"^newreport$"))
tg_app.add_handler(CallbackQueryHandler(handle_category, pattern=r"^cat\|"))
tg_app.add_handler(CallbackQueryHandler(mod_action, pattern=r"^mod\|"))

tg_app.add_handler(ConversationHandler(
    entry_points=[CallbackQueryHandler(ask_admin_msg, pattern=r"^adminmsg$")],
    states={ADMIN_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_msg_text)]},
    fallbacks=[]
))

tg_app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, handle_media))

# ========= FASTAPI LIFECYCLE =========
@app.on_event("startup")
async def on_startup():
    await init_db()
    await tg_app.initialize()
    await tg_app.start()

@app.on_event("shutdown")
async def on_shutdown():
    await tg_app.stop()
    await tg_app.shutdown()

@app.get("/")
async def root():
    return {"ok": True}

@app.post(f"/webhook/{{secret}}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}
