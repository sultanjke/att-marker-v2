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


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_TELEGRAM_ID = _env_int("ADMIN_TELEGRAM_ID", 0)
MAX_ACTIVE_MONITORS = _env_int("MAX_ACTIVE_MONITORS", 30)

SINGLE_USER_MODE = _env_bool("SINGLE_USER_MODE", False)
SINGLE_USER_TELEGRAM_ID = _env_int("SINGLE_USER_TELEGRAM_ID", ADMIN_TELEGRAM_ID)
SINGLE_USER_AUTOSTART = _env_bool("SINGLE_USER_AUTOSTART", True)
SINGLE_USER_MONITOR_MODE_RAW = os.getenv("SINGLE_USER_MONITOR_MODE", "automatic").strip().lower()
SINGLE_USER_MONITOR_MODE = (
    SINGLE_USER_MONITOR_MODE_RAW if SINGLE_USER_MONITOR_MODE_RAW in {"automatic", "manual"} else "automatic"
)
KBTU_USERNAME = os.getenv("KBTU_USERNAME", "").strip()
KBTU_PASSWORD = os.getenv("KBTU_PASSWORD", "").strip()
SINGLE_USER_AUTOSTART_MAX_ATTEMPTS = max(1, _env_int("SINGLE_USER_AUTOSTART_MAX_ATTEMPTS", 12))
SINGLE_USER_AUTOSTART_BASE_DELAY = max(1, _env_int("SINGLE_USER_AUTOSTART_BASE_DELAY", 5))
EFFECTIVE_MAX_ACTIVE_MONITORS = 1 if SINGLE_USER_MODE else max(1, MAX_ACTIVE_MONITORS)

storage = Storage()
monitors: dict[int, AttendanceMonitor] = {}  # telegram_id -> AttendanceMonitor

# Conversation states
AWAITING_CODE = 1
AWAITING_USERNAME = 2
AWAITING_PASSWORD = 3

SINGLE_USER_AUTOSTART_JOB_NAME = "single-user-autostart"
single_user_runtime_mode = SINGLE_USER_MONITOR_MODE
single_user_autostart_disabled = False
single_user_autostart_attempts = 0


# Helpers

def is_admin(telegram_id: int) -> bool:
    return telegram_id == ADMIN_TELEGRAM_ID


def is_allowed_user(telegram_id: int) -> bool:
    if not SINGLE_USER_MODE:
        return True
    return telegram_id == SINGLE_USER_TELEGRAM_ID


def single_user_config_error() -> str | None:
    if not SINGLE_USER_MODE:
        return None
    if SINGLE_USER_TELEGRAM_ID <= 0:
        return "Single-user mode misconfigured: set SINGLE_USER_TELEGRAM_ID or ADMIN_TELEGRAM_ID."
    if not KBTU_USERNAME or not KBTU_PASSWORD:
        return "Single-user mode misconfigured: set KBTU_USERNAME and KBTU_PASSWORD."
    return None


def get_student(telegram_id: int) -> dict | None:
    if SINGLE_USER_MODE:
        if telegram_id != SINGLE_USER_TELEGRAM_ID:
            return None
        if not KBTU_USERNAME or not KBTU_PASSWORD:
            return None
        return {
            "telegram_id": telegram_id,
            "username": KBTU_USERNAME,
            "password": KBTU_PASSWORD,
            "mode": single_user_runtime_mode,
            "monitoring": telegram_id in monitors and monitors[telegram_id].is_running(),
        }
    return storage.get_student(telegram_id)


