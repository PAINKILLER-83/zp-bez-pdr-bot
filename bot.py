import os, time, aiosqlite
from fastapi import FastAPI, Request, HTTPException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

# ========= ENV =========
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@zp_bez_pdr")  # @public або -100... для приватного
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "zapbezpdr2025")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")           # -100... або id групи з модераторами
TRUST_QUOTA = int(os.environ.get("TRUST_QUOTA", "0"))     # скільки перших постів модеруємо

# ========= КАТЕГОРІЇ (короткий код -> довга назва) =========
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

# ========= RULES =========
RULES_TEXT = (
    "📜 Правила публікацій:\n"
    "1) Публікуємо факти: фото/відео + короткий опис. Без образ та оцінок.\n"
    "2) Не публікуємо зайві персональні дані, що не потрібні для фіксації порушення.\n"
    "3) Якщо в кадрі чітко видно обличчя сторонніх людей/дітей — по можливості не знімайте крупним планом.\n"
    "4) Пости — це повідомлення про можливе порушення. Остаточне рішення — за поліцією.\n\n"
    "Звʼязок із адміністратором — через кнопку «Звернутись до адміністратора» в боті."
)

# ========= FASTAPI + PTB =========
app = FastAPI()
tg_app: Application = Application.builder().token(BOT_TOKEN).build()

