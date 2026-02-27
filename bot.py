import os
import asyncio
import logging
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from storage import Storage
from monitor import AttendanceMonitor

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))

storage = Storage()
monitors: dict[int, AttendanceMonitor] = {}  # telegram_id -> AttendanceMonitor

# Conversation states
AWAITING_CODE = 1
AWAITING_USERNAME = 2
AWAITING_PASSWORD = 3


# ── Helpers ──

def is_admin(telegram_id: int) -> bool:
    return telegram_id == ADMIN_TELEGRAM_ID


def get_main_menu(student: dict) -> InlineKeyboardMarkup:
    mode = student.get("mode", "automatic")
    monitoring = student.get("telegram_id") in monitors and monitors[student["telegram_id"]].is_running()
    mode_label = "Manual" if mode == "automatic" else "Automatic"

    buttons = []
    if monitoring:
        buttons.append([InlineKeyboardButton("Stop Monitoring", callback_data="stop")])
    else:
        buttons.append([InlineKeyboardButton("Start Monitoring", callback_data="start")])
    buttons.append([InlineKeyboardButton(f"Switch to {mode_label}", callback_data="switch_mode")])
    buttons.append([InlineKeyboardButton("Status", callback_data="status")])
    if is_admin(student.get("telegram_id", 0)):
        buttons.append([InlineKeyboardButton("Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(buttons)


def get_admin_menu() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("Generate Invitation Code", callback_data="admin_generate")],
        [InlineKeyboardButton("View All Students", callback_data="admin_students")],
        [InlineKeyboardButton("View Active Monitors", callback_data="admin_active")],
    ]
    return InlineKeyboardMarkup(buttons)


# ── Attendance callbacks (called from monitor threads) ──

def make_attendance_callback(telegram_id: int, app: Application):
    """Create callback closures for a specific user's monitor."""
    loop = asyncio.get_running_loop()

    def on_attendance_found(username: str, status: str):
        if status == "marked":
            msg = f"[{username}] Attendance Marked."
            coro = app.bot.send_message(chat_id=telegram_id, text=msg)
        else:
            msg = f"[{username}] Attendance available! Press the button to mark."
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Mark Now", callback_data="mark_now")]
            ])
            coro = app.bot.send_message(chat_id=telegram_id, text=msg, reply_markup=keyboard)
        asyncio.run_coroutine_threadsafe(coro, loop)

    def on_status_update(username: str, message: str):
        logger.info(message)

    return on_attendance_found, on_status_update