def active_monitor_count() -> int:
    return sum(1 for mon in monitors.values() if mon.is_running())


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
    if not SINGLE_USER_MODE and is_admin(student.get("telegram_id", 0)):
        buttons.append([InlineKeyboardButton("Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(buttons)


def get_admin_menu() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("Generate Invitation Code", callback_data="admin_generate")],
        [InlineKeyboardButton("View All Students", callback_data="admin_students")],
        [InlineKeyboardButton("View Active Monitors", callback_data="admin_active")],
    ]
    return InlineKeyboardMarkup(buttons)


def _cancel_single_user_autostart_jobs(job_queue):
    for job in job_queue.get_jobs_by_name(SINGLE_USER_AUTOSTART_JOB_NAME):
        job.schedule_removal()


def _schedule_single_user_autostart(job_queue, delay_seconds: int):
    _cancel_single_user_autostart_jobs(job_queue)
    job_queue.run_once(
        _single_user_autostart_job,
        when=max(0, delay_seconds),
        name=SINGLE_USER_AUTOSTART_JOB_NAME,
    )


def _start_monitor_for_user(telegram_id: int, student: dict, app: Application) -> tuple[bool, str]:
    existing = monitors.get(telegram_id)
    if existing and existing.is_running():
        return False, "already_running"
    if existing and not existing.is_running():
        monitors.pop(telegram_id, None)

    active = active_monitor_count()
    if active >= EFFECTIVE_MAX_ACTIVE_MONITORS:
        return False, f"capacity:{active}/{EFFECTIVE_MAX_ACTIVE_MONITORS}"

    try:
        on_found, on_status = make_attendance_callback(telegram_id, app)
        monitor = AttendanceMonitor(
            username=student["username"],
            password=student["password"],
            mode=student.get("mode", "automatic"),
            on_attendance_found=on_found,
            on_status_update=on_status,
        )
        monitors[telegram_id] = monitor
        monitor.start()
        if not SINGLE_USER_MODE:
            storage.update_student(telegram_id, monitoring=True)
        return True, "started"
    except Exception as e:
        monitors.pop(telegram_id, None)
        logger.exception("Failed to start monitor for %s: %s", telegram_id, e)
        return False, f"exception:{type(e).__name__}"


def _stop_monitor_for_user(telegram_id: int):
    monitor = monitors.pop(telegram_id, None)
    if monitor:
        monitor.stop()
    if not SINGLE_USER_MODE:
        storage.update_student(telegram_id, monitoring=False)


# Attendance callbacks (called from monitor threads)

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


async def _single_user_autostart_job(context: ContextTypes.DEFAULT_TYPE):
    global single_user_autostart_attempts

    if not SINGLE_USER_MODE or not SINGLE_USER_AUTOSTART or single_user_autostart_disabled:
        return

    cfg_error = single_user_config_error()
    if cfg_error:
        logger.error(cfg_error)
        return

    student = get_student(SINGLE_USER_TELEGRAM_ID)
    if not student:
        logger.error("Single-user autostart aborted: student context is unavailable.")
        return

    started, reason = _start_monitor_for_user(
        telegram_id=SINGLE_USER_TELEGRAM_ID,
        student=student,
        app=context.application,
    )
    if started or reason == "already_running":
        single_user_autostart_attempts = 0
        if started:
            logger.info("Single-user monitor auto-started successfully.")
            try:
                await context.bot.send_message(
                    chat_id=SINGLE_USER_TELEGRAM_ID,
                    text=f"[{student['username']}] Monitoring auto-started.",
                )
            except Exception:
                pass
        return

    single_user_autostart_attempts += 1
    if single_user_autostart_attempts > SINGLE_USER_AUTOSTART_MAX_ATTEMPTS:
        logger.error(
            "Single-user autostart gave up after %d attempts. Last reason: %s",
            SINGLE_USER_AUTOSTART_MAX_ATTEMPTS,
            reason,
        )
        return

    delay = min(60, SINGLE_USER_AUTOSTART_BASE_DELAY * single_user_autostart_attempts)
    logger.warning(
        "Single-user autostart failed (%s). Retrying in %ss (attempt %d/%d).",
        reason,
        delay,
        single_user_autostart_attempts,
        SINGLE_USER_AUTOSTART_MAX_ATTEMPTS,
    )
    _schedule_single_user_autostart(context.job_queue, delay)


# /start command

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id

    if not is_allowed_user(telegram_id):
        await update.message.reply_text("Access denied. This bot is configured for one specific Telegram user.")
        return ConversationHandler.END

    if SINGLE_USER_MODE:
        cfg_error = single_user_config_error()
        if cfg_error:
            await update.message.reply_text(cfg_error)
            return ConversationHandler.END

        student = get_student(telegram_id)
        await update.message.reply_text(
            f"[{student['username']}] Welcome back!",
            reply_markup=get_main_menu(student),
        )
        return ConversationHandler.END

    student = storage.get_student(telegram_id)
    if student:
        username = student["username"]
        await update.message.reply_text(
            f"[{username}] Welcome back!",
            reply_markup=get_main_menu(student),
        )
        return ConversationHandler.END

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

    if SINGLE_USER_MODE:
        await query.edit_message_text("Registration is disabled in single-user mode.")
        return ConversationHandler.END

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
    if SINGLE_USER_MODE:
        await update.message.reply_text("Registration is disabled in single-user mode.")
        return ConversationHandler.END

    code = update.message.text.strip().upper()

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
    if SINGLE_USER_MODE:
        await update.message.reply_text("Registration is disabled in single-user mode.")
        return ConversationHandler.END
    context.user_data["kbtu_username"] = update.message.text.strip()
    await update.message.reply_text("Now enter your KBTU password:")
    return AWAITING_PASSWORD


async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if SINGLE_USER_MODE:
        await update.message.reply_text("Registration is disabled in single-user mode.")
        return ConversationHandler.END

    telegram_id = update.effective_user.id
    password = update.message.text.strip()
    username = context.user_data["kbtu_username"]
    code = context.user_data["invitation_code"]

    try:
        await update.message.delete()
    except Exception:
        pass

    storage.add_student(telegram_id, username, password, code)
    storage.use_invitation(code, telegram_id)
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

    if not is_allowed_user(telegram_id):
        await query.edit_message_text("Access denied. This bot is configured for one specific Telegram user.")
        return

    try:
        await _handle_button(query, telegram_id, data, context)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            raise


async def _handle_button(query, telegram_id, data, context):
    global single_user_runtime_mode
    global single_user_autostart_disabled
    global single_user_autostart_attempts

    if SINGLE_USER_MODE and data in ("enter_code", "admin_panel", "admin_generate", "admin_students", "admin_active"):
        await query.edit_message_text("This action is disabled in single-user mode.")
        return

    cfg_error = single_user_config_error()
    if SINGLE_USER_MODE and cfg_error and data != "stop":
        await query.edit_message_text(cfg_error)
        return

    student = get_student(telegram_id)
    if not SINGLE_USER_MODE and not student and data not in (
        "enter_code",
        "admin_panel",
        "admin_generate",
        "admin_students",
        "admin_active",
    ):
        await query.edit_message_text("You are not registered. Use /start to begin.")
        return

    if data == "start":
        if not student:
            await query.edit_message_text("Cannot start monitor: student context is unavailable.")
            return

        if SINGLE_USER_MODE:
            single_user_autostart_disabled = False
            single_user_autostart_attempts = 0
            _cancel_single_user_autostart_jobs(context.application.job_queue)

        started, reason = _start_monitor_for_user(telegram_id, student, context.application)
        username = student["username"]
        if not started and reason == "already_running":
            await query.edit_message_text(
                f"[{username}] Monitoring is already active.",
                reply_markup=get_main_menu(student),
            )
            return
        if not started and reason.startswith("capacity:"):
            active = active_monitor_count()
            await query.edit_message_text(
                f"[{username}] Monitor capacity reached ({active}/{EFFECTIVE_MAX_ACTIVE_MONITORS}). "
                "Try again later or stop another monitor first.",
                reply_markup=get_main_menu(student),
            )
            return
        if not started:
            await query.edit_message_text(
                f"[{username}] Failed to start monitor ({reason}).",
                reply_markup=get_main_menu(student),
            )
            return

        refreshed = get_student(telegram_id) or student
        await query.edit_message_text(
            f"[{username}] Monitoring started. Mode: {refreshed.get('mode', 'automatic')}.",
            reply_markup=get_main_menu(refreshed),
        )

    elif data == "stop":
        if SINGLE_USER_MODE:
            single_user_autostart_disabled = True
            single_user_autostart_attempts = 0
            _cancel_single_user_autostart_jobs(context.application.job_queue)

        username = student["username"] if student else str(telegram_id)
        _stop_monitor_for_user(telegram_id)
        refreshed = get_student(telegram_id)
        reply_markup = get_main_menu(refreshed) if refreshed else None
        await query.edit_message_text(
            f"[{username}] Monitoring stopped.",
            reply_markup=reply_markup,
        )

    elif data == "switch_mode":
        if not student:
            await query.edit_message_text("Cannot switch mode: student context is unavailable.")
            return
        username = student["username"]
        new_mode = "manual" if student.get("mode", "automatic") == "automatic" else "automatic"
        if SINGLE_USER_MODE:
            single_user_runtime_mode = new_mode
        else:
            storage.update_student(telegram_id, mode=new_mode)
        if telegram_id in monitors:
            monitors[telegram_id].set_mode(new_mode)

        refreshed = get_student(telegram_id) or student
        await query.edit_message_text(
            f"[{username}] Mode switched to {new_mode}.",
            reply_markup=get_main_menu(refreshed),
        )

    elif data == "status":
        if not student:
            await query.edit_message_text("Cannot show status: student context is unavailable.")
            return
        username = student["username"]
        mode = student.get("mode", "automatic")
        running = telegram_id in monitors and monitors[telegram_id].is_running()
        status_text = "active" if running else "inactive"

        refreshed = get_student(telegram_id) or student
        await query.edit_message_text(
            f"[{username}] Status\n"
            f"Mode: {mode}\n"
            f"Monitoring: {status_text}",
            reply_markup=get_main_menu(refreshed),
        )

    elif data == "mark_now":
        if not student:
            await query.edit_message_text("Cannot mark attendance: student context is unavailable.")
            return
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
            lines.append(f"- {s['username']} | {s['mode']} | {status}")

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
                    active.append(f"- {s['username']} | {s['mode']}")
        if not active:
            await query.edit_message_text("No active monitors.", reply_markup=get_admin_menu())
        else:
            await query.edit_message_text(
                "Active Monitors:\n\n" + "\n".join(active),
                reply_markup=get_admin_menu(),
            )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    if not is_allowed_user(telegram_id):
        await update.message.reply_text("Access denied. This bot is configured for one specific Telegram user.")
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set in .env")
        return

    if SINGLE_USER_MODE:
        logger.info(
            "Single-user mode enabled for Telegram ID %s with monitor cap %s.",
            SINGLE_USER_TELEGRAM_ID,
            EFFECTIVE_MAX_ACTIVE_MONITORS,
        )
        if SINGLE_USER_MONITOR_MODE_RAW not in {"automatic", "manual"}:
            logger.warning(
                "Invalid SINGLE_USER_MONITOR_MODE='%s'. Falling back to 'automatic'.",
                SINGLE_USER_MONITOR_MODE_RAW,
            )
        cfg_error = single_user_config_error()
        if cfg_error:
            logger.error(cfg_error)
    else:
        if not ADMIN_TELEGRAM_ID:
            logger.warning("ADMIN_TELEGRAM_ID not set in .env - admin features disabled")

        students = storage.get_all_students()
        for tid, s in students.items():
            if s.get("monitoring"):
                storage.update_student(int(tid), monitoring=False)
                logger.info("[%s] Reset stale monitoring flag", s["username"])

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    if SINGLE_USER_MODE:
        app.add_handler(CommandHandler("start", start_command))
        app.add_handler(CommandHandler("cancel", cancel))
    else:
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

    async def watchdog(context: ContextTypes.DEFAULT_TYPE):
        dead = []
        for tid, mon in list(monitors.items()):
            if not mon.is_running():
                dead.append(tid)
        for tid in dead:
            monitor = monitors.pop(tid, None)
            if monitor:
                try:
                    monitor.stop()
                except Exception:
                    pass

            username = str(tid)
            student = get_student(tid) if SINGLE_USER_MODE else storage.get_student(tid)
            if student:
                username = student["username"]
            if not SINGLE_USER_MODE:
                storage.update_student(tid, monitoring=False)

            logger.warning("[%s] Monitor thread died - cleaning up", username)
            try:
                kwargs = {}
                if student:
                    kwargs["reply_markup"] = get_main_menu(student)
                await context.bot.send_message(
                    chat_id=tid,
                    text=f"[{username}] Monitor crashed and stopped. Press Start Monitoring to restart.",
                    **kwargs,
                )
            except Exception:
                pass

    app.job_queue.run_repeating(watchdog, interval=60, first=30)

    if SINGLE_USER_MODE and SINGLE_USER_AUTOSTART:
        if single_user_config_error() is None:
            _schedule_single_user_autostart(app.job_queue, delay_seconds=2)
            logger.info("Single-user autostart scheduled.")
        else:
            logger.error("Single-user autostart skipped due to configuration errors.")

    logger.info("Bot started. Polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
