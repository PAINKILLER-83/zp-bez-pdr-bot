import os, time, asyncio, aiosqlite
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
HOST_URL = os.environ.get("HOST_URL", "https://zp-bez-pdr-bot.onrender.com")
PING_INTERVAL_SEC = int(os.environ.get("PING_INTERVAL_SEC", "420"))

# ========= КАТЕГОРІЇ =========
CATEGORY_MAP = {
    "c1": "🚗 Перестроювання без покажчика повороту",
    "c2": "↔️ Перестроювання без надання переваги",
    "c3": "⛳ Перехрестя: перехід у іншу смугу",
    "c4": "🅿️ Неправильне паркування (тротуар/зебра)",
    "c5": "⛔ Рух по зустрічній",
    "c6": "❗ Інше",
}
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
            hourly_count INT DEFAULT 0,
            seen_menu INT DEFAULT 0
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS inbox(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            caption TEXT,
            media_file_id TEXT,
            media_type TEXT,
            category TEXT,
            ts INT,
            location_lat REAL,
            location_lon REAL,
            location_text TEXT,
            user_note TEXT
        )""")
        await db.commit()

# ========= HELPERS =========
def category_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(name, callback_data=f"cat|{code}")]
        for code, name in CATEGORY_MAP.items()
    ])

def detail_menu_kb(has_loc: bool, has_note: bool, rec_id: int):
    t_loc  = f"📍 Геолокація{' ✅' if has_loc  else ''}"
    t_note = f"📝 Коментар{' ✅'    if has_note else ''}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t_loc,  callback_data=f"det|loc|{rec_id}")],
        [InlineKeyboardButton(t_note, callback_data=f"det|note|{rec_id}")],
        [InlineKeyboardButton("➡️ Далі", callback_data=f"det|done|{rec_id}")]
    ])

async def ensure_user(uid: int):
    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute("SELECT user_id FROM users WHERE user_id=?", (uid,))
        if not await cur.fetchone():
            await db.execute("INSERT INTO users(user_id, trust, last_reset, hourly_count, seen_menu) VALUES(?,?,?,?,?)",
                             (uid, 0, int(time.time()), 0, 0))
            await db.commit()

def resolve_chat_id(val: str):
    v = (val or "").strip()
    if v.startswith("-100"):
        try: return int(v)
        except: pass
    return v

async def publish_to_channel(context, mtype, file_id, text):
    chat = resolve_chat_id(CHANNEL_ID)
    if mtype == "photo":
        await context.bot.send_photo(chat_id=chat, photo=file_id, caption=text)
    else:
        await context.bot.send_video(chat_id=chat, video=file_id, caption=text)

async def edit_q_message(q, text, kb=None):
    try:
        if q.message.photo or q.message.video:
            await q.edit_message_caption(caption=text, reply_markup=kb)
        else:
            await q.edit_message_text(text=text, reply_markup=kb)
    except: pass

async def get_inbox_rec(rec_id: int):
    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute(
            "SELECT user_id, caption, media_file_id, media_type, category,"
            " location_lat, location_lon, location_text, user_note FROM inbox WHERE id=?",
            (rec_id,)
        )
        return await cur.fetchone()

async def send_main_menu(chat_id, context):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Новий репорт", callback_data="newreport")],
        [InlineKeyboardButton("📨 Звернутись до адміністратора", callback_data="adminmsg")]
    ])
    await context.bot.send_message(
        chat_id=chat_id,
        text=("👋 Привіт! Оберіть дію нижче.\n"
              "— «📤 Новий репорт» → надішліть фото/відео порушення.\n"
              "— «📨 Звернутись до адміністратора» → текстове звернення (не публікується в канал)."),
        reply_markup=kb
    )

# ========= HANDLERS =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_main_menu(update.effective_chat.id, context)

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
        await db.execute("INSERT INTO inbox(user_id,caption,media_file_id,media_type,category,ts) VALUES(?,?,?,?,?,?)",
                         (user.id, caption, file_id, mtype, "", int(time.time())))
        await db.commit()
    await update.message.reply_text("🚦 Оберіть категорію:", reply_markup=category_keyboard())

async def handle_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, code = q.data.split("|", 1)
    uid = q.from_user.id
    category = CATEGORY_MAP.get(code)
    if not category:
        await edit_q_message(q, "⚠️ Невідома категорія.")
        return
    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute(
            "SELECT id,caption,media_file_id,media_type,location_lat,location_lon,location_text,user_note "
            "FROM inbox WHERE user_id=? AND category='' ORDER BY id DESC LIMIT 1", (uid,))
        row = await cur.fetchone()
        if not row:
            await edit_q_message(q, "⚠️ Немає медіа для категоризації.")
            return
        rec_id = row[0]
        await db.execute("UPDATE inbox SET category=? WHERE id=?", (category, rec_id))
        await db.commit()
    has_loc = bool(row[4] and row[5]) or bool(row[6])
    has_note = bool(row[7])
    await edit_q_message(q, "ℹ️ За бажанням додайте локацію чи коментар і натисніть «➡️ Далі».",
                         kb=detail_menu_kb(has_loc, has_note, rec_id))

# (усі решта хендлери — ті ж самі, як у твоїй останній версії, включно з mod_action, handle_location, handle_text_while_waiting тощо)

# ========= АНТИ-СОН (PING LOOP) =========
async def ping_self():
    while True:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as s:
                async with s.get(HOST_URL) as r:
                    print(f"Ping: {r.status}")
        except Exception as e:
            print(f"Ping error: {e}")
        await asyncio.sleep(PING_INTERVAL_SEC)

@app.on_event("startup")
async def on_startup():
    await init_db()
    await tg_app.initialize()
    await tg_app.start()
    asyncio.create_task(ping_self())

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