# ========= DB =========
async def init_db():
    async with aiosqlite.connect("bot.db") as db:
        # користувачі
        await db.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            trust INT DEFAULT 0,
            last_reset INT DEFAULT 0,
            hourly_count INT DEFAULT 0,
            seen_menu INT DEFAULT 0
        )""")
        # вхідні репорти
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
        # сумісність зі старою схемою (idempotent)
        try: await db.execute("ALTER TABLE users ADD COLUMN seen_menu INT DEFAULT 0")
        except: pass
        try: await db.execute("ALTER TABLE inbox ADD COLUMN location_lat REAL")
        except: pass
        try: await db.execute("ALTER TABLE inbox ADD COLUMN location_lon REAL")
        except: pass
        try: await db.execute("ALTER TABLE inbox ADD COLUMN location_text TEXT")
        except: pass
        try: await db.execute("ALTER TABLE inbox ADD COLUMN user_note TEXT")
        except: pass
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
            await db.execute(
                "INSERT INTO users(user_id, trust, last_reset, hourly_count, seen_menu) VALUES(?,?,?,?,?)",
                (uid, 0, int(time.time()), 0, 0)
            )
            await db.commit()

def resolve_chat_id(val: str):
    v = (val or "").strip()
    if v.startswith("-100"):
        try: return int(v)
        except: pass
    return v  # @username

async def publish_to_channel(context: ContextTypes.DEFAULT_TYPE, mtype: str, file_id: str, text: str):
    chat = resolve_chat_id(CHANNEL_ID)
    if mtype == "photo":
        await context.bot.send_photo(chat_id=chat, photo=file_id, caption=text)
    else:
        await context.bot.send_video(chat_id=chat, video=file_id, caption=text)

async def edit_q_message(q: "telegram.CallbackQuery", text: str, kb=None):
    try:
        if q.message.photo or q.message.video:
            await q.edit_message_caption(caption=text, reply_markup=kb)
        else:
            await q.edit_message_text(text=text, reply_markup=kb)
    except Exception:
        pass

async def get_inbox_rec(rec_id: int):
    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute(
            "SELECT user_id, caption, media_file_id, media_type, category,"
            " location_lat, location_lon, location_text, user_note "
            "FROM inbox WHERE id=?", (rec_id,)
        )
        return await cur.fetchone()

async def send_main_menu(chat_id, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Новий репорт", callback_data="newreport")],
        [InlineKeyboardButton("📨 Звернутись до адміністратора", callback_data="adminmsg")],
        [InlineKeyboardButton("📜 Правила / Дисклеймер", callback_data="showrules")]
    ])
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=("👋 Привіт! Оберіть дію нижче.\n"
                  "— «📤 Новий репорт» → надішліть фото/відео порушення.\n"
                  "— «📨 Звернутись до адміністратора» → текстове звернення (не публікується в канал).\n"
                  "— «📜 Правила / Дисклеймер» — ознайомитись з правилами публікацій."),
            reply_markup=kb
        )
    except Exception:
        pass

# ========= HANDLERS =========
# /start + deep-link ?start=report
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args and len(context.args) > 0 and context.args[0].lower() == "report":
        if update.message:
            await update.message.reply_text("📸 Надішліть фото або відео порушення. Потім оберіть категорію.")
        else:
            await send_main_menu(update.effective_chat.id, context)
        return
    await send_main_menu(update.effective_chat.id, context)

# /report — швидкий старт репорту
async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 Надішліть фото або відео порушення. Потім оберіть категорію.")

# /rules — показати правила
async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(RULES_TEXT, disable_web_page_preview=True)

# Кнопка “📜 Правила / Дисклеймер”
async def show_rules_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await edit_q_message(q, RULES_TEXT)

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
    _, code = q.data.split("|", 1)
    uid = q.from_user.id

    category = CATEGORY_MAP.get(code)
    if not category:
        await edit_q_message(q, "⚠️ Невідома категорія.")
        return

    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute(
            "SELECT id,caption,media_file_id,media_type,location_lat,location_lon,location_text,user_note "
            "FROM inbox WHERE user_id=? AND category='' ORDER BY id DESC LIMIT 1",
            (uid,)
        )
        row = await cur.fetchone()
        if not row:
            await edit_q_message(q, "⚠️ Немає медіа для категоризації. Спробуйте ще раз.")
            return
        rec_id = row[0]
        await db.execute("UPDATE inbox SET category=? WHERE id=?", (category, rec_id))
        await db.commit()

    has_loc = bool(row[4] and row[5]) or bool(row[6])
    has_note = bool(row[7])
    await edit_q_message(
        q,
        "ℹ️ За бажанням додайте локацію та/або коментар. Потім натисніть «➡️ Далі».",
        kb=detail_menu_kb(has_loc, has_note, rec_id)
    )

# ===== Деталі репорту (локація/нотатка/фініш) =====
async def det_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, action, rec_s = q.data.split("|", 2)
    rec_id = int(rec_s)

    if action == "loc":
        context.user_data["await_loc_rec"] = rec_id
        await q.message.reply_text(
            "📍 Надішліть геолокацію (Скріпка → Локація) АБО напишіть текст-адресу.\n"
            "Коли закінчите, знову натисніть «➡️ Далі»."
        )
        row = await get_inbox_rec(rec_id)
        has_loc = bool(row[5] and row[6]) or bool(row[7])
        has_note = bool(row[8])
        await edit_q_message(q, "ℹ️ Додайте деталі або тисніть «➡️ Далі».",
                             kb=detail_menu_kb(has_loc, has_note, rec_id))
        return

    if action == "note":
        context.user_data["await_note_rec"] = rec_id
        await q.message.reply_text("📝 Надішліть текстовий коментар (номер авто, час, смуги тощо).")
        row = await get_inbox_rec(rec_id)
        has_loc = bool(row[5] and row[6]) or bool(row[7])
        has_note = bool(row[8])
        await edit_q_message(q, "ℹ️ Додайте деталі або тисніть «➡️ Далі».",
                             kb=detail_menu_kb(has_loc, has_note, rec_id))
        return

    if action == "done":
        row = await get_inbox_rec(rec_id)
        if not row:
            await edit_q_message(q, "❗ Запис не знайдено.")
            return
        uid, caption, file_id, mtype, category, lat, lon, loc_text, user_note = row
        uname = update.effective_user.username or "без_ніка"

        parts = [
            "🚗 Порушення ПДР | Запоріжжя",
            f"🗂 Категорія: {category}",
            f"🧾 {PDR_MAP.get(category,'ПДР: (уточнити)')}",
            f"👤 Від: @{uname} (id {uid})",
        ]
        if (lat is not None and lon is not None):
            parts.append(f"📍 Локація: https://maps.google.com/?q={lat:.6f},{lon:.6f}")
        elif loc_text:
            parts.append(f"📍 Локація: {loc_text}")
        if user_note:
            parts.append(f"📝 Примітка: {user_note}")
        if caption:
            parts.append("")
            parts.append(caption)
        base_text = "\n".join(parts)

        async with aiosqlite.connect("bot.db") as db:
            cur = await db.execute("SELECT trust FROM users WHERE user_id=?", (uid,))
            trust = (await cur.fetchone() or (0,))[0]

        if TRUST_QUOTA > 0 and ADMIN_CHAT_ID and trust < TRUST_QUOTA:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Опублікувати", callback_data=f"mod|ok|{rec_id}"),
                InlineKeyboardButton("❌ Відхилити",   callback_data=f"mod|no|{rec_id}")
            ]])
            adm_caption = "📝 На модерацію\n" + base_text
            if mtype == "photo":
                await tg_app.bot.send_photo(chat_id=int(ADMIN_CHAT_ID), photo=file_id, caption=adm_caption, reply_markup=kb)
            else:
                await tg_app.bot.send_video(chat_id=int(ADMIN_CHAT_ID), video=file_id, caption=adm_caption, reply_markup=kb)
            await edit_q_message(q, "🔎 Репорт надіслано на модерацію. Дякуємо!")
            return

        try:
            await publish_to_channel(context, mtype, file_id, base_text)
            await edit_q_message(q, "✅ Опубліковано в канал. Дякуємо!")
        except Exception as e:
            await edit_q_message(q, f"❗ Не вдалося опублікувати: {e}")

# ===== Прийом геолокації / адреси / нотатки =====
async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "await_loc_rec" not in context.user_data:
        return
    rec_id = context.user_data.pop("await_loc_rec")
    loc = update.message.location
    if not loc:
        return
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("UPDATE inbox SET location_lat=?, location_lon=?, location_text=NULL WHERE id=?",
                         (loc.latitude, loc.longitude, rec_id))
        await db.commit()
    await update.message.reply_text("✅ Локацію збережено.")

async def handle_text_while_waiting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    if "await_loc_rec" in context.user_data:
        rec_id = context.user_data.pop("await_loc_rec")
        async with aiosqlite.connect("bot.db") as db:
            await db.execute("UPDATE inbox SET location_text=?, location_lat=NULL, location_lon=NULL WHERE id=?",
                             (text, rec_id))
            await db.commit()
        await update.message.reply_text("✅ Адресу збережено.")
        return
    if "await_note_rec" in context.user_data:
        rec_id = context.user_data.pop("await_note_rec")
        async with aiosqlite.connect("bot.db") as db:
            await db.execute("UPDATE inbox SET user_note=? WHERE id=?", (text, rec_id))
            await db.commit()
        await update.message.reply_text("✅ Коментар збережено.")
        return

# ===== Авто-меню для новачків (без /start) =====
async def auto_menu_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "await_loc_rec" in context.user_data or "await_note_rec" in context.user_data:
        return
    uid = update.effective_user.id
    await ensure_user(uid)
    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute("SELECT seen_menu FROM users WHERE user_id=?", (uid,))
        row = await cur.fetchone()
        seen = (row[0] if row else 0)
        if not seen:
            await db.execute("UPDATE users SET seen_menu=1 WHERE user_id=?", (uid,))
            await db.commit()
            await send_main_menu(update.effective_chat.id, context)

# ===== Модерація =====
async def mod_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, decision, rec_s = q.data.split("|", 2)
    rec_id = int(rec_s)

    row = await get_inbox_rec(rec_id)
    if not row:
        await edit_q_message(q, "Запис не знайдено.")
        return
    uid, caption, file_id, mtype, category, lat, lon, loc_text, user_note = row

    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute("SELECT trust FROM users WHERE user_id=?", (uid,))
        trust = (await cur.fetchone() or (0,))[0]

    if decision == "ok":
        parts = [
            "🚗 Порушення ПДР | Запоріжжя",
            f"🗂 Категорія: {category}",
            f"🧾 {PDR_MAP.get(category,'ПДР: (уточнити)')}",
        ]
        if (lat is not None and lon is not None):
            parts.append(f"📍 Локація: https://maps.google.com/?q={lat:.6f},{lon:.6f}")
        elif loc_text:
            parts.append(f"📍 Локація: {loc_text}")
        if user_note:
            parts.append(f"📝 Примітка: {user_note}")
        if caption:
            parts.append("")
            parts.append(caption)
        text = "\n".join(parts)

        try:
            await publish_to_channel(context, mtype, file_id, text)
            new_trust = min(trust + 1, TRUST_QUOTA)
            async with aiosqlite.connect("bot.db") as db:
                await db.execute("UPDATE users SET trust=? WHERE user_id=?", (new_trust, uid))
                await db.commit()
            await edit_q_message(q, f"✅ Опубліковано. Довіра користувача: {new_trust}/{TRUST_QUOTA}")
        except Exception as e:
            await edit_q_message(q, f"❗ Не вдалося опублікувати: {e}\nПеревірте CHANNEL_ID та права бота.")
    else:
        await edit_q_message(q, "❌ Відхилено.")

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
tg_app.add_handler(CommandHandler("report", report_cmd))
tg_app.add_handler(CommandHandler("rules", rules_cmd))        # ← додано
tg_app.add_handler(CommandHandler("chatid", chatid))

tg_app.add_handler(CallbackQueryHandler(start_new_report, pattern=r"^newreport$"))
tg_app.add_handler(CallbackQueryHandler(handle_category,   pattern=r"^cat\|"))
tg_app.add_handler(CallbackQueryHandler(det_action,        pattern=r"^det\|"))
tg_app.add_handler(CallbackQueryHandler(mod_action,        pattern=r"^mod\|"))
tg_app.add_handler(CallbackQueryHandler(show_rules_btn,    pattern=r"^showrules$"))  # ← додано

tg_app.add_handler(ConversationHandler(
    entry_points=[CallbackQueryHandler(ask_admin_msg, pattern=r"^adminmsg$")],
    states={ADMIN_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_msg_text)]},
    fallbacks=[]
))

# прийом медіа
tg_app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, handle_media))
# прийом геолокації/тексту під час очікування
tg_app.add_handler(MessageHandler(filters.LOCATION, handle_location))
tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_while_waiting))
# авто-меню як останній текстовий хендлер
tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, auto_menu_fallback))

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

@app.post(f"/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}
