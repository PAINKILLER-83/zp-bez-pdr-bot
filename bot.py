import os, asyncio, aiosqlite, time
from fastapi import FastAPI, Request, HTTPException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

# ---- ENV ----
BOT_TOKEN = os.environ["BOT_TOKEN"]  # обов'язково в Environment на Render
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@zp_bez_pdr")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "zapbezpdr2025")

# ---- Категорії / ПДР ----
CATEGORIES = [
    "🚗 Паркування на тротуарі",
    "🚦 Проїзд на червоне",
    "⛔ Рух по зустрічній",
    "🅿️ Стоянка на зебрі",
    "❗ Інше"
]
PDR_MAP = {
    "🚗 Паркування на тротуарі": "ПДР: п.15.10",
    "🚦 Проїзд на червоне": "ПДР: п.8.7",
    "⛔ Рух по зустрічній": "ПДР: п.11.4",
    "🅿️ Стоянка на зебрі": "ПДР: п.15.9",
    "❗ Інше": "ПДР: (уточнити)"
}

# ---- Ліміти (можеш не чіпати) ----
NEW_USER_TRUST_QUOTA = 2
USER_RATE_LIMIT = 4  # постів/годину

# ---- FastAPI + PTB ----
app = FastAPI()
tg_app: Application = Application.builder().token(BOT_TOKEN).build()

# ---- DB ----
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

# ---- Helpers ----
def category_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton(c, callback_data=f"cat|{c}")] for c in CATEGORIES])

# ---- Handlers ----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привіт! Надішліть фото або відео порушення ПДР у Запоріжжі.\n"
        "Після цього оберіть категорію."
    )

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
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

    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute(
            "SELECT id,caption,media_file_id,media_type FROM inbox "
            "WHERE user_id=? AND category='' ORDER BY id DESC LIMIT 1", (uid,)
        )
        row = await cur.fetchone()
        if not row:
            await q.edit_message_text("⚠️ Немає медіа для категоризації. Спробуйте знову.")
            return
        rec_id, caption, file_id, mtype = row
        await db.execute("UPDATE inbox SET category=? WHERE id=?", (category, rec_id))
        await db.commit()

    text = (
        "🚗 Порушення ПДР у Запоріжжі\n"
        f"🗂 Категорія: {category}\n"
        f"📋 {PDR_MAP.get(category, '')}\n\n"
        f"{caption or ''}"
    )
    if mtype == "photo":
        await context.bot.send_photo(CHANNEL_ID, photo=file_id, caption=text)
    else:
        await context.bot.send_video(CHANNEL_ID, video=file_id, caption=text)
    await q.edit_message_text("✅ Опубліковано в канал. Дякуємо!")

# /chatid для зручного отримання chat_id (корисно для адмін-групи)
async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"chat_id: {update.effective_chat.id}")

# ---- PTB routing ----
tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(CallbackQueryHandler(handle_category, pattern=r"^cat\|"))
tg_app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, handle_media))
tg_app.add_handler(CommandHandler("chatid", chatid))

# ---- FastAPI lifecycle ----
@app.on_event("startup")
async def on_startup():
    await init_db()
    # важливо: ініціалізувати і запустити PTB-Application
    await tg_app.initialize()
    await tg_app.start()

@app.on_event("shutdown")
async def on_shutdown():
    # коректно зупинити PTB-Application
    await tg_app.stop()
    await tg_app.shutdown()

# Healthcheck (не обов'язково, просто щоб / давав 200)
@app.get("/")
async def root():
    return {"ok": True}

# ---- Webhook endpoint ----
@app.post(f"/webhook/{{secret}}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}
