# Attendance Marker v2 - KBTU

Automated attendance marking system for KBTU's online registration portal (`wsp.kbtu.kz`), controlled entirely through a private Telegram bot with an invitation-based whitelist system.

When a teacher opens online attendance on the portal, the system detects it and either marks it automatically and notifies the student via Telegram — depending on the chosen mode.

## How It Works

1. **Admin** generates a one-time invitation code via the Telegram bot
2. **Whitelisted Student** enters the invitation code in the bot, provides KBTU credentials (username + password)
3. Student's Telegram account is **permanently linked via telegram_id** — no logout, lifetime session
4. Student presses **Start Monitoring** — the system launches a headless Chrome instance, logs into `wsp.kbtu.kz`, and polls the page every 20 seconds
5. When the `Отметиться` (Check In) button appears:
   - **Automatic mode** (default): clicks the button immediately, sends `[students_email] Attendance Marked.`
   - **Manual mode**: sends `[students_email] Attendance available!` with a `Mark Now` inline button — student decides when to click
6. If the session expires, the system re-authenticates automatically

## Architecture

```
bot.py          Main entry point. Telegram bot (async) + monitor orchestration
monitor.py      Attendance Monitor class. One headless Chrome thread per user
storage.py      Thread-safe JSON file persistence (students, invitation codes)
attendance.py   Legacy standalone script (kept for reference)
data/           Auto-created directory for persistent JSON data
```

**Single-process design**: The Telegram bot runs in the main thread (asyncio), while each student's attendance monitor runs in its own background thread with its own Chrome instance. Communication between monitor threads and the async bot uses `asyncio.run_coroutine_threadsafe()`.

### Data Flow

```
Student presses "Start Monitoring" in Telegram
  → bot.py creates AttendanceMonitor with callbacks
    → monitor.py spawns thread, launches headless Chrome
      → Logs into wsp.kbtu.kz
      → Polls every 35 seconds for "Отметиться" button
        → Button found?
          → Automatic: click + callback → bot sends "[email] Attendance Marked."
          → Manual: callback → bot sends message with "Mark Now" button
            → Student presses "Mark Now" → bot calls monitor.mark_now() → click
```

## Project Structure

```
att-marker-v2/
├── bot.py                 Telegram bot + main entry point
├── monitor.py             Selenium-based attendance monitor class
├── storage.py             Thread-safe JSON storage (students, invitations)
├── attendance.py          Legacy standalone script
├── requirements.txt       Python dependencies
├── Dockerfile             Docker image (Python 3.11 + Chrome)
├── docker-compose.yml     Docker Compose config
├── .env                   Environment variables (not in repo)
└── data/                  Persistent data directory (not in repo)
    ├── students.json      Registered student records
    └── invitations.json   Invitation codes
```

## Setup

### Prerequisites

