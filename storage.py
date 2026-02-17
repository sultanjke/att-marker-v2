import os
import json
import string
import random
import threading
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
STUDENTS_FILE = os.path.join(DATA_DIR, "students.json")
INVITATIONS_FILE = os.path.join(DATA_DIR, "invitations.json")


class Storage:
    def __init__(self):
        self._lock = threading.Lock()
        self._ensure_data_dir()

    def _ensure_data_dir(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        if not os.path.exists(STUDENTS_FILE):
            self._write_json(STUDENTS_FILE, {})
        if not os.path.exists(INVITATIONS_FILE):
            self._write_json(INVITATIONS_FILE, {})

    def _read_json(self, path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def _write_json(self, path, data):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    # students

    def get_all_students(self):
        with self._lock:
            return self._read_json(STUDENTS_FILE)

    def get_student(self, telegram_id):
        with self._lock:
            students = self._read_json(STUDENTS_FILE)
            return students.get(str(telegram_id))

    def add_student(self, telegram_id, username, password, invitation_code):
        with self._lock:
            students = self._read_json(STUDENTS_FILE)
            students[str(telegram_id)] = {
                "telegram_id": telegram_id,
                "username": username,
                "password": password,
                "mode": "automatic",
                "monitoring": False,
                "invitation_code": invitation_code,
                "registered_at": datetime.now().isoformat(),
            }
            self._write_json(STUDENTS_FILE, students)

    def update_student(self, telegram_id, **kwargs):
        with self._lock:
            students = self._read_json(STUDENTS_FILE)
            key = str(telegram_id)
            if key in students:
                students[key].update(kwargs)
                self._write_json(STUDENTS_FILE, students)
                return True
            return False

    # invitation codes

    def get_all_invitations(self):
        with self._lock:
            return self._read_json(INVITATIONS_FILE)

    def get_invitation(self, code):
        with self._lock:
            invitations = self._read_json(INVITATIONS_FILE)
            return invitations.get(code)

    def create_invitation(self, created_by):
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
        with self._lock:
            invitations = self._read_json(INVITATIONS_FILE)
            invitations[code] = {
                "created_by": created_by,
                "created_at": datetime.now().isoformat(),
                "used_by": None,
                "used_at": None,
            }
            self._write_json(INVITATIONS_FILE, invitations)
        return code

    def use_invitation(self, code, telegram_id):
        with self._lock:
            invitations = self._read_json(INVITATIONS_FILE)
            if code not in invitations:
                return False
            if invitations[code]["used_by"] is not None:
                return False
            invitations[code]["used_by"] = telegram_id
            invitations[code]["used_at"] = datetime.now().isoformat()
            self._write_json(INVITATIONS_FILE, invitations)
            return True