# ── /start command ──

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    student = storage.get_student(telegram_id)

    if student:
        username = student["username"]
        await update.message.reply_text(
            f"[{username}] Welcome back!",
            reply_markup=get_main_menu(student),
        )
        return ConversationHandler.END

    # Unregistered user
    buttons = [[InlineKeyboardButton("Enter Invitation Code", callback_data="enter_code")]]
    if is_admin(telegram_id):
        buttons.append([InlineKeyboardButton("Admin Panel", callback_data="admin_panel")])
    await update.message.reply_text(
        "Welcome to KBTU Attendance Bot!\nChoose an option:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ConversationHandler.END


# Invitation code flow

async def enter_code_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Check if already registered
    if storage.get_student(query.from_user.id):
        student = storage.get_student(query.from_user.id)
        await query.edit_message_text(
            f"[{student['username']}] You are already registered!",
            reply_markup=get_main_menu(student),
        )
        return ConversationHandler.END

    await query.edit_message_text("Please enter your invitation code:")
    return AWAITING_CODE


async def receive_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().upper()
    telegram_id = update.effective_user.id

    invitation = storage.get_invitation(code)
    if not invitation:
        await update.message.reply_text("Invalid invitation code. Please try again or contact admin.")
        return AWAITING_CODE

    if invitation.get("used_by") is not None:
        await update.message.reply_text("This invitation code has already been used.")
        return AWAITING_CODE

    context.user_data["invitation_code"] = code
    await update.message.reply_text("Code accepted! Please enter your KBTU username (email):")
    return AWAITING_USERNAME


async def receive_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["kbtu_username"] = update.message.text.strip()
    await update.message.reply_text("Now enter your KBTU password:")
    return AWAITING_PASSWORD


async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    password = update.message.text.strip()
    username = context.user_data["kbtu_username"]
    code = context.user_data["invitation_code"]

    # Delete the password message for security
    try:
        await update.message.delete()
    except Exception:
        pass

    # Register student
    storage.add_student(telegram_id, username, password, code)
    storage.use_invitation(code, telegram_id)

    # Clean up user_data
    context.user_data.clear()

    student = storage.get_student(telegram_id)
    await context.bot.send_message(
        chat_id=telegram_id,
        text=(
            f"[{username}] Registration complete!\n"
            f"Mode: Automatic\n\n"
            f"Use the buttons below to start monitoring."
        ),
        reply_markup=get_main_menu(student),
    )
    return ConversationHandler.END


# Student actions

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = query.from_user.id
    data = query.data

    try:
        await _handle_button(query, telegram_id, data, context)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            raise


async def _handle_button(query, telegram_id, data, context):
    student = storage.get_student(telegram_id)
    if not student and data not in ("enter_code", "admin_panel", "admin_generate", "admin_students", "admin_active"):
        await query.edit_message_text("You are not registered. Use /start to begin.")
        return

    if data == "start":
        username = student["username"]
        if telegram_id in monitors and monitors[telegram_id].is_running():
            await query.edit_message_text(
                f"[{username}] Monitoring is already active.",
                reply_markup=get_main_menu(student),
            )
            return

        on_found, on_status = make_attendance_callback(telegram_id, context.application)
        monitor = AttendanceMonitor(
            username=student["username"],
            password=student["password"],
            mode=student.get("mode", "automatic"),
            on_attendance_found=on_found,
            on_status_update=on_status,
        )
        monitors[telegram_id] = monitor
        monitor.start()
        storage.update_student(telegram_id, monitoring=True)

        await query.edit_message_text(
            f"[{username}] Monitoring started. Mode: {student.get('mode', 'automatic')}.",
            reply_markup=get_main_menu(storage.get_student(telegram_id)),
        )

    elif data == "stop":
        username = student["username"]
        if telegram_id in monitors:
            monitors[telegram_id].stop()
            del monitors[telegram_id]
        storage.update_student(telegram_id, monitoring=False)

        await query.edit_message_text(
            f"[{username}] Monitoring stopped.",
            reply_markup=get_main_menu(storage.get_student(telegram_id)),
        )

    elif data == "switch_mode":
        username = student["username"]
        new_mode = "manual" if student.get("mode", "automatic") == "automatic" else "automatic"
        storage.update_student(telegram_id, mode=new_mode)
        if telegram_id in monitors:
            monitors[telegram_id].set_mode(new_mode)

        await query.edit_message_text(
            f"[{username}] Mode switched to {new_mode}.",
            reply_markup=get_main_menu(storage.get_student(telegram_id)),
        )

    elif data == "status":
        username = student["username"]
        mode = student.get("mode", "automatic")
        running = telegram_id in monitors and monitors[telegram_id].is_running()
        status_text = "active" if running else "inactive"

        await query.edit_message_text(
            f"[{username}] Status\n"
            f"Mode: {mode}\n"
            f"Monitoring: {status_text}",
            reply_markup=get_main_menu(storage.get_student(telegram_id)),
        )

    elif data == "mark_now":
        username = student["username"]
        if telegram_id not in monitors:
            await query.edit_message_text(f"[{username}] Monitor is not running.")
            return
        success = monitors[telegram_id].mark_now()
        if success:
            await query.edit_message_text(f"[{username}] Attendance Marked.")
        else:
            await query.edit_message_text(f"[{username}] Failed to mark. Button may have expired.")

    # Admin callbacks

    elif data == "admin_panel":
        if not is_admin(telegram_id):
            await query.edit_message_text("Access denied.")
            return
        await query.edit_message_text("Admin Panel", reply_markup=get_admin_menu())

    elif data == "admin_generate":
        if not is_admin(telegram_id):
            await query.edit_message_text("Access denied.")
            return
        code = storage.create_invitation(telegram_id)
        await query.edit_message_text(
            f"New invitation code:\n\n`{code}`\n\nShare this with a student.",
            reply_markup=get_admin_menu(),
            parse_mode="Markdown",
        )

    elif data == "admin_students":
        if not is_admin(telegram_id):
            await query.edit_message_text("Access denied.")
            return
        students = storage.get_all_students()
        if not students:
            await query.edit_message_text("No registered students.", reply_markup=get_admin_menu())
            return

        lines = []
        for tid, s in students.items():
            running = int(tid) in monitors and monitors[int(tid)].is_running()
            status = "ACTIVE" if running else "inactive"
            lines.append(f"• {s['username']} | {s['mode']} | {status}")

        await query.edit_message_text(
            "Registered Students:\n\n" + "\n".join(lines),
            reply_markup=get_admin_menu(),
        )

    elif data == "admin_active":
        if not is_admin(telegram_id):
            await query.edit_message_text("Access denied.")
            return
        active = []
        for tid, mon in monitors.items():
            if mon.is_running():
                s = storage.get_student(tid)
                if s:
                    active.append(f"• {s['username']} | {s['mode']}")
        if not active:
            await query.edit_message_text("No active monitors.", reply_markup=get_admin_menu())
        else:
            await query.edit_message_text(
                "Active Monitors:\n\n" + "\n".join(active),
                reply_markup=get_admin_menu(),
            )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set in .env")
        return
    if not ADMIN_TELEGRAM_ID:
        logger.warning("ADMIN_TELEGRAM_ID not set in .env — admin features disabled")

    # Reset stale monitoring flags from previous run
    students = storage.get_all_students()
    for tid, s in students.items():
        if s.get("monitoring"):
            storage.update_student(int(tid), monitoring=False)
            logger.info(f"[{s['username']}] Reset stale monitoring flag")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Conversation handler for registration flow
    registration_conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_command),
            CallbackQueryHandler(enter_code_callback, pattern="^enter_code$"),
        ],
        states={
            AWAITING_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_code)],
            AWAITING_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_username)],
            AWAITING_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=True,
    )

    app.add_handler(registration_conv)
    app.add_handler(CallbackQueryHandler(button_callback))

    # Watchdog: periodically check for dead monitors and clean them up
    async def watchdog(context: ContextTypes.DEFAULT_TYPE):
        dead = []
        for tid, mon in list(monitors.items()):
            if not mon.is_running():
                dead.append(tid)
        for tid in dead:
            s = storage.get_student(tid)
            username = s["username"] if s else str(tid)
            logger.warning(f"[{username}] Monitor thread died — cleaning up")
            del monitors[tid]
            storage.update_student(tid, monitoring=False)
            try:
                await context.bot.send_message(
                    chat_id=tid,
                    text=f"[{username}] Monitor crashed and stopped. Press Start Monitoring to restart.",
                    reply_markup=get_main_menu(storage.get_student(tid)),
                )
            except Exception:
                pass

    app.job_queue.run_repeating(watchdog, interval=60, first=30)

    logger.info("Bot started. Polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
