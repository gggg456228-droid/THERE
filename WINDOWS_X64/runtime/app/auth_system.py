import json
import os
import threading
import time
from functools import wraps

from flask import session

ADMIN_USERNAME = "local"
_state_lock = threading.RLock()


def _data_dir():
    path = os.environ.get("THERE_DATA_DIR") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local", "THERE", "data"
    )
    os.makedirs(path, exist_ok=True)
    return path


def _profile_path():
    return os.path.join(_data_dir(), "local_profile.json")


def _clean_profile(value):
    return dict(value) if isinstance(value, dict) else {}


def load_local_state():
    acquired = _state_lock.acquire(timeout=3.0)
    if not acquired:
        return {ADMIN_USERNAME: {}}
    try:
        profile = {}
        try:
            with open(_profile_path(), "r", encoding="utf-8") as handle:
                profile = _clean_profile(json.load(handle))
        except Exception:
            pass
        return {ADMIN_USERNAME: profile}
    finally:
        _state_lock.release()


def save_local_state(data):
    acquired = _state_lock.acquire(timeout=5.0)
    if not acquired:
        return False

    temporary = None
    try:
        profile = _clean_profile(dict((data or {}).get(ADMIN_USERNAME) or {}))
        target = _profile_path()
        temporary = f"{target}.{os.getpid()}.{threading.get_ident()}.tmp"

        with open(temporary, "w", encoding="utf-8", newline="") as handle:
            json.dump(profile, handle, ensure_ascii=False, indent=2)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except Exception:
                pass

        last_error = None
        for attempt in range(7):
            try:
                os.replace(temporary, target)
                last_error = None
                break
            except PermissionError as error:
                last_error = error
                time.sleep(0.05 * (attempt + 1))

        if last_error is not None:
            raise last_error
        return True
    except Exception:
        return False
    finally:
        if temporary:
            try:
                if os.path.exists(temporary):
                    os.remove(temporary)
            except Exception:
                pass
        _state_lock.release()


def init_auth_system():
    return True


def get_current_user():
    return {"username": ADMIN_USERNAME, "role": "admin"}


def is_admin():
    return True


def is_player():
    return False


def require_login(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        session.setdefault("username", ADMIN_USERNAME)
        return function(*args, **kwargs)
    return wrapped


def require_admin(function):
    return require_login(function)


def get_user_character_name(username=None):
    return (load_local_state().get(ADMIN_USERNAME) or {}).get("character_name", "")


def accept_license_for_user(username, version):
    return "ok"