- Python 3.11+
- Google Chrome (for local development)
- Docker + Docker Compose (for deployment)
- A Telegram Bot token (from [@BotFather](https://t.me/BotFather))

### 1. Clone the Repository

```bash
git clone <repo-url>
cd att-marker-v2
```

### 2. Create `.env` File

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
ADMIN_TELEGRAM_ID=your_telegram_user_id_here
```

To find your Telegram user ID, message [@userinfobot](https://t.me/userinfobot) on Telegram.

### 3a. Run with Docker (Recommended)

```bash
docker compose up --build -d
```

This builds the image with Chrome pre-installed and starts the bot. Data persists in the container. To view logs:

```bash
docker compose logs -f
```

### 3b. Run Locally (Development)

```bash
pip install -r requirements.txt
python bot.py
```

Requires Google Chrome installed on the system. ChromeDriver is managed automatically by `webdriver-manager`.

## Telegram Bot Usage

### First-Time Setup (Admin)

1. Send `/start` to the bot from the account matching `ADMIN_TELEGRAM_ID`
2. Press **Admin Panel**
3. Press **Generate Invitation Code** — the bot returns an 8-character code (e.g. `A3KX9M7P`)
4. Share this code with a student

### Student Registration

1. Student sends `/start` to the bot
2. Presses **Enter Invitation Code**
3. Enters the code received from admin
4. Enters KBTU username (email)
5. Enters KBTU password (the message is deleted automatically for security)
6. Registration complete — account permanently linked to this Telegram ID

### Student Controls

After registration, `/start` shows the main menu:

| Button | Action |
|---|---|
| **Start Monitoring** | Launches headless Chrome, logs into KBTU portal, begins polling |
| **Stop Monitoring** | Stops the Chrome instance and polling |
| **Switch to Manual/Automatic** | Toggles between auto-click and manual confirmation modes |
| **Status** | Shows current mode and monitoring state |

### Admin Controls

The admin panel (only visible to `ADMIN_TELEGRAM_ID`):

| Button | Action |
|---|---|
| **Generate Invitation Code** | Creates a new single-use 8-character code |
| **View All Students** | Lists all registered students with their mode and active/inactive status |
| **View Active Monitors** | Lists only students with currently running monitors |

### Monitoring Modes

**Automatic** (default): When the attendance button appears on the portal, the script clicks it immediately and sends:
```
[student@kbtu.kz] Attendance Marked.
```

**Manual**: When the attendance button appears, the bot sends a message with an inline button:
```
[student@kbtu.kz] Attendance available! Press the button to mark.
[Mark Now]
```
The student has up to 5 minutes to press `Mark Now`. If not pressed, the opportunity times out and will be detected again on the next polling cycle.

### Bot Commands

| Command | Description |
|---|---|
| `/start` | Main menu (registration for new users, controls for existing) |
| `/cancel` | Cancel the current registration flow |

## Module Reference

### `bot.py`

Main entry point. Handles:
- Telegram bot polling via `python-telegram-bot` v21 (async)
- `ConversationHandler` for the registration flow (code → username → password)
- Inline keyboard callbacks for all student and admin actions
- Creates `AttendanceMonitor` instances with callbacks that bridge monitor threads to the async bot

### `monitor.py`

`AttendanceMonitor` class — one instance per active student:
- Launches headless Chrome in a daemon thread
- Logs into `wsp.kbtu.kz/RegistrationOnline`
- Polls every 35 seconds for the `Отметиться` button
- Handles automatic clicking or manual mode with `mark_now()` method
- Auto-detects session expiry and re-authenticates
- Thread-safe driver access via `threading.Lock`

**Public API:**
```python
monitor = AttendanceMonitor(username, password, mode, on_attendance_found, on_status_update)
monitor.start()         # Spawn monitoring thread
monitor.stop()          # Stop monitoring, quit browser
monitor.set_mode(mode)  # "automatic" or "manual"
monitor.mark_now()      # Click button (manual mode)
monitor.is_running()    # Check if thread is alive
```

### `storage.py`

`Storage` class — thread-safe JSON persistence:
- All reads/writes protected by `threading.Lock`
- Auto-creates `data/` directory and empty JSON files on first run
- Manages `data/students.json` and `data/invitations.json`

**Data schemas:**

`students.json` — keyed by Telegram user ID:
```json
{
  "123456789": {
    "telegram_id": 123456789,
    "username": "student@kbtu.kz",
    "password": "...",
    "mode": "automatic",
    "monitoring": false,
    "invitation_code": "A3KX9M7P",
    "registered_at": "2025-01-01T00:00:00"
  }
}
```

`invitations.json` — keyed by code:
```json
{
  "A3KX9M7P": {
    "created_by": 111111111,
    "created_at": "2025-01-01T00:00:00",
    "used_by": 123456789,
    "used_at": "2025-01-02T00:00:00"
  }
}
```

## Configuration

All configuration is done through the `.env` file:

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `ADMIN_TELEGRAM_ID` | Yes | Telegram user ID of the admin account |

Hardcoded constants in `monitor.py`:

| Constant | Value | Description |
|---|---|---|
| `LOGIN_URL` | `https://wsp.kbtu.kz/RegistrationOnline` | KBTU portal URL |
| `REFRESH_INTERVAL` | `20` | Seconds between page polls |

## Docker

### Dockerfile

Based on `python:3.11-slim` with Google Chrome installed. Runs `bot.py` as the entry point. The `data/` directory is created at build time for persistent storage.

### docker-compose.yml

```yaml
services:
  autoscraper:
    build: .
    container_name: kbtu-attendance
    restart: unless-stopped
    env_file:
      - .env
```

To persist data across container rebuilds, add a volume:

```yaml
services:
  autoscraper:
    build: .
    container_name: kbtu-attendance
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - ./data:/app/data
```

## Security Notes

- **Password messages are deleted** from Telegram chat immediately after the bot reads them
- **Invitation codes are single-use** — once redeemed, they cannot be reused
- **Permanent sessions** — once a student registers, their Telegram ID is permanently linked (no logout)
- **Credentials stored in plain text** in `data/students.json` — ensure this file is protected. The `data/` directory is in `.gitignore`
- **`.env` is in `.gitignore`** — never commit bot tokens or admin IDs

## Tech Stack

- **Python 3.11**
- **Selenium 4.40** — headless Chrome automation
- **python-telegram-bot 21.6** — async Telegram Bot API
- **webdriver-manager** — automatic ChromeDriver management
- **python-dotenv** — environment variable loading
- **Docker** — containerized deployment with Chrome
