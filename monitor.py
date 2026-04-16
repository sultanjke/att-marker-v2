import os
import time
import threading
import hashlib
import shutil
import tempfile
import signal
from selenium import webdriver
from selenium.common.exceptions import SessionNotCreatedException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

LOGIN_URL = "https://wsp.kbtu.kz/RegistrationOnline"
REFRESH_INTERVAL = 30 # seconds


def _env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)).strip())
    except Exception:
        return default


class AttendanceMonitor:
    """
    Monitors the KBTU attendance page for a single user.
    Runs in its own thread with its own Chrome instance.

    Callbacks:
        on_attendance_found(username, status)
            status = "marked"  → auto mode, button already clicked
            status = "found"   → manual mode, waiting for user action
        on_status_update(username, message)
            general status messages (login, errors, etc.)
    """

    def __init__(self, username, password, on_attendance_found=None, on_status_update=None, mode="automatic", url=None, skip_login=False):
        self.username = username
        self.password = password
        self.mode = mode
        self.url = url or LOGIN_URL
        self.skip_login = skip_login
        self.on_attendance_found = on_attendance_found
        self.on_status_update = on_status_update

        self._stop_event = threading.Event()
        self._thread = None
        self._driver = None
        self._driver_lock = threading.Lock()
        self._pending_mark = False  # True when manual mode found button, waiting for user
        safe_id = hashlib.sha1(username.encode("utf-8")).hexdigest()[:12]
        self._profile_tag = f"kbtu-chrome-profile-{safe_id}"
        tmp_dir = tempfile.gettempdir()
        self._profile_dir = os.path.join(tmp_dir, self._profile_tag)
        self._chromedriver_log_path = os.path.join(tmp_dir, f"kbtu-chromedriver-{safe_id}.log")
        self._chrome_restart_every = max(0, _env_int("CHROME_RESTART_EVERY", 40))
        self._pid_min_free = max(5, _env_int("CHROME_PID_MIN_FREE", 20))
        self._pid_wait_max = max(0, _env_int("CHROME_PID_WAIT_MAX", 300))

    # Public API

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        with self._driver_lock:
            if self._driver:
                try:
                    self._driver.quit()
                except Exception:
                    pass
                self._driver = None
        self._kill_stale_profile_processes()
        if (
            self._thread
            and self._thread.is_alive()
            and threading.current_thread() is not self._thread
        ):
            self._thread.join(timeout=10)

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def set_mode(self, mode):
        self.mode = mode

    def mark_now(self):
        """Called from bot when user presses 'Mark Now' in manual mode."""
        with self._driver_lock:
            if not self._driver or not self._pending_mark:
                return False
            try:
                btn = WebDriverWait(self._driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH,
                        "//span[@class='v-button-caption' and text()='\u041e\u0442\u043c\u0435\u0442\u0438\u0442\u044c\u0441\u044f']"
                        "/ancestor::div[contains(@class, 'v-button')]"))
                )
                btn.click()
                self._pending_mark = False
                self._notify_found(self.username, "marked")
                return True
            except Exception as e:
                self._notify_status(f"[{self.username}] Failed to mark: {e}")
                return False

    # ── Internal ──

    def _notify_found(self, username, status):
        if self.on_attendance_found:
            try:
                self.on_attendance_found(username, status)
            except Exception:
                pass

    def _notify_status(self, message):
        if self.on_status_update:
            try:
                self.on_status_update(self.username, message)
            except Exception:
                pass

    def _reset_profile_dir(self):
        try:
            shutil.rmtree(self._profile_dir, ignore_errors=True)
            os.makedirs(self._profile_dir, exist_ok=True)
        except Exception:
            pass

    def _purge_profile_dir(self):
        try:
            shutil.rmtree(self._profile_dir, ignore_errors=True)
        except Exception:
            pass

    def _tail_chromedriver_log(self, lines=40):
        try:
            with open(self._chromedriver_log_path, "r", encoding="utf-8", errors="replace") as f:
                data = f.readlines()
            return "".join(data[-lines:]).strip()
        except Exception:
            return ""

    def _pick_chrome_binary(self):
        configured = os.environ.get("CHROME_BIN")
        if configured and os.path.exists(configured):
            return configured

        # Prefer the real Chromium binary over the wrapper script in constrained containers.
        candidates = (
            "/usr/lib/chromium/chromium",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
        )
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        return None

    def _pid_pressure_snapshot(self):
        values = []
        for path, label in (
            ("/sys/fs/cgroup/pids.current", "pids.current"),
            ("/sys/fs/cgroup/pids.max", "pids.max"),
            ("/sys/fs/cgroup/pids/pids.current", "pids.current(v1)"),
            ("/sys/fs/cgroup/pids/pids.max", "pids.max(v1)"),
        ):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    values.append(f"{label}={f.read().strip()}")
            except Exception:
                pass
        return ", ".join(values)

    def _read_cgroup_pid_stats(self):
        # cgroup v2 first, then v1 fallback
        candidates = (
            ("/sys/fs/cgroup/pids.current", "/sys/fs/cgroup/pids.max"),
            ("/sys/fs/cgroup/pids/pids.current", "/sys/fs/cgroup/pids/pids.max"),
        )
        for current_path, max_path in candidates:
            try:
                with open(current_path, "r", encoding="utf-8") as f:
                    current_raw = f.read().strip()
                with open(max_path, "r", encoding="utf-8") as f:
                    max_raw = f.read().strip()
            except Exception:
                continue

            try:
                current = int(current_raw)
            except Exception:
                continue

            if max_raw == "max":
                return current, None
            try:
                return current, int(max_raw)
            except Exception:
                return current, None
        return None

    def _wait_for_pid_headroom(self):
        min_free = self._pid_min_free
        max_wait = self._pid_wait_max

        stats = self._read_cgroup_pid_stats()
        if not stats:
            return True
        current, max_pids = stats
        if max_pids is None:
            return True
        if (max_pids - current) >= min_free:
            return True

        deadline = time.time() + max_wait
        next_log = 0
        while not self._stop_event.is_set():
            stats = self._read_cgroup_pid_stats()
            if not stats:
                return True
            current, max_pids = stats
            if max_pids is None:
                return True
            free = max_pids - current
            if free >= min_free:
                return True

            now = time.time()
            if now >= next_log:
                self._notify_status(
                    f"[{self.username}] Waiting for PID headroom before Chrome start "
                    f"(current={current}, max={max_pids}, free={free}, need>={min_free})"
                )
                next_log = now + 10

            if now >= deadline:
                return False
            time.sleep(1)
        return False

    def _kill_stale_profile_processes(self):
        if os.name != "posix":
            return
        proc_dir = "/proc"
        if not os.path.isdir(proc_dir):
            return

        killed = 0
        me = os.getpid()
        for name in os.listdir(proc_dir):
            if not name.isdigit():
                continue
            pid = int(name)
            if pid == me:
                continue
            cmdline_path = os.path.join(proc_dir, name, "cmdline")
            try:
                with open(cmdline_path, "rb") as f:
                    raw = f.read()
            except Exception:
                continue
            if not raw:
                continue
            cmd = raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore")
            if self._profile_tag not in cmd:
                continue

            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except ProcessLookupError:
                pass
            except PermissionError:
                pass
            except Exception:
                pass

        if killed:
            self._notify_status(f"[{self.username}] Cleaned stale Chromium processes: {killed}")

    def _create_driver(self):
        self._kill_stale_profile_processes()
        if not self._wait_for_pid_headroom():
            raise SessionNotCreatedException(
                "PID_HEADROOM_TIMEOUT: Insufficient PID headroom to launch Chrome."
            )
        self._reset_profile_dir()

        options = Options()
        options.add_argument("--ignore-certificate-errors")

        headless_mode = os.environ.get("CHROME_HEADLESS_MODE", "new").strip().lower()
        if headless_mode in ("legacy", "old", "classic"):
            options.add_argument("--headless")
        else:
            options.add_argument("--headless=new")

        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-default-apps")
        options.add_argument("--disable-sync")
        options.add_argument("--disable-translate")
        options.add_argument("--disable-breakpad")
        options.add_argument("--disable-crash-reporter")
        options.add_argument("--disable-features=Crashpad")
        options.add_argument("--disable-background-timer-throttling")
        options.add_argument("--disable-renderer-backgrounding")
        options.add_argument("--disable-backgrounding-occluded-windows")
        options.add_argument("--renderer-process-limit=1")
        options.add_argument("--no-zygote")
        pid_stats = self._read_cgroup_pid_stats()
        if pid_stats and pid_stats[1] and pid_stats[1] <= 1500:
            # Low PID ceilings benefit from single-process mode.
            options.add_argument("--single-process")
        options.add_argument("--no-first-run")
        options.add_argument("--window-size=1280,800")
        options.add_argument("--remote-debugging-pipe")
        options.add_argument(f"--user-data-dir={self._profile_dir}")

        # Use a known local browser binary path; avoid wrapper scripts when possible.
        chrome_bin = self._pick_chrome_binary()
        if chrome_bin:
            options.binary_location = chrome_bin

        extra_args = os.environ.get("CHROME_EXTRA_ARGS", "").strip()
        if extra_args:
            for arg in extra_args.split():
                options.add_argument(arg)

        service_args = ["--verbose", f"--log-path={self._chromedriver_log_path}"]
        chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
        if chromedriver_path:
            driver = webdriver.Chrome(
                service=Service(chromedriver_path, service_args=service_args),
                options=options,
            )
        else:
            driver = webdriver.Chrome(
                service=Service(service_args=service_args),
                options=options,
            )
        return driver

    def _do_login(self, driver, wait):
        self._notify_status(f"[{self.username}] Logging in...")
        driver.get(self.url)

        username_field = wait.until(
            EC.presence_of_element_located(
                (By.XPATH, "//input[contains(@class, 'v-filterselect-input')]")
            )
        )
        username_field.clear()
        username_field.send_keys(self.username)
        time.sleep(0.5)

        password_field = driver.find_element(By.XPATH, "//input[@type='password']")
        password_field.clear()
        password_field.send_keys(self.password)

        login_button = driver.find_element(
            By.XPATH, "//div[contains(@class, 'v-button') and contains(@class, 'primary')]"
        )
        login_button.click()
        time.sleep(5)

        self._notify_status(f"[{self.username}] After login URL: {driver.current_url}")

        # Screenshot
        try:
            screenshot_path = f"/tmp/login_result_{self.username}.png"
            driver.save_screenshot(screenshot_path)
            self._notify_status(f"[{self.username}] Screenshot saved to {screenshot_path}")
        except Exception:
            pass

        # Check for errors on page
        try:
            errors = driver.find_elements(By.XPATH,
                "//*[contains(@class, 'error') or contains(@class, 'v-Notification') or contains(@class, 'warning')]")
            for err in errors:
                if err.text.strip():
                    self._notify_status(f"[{self.username}] [ERROR ON PAGE] {err.text}")
        except Exception:
            pass

        # Page text (first 500 chars)
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text
            self._notify_status(f"[{self.username}] [PAGE TEXT] {body_text[:500]}")
        except Exception:
            pass

        # Post-login buttons
        try:
            all_buttons = driver.find_elements(By.XPATH, "//span[@class='v-button-caption']")
            btn_texts = [b.text for b in all_buttons if b.text.strip()]
            self._notify_status(f"[{self.username}] [POST-LOGIN BUTTONS] {btn_texts}")
            if '\u041a\u0456\u0440\u0443' in btn_texts or '\u0412\u043e\u0439\u0442\u0438' in btn_texts:
                self._notify_status(f"[{self.username}] !!! LOGIN FAILED - still on login page !!!")
            else:
                self._notify_status(f"[{self.username}] LOGIN SUCCESS - inside the app")
        except Exception as e:
            self._notify_status(f"[{self.username}] Error checking buttons: {e}")

    def _is_session_expired(self, driver):
        try:
            buttons = driver.find_elements(By.XPATH, "//span[@class='v-button-caption']")
            for btn in buttons:
                if btn.text in ["\u041a\u0456\u0440\u0443", "\u0412\u043e\u0439\u0442\u0438", "Login"]:
                    return True
            login_fields = driver.find_elements(By.XPATH, "//input[@type='password']")
            if login_fields:
                return True
            return False
        except Exception:
            return False

    def _run(self):
        self._notify_status(f"[{self.username}] Starting monitor...")
        restart_count = 0
        max_restarts = 50

        while not self._stop_event.is_set() and restart_count <= max_restarts:
            if restart_count > 0:
                wait_time = min(30, 5 * restart_count)
                self._notify_status(f"[{self.username}] Restarting monitor (attempt {restart_count}/{max_restarts}) in {wait_time}s...")
                for _ in range(wait_time):
                    if self._stop_event.is_set():
                        return
                    time.sleep(1)

            try:
                with self._driver_lock:
                    self._driver = self._create_driver()
                driver = self._driver
                wait = WebDriverWait(driver, 15)
                if not self.skip_login:
                    self._do_login(driver, wait)

                restart_count = 0  # Reset on successful start

                refresh_count = 0
                while not self._stop_event.is_set():
                    refresh_count += 1

                    # Periodic Chrome restart to prevent memory leaks
                    if self._chrome_restart_every > 0 and refresh_count > self._chrome_restart_every:
                        self._notify_status(f"[{self.username}] Restarting Chrome to free memory...")
                        break  # exits inner loop, goes to finally which quits driver, then outer loop creates new one

                    self._notify_status(
                        f"[{self.username}] [{time.strftime('%H:%M:%S')}] Refresh #{refresh_count}"
                    )

                    with self._driver_lock:
                        if self._driver is None:
                            break
                        driver.get(self.url)

                    time.sleep(3)

                    # Debug: show current URL and all buttons
                    self._notify_status(f"[{self.username}] [URL] {driver.current_url}")
                    try:
                        all_buttons = driver.find_elements(By.XPATH, "//span[@class='v-button-caption']")
                        btn_texts = [b.text for b in all_buttons if b.text.strip()]
                        self._notify_status(f"[{self.username}] [ALL BUTTONS] {btn_texts}")
                    except Exception:
                        pass

                    if not self.skip_login and self._is_session_expired(driver):
                        self._notify_status(f"[{self.username}] Session expired, re-logging in...")
                        self._do_login(driver, wait)
                        time.sleep(5)

                    # Look for attendance button
                    try:
                        otmetitsya_button = WebDriverWait(driver, 5).until(
                            EC.element_to_be_clickable((By.XPATH,
                                "//span[@class='v-button-caption' and text()='\u041e\u0442\u043c\u0435\u0442\u0438\u0442\u044c\u0441\u044f']"
                                "/ancestor::div[contains(@class, 'v-button')]"))
                        )

                        if self.mode == "automatic":
                            otmetitsya_button.click()
                            self._notify_status(f"[{self.username}] Attendance button clicked!")
                            self._notify_found(self.username, "marked")
                            time.sleep(2)
                        else:
                            # Manual mode: notify user, wait for mark_now()
                            self._pending_mark = True
                            self._notify_found(self.username, "found")
                            self._notify_status(f"[{self.username}] Attendance available! Waiting for manual mark...")
                            # Wait up to 5 minutes for user to press Mark Now
                            waited = 0
                            while self._pending_mark and waited < 300 and not self._stop_event.is_set():
                                time.sleep(2)
                                waited += 2
                            if self._pending_mark:
                                self._notify_status(f"[{self.username}] Manual mark timed out.")
                                self._pending_mark = False

                    except Exception:
                        # Button not available — debug: show visible buttons
                        try:
                            buttons = driver.find_elements(By.XPATH,
                                "//div[contains(@class, 'v-button')]//span[@class='v-button-caption']")
                            btn_texts = [b.text for b in buttons if b.text.strip()]
                            if btn_texts:
                                self._notify_status(f"[{self.username}] [DEBUG] Buttons on page: {btn_texts}")
                        except Exception:
                            pass

                    # Wait before next refresh
                    for _ in range(REFRESH_INTERVAL):
                        if self._stop_event.is_set():
                            break
                        time.sleep(1)

            except Exception as e:
                self._notify_status(f"[{self.username}] Monitor error ({type(e).__name__}): {e}")
                if isinstance(e, SessionNotCreatedException):
                    error_text = str(e)
                    if "PID_HEADROOM_TIMEOUT" in error_text:
                        self._notify_status(
                            f"[{self.username}] Chrome start blocked: PID headroom wait timed out."
                        )
                    else:
                        self._notify_status(
                            f"[{self.username}] Chrome start failed during Selenium session creation."
                        )
                    pressure = self._pid_pressure_snapshot()
                    if pressure:
                        self._notify_status(f"[{self.username}] [PID pressure] {pressure}")
                    log_tail = self._tail_chromedriver_log()
                    if log_tail:
                        self._notify_status(f"[{self.username}] [ChromeDriver log tail]\n{log_tail}")
                restart_count += 1
            finally:
                with self._driver_lock:
                    if self._driver:
                        try:
                            self._driver.quit()
                        except Exception:
                            pass
                        self._driver = None
                self._pending_mark = False
                self._purge_profile_dir()
                self._kill_stale_profile_processes()

        if restart_count > max_restarts:
            self._notify_status(f"[{self.username}] Monitor gave up after {max_restarts} restarts.")
        self._notify_status(f"[{self.username}] Monitor stopped.")
