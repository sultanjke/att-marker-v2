import time
import threading
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

LOGIN_URL = "https://wsp.kbtu.kz/RegistrationOnline"
REFRESH_INTERVAL = 20 # seconds


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

    def _create_driver(self):
        options = Options()
        options.add_argument("--ignore-certificate-errors")
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
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

        self._notify_status(f"[{self.username}] Login attempted. URL: {driver.current_url}")

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
        try:
            with self._driver_lock:
                self._driver = self._create_driver()
            driver = self._driver
            wait = WebDriverWait(driver, 15)
            if not self.skip_login:
                self._do_login(driver, wait)

            refresh_count = 0
            while not self._stop_event.is_set():
                refresh_count += 1
                self._notify_status(
                    f"[{self.username}] [{time.strftime('%H:%M:%S')}] Refresh #{refresh_count}"
                )

                with self._driver_lock:
                    if self._driver is None:
                        break
                    driver.get(self.url)

                time.sleep(3)

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
                    pass  # Button not available

                # Wait before next refresh
                for _ in range(REFRESH_INTERVAL):
                    if self._stop_event.is_set():
                        break
                    time.sleep(1)

        except Exception as e:
            self._notify_status(f"[{self.username}] Monitor error: {e}")
        finally:
            with self._driver_lock:
                if self._driver:
                    try:
                        self._driver.quit()
                    except Exception:
                        pass
                    self._driver = None
            self._notify_status(f"[{self.username}] Monitor stopped.")
