from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session, make_response
import json
import math
import os
import random
from datetime import datetime, timedelta
import re
from copy import deepcopy
import threading
import time
import warnings
import sys
import hashlib
import zipfile
from io import BytesIO
import shutil
import secrets
from urllib.parse import quote
from pathlib import Path
from jinja2 import DictLoader
from embedded_templates import TEMPLATES
from github_character_sync import GitHubSyncError, character_summaries, load_github_characters

# Импорт модулей безопасности
from auth_system import (
    init_auth_system,
    get_current_user,
    is_admin,
    is_player,
    require_login,
    require_admin,
    get_user_character_name,
    load_local_state,
    save_local_state,
    accept_license_for_user,
    ADMIN_USERNAME,
)
from security_system import (
    init_security_system, check_security_before_request, log_action,
    get_client_ip, is_ip_blocked, block_ip, check_login_throttle
)
from captcha_system import generate_captcha, verify_captcha, cleanup_old_captchas
from backup_system import init_backup_system, create_backup, list_backups, restore_backup

app = Flask(__name__)
app.jinja_loader = DictLoader(TEMPLATES)

# За обратным прокси (localhost.run, ngrok): корректные схема/хост для редиректов и cookie Secure.
try:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)
except Exception:
    pass

# ВАЖНО: secret_key должен быть постоянным, иначе "запомнить меня" и любые сессии будут слетать после перезапуска сервера.
# Можно задать через переменную окружения FLASK_SECRET_KEY или хранить в локальном файле.
APP_DATA_DIR = os.environ.get("THERE_DATA_DIR") or os.path.join(os.path.expanduser("~"), "AppData", "Local", "THERE", "data")
os.makedirs(APP_DATA_DIR, exist_ok=True)
SECRET_KEY_FILE = os.path.join(APP_DATA_DIR, "session.key")

def _load_or_create_secret_key():
    env_key = os.environ.get("FLASK_SECRET_KEY")
    if env_key and len(env_key.strip()) >= 32:
        return env_key.strip()
    try:
        if os.path.exists(SECRET_KEY_FILE):
            with open(SECRET_KEY_FILE, "r", encoding="utf-8") as f:
                key = (f.read() or "").strip()
                if len(key) >= 32:
                    return key
    except Exception:
        pass
    key = os.urandom(64).hex()
    try:
        with open(SECRET_KEY_FILE, "w", encoding="utf-8") as f:
            f.write(key)
    except Exception:
        pass
    return key

app.secret_key = _load_or_create_secret_key()

@app.get("/healthz")
def healthz():
    return {"ok": True}, 200


def _is_fast_save_request():
    return (
        request.headers.get("X-THERE-Save") == "1"
        or request.accept_mimetypes.best == "application/json"
    )

# Подавляем предупреждение о development server (для локального использования это нормально)
warnings.filterwarnings('ignore', category=UserWarning, module='werkzeug')
warnings.filterwarnings('ignore', message='.*development server.*', category=UserWarning)
import logging
# Скрываем предупреждения werkzeug
logging.getLogger('werkzeug').setLevel(logging.ERROR)
# Скрываем предупреждения Flask
logging.getLogger('flask').setLevel(logging.ERROR)
warnings.filterwarnings('ignore', message='.*development server.*', category=UserWarning)
import logging
logging.getLogger('werkzeug').setLevel(logging.ERROR)  # Скрываем логи werkzeug

# Настройки для улучшения производительности и стабильности
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB максимум
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)

# Более надежные настройки cookie сессии (безопаснее)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Конфигурация (файл рядом с сервером — как local_profile.json в auth_system, не зависит от cwd)
_SERVERS_DIR = APP_DATA_DIR
CHARACTERS_FILE = os.path.join(APP_DATA_DIR, "characters.json")
CHARACTER_SNAPSHOTS_DIR = os.path.join(APP_DATA_DIR, "character_snapshots")
ACHIEVEMENTS_FILE = os.path.join(APP_DATA_DIR, "achievements.json")
ACHIEVEMENT_EVENT_AUTOSAVE = "9b6d876c6cc04aca8d9b2d5d98bb4ff4"
ACHIEVEMENT_EVENT_CLICKER = "72f0db23431847ebaa7230b58db3f193"
ACHIEVEMENT_EVENT_QUASAR = "11e9ad9c1f044a648864c547d29b1208"
ACHIEVEMENT_DEFINITIONS = {
    "a1": {"slot": 0, "title": "Сохрани меня полностью."},
    "a2": {"slot": 1, "title": "Симулятор взрыва пк"},
    "a3": {"slot": 2, "title": "Добропорядочный"},
    "a4": {"slot": 3, "title": "Кабуум"},
    "a5": {"slot": 4, "title": "Архивариус"},
}

ACHIEVEMENT_LOCK = threading.RLock()
RUNTIME_GESTURE_TOKENS = {
    "version": secrets.token_urlsafe(24),
}

@app.context_processor
def _runtime_template_tokens():
    return {"there_runtime_token": RUNTIME_GESTURE_TOKENS["version"]}
CACHE_VERSION_FILE = os.path.join(APP_DATA_DIR, "site_cache_version.json")
# (legacy) раньше бои были общие в одном файле:
BATTLES_SHARED_FILE = os.path.join(APP_DATA_DIR, "battles.json")
# теперь бои хранятся отдельно на пользователя:
BATTLES_DIR = os.path.join(APP_DATA_DIR, "battles")
SESSION_TIMEOUT_MINUTES = 30
REMEMBER_ME_DAYS = 30
MAX_SESSIONS_PER_USER = 3


def _load_site_cache_version() -> int:
    try:
        if os.path.exists(CACHE_VERSION_FILE):
            with open(CACHE_VERSION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            v = int((data or {}).get("version", 1))
            return max(1, v)
    except Exception:
        pass
    return 1


def _save_site_cache_version(version: int) -> int:
    version = max(1, int(version))
    try:
        with open(CACHE_VERSION_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"version": version, "updated_at": datetime.now().isoformat()},
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception:
        pass
    return version


def _bump_site_cache_version() -> int:
    current = _load_site_cache_version()
    return _save_site_cache_version(current + 1)

# ===== Лицензионное соглашение (гейт перед использованием) =====
# Важно: меняйте версию только когда реально обновили текст — иначе всех снова гоняют через экран.
# Должно совпадать с последней принятой версией в local_profile.json (иначе вечный цикл «принял — снова логин/лицензия»).
LICENSE_VERSION = "2026-07-19-local-v2.6.3"
# В сессии после успешного POST принятия лицензии (на случай задержки чтения local_profile.json с диска).
_SESSION_LICENSE_VER_KEY = "_dnd_license_accepted_ver"
LICENSE_TEXT = """THERE — ЛИЦЕНЗИОННОЕ СОГЛАШЕНИЕ ДЛЯ ЛОКАЛЬНОГО ПРИЛОЖЕНИЯ
Дата редакции: 2026-07-19
Версия: 2.6.3 LOCAL

ТЕРМИНЫ
1.1. «Программа» — локальное приложение THERE и открываемый им сайт на адресе 127.0.0.1.
1.2. «Пользователь» — человек, который запустил приложение на своём устройстве.
1.3. «Разработчик» — pluf255.

ПРИНЯТИЕ СОГЛАШЕНИЯ
2.1. Запуская THERE и используя листы персонажей, пользователь соглашается с этим документом.
2.2. Если вы не согласны, закройте окно запуска. Сервер остановится, вселенная продолжит существовать без вашего персонажа.

ЛИЦЕНЗИЯ И ПРАВО ИСПОЛЬЗОВАНИЯ
3.1. Программу можно использовать для личных и игровых целей: создавать и редактировать персонажей, вести бои, хранить заметки и обмениваться состоянием листа с мастером.
3.2. Программа предоставляется «как есть». Она может обновляться, иногда вести себя странно и не обязана содержать вообще всё сразу.
3.3. Если THERE внезапно решит сделать вид, что не работает ровно за пять минут до сессии, это классика жанра. Сначала перезапустите приложение, потом браузер, потом уже проклинайте кодера.
3.4. Лист персонажа не гарантирует удачный бросок кубов. Для этого нужна магия, подкуп мастера или очень убедительное выражение лица.

ЛОКАЛЬНАЯ РАБОТА И ДАННЫЕ
4.1. THERE слушает только локальный адрес 127.0.0.1. По умолчанию сайт недоступен другим устройствам и людям из интернета.
4.2. Аккаунтов и паролей в локальной версии нет.
4.3. Персонажи, бои, заметки и настройки хранятся на устройстве пользователя в папке данных THERE.
4.4. Программа не отправляет содержимое персонажей разработчику.
4.5. Перед удалением приложения или переустановкой системы рекомендуется экспортировать нужные данные. Если вы удалили персонажа, потом задумались и сказали «ой», программа не обязана владеть некромантией.
4.6. Не записывайте в заметки пароли от банков, почты и других сервисов. Поле «Предыстория» не является менеджером паролей, даже если биография персонажа очень секретная.

ОГРАНИЧЕНИЯ
5.1. Запрещено распространять изменённую сборку под видом официальной версии THERE.
5.2. Запрещено удалять сведения об авторстве и выдавать разработку за собственную.
5.3. Запрещено внедрять в поля заведомо вредоносные скрипты и распространять заражённую сборку.
5.4. Не просите «версию без защиты, а то мне неудобно». Если защита мешает именно вам, возможно, она нашла свою работу.
5.5. Интерфейс, который браузер уже получил, технически можно изучать через инструменты разработчика. Это не даёт права копировать проект целиком и распространять его как свою работу.

БЕЗОПАСНОСТЬ
6.1. Программа не требует прав администратора для обычной работы.
6.2. Если на устройстве нет подходящего Python и используется загрузочная сборка, THERE может предложить скачать официальный установщик с python.org. Загрузка не должна происходить скрытно.
6.3. Не запускайте THERE из подозрительных перепаковок. Сверяйте контрольную сумму релиза, если она опубликована.
6.4. Если вы считаете, что пароль qwerty достаточно сильный, хорошая новость: в локальной версии паролей вообще нет.

ОТВЕТСТВЕННОСТЬ
7.1. Разработчик не гарантирует отсутствие ошибок, абсолютную сохранность данных и совместимость со всеми браузерами и устройствами.
7.2. Пользователь отвечает за резервные копии.
7.3. Приложение не несёт ответственности за критические провалы, ссоры в партии, внезапную смерть барда и решения мастера, принятые после трёх часов ночи.

ПОДДЕРЖКА
8.1. Если что-то не работает, укажите устройство, систему, браузер, последовательность действий, ожидаемый и фактический результат.
8.2. Сообщение «не работает» без подробностей имеет примерно такую же диагностическую ценность, как заклинание без компонентов.
8.3. Контакт разработчика: https://t.me/pluf255

ИЗМЕНЕНИЯ
9.1. Соглашение и программа могут обновляться.
9.2. Продолжая использовать новую версию, пользователь принимает её актуальные условия.
"""

def _license_accepted(username: str) -> bool:
    if not username:
        return False
    try:
        # Админ вошёл от имени игрока — лицензия уже принята у администратора
        if session.get('impersonator'):
            return True
        if str(session.get(_SESSION_LICENSE_VER_KEY)) == str(LICENSE_VERSION):
            return True
        users = load_local_state()
        u = users.get(username) or {}
        return str(u.get("license_version", "")) == str(LICENSE_VERSION)
    except Exception:
        return False

# ===== Персонажи: поддержка нескольких персонажей на аккаунт =====
def _char_owner(char: dict) -> str:
    if not isinstance(char, dict):
        return ""
    return (char.get("owner") or char.get("owner_username") or "").strip()

def _get_username_aliases(username: str) -> set:
    out = set()
    u = (username or "").strip()
    if not u:
        return out
    out.add(u)
    try:
        users = load_local_state()
        rec = users.get(u) or {}
        legacy = rec.get("legacy_usernames")
        if isinstance(legacy, list):
            for x in legacy:
                sx = (str(x) if x is not None else "").strip()
                if sx:
                    out.add(sx)
    except Exception:
        pass
    return out

def _player_can_access_character(username: str, character_name: str, characters: dict = None) -> bool:
    """Игрок может видеть/редактировать/удалять ТОЛЬКО свои персонажи."""
    if not username or not character_name:
        return False
    if is_admin():
        return True
    if characters is None:
        characters = load_characters()
    ch = characters.get(character_name)
    if not isinstance(ch, dict):
        return False
    owner = _char_owner(ch)
    aliases = _get_username_aliases(username)
    if owner:
        return owner in aliases
    # legacy fallback: если персонаж привязан через users[username].character_name / character_names или совпадает с username
    try:
        users = load_local_state()
        u = users.get(username) or {}
        legacy = (u.get("character_name") or username)
        if legacy == character_name:
            return True
        lst = u.get("character_names")
        if isinstance(lst, list) and character_name in lst:
            return True
    except Exception:
        pass
    return character_name == username

def _ensure_owner_for_legacy(username: str, character_name: str, characters: dict) -> None:
    """Ленивая миграция: если owner не выставлен, но это явно персонаж пользователя — выставляем owner и сохраняем."""
    try:
        if not username or not character_name or not isinstance(characters, dict):
            return
        ch = characters.get(character_name)
        if not isinstance(ch, dict):
            return
        if _char_owner(ch):
            return
        # Важно: _player_can_access_character() сам опирается на owner/character_names,
        # поэтому при поломанной legacy-привязке может вернуть False. Здесь делаем
        # более прямую проверку "явно мой персонаж" и только тогда ставим owner.
        try:
            users = load_local_state()
            u = users.get(username) or {}
            legacy_active = (u.get("character_name") or username)
            lst = u.get("character_names")
            if not isinstance(lst, list):
                lst = []
            if not (character_name == username or character_name == legacy_active or character_name in lst):
                return
        except Exception:
            # безопасный минимум: совпадение по имени
            if character_name != username:
                return
        ch["owner"] = username
        characters[character_name] = ch
        save_characters(characters)
    except Exception:
        pass

def _ensure_user_char_list(username: str, characters: dict) -> None:
    """Ленивая миграция списка персонажей пользователя: добавляем legacy-активного в character_names, если его там нет."""
    try:
        if not username:
            return
        users = load_local_state()
        if username not in users:
            return
        u = users.get(username) or {}
        legacy = (u.get("character_name") or username)
        lst = u.get("character_names")
        if not isinstance(lst, list):
            lst = []
        changed = False
        if legacy and isinstance(characters, dict) and legacy in characters and legacy not in lst:
            lst.append(legacy)
            changed = True
        # также добавим username, если у него есть персонаж с таким именем
        if username and isinstance(characters, dict) and username in characters and username not in lst:
            lst.append(username)
            changed = True
        if changed:
            u["character_names"] = lst[:200]
            users[username] = u
            save_local_state(users)
    except Exception:
        pass

# ===== Постоянное предупреждение об автосохранении (до "больше не показывать") =====
AUTOSAVE_NOTICE_VERSION = "2026-01-30"
AUTOSAVE_NOTICE_TITLE = "ВНИМНИЕ"
AUTOSAVE_NOTICE_TEXT = "Все данные сохроняться раз в 3 секунды, если что то изменили не спишите переключаться между вкладками, сервер не поспивает за вашими пальцами."

# ===== Предупреждение о плановом перезапуске (используется restart_launcher.py) =====
# Файл создаётся/обновляется лаунчером каждые 2 часа.
RESTART_SCHEDULE_FILE = os.path.join(APP_DATA_DIR, ".restart_schedule.json")

def _read_restart_schedule():
    """Читает ближайшее время перезапуска из файла лаунчера. Возвращает datetime или None."""
    try:
        if not os.path.exists(RESTART_SCHEDULE_FILE):
            return None
        with open(RESTART_SCHEDULE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        ts = (data.get("next_restart") or "").strip()
        if not ts:
            return None
        return datetime.fromisoformat(ts)
    except Exception:
        return None

def _autosave_notice_dismissed(username: str) -> bool:
    try:
        users = load_local_state()
        u = users.get(username) or {}
        return (u.get('autosave_notice_dismissed_version') == AUTOSAVE_NOTICE_VERSION)
    except Exception:
        return False

@app.context_processor
def _inject_globals():
    """Глобальные переменные для шаблонов (base.html)."""
    try:
        uname = session.get('username') if 'username' in session else None
        show = False
        if uname and _license_accepted(uname) and (not _autosave_notice_dismissed(uname)):
            show = True

        next_restart_dt = _read_restart_schedule()
        now_dt = datetime.now()
        next_restart_iso = next_restart_dt.isoformat() if next_restart_dt else None
        seconds_left = None
        if next_restart_dt:
            seconds_left = int((next_restart_dt - now_dt).total_seconds())

        return {
            'autosave_notice_show': show,
            'autosave_notice_title': AUTOSAVE_NOTICE_TITLE,
            'autosave_notice_text': AUTOSAVE_NOTICE_TEXT,
            'autosave_notice_version': AUTOSAVE_NOTICE_VERSION,
            'restart_next_iso': next_restart_iso,
            'restart_seconds_left': seconds_left,
        }
    except Exception:
        return {
            'autosave_notice_show': False,
            'autosave_notice_title': AUTOSAVE_NOTICE_TITLE,
            'autosave_notice_text': AUTOSAVE_NOTICE_TEXT,
            'autosave_notice_version': AUTOSAVE_NOTICE_VERSION,
            'restart_next_iso': None,
            'restart_seconds_left': None,
        }

# Настройки производительностино
DEFAULT_WAITRESS_THREADS = min(32, (os.cpu_count() or 4) * 4)
WAITRESS_THREADS = int(os.environ.get("DND_WAITRESS_THREADS", str(DEFAULT_WAITRESS_THREADS)))
WAITRESS_CONNECTION_LIMIT = int(os.environ.get("DND_WAITRESS_CONNECTION_LIMIT", "1000"))
WAITRESS_BACKLOG = int(os.environ.get("DND_WAITRESS_BACKLOG", "2048"))
WAITRESS_CHANNEL_TIMEOUT = int(os.environ.get("DND_WAITRESS_CHANNEL_TIMEOUT", "120"))
# HTTP для туннелей только на loopback: на Windows SSH -R ... localhost:5001 часто идёт в ::1, а сервер слушает только IPv4 → «пустая страница».
TUNNEL_HTTP_HOST = (os.environ.get("DND_TUNNEL_HTTP_BIND") or "127.0.0.1").strip() or "127.0.0.1"

# JSON формат сохранения (0 = компактно и заметно быстрее)
try:
    JSON_INDENT = int(os.environ.get("DND_JSON_INDENT", "0"))
except Exception:
    JSON_INDENT = 0

# Кэш персонажей (уменьшает чтение файла на каждом запросе)
_characters_lock = threading.RLock()
_characters_cache = None
_characters_cache_mtime = None

# Кэш боёв (уменьшает чтение файла на каждом запросе), теперь по пользователю
_battles_lock = threading.RLock()
_battles_cache_by_file = {}
_battles_cache_mtime_by_file = {}

def _safe_user_key(username: str) -> str:
    s = (username or "").strip()
    s = re.sub(r'[^A-Za-z0-9_\-]+', '_', s)
    s = s.strip('_')
    return (s or "user")[:64]

def _battles_file_for_user(username: str) -> str:
    try:
        os.makedirs(BATTLES_DIR, exist_ok=True)
    except Exception:
        pass
    safe = _safe_user_key(username)
    return os.path.join(BATTLES_DIR, f"battles_{safe}.json")

def load_battles_for_user(username: str):
    """Загрузка боёв из JSON файла (отдельно для каждого пользователя)."""
    path = _battles_file_for_user(username)
    with _battles_lock:
        try:
            if not os.path.exists(path):
                _battles_cache_by_file[path] = {"battles": []}
                _battles_cache_mtime_by_file[path] = None
                return {"battles": []}
            mtime = os.path.getmtime(path)
            if _battles_cache_by_file.get(path) is not None and _battles_cache_mtime_by_file.get(path) == mtime:
                return deepcopy(_battles_cache_by_file[path])
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f) or {}
            if not isinstance(data, dict):
                data = {"battles": []}
            if "battles" not in data or not isinstance(data["battles"], list):
                data["battles"] = []
            _battles_cache_by_file[path] = data
            _battles_cache_mtime_by_file[path] = mtime
            return deepcopy(_battles_cache_by_file[path])
        except Exception as e:
            print(f"Ошибка загрузки боёв ({path}): {e}")
            return {"battles": []}

def save_battles_for_user(username: str, data):
    """Сохранение боёв в JSON файл пользователя (атомарно)."""
    path = _battles_file_for_user(username)
    with _battles_lock:
        tmp_path = path + ".tmp"
        try:
            if not isinstance(data, dict):
                data = {"battles": []}
            if "battles" not in data or not isinstance(data["battles"], list):
                data["battles"] = []
            with open(tmp_path, 'w', encoding='utf-8') as f:
                if JSON_INDENT > 0:
                    json.dump(data, f, ensure_ascii=False, indent=JSON_INDENT)
                else:
                    json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
            os.replace(tmp_path, path)
            try:
                _battles_cache_mtime_by_file[path] = os.path.getmtime(path)
            except Exception:
                _battles_cache_mtime_by_file[path] = None
            _battles_cache_by_file[path] = deepcopy(data)
            return True
        except Exception as e:
            print(f"Ошибка сохранения боёв ({path}): {e}")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            return False

def _sanitize_bm_text(v, max_len=5000):
    if v is None:
        return ""
    s = str(v)
    s = ''.join(ch for ch in s if ch == '\n' or ch == '\t' or ord(ch) >= 32)
    if len(s) > max_len:
        s = s[:max_len]
    return s

def _sanitize_bm_one_line(v, max_len=500):
    """Однострочный безопасный текст: убирает переводы строк/табуляции и схлопывает пробелы."""
    s = _sanitize_bm_text(v, max_len=max_len)
    s = s.replace('\r', ' ').replace('\n', ' ').replace('\t', ' ')
    s = ' '.join(s.split())
    return s.strip()

def _to_int_safe(v, default=0, min_v=None, max_v=None):
    try:
        iv = int(v)
    except Exception:
        iv = default
    if min_v is not None:
        iv = max(min_v, iv)
    if max_v is not None:
        iv = min(max_v, iv)
    return iv

def _sanitize_participant(p):
    if not isinstance(p, dict):
        return None
    pid = _sanitize_bm_one_line(p.get("pid") or "", 80)
    if pid and not re.match(r'^[A-Za-z0-9_]{1,80}$', pid):
        pid = ""

    name = _sanitize_bm_one_line(p.get("name"), 80)
    if not name:
        return None
    hp_unknown = bool(p.get("hp_unknown", False))
    hide_hp = bool(p.get("hide_hp", False))
    max_hp = _to_int_safe(p.get("max_hp", 0), 0, 0, 1_000_000)
    if hp_unknown:
        max_hp = 0
    current_hp = _to_int_safe(p.get("current_hp", 0), 0, -1_000_000, 1_000_000)
    initiative = _to_int_safe(p.get("initiative", 0), 0, -1000, 1000)
    status = _sanitize_bm_one_line(p.get("status"), 200)
    temp_hp = _to_int_safe(p.get("temp_hp", 0), 0, 0, 1_000_000)
    notes = _sanitize_bm_text(p.get("notes"), 2000)
    is_boss = bool(p.get("is_boss", False))

    # ===== Автоматизация боя (расширения) =====
    speed_base = _to_int_safe(p.get("speed_base", 30), 30, 0, 1000)
    speed_mod = _to_int_safe(p.get("speed_mod", 0), 0, -1000, 1000)

    def _sanitize_damage_type(v):
        t = _sanitize_bm_one_line(v, 30).lower()
        allowed = {
            "none", "fire", "force", "psychic", "radiant", "necrotic",
            "cold", "lightning", "poison", "acid",
            "bludgeoning", "piercing", "slashing"
        }
        return t if t in allowed else "none"

    def _sanitize_resist_map(v):
        if not isinstance(v, dict):
            return {
                "fire": "normal", "force": "normal", "psychic": "normal", "radiant": "normal", "necrotic": "normal",
                "cold": "normal", "lightning": "normal", "poison": "normal", "acid": "normal"
            }
        out = {}
        allowed_v = {"normal", "resist", "immune", "vuln"}
        for k in ["fire", "force", "psychic", "radiant", "necrotic", "cold", "lightning", "poison", "acid"]:
            vv = _sanitize_bm_one_line(v.get(k, "normal"), 20).lower()
            out[k] = vv if vv in allowed_v else "normal"
        return out

    def _sanitize_resources(v):
        if not isinstance(v, dict):
            return {}
        out = {}
        for key, it in list(v.items())[:80]:
            k = _sanitize_bm_one_line(key, 32)
            if not re.match(r'^[A-Za-z0-9_]{1,32}$', k):
                continue
            if not isinstance(it, dict):
                it = {}
            out[k] = {
                "name": _sanitize_bm_one_line(it.get("name") or k, 60) or k,
                "cur": _to_int_safe(it.get("cur", 0), 0, 0, 1_000_000),
                "max": _to_int_safe(it.get("max", 0), 0, 0, 1_000_000),
            }
            if out[k]["cur"] > out[k]["max"]:
                out[k]["cur"] = out[k]["max"]
        return out

    def _sanitize_effects(v):
        if not isinstance(v, list):
            return []
        out = []
        for e in v[:200]:
            if not isinstance(e, dict):
                continue
            name_e = _sanitize_bm_one_line(e.get("name"), 80)
            if not name_e:
                continue
            tick_on = _sanitize_bm_one_line(e.get("tick_on", "none"), 20).lower()
            if tick_on not in {"none", "start", "end"}:
                tick_on = "none"
            flags = e.get("flags", [])
            if not isinstance(flags, list):
                flags = []
            flags2 = [_sanitize_bm_one_line(x, 30).lower() for x in flags][:10]
            flags2 = [x for x in flags2 if x]
            out.append({
                "id": _sanitize_bm_one_line(e.get("id") or "", 80),
                "name": name_e,
                "turns": _to_int_safe(e.get("turns", 1), 1, 0, 1_000_000),
                "tick_on": tick_on,
                "damage_type": _sanitize_damage_type(e.get("damage_type", "none")),
                "damage_formula": _sanitize_bm_one_line(e.get("damage_formula", ""), 40),
                "heal_formula": _sanitize_bm_one_line(e.get("heal_formula", ""), 40),
                "thp_formula": _sanitize_bm_one_line(e.get("thp_formula", ""), 40),
                "status": _sanitize_bm_one_line(e.get("status", ""), 80),
                "flags": flags2,
            })
        return out

    def s_int(key, d=0, mn=0, mx=10_000_000):
        return _to_int_safe(p.get(key, d), d, mn, mx)

    return {
        "pid": pid or None,
        "name": name,
        "max_hp": max_hp,
        "current_hp": current_hp,
        "initiative": initiative,
        "status": status,
        "temp_hp": temp_hp,
        "notes": notes,
        "hp_unknown": hp_unknown,
        "hide_hp": hide_hp,
        "total_damage_dealt": s_int("total_damage_dealt", 0),
        "total_damage_taken": s_int("total_damage_taken", 0),
        "total_healing_done": s_int("total_healing_done", 0),
        "total_healing_received": s_int("total_healing_received", 0),
        "kills": s_int("kills", 0),
        "is_boss": is_boss,
        "rounds_participated": s_int("rounds_participated", 0, 0, 100000),
        "first_round": p.get("first_round", None),
        "speed_base": speed_base,
        "speed_mod": speed_mod,
        "resist": _sanitize_resist_map(p.get("resist")),
        "resources": _sanitize_resources(p.get("resources")),
        "effects": _sanitize_effects(p.get("effects")),
    }

def _sanitize_battle(b):
    if not isinstance(b, dict):
        return None
    bid = _sanitize_bm_text(b.get("id"), 80).strip()
    if not re.match(r'^[a-f0-9]{8,64}$', bid):
        return None
    name = _sanitize_bm_one_line(b.get("name"), 120) or "Новый бой"
    rnd = _to_int_safe(b.get("round", 1), 1, 1, 1000000)
    cur = _to_int_safe(b.get("current_turn", 0), 0, 0, 1000000)
    notes = _sanitize_bm_text(b.get("notes"), 20000)
    journal_draft = _sanitize_bm_text(b.get("journal_draft"), 200000)
    settings_in = b.get("settings", {})
    if not isinstance(settings_in, dict):
        settings_in = {}
    ui_mode = _sanitize_bm_one_line(settings_in.get("ui_mode") or "simple", 20).lower()
    if ui_mode not in {"simple", "advanced"}:
        ui_mode = "simple"
    settings = {
        "auto_tick": bool(settings_in.get("auto_tick", True)),
        "ui_mode": ui_mode,
        "guide_open": bool(settings_in.get("guide_open", True)),
        "log_as_environment": bool(settings_in.get("log_as_environment", False)),
    }
    log = b.get("log", [])
    if not isinstance(log, list):
        log = []
    log2 = [_sanitize_bm_one_line(x, 500) for x in log][:5000]
    created_at = _sanitize_bm_one_line(b.get("created_at") or datetime.now().isoformat(), 40)
    parts = b.get("participants", [])
    if not isinstance(parts, list):
        parts = []
    parts2 = []
    for p in parts[:500]:
        sp = _sanitize_participant(p)
        if sp:
            parts2.append(sp)
    if parts2:
        cur = max(0, min(cur, len(parts2) - 1))
    else:
        cur = 0
    # zones
    zones_in = b.get("zones", [])
    zones2 = []
    if isinstance(zones_in, list):
        for z in zones_in[:200]:
            if not isinstance(z, dict):
                continue
            zid = _sanitize_bm_one_line(z.get("id") or "", 80)
            name_z = _sanitize_bm_one_line(z.get("name") or "Зона", 80) or "Зона"
            turns_in = z.get("turns", 1)
            if turns_in is None:
                turns_z = None
            else:
                turns_z = _to_int_safe(turns_in, 1, 0, 1_000_000)
            tick_on = _sanitize_bm_one_line(z.get("tick_on", "start_round"), 20).lower()
            if tick_on not in {"start_round", "start_turn", "end_turn"}:
                tick_on = "start_round"
            dmg_type = _sanitize_bm_one_line(z.get("damage_type", "fire"), 30).lower()
            if dmg_type not in {"fire","force","psychic","radiant","necrotic","cold","lightning","poison","acid","none"}:
                dmg_type = "fire"
            dmg_formula = _sanitize_bm_one_line(z.get("damage_formula", ""), 40)
            apply_eff = _sanitize_bm_one_line(z.get("apply_effect", ""), 120)
            enabled = bool(z.get("enabled", True))
            targets = z.get("targets", [])
            if not isinstance(targets, list):
                targets = []
            targets2 = [_sanitize_bm_one_line(x, 80) for x in targets][:500]
            targets2 = [x for x in targets2 if x]
            zones2.append({
                "id": zid,
                "name": name_z,
                "turns": turns_z,
                "tick_on": tick_on,
                "damage_type": dmg_type,
                "damage_formula": dmg_formula,
                "apply_effect": apply_eff,
                "targets": targets2,
                "enabled": enabled,
            })

    # macros
    macros_in = b.get("macros", [])
    macros2 = []
    if isinstance(macros_in, list):
        for m in macros_in[:300]:
            if not isinstance(m, dict):
                continue
            mid = _sanitize_bm_one_line(m.get("id") or "", 80)
            name_m = _sanitize_bm_one_line(m.get("name") or "Макрос", 80) or "Макрос"
            target = _sanitize_bm_one_line(m.get("target") or "selected", 30)
            kind = _sanitize_bm_one_line(m.get("kind") or "damage", 30)
            dmg_type = _sanitize_bm_one_line(m.get("damage_type") or "none", 30).lower()
            if dmg_type not in {"none","fire","force","psychic","radiant","necrotic","cold","lightning","poison","acid"}:
                dmg_type = "none"
            macros2.append({
                "id": mid,
                "name": name_m,
                "target": target,
                "kind": kind,
                "damage_type": dmg_type,
                "formula": _sanitize_bm_one_line(m.get("formula", ""), 40),
                "effect_spec": _sanitize_bm_one_line(m.get("effect_spec", ""), 140),
                "cost": _sanitize_bm_one_line(m.get("cost", ""), 60),
                "log": _sanitize_bm_one_line(m.get("log", ""), 200),
            })

    return {
        "id": bid,
        "name": name,
        "participants": parts2,
        "round": rnd,
        "current_turn": cur,
        "created_at": created_at,
        "notes": notes,
        "journal_draft": journal_draft,
        "log": log2,
        "settings": settings,
        "zones": zones2,
        "macros": macros2,
    }


def _load_achievement_doc():
    try:
        with open(ACHIEVEMENTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    data.setdefault("unlocked", {})
    data.setdefault("events", {})
    return data


def _save_achievement_doc(data):
    try:
        os.makedirs(os.path.dirname(ACHIEVEMENTS_FILE), exist_ok=True)
        tmp = ACHIEVEMENTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ACHIEVEMENTS_FILE)
        return True
    except Exception:
        return False


def _unlock_achievement(achievement_id):
    definition = ACHIEVEMENT_DEFINITIONS.get(str(achievement_id))
    if not definition:
        return None
    doc = _load_achievement_doc()
    unlocked = doc.setdefault("unlocked", {})
    if achievement_id in unlocked:
        return None
    unlocked[achievement_id] = {"at": datetime.now().isoformat()}
    _save_achievement_doc(doc)
    return {"slot": definition["slot"], "title": definition["title"]}


def _record_achievement_event(code, client_ms=None):
    code = str(code or "")
    unlocked = None
    with ACHIEVEMENT_LOCK:
        doc = _load_achievement_doc()
        if code == ACHIEVEMENT_EVENT_AUTOSAVE:
            events = doc.setdefault("events", {})
            taps = events.get("autosave_taps_ms")
            if not isinstance(taps, list):
                taps = []
            try:
                stamp = float(client_ms)
            except Exception:
                stamp = time.time() * 1000.0
            if stamp <= 0 or abs(stamp - time.time() * 1000.0) > 300000:
                stamp = time.time() * 1000.0
            taps = sorted(float(x) for x in taps if isinstance(x, (int, float)) and stamp - float(x) <= 1800 and float(x) <= stamp + 250)
            taps.append(stamp)
            taps = sorted(taps)[-20:]
            events["autosave_taps_ms"] = taps
            _save_achievement_doc(doc)
            if len(taps) >= 10 and taps[-1] - taps[-10] <= 1800:
                unlocked = _unlock_achievement("a1")
        elif code == ACHIEVEMENT_EVENT_CLICKER:
            unlocked = _unlock_achievement("a2")
        elif code == ACHIEVEMENT_EVENT_QUASAR:
            unlocked = _unlock_achievement("a4")
    return unlocked


@app.route('/api/achievements', methods=['GET'])
@require_login
def api_achievements():
    doc = _load_achievement_doc()
    unlocked = []
    for achievement_id, meta in (doc.get("unlocked") or {}).items():
        definition = ACHIEVEMENT_DEFINITIONS.get(achievement_id)
        if not definition:
            continue
        unlocked.append({
            "slot": definition["slot"],
            "title": definition["title"],
            "at": str((meta or {}).get("at") or ""),
        })
    unlocked.sort(key=lambda x: x.get("at") or "", reverse=True)
    return jsonify({"success": True, "total": 18, "unlocked": unlocked})


@app.route('/api/runtime/pulse', methods=['POST'])
@require_login
def api_achievement_event():
    payload = request.get_json(silent=True) or {}
    unlocked = _record_achievement_event(payload.get("code"), payload.get("at"))
    return jsonify({"success": True, "unlocked": unlocked})



def _debug_panel_fragment(action_url, operations):
    return f'''<div data-there-private-panel="1" style="width:min(520px,94vw);padding:16px;border-radius:14px;border:1px solid rgba(255,215,0,.34);background:linear-gradient(150deg,rgba(18,18,34,.98),rgba(3,3,10,.98));color:#e2e8f0;box-shadow:0 28px 90px rgba(0,0,0,.75)">
<div style="font-weight:900;color:#ffd700;font-size:18px;margin-bottom:12px">DEBUG THERE</div>
<button class="btn btn-success" data-there-private-op="{operations['open']}" style="width:100%;margin-bottom:9px" type="button">Открыть меню клика</button>
<div style="display:grid;grid-template-columns:1fr auto;gap:8px;margin-bottom:9px"><input data-there-private-value="1" type="number" min="0" max="1000000000" placeholder="Сколько очков выдать" style="min-width:0;padding:9px 11px;border-radius:10px;border:1px solid rgba(255,255,255,.18);background:rgba(0,0,0,.35);color:#fff"><button class="btn btn-primary" data-there-private-op="{operations['points']}" type="button">Выдать</button></div>
<button class="btn btn-danger" data-there-private-op="{operations['reset']}" style="width:100%;margin-bottom:9px" type="button">Сбросить кликер</button>
<button class="btn" data-there-private-close="1" style="width:100%" type="button">Закрыть</button>
</div>'''


@app.route('/api/runtime/cache', methods=['POST'])
@require_login
def api_runtime_cache():
    payload = request.get_json(silent=True) or {}
    token = str(payload.get('token') or '')
    if not secrets.compare_digest(token, RUNTIME_GESTURE_TOKENS['version']):
        return jsonify({'success': True})
    now = time.time()
    taps = session.get('_there_runtime_taps')
    if not isinstance(taps, list):
        taps = []
    taps = [float(x) for x in taps if isinstance(x, (int, float)) and now - float(x) <= 12.0]
    taps.append(now)
    session['_there_runtime_taps'] = taps[-12:]
    if len(taps) < 10:
        return jsonify({'success': True})
    session['_there_runtime_taps'] = []
    gate = secrets.token_urlsafe(30)
    operations = {key: secrets.token_urlsafe(18) for key in ('open', 'points', 'reset')}
    session['_there_private_gate'] = gate
    session['_there_private_ops'] = operations
    action_url = url_for('api_runtime_private_action', gate=gate)
    return jsonify({'success': True, 'panel': _debug_panel_fragment(action_url, operations), 'action': action_url})


@app.route('/api/runtime/private/<gate>', methods=['POST'])
@require_login
def api_runtime_private_action(gate):
    expected = str(session.get('_there_private_gate') or '')
    if not expected or not secrets.compare_digest(str(gate or ''), expected):
        return jsonify({'success': False}), 404
    payload = request.get_json(silent=True) or {}
    op = str(payload.get('op') or '')
    operations = session.get('_there_private_ops') if isinstance(session.get('_there_private_ops'), dict) else {}
    if secrets.compare_digest(op, str(operations.get('open') or '')):
        return jsonify({'success': True, 'bridge': {'k': 1}})
    if secrets.compare_digest(op, str(operations.get('points') or '')):
        try:
            amount = max(0, min(1000000000, int(payload.get('value') or 0)))
        except Exception:
            amount = 0
        return jsonify({'success': True, 'bridge': {'k': 2, 'n': amount}})
    if secrets.compare_digest(op, str(operations.get('reset') or '')):
        return jsonify({'success': True, 'bridge': {'k': 3}})
    return jsonify({'success': False}), 400


@app.route('/api/license/accept-again', methods=['POST'])
@require_login
def api_license_accept_again():
    unlocked = _unlock_achievement("a3")
    return jsonify({"success": True, "unlocked": unlocked})

# Инициализация систем
init_auth_system()
init_security_system()
init_backup_system()

# Словарь активных сессий пользователей
user_sessions = {}

# Константы
DEFAULT_RACES = ["Человек", "Эльф", "Дварф", "Полурослик", "Драконорожденный", "Тифлинг", "Полуэльф", "Гном"]
CLASSES = ["Воин", "Маг", "Жрец", "Плут", "Следопыт", "Паладин", "Чернокнижник", "Бард", "Друид", "Монах", "Заклинатель", "Волшебник"]
ALIGNMENTS = ["Законно-добрый", "Нейтрально-добрый", "Хаотично-добрый",
              "Законно-нейтральный", "Нейтральный", "Хаотично-нейтральный",
              "Законно-злой", "Нейтрально-злой", "Хаотично-злой"]

SKILLS = [
    "Акробатика", "Атлетика", "Восприятие", "Выживание", "Выступление",
    "Запугивание", "История", "Ловкость рук", "Магия", "Медицина", "Обман",
    "Обращение с животными", "Природа", "Проницательность", "Расследование",
    "Религия", "Скрытность", "Убеждение"
]

SKILL_TO_STAT = {
    "Акробатика": "dexterity",
    "Атлетика": "strength",
    "Восприятие": "wisdom",
    "Выживание": "wisdom",
    "Выступление": "charisma",
    "Запугивание": "charisma",
    "История": "intelligence",
    "Ловкость рук": "dexterity",
    "Магия": "intelligence",
    "Медицина": "wisdom",
    "Обман": "charisma",
    "Обращение с животными": "wisdom",
    "Природа": "intelligence",
    "Проницательность": "wisdom",
    "Расследование": "intelligence",
    "Религия": "intelligence",
    "Скрытность": "dexterity",
    "Убеждение": "charisma"
}

EQUIPMENT_SLOTS = [
    "head",
    "body",
    "left_hand",
    "right_hand",
    "left_weapon",
    "right_weapon",
    "left_finger",
    "right_finger",
    "legs",
    "feet"
]

# Функции работы с данными

def _clean_note_records(value):
    """Нормализует только заметки. Поля черт сюда никогда не подмешиваются."""
    result = []
    source = value if isinstance(value, list) else []
    for raw in source:
        extra = {}
        if isinstance(raw, str):
            title = ""
            content = raw
        elif isinstance(raw, dict):
            title = raw.get("title", "")
            content = raw.get("content", "")
            # Единственный старый алиас заметок. name/label/description намеренно не читаются.
            if content in (None, "") and "text" in raw:
                content = raw.get("text", "")
            extra = {
                str(key)[:80]: deepcopy(val)
                for key, val in raw.items()
                if key not in {"title", "content", "text", "name", "label", "description"}
            }
        else:
            continue
        title = str(title or "").strip()[:160]
        content = str(content or "")[:32000]
        if not title and not content.strip():
            continue
        item = extra
        item["title"] = title or f"Заметка {len(result) + 1}"
        item["content"] = content
        result.append(item)
    return result[:1000]


def _clean_trait_records(value):
    """Нормализует только черты. Поля заметок сюда никогда не подмешиваются."""
    result = []
    source = value if isinstance(value, list) else []
    for raw in source:
        extra = {}
        if isinstance(raw, str):
            name = raw
            description = ""
        elif isinstance(raw, dict):
            name = raw.get("name", "")
            description = raw.get("description", "")
            # label поддерживается только как старое имя черты. title/content/text запрещены.
            if not name and "label" in raw:
                name = raw.get("label", "")
            extra = {
                str(key)[:80]: deepcopy(val)
                for key, val in raw.items()
                if key not in {"name", "description", "label", "title", "content", "text"}
            }
        else:
            continue
        name = str(name or "").strip()[:160]
        description = str(description or "")[:16000]
        if not name and not description.strip():
            continue
        item = extra
        item["name"] = name or f"Черта {len(result) + 1}"
        item["description"] = description
        result.append(item)
    return result[:500]

def invalidate_characters_cache():
    """Сброс кеша после внешней записи в characters.json (например восстановление из бэкапа)."""
    global _characters_cache, _characters_cache_mtime
    with _characters_lock:
        _characters_cache = None
        _characters_cache_mtime = None


def load_characters():
    """Загрузка персонажей из JSON файла с обработкой ошибок"""
    global _characters_cache, _characters_cache_mtime
    with _characters_lock:
        try:
            if not os.path.exists(CHARACTERS_FILE):
                _characters_cache = {}
                _characters_cache_mtime = None
                return {}

            mtime = os.path.getmtime(CHARACTERS_FILE)
            if _characters_cache is not None and _characters_cache_mtime == mtime:
                # возвращаем копию чтобы разные потоки/запросы не делили один объект
                return deepcopy(_characters_cache)

            with open(CHARACTERS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # Валидация и исправление данных
            for name, char in data.items():
                # HP: добавили временные хиты (temp) — миграция старых сохранений
                if 'hp' not in char or not isinstance(char.get('hp'), dict):
                    char['hp'] = {"current": 10, "max": 10, "temp": 0}
                else:
                    if 'current' not in char['hp']:
                        char['hp']['current'] = 10
                    if 'max' not in char['hp']:
                        char['hp']['max'] = 10
                    if 'temp' not in char['hp']:
                        char['hp']['temp'] = 0
                    try:
                        char['hp']['current'] = max(0, int(char['hp'].get('current', 0)))
                    except Exception:
                        char['hp']['current'] = 0
                    try:
                        char['hp']['max'] = max(1, int(char['hp'].get('max', 10)))
                    except Exception:
                        char['hp']['max'] = 10
                    try:
                        char['hp']['temp'] = max(0, int(char['hp'].get('temp', 0)))
                    except Exception:
                        char['hp']['temp'] = 0

                # Общая информация / внешность — мягкая миграция (не ломаем старые данные)
                if 'general_info' not in char or not isinstance(char.get('general_info'), dict):
                    char['general_info'] = {
                        "vision": "Обычное",
                        "armor": "",
                        "weapons": "",
                        "tools": "",
                        "languages": "",
                        "traits": [],
                    }
                else:
                    gi = char['general_info']
                    if 'vision' not in gi:
                        gi['vision'] = "Обычное"
                    for k in ['armor', 'weapons', 'tools', 'languages']:
                        if k not in gi:
                            gi[k] = ""
                    if 'traits' not in gi or not isinstance(gi.get('traits'), list):
                        gi['traits'] = []
                char['general_info']['traits'] = _clean_trait_records(char['general_info'].get('traits'))

                # Внешность: список {label,value}
                if 'appearance' not in char:
                    char['appearance'] = [
                        {"label": "Рост", "value": ""},
                        {"label": "Вес", "value": ""},
                        {"label": "Возраст", "value": ""},
                        {"label": "Глаза", "value": ""},
                        {"label": "Кожа", "value": ""},
                        {"label": "Волосы", "value": ""},
                    ]
                else:
                    ap = char.get('appearance')
                    if isinstance(ap, dict):
                        # legacy: словарь -> список
                        char['appearance'] = [{"label": str(k)[:60], "value": str(v)[:500]} for k, v in ap.items()]
                    elif isinstance(ap, list):
                        norm = []
                        for x in ap[:200]:
                            if isinstance(x, dict):
                                lab = (x.get('label') or x.get('name') or "").strip()
                                val = (x.get('value') or x.get('text') or "").strip()
                                if lab or val:
                                    norm.append({"label": lab[:60], "value": val[:500]})
                            elif isinstance(x, (list, tuple)) and len(x) >= 2:
                                lab = str(x[0] or "").strip()
                                val = str(x[1] or "").strip()
                                if lab or val:
                                    norm.append({"label": lab[:60], "value": val[:500]})
                            elif x is not None:
                                s = str(x).strip()
                                if s:
                                    norm.append({"label": s[:60], "value": ""})
                        if not norm:
                            norm = [
                                {"label": "Рост", "value": ""},
                                {"label": "Вес", "value": ""},
                                {"label": "Возраст", "value": ""},
                                {"label": "Глаза", "value": ""},
                                {"label": "Кожа", "value": ""},
                                {"label": "Волосы", "value": ""},
                            ]
                        char['appearance'] = norm
                    else:
                        char['appearance'] = [
                            {"label": "Рост", "value": ""},
                            {"label": "Вес", "value": ""},
                            {"label": "Возраст", "value": ""},
                            {"label": "Глаза", "value": ""},
                            {"label": "Кожа", "value": ""},
                            {"label": "Волосы", "value": ""},
                        ]

                if 'equipment' not in char or not isinstance(char.get('equipment'), list):
                    char['equipment'] = []

                # Валюта: сайт ожидает список вида [{name, amount}].
                # V7 случайно записал персонажу dict {gold,silver,copper}, из-за чего /edit/<name> падал
                # в шаблоне на character.currency[i]. Мигрируем любые старые форматы мягко.
                def _normalize_currency_list(cur):
                    default_cur = [
                        {"name": "Золото", "amount": 0},
                        {"name": "Серебро", "amount": 0},
                        {"name": "Медь", "amount": 0},
                    ]
                    def _amount(v):
                        try:
                            return int(v)
                        except Exception:
                            return 0
                    if isinstance(cur, list):
                        out = []
                        for c in cur[:50]:
                            if isinstance(c, dict):
                                nm = str(c.get('name') or c.get('label') or '').strip()
                                if not nm:
                                    continue
                                out.append({"name": nm[:40], "amount": _amount(c.get('amount', c.get('value', 0)))})
                        return out or default_cur
                    if isinstance(cur, dict):
                        mapping = [("gold", "Золото"), ("silver", "Серебро"), ("copper", "Медь")]
                        out = []
                        used = set()
                        for key, label in mapping:
                            if key in cur:
                                out.append({"name": label, "amount": _amount(cur.get(key, 0))})
                                used.add(key)
                        for key, value in list(cur.items())[:50]:
                            if key in used:
                                continue
                            label = str(key).strip() or "Валюта"
                            out.append({"name": label[:40], "amount": _amount(value)})
                        return out or default_cur
                    return default_cur

                char['currency'] = _normalize_currency_list(char.get('currency'))

                if 'notes' not in char or not isinstance(char.get('notes'), list):
                    char['notes'] = []
                char['notes'] = _clean_note_records(char.get('notes'))
                if 'classes' not in char:
                    char['classes'] = []
                if 'spells' not in char:
                    char['spells'] = []
                if 'spell_schools' not in char:
                    char['spell_schools'] = ['Некромантия', 'Эвокация', 'Ограждение', 'Преобразование', 'Прорицание', 'Очарование', 'Вызов', 'Иллюзия']
                # Миграция старых заклинаний
                if char.get('spells') and isinstance(char['spells'], list) and len(char['spells']) > 0:
                    if isinstance(char['spells'][0], dict) and 'school' not in char['spells'][0]:
                        for spell in char['spells']:
                            if 'school' not in spell:
                                spell['school'] = char['spell_schools'][0] if char['spell_schools'] else 'Некромантия'
                            if 'level' not in spell:
                                # Пытаемся определить уровень по damage
                                if 'damage' in spell and spell['damage']:
                                    levels = [int(k) for k in spell['damage'].keys() if k.isdigit()]
                                    spell['level'] = min(levels) if levels else 1
                                else:
                                    spell['level'] = 1
                            if 'classes' not in spell:
                                spell['classes'] = []
                if 'spell_slots' not in char:
                    char['spell_slots'] = {str(i): 0 for i in range(0, 10)}
                if 'spell_slots_used' not in char:
                    char['spell_slots_used'] = {str(i): 0 for i in range(0, 10)}
                if 'prepared_spells' not in char or not isinstance(char.get('prepared_spells'), list):
                    char['prepared_spells'] = []
                if 'equipped' not in char:
                    char['equipped'] = {slot: None for slot in EQUIPMENT_SLOTS}
                meta = char.get('_there_meta') if isinstance(char.get('_there_meta'), dict) else {}
                try:
                    revision = max(0, int(meta.get('revision', 0)))
                except Exception:
                    revision = 0
                char['_there_meta'] = {
                    'revision': revision,
                    'updated_at': str(meta.get('updated_at') or ''),
                    'import_epoch': str(meta.get('import_epoch') or ''),
                }

            _characters_cache = data
            _characters_cache_mtime = mtime
            return deepcopy(_characters_cache)

        except json.JSONDecodeError as e:
            print(f"Ошибка парсинга JSON: {e}")
            # Создаем резервную копию поврежденного файла
            backup_name = f"{CHARACTERS_FILE}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            try:
                if os.path.exists(CHARACTERS_FILE):
                    os.rename(CHARACTERS_FILE, backup_name)
                    print(f"Создана резервная копия: {backup_name}")
            except Exception:
                pass
            _characters_cache = {}
            _characters_cache_mtime = None
            return {}
        except Exception as e:
            print(f"Ошибка загрузки файла сохранений: {e}")
            return {}

def save_characters(characters):
    """Атомарное сохранение персонажей без бесконечного ожидания блокировки."""
    global _characters_cache, _characters_cache_mtime

    acquired = _characters_lock.acquire(timeout=8.0)
    if not acquired:
        print("Сохранение отменено: хранилище занято дольше 8 секунд")
        return False

    tmp_path = None
    try:
        previous = deepcopy(_characters_cache) if isinstance(_characters_cache, dict) else {}
        now_iso = datetime.now().isoformat()

        for char_name, char_data in (characters or {}).items():
            if not isinstance(char_data, dict):
                continue

            char_data['notes'] = _clean_note_records(char_data.get('notes'))
            if not isinstance(char_data.get('general_info'), dict):
                char_data['general_info'] = {}
            char_data['general_info']['traits'] = _clean_trait_records(char_data['general_info'].get('traits'))

            prev_data = previous.get(char_name) if isinstance(previous.get(char_name), dict) else None
            prev_meta = (
                prev_data.get('_there_meta')
                if isinstance(prev_data, dict) and isinstance(prev_data.get('_there_meta'), dict)
                else {}
            )
            try:
                prev_revision = max(0, int(prev_meta.get('revision', 0)))
            except Exception:
                prev_revision = 0

            clean_current = deepcopy(char_data)
            clean_current.pop('_there_meta', None)
            clean_previous = deepcopy(prev_data) if isinstance(prev_data, dict) else None
            if isinstance(clean_previous, dict):
                clean_previous.pop('_there_meta', None)

            changed = clean_previous is None or clean_current != clean_previous
            current_meta = char_data.get('_there_meta') if isinstance(char_data.get('_there_meta'), dict) else {}
            merged_meta = {}

            for source_meta in (prev_meta, current_meta):
                for meta_key, meta_value in source_meta.items():
                    if isinstance(meta_key, str) and not meta_key.startswith('__'):
                        merged_meta[meta_key[:80]] = meta_value

            merged_meta['revision'] = prev_revision + (1 if changed else 0)
            merged_meta['updated_at'] = now_iso if changed else str(prev_meta.get('updated_at') or now_iso)
            merged_meta['import_epoch'] = str(
                current_meta.get('import_epoch')
                or prev_meta.get('import_epoch')
                or ''
            )
            char_data['_there_meta'] = merged_meta

        if JSON_INDENT > 0:
            serialized = json.dumps(characters, ensure_ascii=False, indent=JSON_INDENT)
        else:
            serialized = json.dumps(characters, ensure_ascii=False, separators=(',', ':'))

        target_dir = os.path.dirname(os.path.abspath(CHARACTERS_FILE)) or '.'
        os.makedirs(target_dir, exist_ok=True)
        tmp_path = f"{CHARACTERS_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"

        with open(tmp_path, 'w', encoding='utf-8', newline='') as file_handle:
            file_handle.write(serialized)
            file_handle.flush()
            try:
                os.fsync(file_handle.fileno())
            except Exception:
                pass

        last_error = None
        for attempt in range(7):
            try:
                os.replace(tmp_path, CHARACTERS_FILE)
                last_error = None
                break
            except PermissionError as error:
                last_error = error
                time.sleep(0.05 * (attempt + 1))

        if last_error is not None:
            raise last_error

        try:
            _characters_cache_mtime = os.path.getmtime(CHARACTERS_FILE)
        except Exception:
            _characters_cache_mtime = None
        _characters_cache = deepcopy(characters)
        return True

    except Exception as error:
        print(f"Ошибка сохранения: {error}")
        return False
    finally:
        if tmp_path:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
        _characters_lock.release()


def parse_dice(dice_str):
    """Парсинг строк с описанием костей (например, '2d6+3')"""
    if not dice_str:
        return 0
    try:
        total = 0
        # Обработка вычитания
        parts = re.split(r'([+-])', dice_str)
        sign = 1
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if part == '+':
                sign = 1
                continue
            elif part == '-':
                sign = -1
                continue
            
            if 'd' in part.lower():
                # Бросок кубика
                dice_parts = re.split(r'[dD]', part)
                num = int(dice_parts[0]) if dice_parts[0] else 1
                sides = int(dice_parts[1])
                total += sign * sum(random.randint(1, sides) for _ in range(num))
            else:
                # Число
                total += sign * int(part)
        return total
    except Exception as e:
        raise ValueError(f"Неверный формат броска: {dice_str}")

def calculate_modifier(stat_value):
    """Расчет модификатора характеристики"""
    return (stat_value - 10) // 2

def get_skill_modifier(character, skill):
    """Расчет модификатора навыка с учетом характеристики и бонуса мастерства (D&D 5e)"""
    stat_key = SKILL_TO_STAT.get(skill, "strength")
    stat_value = character['stats'].get(stat_key, 10)
    
    # Учитываем бонусы от экипировки к характеристике
    bonuses = get_equipment_bonuses(character)
    stat_bonus = bonuses.get('stats', {}).get(stat_key, 0)
    total_stat = stat_value + stat_bonus
    base_mod = calculate_modifier(total_stat)
    
    # В D&D 5e: если есть мастерство, добавляется proficiency_bonus
    has_proficiency = character['skills'].get(skill, 0) > 0
    proficiency_bonus = character.get('proficiency_bonus', 2) if has_proficiency else 0
    
    # Добавляем бонусы от экипировки к навыку
    skill_bonus_equipment = bonuses.get('skills', {}).get(skill, 0)
    
    # Добавляем ручной бонус к навыку
    skill_bonus_manual = character.get('skill_bonuses', {}).get(skill, 0)
    
    return base_mod + proficiency_bonus + skill_bonus_equipment + skill_bonus_manual

def get_equipment_bonuses(character):
    """Расчет всех бонусов от надетой экипировки"""
    bonuses = {
        "basic": {},
        "stats": {},
        "skills": {}
    }
    
    for slot, item_name in character.get('equipped', {}).items():
        if not item_name:
            continue
        
        # Находим предмет в инвентаре
        for item in character.get('equipment', []):
            if item.get('name') == item_name and item.get('equipped', False) and item.get('type') in ('equipment', 'weapon'):
                item_bonuses = item.get('bonuses', {})
                if isinstance(item_bonuses, str):
                    try:
                        item_bonuses = json.loads(item_bonuses)
                    except:
                        item_bonuses = {}
                
                # Суммируем бонусы
                for bonus_type, bonus_values in item_bonuses.items():
                    if bonus_type in bonuses:
                        for key, value in bonus_values.items():
                            if key in bonuses[bonus_type]:
                                bonuses[bonus_type][key] += value
                            else:
                                bonuses[bonus_type][key] = value
    
    return bonuses

# Middleware для проверки безопасности
@app.before_request
def before_request():
    if request.endpoint == 'static' or request.path.startswith('/static'):
        return None
    remote = request.remote_addr or ''
    if remote not in {'127.0.0.1', '::1'}:
        return ('Local access only', 403)
    session.permanent = True
    session['username'] = ADMIN_USERNAME
    session['session_id'] = session.get('session_id') or secrets.token_hex(16)
    session['last_activity'] = datetime.now().isoformat()
    session['remember_me'] = True
    return None


@app.after_request
def _disable_browser_cache_for_session_html(response):
    """HTML для залогиненных не кешировать в браузере: иначе префетч прогрева мог сохранить редирект на /login под URL /edit/…."""
    try:
        ct = (response.headers.get('Content-Type') or '').lower()
        if 'text/html' not in ct:
            return response
        # HTML для залогиненных не кешировать в браузере
        if session.get('username'):
            response.headers['Cache-Control'] = 'private, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'

        # Скрываем комменты при сохранении страницы (делаем для ВСЕХ HTML-страниц), но безопасно:
        # - HTML: <!-- ... -->
        # - <style>: /* ... */
        # - <script>: /* ... */ и только // в начале строки (после пробелов), чтобы не ломать https:// и regex.
        try:
            html = response.get_data(as_text=True)

            def strip_html_comments(s: str) -> str:
                if '<!--' not in s:
                    return s
                out = []
                i = 0
                n = len(s)
                while i < n:
                    j = s.find('<!--', i)
                    if j < 0:
                        out.append(s[i:])
                        break
                    out.append(s[i:j])
                    k = s.find('-->', j + 4)
                    if k < 0:
                        break
                    i = k + 3
                return ''.join(out)

            def strip_block_comments(s: str) -> str:
                if '/*' not in s:
                    return s
                out = []
                i = 0
                n = len(s)
                while i < n:
                    j = s.find('/*', i)
                    if j < 0:
                        out.append(s[i:])
                        break
                    out.append(s[i:j])
                    k = s.find('*/', j + 2)
                    if k < 0:
                        break
                    i = k + 2
                return ''.join(out)

            def strip_js_line_comments_start(s: str) -> str:
                # remove lines whose first non-space token is //
                lines = s.splitlines(True)
                out = []
                for ln in lines:
                    stripped = ln.lstrip(' \t')
                    if stripped.startswith('//'):
                        continue
                    out.append(ln)
                return ''.join(out)

            html = strip_html_comments(html)

            # process <style> and <script> blocks
            lowered = html.lower()
            out = []
            i = 0
            n = len(html)
            while i < n:
                s_idx = lowered.find('<style', i)
                j_idx = lowered.find('<script', i)
                next_idx = -1
                kind = ''
                if s_idx >= 0 and (j_idx < 0 or s_idx < j_idx):
                    next_idx = s_idx
                    kind = 'style'
                elif j_idx >= 0:
                    next_idx = j_idx
                    kind = 'script'

                if next_idx < 0:
                    out.append(html[i:])
                    break
                out.append(html[i:next_idx])

                # copy open tag
                tag_end = lowered.find('>', next_idx)
                if tag_end < 0:
                    out.append(html[next_idx:])
                    break
                open_tag = html[next_idx:tag_end + 1]
                out.append(open_tag)

                close_tag = '</' + kind + '>'
                close_idx = lowered.find(close_tag, tag_end + 1)
                if close_idx < 0:
                    inner = html[tag_end + 1:]
                    if kind == 'style':
                        inner = strip_block_comments(inner)
                    else:
                        inner = strip_js_line_comments_start(strip_block_comments(inner))
                    out.append(inner)
                    break

                inner = html[tag_end + 1:close_idx]
                if kind == 'style':
                    inner = strip_block_comments(inner)
                else:
                    inner = strip_js_line_comments_start(strip_block_comments(inner))
                out.append(inner)
                out.append(html[close_idx:close_idx + len(close_tag)])
                i = close_idx + len(close_tag)

            response.set_data(''.join(out))
        except Exception:
            pass
    except Exception:
        pass
    return response


@app.errorhandler(500)
def handle_500(e):
    """Обработка внутренних ошибок — не крашим сайт"""
    import traceback
    traceback.print_exc()
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Внутренняя ошибка сервера. Попробуйте позже.'}), 500
    flash('Произошла ошибка. Попробуйте обновить страницу или вернитесь на главную.', 'error')
    return redirect(url_for('index'))

# Маршруты аутентификации
@app.route('/login', methods=['GET', 'POST'])
def login():
    return redirect(url_for('index'))


@app.route('/logout')
def logout():
    return redirect(url_for('index'))


@app.route('/license/info', methods=['GET'])
def license_info():
    return render_template('license_public.html', license_text=LICENSE_TEXT, license_version=LICENSE_VERSION, is_admin=False)


@app.route('/license', methods=['GET', 'POST'])
def license_agreement():
    return redirect(url_for('license_info'))


@app.route('/api/autosave-notice/dismiss', methods=['POST'])
@require_login
def api_autosave_notice_dismiss():
    """Скрыть предупреждение об автосохранении (до следующей версии текста)."""
    username = session.get('username') or ''
    try:
        users = load_local_state()
        if username in users:
            users[username]['autosave_notice_dismissed_version'] = AUTOSAVE_NOTICE_VERSION
            users[username]['autosave_notice_dismissed_at'] = datetime.now().isoformat()
            save_local_state(users)
    except Exception:
        pass
    return jsonify({"success": True, "version": AUTOSAVE_NOTICE_VERSION})


# ===== Промокоды для мини-игры копирования =====
PROMO_CODES_FILE = os.path.join(APP_DATA_DIR, "promo_codes.json")

def _load_promo_codes() -> dict:
    try:
        if not os.path.exists(PROMO_CODES_FILE):
            return {}
        with open(PROMO_CODES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_promo_codes(data: dict) -> bool:
    try:
        if not isinstance(data, dict):
            return False
        with open(PROMO_CODES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False

def _generate_promo_code(existing: set) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    for _ in range(40):
        code = "".join(random.choice(alphabet) for _ in range(10))
        if code not in existing:
            return code
    # fallback (крайне маловероятно)
    return "".join(random.choice(alphabet) for _ in range(14))


@app.route('/api/copygame/save', methods=['POST'])
@require_login
def api_copygame_save():
    """Онлайн-сохранение рекорда мини-игры копирования."""
    username = (session.get("username") or "").strip()
    if not username:
        return jsonify({"error": "no_user"}), 401
    try:
        payload = request.get_json(silent=True) or {}
        # Минимальная валидация: ограничим размер и типы.
        if not isinstance(payload, dict):
            return jsonify({"error": "bad_payload"}), 400
        if len(json.dumps(payload, ensure_ascii=False)) > 60_000:
            return jsonify({"error": "too_large"}), 413

        users = load_local_state()
        if username not in users:
            return jsonify({"error": "missing_user"}), 400
        users[username]["copygame_state"] = payload
        users[username]["copygame_saved_at"] = datetime.now().isoformat()
        if not save_local_state(users):
            return jsonify({"error": "save_failed"}), 500
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/copygame/load', methods=['GET'])
@require_login
def api_copygame_load():
    """Загрузка сохранения мини-игры копирования (из local_profile.json)."""
    username = (session.get("username") or "").strip()
    if not username:
        return jsonify({"error": "no_user"}), 401
    try:
        users = load_local_state()
        if username not in users:
            return jsonify({"error": "missing_user"}), 400
        st = users.get(username, {}).get("copygame_state") or {}
        saved_at = users.get(username, {}).get("copygame_saved_at")
        if not isinstance(st, dict):
            st = {}
        return jsonify({"ok": True, "state": st, "saved_at": saved_at})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/copygame/reset', methods=['POST'])
@require_login
def api_copygame_reset():
    """Сброс сохранения мини-игры копирования (удалить copygame_state из local_profile.json)."""
    username = (session.get("username") or "").strip()
    if not username:
        return jsonify({"error": "no_user"}), 401
    try:
        users = load_local_state()
        if username not in users:
            return jsonify({"error": "missing_user"}), 400
        users[username].pop("copygame_state", None)
        users[username].pop("copygame_saved_at", None)
        if not save_local_state(users):
            return jsonify({"error": "save_failed"}), 500
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/')
@require_login
def index():
    characters = load_characters()
    return render_template('index.html', characters=characters, races=DEFAULT_RACES, classes=CLASSES, skills=SKILLS, alignments=ALIGNMENTS, skill_to_stat=SKILL_TO_STAT, is_admin=False, auto_open_create=(len(characters) == 0))



@app.route('/api/github-characters/targets', methods=['GET'])
@require_login
def api_github_characters_targets():
    characters = load_characters()
    username = (session.get('username') or '').strip()
    targets = []
    for name, character in characters.items():
        if not _player_can_access_character(username, name, characters):
            continue
        display_name = name
        if isinstance(character, dict):
            display_name = str(character.get('name') or name)
        targets.append({'name': name, 'display_name': display_name[:120]})
    targets.sort(key=lambda item: item['display_name'].casefold())
    return jsonify({'ok': True, 'targets': targets})


@app.route('/api/github-characters/preview', methods=['POST'])
@require_login
def api_github_characters_preview():
    payload = request.get_json(silent=True) or request.form or {}
    source_url = str(payload.get('url') or '').strip()
    try:
        items = load_github_characters(source_url)
        return jsonify({'ok': True, 'characters': character_summaries(items)})
    except GitHubSyncError as error:
        return jsonify({'ok': False, 'error': str(error)}), 400
    except Exception as error:
        log_action('GITHUB_CHARACTER_PREVIEW_FAILED', session.get('username'), {'error': str(error)[:300]})
        return jsonify({'ok': False, 'error': 'Не удалось прочитать персонажей с GitHub'}), 500


@app.route('/api/github-characters/update', methods=['POST'])
@require_login
def api_github_characters_update():
    payload = request.get_json(silent=True) or request.form or {}
    source_url = str(payload.get('url') or '').strip()
    source_key = str(payload.get('source_key') or '').strip()
    target_name = str(payload.get('target') or '').strip()
    username = (session.get('username') or '').strip()

    if not target_name or not source_key:
        return jsonify({'ok': False, 'error': 'Выбери персонажа для обновления'}), 400
    if not _player_can_access_character(username, target_name):
        return jsonify({'ok': False, 'error': 'Нет доступа к этому персонажу'}), 403

    characters = load_characters()
    _ensure_owner_for_legacy(username, target_name, characters)
    if target_name not in characters:
        return jsonify({'ok': False, 'error': 'Персонаж не найден'}), 404

    try:
        remote_items = load_github_characters(source_url)
        remote = next((item for item in remote_items if item.get('key') == source_key), None)
        if remote is None:
            raise GitHubSyncError('Персонаж больше не найден в GitHub источнике')

        current = characters[target_name]
        bonuses = get_equipment_bonuses(current)
        imported = remote.get('data') or {}
        if _looks_like_lss_payload(imported):
            imported = _convert_lss_payload(imported)
        sanitized = _sanitize_import_character(imported, target_name, current, bonuses)
        characters[target_name] = sanitized
        if not save_characters(characters):
            raise RuntimeError('save_failed')

        log_action('CHARACTER_UPDATED_FROM_GITHUB', username, {
            'character': target_name,
            'source_character': str(remote.get('name') or '')[:120],
            'source_path': str(remote.get('source_path') or '')[:300],
        })
        return jsonify({
            'ok': True,
            'target': target_name,
            'source_name': remote.get('name') or '',
        })
    except GitHubSyncError as error:
        return jsonify({'ok': False, 'error': str(error)}), 400
    except Exception as error:
        log_action('GITHUB_CHARACTER_UPDATE_FAILED', username, {
            'character': target_name,
            'error': str(error)[:300],
        })
        return jsonify({'ok': False, 'error': 'Не удалось обновить персонажа'}), 500


@app.route('/api/presence/ping', methods=['POST'])
@require_login
def api_presence_ping():
    """Легковесный heartbeat клиента для online-статуса (без лишней нагрузки)."""
    uname = (session.get('username') or '').strip()
    sid = (session.get('session_id') or '').strip()
    now_iso = datetime.now().isoformat()
    session['last_activity'] = now_iso
    if uname and sid:
        if uname not in user_sessions:
            user_sessions[uname] = {}
        user_sessions[uname][sid] = now_iso
    return jsonify({'ok': True})


@app.route('/create', methods=['GET', 'POST'])
@require_login
def create_character():
    if request.method != 'POST':
        return redirect(url_for('index', create='1'))
    name = (request.form.get('name') or request.form.get('character_name') or '').strip()
    name = re.sub(r'[\\/\x00-\x1f]+', ' ', name).strip()
    if not name:
        flash('Введите имя персонажа', 'error')
        return redirect(url_for('index', create='1'))
    if len(name) > 80:
        flash('Имя слишком длинное', 'error')
        return redirect(url_for('index', create='1'))
    characters = load_characters()
    if name in characters:
        flash('Персонаж с таким именем уже существует', 'error')
        return redirect(url_for('index', create='1'))
    character_data = create_default_character(name)
    character_data['owner'] = ADMIN_USERNAME
    character_data['is_placeholder'] = False
    characters[name] = character_data
    if not save_characters(characters):
        flash('Не удалось сохранить персонажа', 'error')
        return redirect(url_for('index', create='1'))
    return redirect(url_for('edit_character', name=name))

def create_default_character(name):
    """Создание персонажа по умолчанию"""
    return {
        "name": name,
        "owner": name,
        "is_placeholder": True,
        "race": "",
        "custom_races": [],
        "custom_skills": {},
        "custom_stats": {},
        "custom_fields": {},
        "classes": [],
        "level": 0,
        "hp": {"current": 10, "max": 10, "temp": 0},
        "speed": 30,
        "initiative": 0,
        "passive_perception": 10,
        "ac": 10,
        "proficiency_bonus": 2,
        "proficiencies": "",
        "stats": {
            "strength": 10, "dexterity": 10, "constitution": 10,
            "intelligence": 10, "wisdom": 10, "charisma": 10
        },
        "skills": {skill: 0 for skill in SKILLS},
        "personality": {
            "traits": "", "ideals": "", "bonds": "", "flaws": "",
            "backstory": "", "alignment": "Нейтральный"
        },
        "general_info": {
            "vision": "Обычное",
            "armor": "",
            "weapons": "",
            "tools": "",
            "languages": "",
            "traits": [],
        },
        "appearance": [
            {"label": "Рост", "value": ""},
            {"label": "Вес", "value": ""},
            {"label": "Возраст", "value": ""},
            {"label": "Глаза", "value": ""},
            {"label": "Кожа", "value": ""},
            {"label": "Волосы", "value": ""},
        ],
        "equipment": [],
        "equipped": {slot: None for slot in EQUIPMENT_SLOTS},
        "currency": [
            {"name": "Золото", "amount": 0},
            {"name": "Серебро", "amount": 0},
            {"name": "Медь", "amount": 0}
        ],
        "notes": [],
        "spell_slots": {str(i): 0 for i in range(0, 10)},
        "spell_slots_used": {str(i): 0 for i in range(0, 10)},
        "prepared_spells": [],
        "spell_schools": ['Некромантия', 'Эвокация', 'Ограждение', 'Преобразование', 'Прорицание', 'Очарование', 'Вызов', 'Иллюзия'],
        "spells": [],
        "favorite_spells": [],
        "created_at": datetime.now().isoformat()
    }

@app.route('/view/<name>')
@require_login
def view_character(name):
    """Просмотр персонажа"""
    user = get_current_user()
    
    # Проверка доступа
    if not _player_can_access_character(session.get('username') or '', name):
        log_action('UNAUTHORIZED_ACCESS', session.get('username'), {
            'attempted_character': name
        })
        flash('Доступ запрещен', 'error')
        return redirect(url_for('index'))
    
    characters = load_characters()
    _ensure_owner_for_legacy(session.get('username') or '', name, characters)
    if name not in characters:
        flash('Персонаж не найден', 'error')
        return redirect(url_for('index'))
    
    character = characters[name]
    bonuses = get_equipment_bonuses(character)
    # Нормализация: старые/импортированные персонажи могут не иметь всех навыков
    character = dict(character)
    c_skills = dict(character.get('skills') or {})
    for sk in SKILLS:
        c_skills.setdefault(sk, 0)
    character['skills'] = c_skills
    c_stats = dict(character.get('stats') or {})
    for stat_key in ['strength', 'dexterity', 'constitution', 'intelligence', 'wisdom', 'charisma']:
        c_stats.setdefault(stat_key, 10)
    character['stats'] = c_stats
    
    log_action('CHARACTER_VIEWED', session.get('username'), {'character': name})
    
    return render_template('view.html', 
                         character=character, 
                         name=name,
                         skills=SKILLS,
                         skill_to_stat=SKILL_TO_STAT,
                         bonuses=bonuses)


def _make_json_export_safe(obj):
    """Данные персонажа → JSON-совместимые типы (NaN/Inf, tuple/set, нестандартные типы)."""
    if isinstance(obj, dict):
        return {str(k): _make_json_export_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_make_json_export_safe(x) for x in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, (str, int, bool)) or obj is None:
        return obj
    if isinstance(obj, (tuple, set, frozenset)):
        return _make_json_export_safe(list(obj))
    return str(obj)


@app.route('/export/<name>/json')
@require_login
def export_character_json(name):
    """Экспорт персонажа в JSON"""
    # Проверка доступа
    if not _player_can_access_character(session.get('username') or '', name):
        flash('Доступ запрещен', 'error')
        return redirect(url_for('index'))
    
    characters = load_characters()
    _ensure_owner_for_legacy(session.get('username') or '', name, characters)
    if name not in characters:
        flash('Персонаж не найден', 'error')
        return redirect(url_for('index'))
    
    character = characters[name]
    safe = _make_json_export_safe(character)
    body = json.dumps(safe, ensure_ascii=False, allow_nan=False)
    safe_fn = re.sub(r'[^\w\-\.\s]', '_', str(name)).strip()[:120] or 'character'
    download_name = f"{safe_fn}_character.json"
    resp = make_response(body)
    resp.headers['Content-Type'] = 'application/json; charset=utf-8'
    # waitress кодирует заголовки как latin-1, поэтому имя файла должно быть ASCII.
    # Делаем fallback ASCII + RFC5987 filename* (UTF-8 percent-encoded).
    ascii_fallback = "character.json"
    resp.headers['Content-Disposition'] = f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(download_name)}"
    
    log_action('CHARACTER_EXPORTED_JSON', session.get('username'), {'character': name})
    return resp

def _sanitize_text(value, max_len=2000):
    """Безопасная строка (обрезаем, убираем управляющие символы)."""
    if value is None:
        return ""
    s = str(value)
    # Убираем управляющие символы (кроме \n \t)
    s = ''.join(ch for ch in s if ch == '\n' or ch == '\t' or ord(ch) >= 32)
    if len(s) > max_len:
        s = s[:max_len]
    return s

def _to_int(value, default=0, min_v=None, max_v=None):
    try:
        iv = int(value)
    except Exception:
        iv = default
    if min_v is not None:
        iv = max(min_v, iv)
    if max_v is not None:
        iv = min(max_v, iv)
    return iv


# Только поля из реального экспорта персонажа; без owner/equipment_bonuses (не импортируем / вычисляются на сервере)
_ALLOWED_IMPORT_TOP_KEYS = frozenset({
    'ac', 'appearance', 'classes', 'conditions', 'created_at', 'currency', 'custom_actions', 'custom_fields',
    'custom_races', 'custom_skills', 'custom_stats', 'death_saves', 'equipment', 'equipped', 'exhaustion_level',
    'favorite_spells', 'general_info', 'hp', 'initiative', 'is_placeholder', 'level', 'name', 'notes',
    'passive_perception', 'personality', 'prepared_spells', 'proficiencies', 'proficiency_bonus', 'race',
    'saved_conditions', 'saved_formulas', 'skill_bonuses', 'skills', 'speed', 'spell_schools', 'spell_slots',
    'spell_slots_used', 'spells', 'stats',
})


def _reject_unknown_import_keys(imported: dict, *, allow_lss: bool = False) -> None:
    # Универсальный импорт: лишние поля не роняют процесс.
    # Мы применяем только whitelisted-поля ниже, остальное безопасно игнорируется.
    return


def _sanitize_import_nested(val, depth: int = 0, max_depth: int = 10):
    """Ограниченная глубина/размер для вложенных структур (general_info, appearance, …)."""
    if depth > max_depth:
        raise ValueError('Слишком глубокая вложенность в JSON')
    if isinstance(val, dict):
        out = {}
        for i, (k, v) in enumerate(val.items()):
            if i >= 400:
                break
            ks = _sanitize_text(str(k), 120)
            if ks.startswith('__'):
                continue
            out[ks] = _sanitize_import_nested(v, depth + 1, max_depth)
        return out
    if isinstance(val, list):
        return [_sanitize_import_nested(x, depth + 1, max_depth) for x in list(val)[:800]]
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return val
    if isinstance(val, str):
        return _sanitize_text(val, 16000)
    raise ValueError('Недопустимый тип значения в JSON')


_LSS_STAT_MAP = {
    "str": "strength",
    "dex": "dexterity",
    "con": "constitution",
    "int": "intelligence",
    "wis": "wisdom",
    "cha": "charisma",
}

_LSS_SKILL_MAP = {
    "acrobatics": "Акробатика",
    "athletics": "Атлетика",
    "perception": "Восприятие",
    "survival": "Выживание",
    "performance": "Выступление",
    "intimidation": "Запугивание",
    "history": "История",
    "sleight of hand": "Ловкость рук",
    "arcana": "Магия",
    "medicine": "Медицина",
    "deception": "Обман",
    "animal handling": "Обращение с животными",
    "nature": "Природа",
    "insight": "Проницательность",
    "investigation": "Расследование",
    "religion": "Религия",
    "stealth": "Скрытность",
    "persuasion": "Убеждение",
}


def _json_loads_lenient(raw: str):
    """
    Более терпимый JSON-парсер:
    - обычный json.loads
    - если в конце файла мусор (например лишняя 'd') — берём первый валидный корневой объект {...}
    """
    try:
        return json.loads(raw)
    except Exception:
        pass

    s = raw or ""
    start = s.find("{")
    if start < 0:
        raise ValueError("Неверный JSON")

    depth = 0
    in_str = False
    esc = False
    end = -1
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        raise ValueError("Неверный JSON")
    return json.loads(s[start:end + 1])


def _lss_extract_text(node, max_len: int = 12000) -> str:
    """Извлекает plain text из вложенного JSON-редактора Long Story Short."""
    chunks = []

    def walk(x):
        if len(''.join(chunks)) >= max_len:
            return
        if x is None:
            return
        if isinstance(x, str):
            t = _sanitize_text(x, 4000)
            if t:
                chunks.append(t)
            return
        if isinstance(x, list):
            for it in x[:500]:
                walk(it)
            return
        if not isinstance(x, dict):
            return

        t = x.get("text")
        if isinstance(t, str) and t.strip():
            chunks.append(_sanitize_text(t, 4000))

        typ = (x.get("type") or "").strip().lower()
        content = x.get("content")
        if isinstance(content, list):
            for it in content[:500]:
                walk(it)
            if typ in ("paragraph", "heading", "listitem", "blockquote"):
                chunks.append("\n")

        for k in ("value", "data"):
            if k in x:
                walk(x.get(k))

    walk(node)
    text = ''.join(chunks)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return _sanitize_text(text, max_len)


def _lss_field_value(obj: dict, key: str, default=""):
    """Берёт поле вида {'value': ...} или прямое значение."""
    if not isinstance(obj, dict):
        return default
    val = obj.get(key, default)
    if isinstance(val, dict) and "value" in val:
        val = val.get("value")
    return val if val is not None else default


def _looks_like_lss_payload(imported: dict) -> bool:
    if not isinstance(imported, dict):
        return False
    if imported.get("jsonType") == "character" and isinstance(imported.get("info"), dict):
        return True
    if "data" not in imported:
        return False
    data = imported.get("data")
    if isinstance(data, dict):
        return data.get("jsonType") == "character" or isinstance(data.get("info"), dict)
    if isinstance(data, str):
        try:
            parsed = _json_loads_lenient(data)
            if isinstance(parsed, dict):
                return parsed.get("jsonType") == "character" or isinstance(parsed.get("info"), dict)
        except Exception:
            pass
        ds = data.strip()
        return ("\"jsonType\"" in ds and "\"character\"" in ds and "\"info\"" in ds) or ("\"template\"" in ds and "\"info\"" in ds)
    return False


def _convert_lss_payload(imported: dict) -> dict:
    """
    Конвертирует Long Story Short JSON в наш формат импорта персонажа.
    Возвращает только безопасные/понятные поля, остальное пройдёт в long_story_short_extra.
    """
    if not isinstance(imported, dict):
        raise ValueError("JSON должен быть объектом")

    root = imported
    inner = imported.get("data")
    if isinstance(inner, str):
        inner = _json_loads_lenient(inner)
    if isinstance(inner, dict):
        root = inner
    if not isinstance(root, dict):
        raise ValueError("Некорректный формат Long Story Short")

    info = root.get("info") if isinstance(root.get("info"), dict) else {}
    sub_info = root.get("subInfo") if isinstance(root.get("subInfo"), dict) else {}
    vitality = root.get("vitality") if isinstance(root.get("vitality"), dict) else {}
    stats = root.get("stats") if isinstance(root.get("stats"), dict) else {}
    skills = root.get("skills") if isinstance(root.get("skills"), dict) else {}
    text = root.get("text") if isinstance(root.get("text"), dict) else {}

    out = {}
    out["race"] = _sanitize_text(_lss_field_value(info, "race", ""), 100)
    out["level"] = _to_int(_lss_field_value(info, "level", 1), 1, 0, 20)
    out["proficiency_bonus"] = _to_int(root.get("proficiency", 2), 2, 0, 20)
    out["speed"] = 30
    out["initiative"] = 0
    out["passive_perception"] = 10

    ac_obj = vitality.get("ac")
    if isinstance(ac_obj, dict):
        out["ac"] = _to_int(ac_obj.get("value", 10), 10, 0, 50)

    hp_obj = {
        "max": _to_int(((vitality.get("hp-max") or {}).get("value", 10) if isinstance(vitality.get("hp-max"), dict) else 10), 10, 1, 9999),
        "current": _to_int(((vitality.get("hp-current") or {}).get("value", 10) if isinstance(vitality.get("hp-current"), dict) else 10), 10, 0, 9999),
        "temp": _to_int(((vitality.get("hp-temp") or {}).get("value", 0) if isinstance(vitality.get("hp-temp"), dict) else 0), 0, 0, 9999),
    }
    hp_obj["current"] = min(hp_obj["current"], hp_obj["max"])
    out["hp"] = hp_obj

    cls_name = _sanitize_text(_lss_field_value(info, "charClass", ""), 80)
    cls_sub = _sanitize_text(_lss_field_value(info, "charSubclass", ""), 80)
    classes = []
    if cls_name:
        classes.append({
            "name": cls_name,
            "level": max(1, out["level"]),
            "description": cls_sub,
        })
    out["classes"] = classes

    converted_stats = {}
    for src, dst in _LSS_STAT_MAP.items():
        sv = stats.get(src)
        if isinstance(sv, dict):
            converted_stats[dst] = _to_int(sv.get("score", 10), 10, 1, 30)
    if converted_stats:
        out["stats"] = converted_stats

    converted_skills = {}
    for src, dst in _LSS_SKILL_MAP.items():
        kv = skills.get(src)
        if not isinstance(kv, dict):
            continue
        prof = kv.get("isProf", 0)
        converted_skills[dst] = 1 if _to_int(prof, 0, 0, 3) > 0 else 0
    if converted_skills:
        out["skills"] = converted_skills

    backstory = _lss_extract_text((text.get("background") or {}).get("value") if isinstance(text.get("background"), dict) else text.get("background"))
    appearance_long = _lss_extract_text((text.get("appearance") or {}).get("value") if isinstance(text.get("appearance"), dict) else text.get("appearance"))
    traits = _lss_extract_text((text.get("traits") or {}).get("value") if isinstance(text.get("traits"), dict) else text.get("traits"))
    feats = _lss_extract_text((text.get("feats") or {}).get("value") if isinstance(text.get("feats"), dict) else text.get("feats"))
    prof_text = _lss_extract_text((text.get("prof") or {}).get("value") if isinstance(text.get("prof"), dict) else text.get("prof"))

    out["personality"] = {
        "alignment": _sanitize_text(_lss_field_value(info, "alignment", ""), 200),
        "backstory": backstory,
        "traits": traits,
        "ideals": "",
        "bonds": "",
        "flaws": "",
    }

    if prof_text:
        out["proficiencies"] = prof_text

    out["general_info"] = {
        "vision": "Обычное",
        "armor": "",
        "weapons": "",
        "tools": "",
        "languages": "",
        "traits": [],
    }

    appearance_rows = []
    labels = {
        "age": "Возраст",
        "height": "Рост",
        "weight": "Вес",
        "eyes": "Глаза",
        "skin": "Кожа",
        "hair": "Волосы",
    }
    for k, label in labels.items():
        v = _sanitize_text(_lss_field_value(sub_info, k, ""), 300)
        appearance_rows.append({"label": label, "value": v})
    out["appearance"] = appearance_rows

    notes = []
    if feats:
        notes.append({"text": f"Черты:\n{feats}"})
    if appearance_long:
        notes.append({"text": f"Описание внешности:\n{appearance_long}"})
    if notes:
        out["notes"] = notes[:200]

    return out


def _sanitize_import_character(imported: dict, target_name: str, base_character: dict, bonuses: dict, *, allow_lss: bool = False):
    """
    Безопасный импорт: whitelist полей + нормализация типов.
    Имя персонажа НЕ меняем (target_name).
    """
    if not isinstance(imported, dict):
        raise ValueError("JSON должен быть объектом")
    _reject_unknown_import_keys(imported, allow_lss=allow_lss)

    base = deepcopy(base_character) if isinstance(base_character, dict) else create_default_character(target_name)

    # гарантируем базовые ключи которые UI ожидает
    base.setdefault("custom_races", [])
    base.setdefault("custom_skills", {})
    base.setdefault("custom_stats", {})
    base.setdefault("custom_fields", {})
    base.setdefault("classes", [])
    base.setdefault("equipment", [])
    base.setdefault("currency", [])
    base.setdefault("notes", [])
    base.setdefault("spell_slots", {str(i): 0 for i in range(0, 10)})
    base.setdefault("spell_slots_used", {str(i): 0 for i in range(0, 10)})
    base.setdefault("spell_schools", ['Некромантия', 'Эвокация', 'Ограждение', 'Преобразование', 'Прорицание', 'Очарование', 'Вызов', 'Иллюзия'])
    base.setdefault("spells", [])
    base.setdefault("favorite_spells", [])
    base.setdefault("custom_actions", [])
    base.setdefault("death_saves", {"successes": [], "failures": []})
    base.setdefault("saved_formulas", [])
    base.setdefault("saved_conditions", [])
    base.setdefault("exhaustion_level", 0)
    base.setdefault("conditions", [])

    # простые поля
    base["name"] = target_name
    if "race" in imported:
        base["race"] = _sanitize_text(imported.get("race", ""), 100)
    if "level" in imported:
        base["level"] = _to_int(imported.get("level", 0), 0, 0, 20)
    if "speed" in imported:
        base["speed"] = _to_int(imported.get("speed", 30), 30, 0, 300)
    if "initiative" in imported:
        base["initiative"] = _to_int(imported.get("initiative", 0), 0, -50, 50)
    if "passive_perception" in imported:
        base["passive_perception"] = _to_int(imported.get("passive_perception", 10), 10, 0, 50)
    if "ac" in imported:
        base["ac"] = _to_int(imported.get("ac", 10), 10, 0, 50)
    if "proficiency_bonus" in imported:
        base["proficiency_bonus"] = _to_int(imported.get("proficiency_bonus", 2), 2, 0, 20)
    if "proficiencies" in imported:
        base["proficiencies"] = _sanitize_text(imported.get("proficiencies", ""), 5000)

    # HP
    hp = imported.get("hp")
    if isinstance(hp, dict):
        max_hp = _to_int(hp.get("max", base.get("hp", {}).get("max", 10)), 10, 1, 9999)
        cur_hp = _to_int(hp.get("current", base.get("hp", {}).get("current", max_hp)), max_hp, 0, max_hp)
        temp_hp = _to_int(hp.get("temp", base.get("hp", {}).get("temp", 0)), 0, 0, 9999)
        base["hp"] = {"current": cur_hp, "max": max_hp, "temp": temp_hp}

    # skill_bonuses (числовые бонусы к навыкам)
    sb_imp = imported.get("skill_bonuses")
    if isinstance(sb_imp, dict):
        base["skill_bonuses"] = {}
        for sk, v in list(sb_imp.items())[:200]:
            sk2 = _sanitize_text(str(sk), 80).strip()
            if not sk2:
                continue
            base["skill_bonuses"][sk2] = _to_int(v, 0, -9999, 9999)

    # general_info / appearance / prepared_spells (как в экспорте)
    if isinstance(imported.get("general_info"), dict):
        base["general_info"] = _sanitize_import_nested(imported.get("general_info"))
    if isinstance(imported.get("appearance"), list):
        base["appearance"] = _sanitize_import_nested(imported.get("appearance"))
    if isinstance(imported.get("prepared_spells"), list):
        base["prepared_spells"] = _sanitize_import_nested(imported.get("prepared_spells"))

    # stats
    stats = imported.get("stats")
    if isinstance(stats, dict):
        base.setdefault("stats", {})
        for k in ["strength","dexterity","constitution","intelligence","wisdom","charisma"]:
            if k in stats:
                base["stats"][k] = _to_int(stats.get(k, 10), 10, 1, 30)
        # custom stats values allowed too
        custom_stats = base.get("custom_stats") if isinstance(base.get("custom_stats"), dict) else {}
        for stat_key in list(custom_stats.keys())[:200]:
            if stat_key in stats:
                base["stats"][stat_key] = _to_int(stats.get(stat_key, 10), 10, -9999, 9999)

    # skills (only allow known skills + custom skills names)
    skills_in = imported.get("skills")
    if isinstance(skills_in, dict):
        base.setdefault("skills", {skill: 0 for skill in SKILLS})
        for skill in SKILLS:
            if skill in skills_in:
                base["skills"][skill] = 1 if _to_int(skills_in.get(skill, 0), 0, 0, 1) > 0 else 0
        # custom skills: allow 0/1 flags
        custom_skills = base.get("custom_skills") if isinstance(base.get("custom_skills"), dict) else {}
        for skill_name in list(custom_skills.keys())[:200]:
            if skill_name in skills_in:
                base["skills"][skill_name] = 1 if _to_int(skills_in.get(skill_name, 0), 0, 0, 1) > 0 else 0

    # personality
    pers = imported.get("personality")
    if isinstance(pers, dict):
        base.setdefault("personality", {})
        for k in ["traits","ideals","bonds","flaws","backstory","alignment"]:
            if k in pers:
                base["personality"][k] = _sanitize_text(pers.get(k, ""), 8000)

    # custom_* dictionaries
    cr = imported.get("custom_races")
    if isinstance(cr, list):
        base["custom_races"] = [_sanitize_text(x, 80) for x in cr[:200] if _sanitize_text(x, 80)]

    cs = imported.get("custom_stats")
    if isinstance(cs, dict):
        safe_cs = {}
        for k, v in list(cs.items())[:200]:
            k2 = _sanitize_text(k, 40).strip().lower()
            if not re.match(r'^[a-z_]+$', k2):
                continue
            safe_cs[k2] = _sanitize_text(v, 80)
        base["custom_stats"] = safe_cs

    cskills = imported.get("custom_skills")
    if isinstance(cskills, dict):
        safe_cskills = {}
        for sk, stat_key in list(cskills.items())[:200]:
            sk2 = _sanitize_text(sk, 80).strip()
            stat2 = _sanitize_text(stat_key, 40).strip().lower()
            if not sk2:
                continue
            # stat key can be standard or custom
            if not re.match(r'^[a-z_]+$', stat2):
                continue
            safe_cskills[sk2] = stat2
        base["custom_skills"] = safe_cskills

    cf = imported.get("custom_fields")
    if isinstance(cf, dict):
        safe_cf = {}
        for k, v in list(cf.items())[:100]:
            k2 = _sanitize_text(k, 80).strip()
            safe_cf[k2] = _to_int(v, 0, -999999, 999999)
        base["custom_fields"] = safe_cf

    # classes
    cls = imported.get("classes")
    if isinstance(cls, list):
        safe_classes = []
        for c in cls[:20]:
            if not isinstance(c, dict):
                continue
            safe_classes.append({
                "name": _sanitize_text(c.get("name", ""), 80),
                "level": _to_int(c.get("level", 1), 1, 1, 20),
                "description": _sanitize_text(c.get("description", ""), 500)
            })
        base["classes"] = safe_classes

    # equipment
    eq = imported.get("equipment")
    if isinstance(eq, list):
        safe_eq = []
        for it in eq[:300]:
            if not isinstance(it, dict):
                continue
            bonuses_obj = it.get("bonuses")
            safe_bonuses = {}
            if isinstance(bonuses_obj, str):
                try:
                    bonuses_obj = json.loads(bonuses_obj)
                except Exception:
                    bonuses_obj = None
            if isinstance(bonuses_obj, dict):
                for cat in ["basic", "stats", "skills"]:
                    if isinstance(bonuses_obj.get(cat), dict):
                        safe_bonuses[cat] = {}
                        for bk, bv in list(bonuses_obj.get(cat).items())[:200]:
                            safe_bonuses[cat][_sanitize_text(bk, 80)] = _to_int(bv, 0, -9999, 9999)
            safe_eq.append({
                "name": _sanitize_text(it.get("name", ""), 120),
                "description": _sanitize_text(it.get("description", ""), 5000),
                "quantity": _to_int(it.get("quantity", 1), 1, 0, 9999),
                "type": _sanitize_text(it.get("type", "item"), 30),
                "slot": _sanitize_text(it.get("slot", ""), 30),
                "weapon_formula": _sanitize_text(it.get("weapon_formula", ""), 120),
                "two_handed": bool(it.get("two_handed", False)),
                "weapon_light": bool(it.get("weapon_light", False)),
                "weapon_finesse": bool(it.get("weapon_finesse", False)),
                "weapon_thrown": bool(it.get("weapon_thrown", False)),
                "weapon_ranged": bool(it.get("weapon_ranged", False)),
                "weapon_heavy": bool(it.get("weapon_heavy", False)),
                "equipped": bool(it.get("equipped", False)),
                "apply_saved_conditions": [ _sanitize_text(x, 120) for x in (it.get("apply_saved_conditions") or [])[:200] ] if isinstance(it.get("apply_saved_conditions"), list) else [],
                "bonuses": safe_bonuses
            })
        base["equipment"] = safe_eq

    # equipped
    equipped = imported.get("equipped")
    if isinstance(equipped, dict):
        base.setdefault("equipped", {slot: None for slot in EQUIPMENT_SLOTS})
        for slot in EQUIPMENT_SLOTS:
            if slot in equipped:
                val = equipped.get(slot)
                base["equipped"][slot] = _sanitize_text(val, 120) if val else None

    # currency
    cur = imported.get("currency")
    if isinstance(cur, list):
        safe_cur = []
        for c in cur[:50]:
            if not isinstance(c, dict):
                continue
            safe_cur.append({"name": _sanitize_text(c.get("name", ""), 40), "amount": _to_int(c.get("amount", 0), 0, -99999999, 99999999)})
        base["currency"] = safe_cur

    # notes
    notes = imported.get("notes")
    if isinstance(notes, list):
        safe_notes = []
        for n in notes[:200]:
            if isinstance(n, dict):
                txt = n.get("text", "")
                if not txt and (n.get("title") or n.get("content")):
                    title = _sanitize_text(n.get("title", ""), 300)
                    content = _sanitize_text(n.get("content", ""), 8000)
                    txt = (f"{title}\n{content}".strip() if title else content)
                safe_notes.append({"text": _sanitize_text(txt, 8000)})
            else:
                safe_notes.append({"text": _sanitize_text(n, 8000)})
        base["notes"] = safe_notes

    # spells
    if isinstance(imported.get("spell_schools"), list):
        base["spell_schools"] = [_sanitize_text(s, 80) for s in imported.get("spell_schools")[:50] if _sanitize_text(s, 80)]
    spells = imported.get("spells")
    if isinstance(spells, list):
        safe_spells = []
        for sp in spells[:500]:
            if not isinstance(sp, dict):
                continue
            dmg = sp.get("damage")
            safe_dmg = {}
            if isinstance(dmg, dict):
                for k, v in list(dmg.items())[:20]:
                    safe_dmg[_sanitize_text(k, 4)] = _sanitize_text(v, 50)
            heal = sp.get("heal")
            safe_heal = {}
            if isinstance(heal, dict):
                for k, v in list(heal.items())[:20]:
                    safe_heal[_sanitize_text(k, 4)] = _sanitize_text(v, 50)
            safe_spells.append({
                "name": _sanitize_text(sp.get("name", ""), 120),
                "description": _sanitize_text(sp.get("description", ""), 8000),
                "school": _sanitize_text(sp.get("school", ""), 80),
                "level": _to_int(sp.get("level", 0), 0, 0, 9),
                "classes": [ _sanitize_text(x, 40) for x in (sp.get("classes") or [])[:50] ] if isinstance(sp.get("classes"), list) else [],
                "damage": safe_dmg,
                "heal": safe_heal
            })
        base["spells"] = safe_spells

    # spell slots
    ss = imported.get("spell_slots")
    if isinstance(ss, dict):
        base["spell_slots"] = {str(i): _to_int(ss.get(str(i), 0), 0, 0, 99) for i in range(0, 10)}
    ssu = imported.get("spell_slots_used")
    if isinstance(ssu, dict):
        base["spell_slots_used"] = {str(i): _to_int(ssu.get(str(i), 0), 0, 0, 99) for i in range(0, 10)}

    fav = imported.get("favorite_spells")
    if isinstance(fav, list):
        base["favorite_spells"] = [ _to_int(x, 0, 0, 9999) for x in fav[:500] ]

    # combat / conditions extras (store as-is but sanitized)
    base["exhaustion_level"] = _to_int(imported.get("exhaustion_level", base.get("exhaustion_level", 0)), 0, 0, 6)

    conds = imported.get("conditions")
    if isinstance(conds, list):
        safe_conds = []
        for c in conds[:200]:
            if not isinstance(c, dict):
                continue
            safe_conds.append({
                "name": _sanitize_text(c.get("name", ""), 120),
                "description": _sanitize_text(c.get("description", ""), 5000),
                "formulas": c.get("formulas", []) if isinstance(c.get("formulas"), list) else []
            })
        base["conditions"] = safe_conds

    # saved_formulas / custom_actions / saved_conditions: keep structure but cap sizes + sanitize strings
    for list_key, max_items in [("saved_formulas", 300), ("custom_actions", 200), ("saved_conditions", 200)]:
        src = imported.get(list_key)
        if isinstance(src, list):
            safe_list = []
            for it in src[:max_items]:
                if not isinstance(it, dict):
                    continue
                safe_item = {}
                for k, v in list(it.items())[:50]:
                    if isinstance(v, (int, float, bool)) or v is None:
                        safe_item[_sanitize_text(k, 50)] = v
                    elif isinstance(v, str):
                        safe_item[_sanitize_text(k, 50)] = _sanitize_text(v, 8000)
                    elif isinstance(v, list):
                        safe_item[_sanitize_text(k, 50)] = v[:200]
                    elif isinstance(v, dict):
                        safe_item[_sanitize_text(k, 50)] = v
                safe_list.append(safe_item)
            base[list_key] = safe_list

    # death saves
    ds = imported.get("death_saves")
    if isinstance(ds, dict):
        succ = ds.get("successes", []) if isinstance(ds.get("successes"), list) else []
        fail = ds.get("failures", []) if isinstance(ds.get("failures"), list) else []
        base["death_saves"] = {
            "successes": [bool(x) for x in succ[:3]],
            "failures": [bool(x) for x in fail[:3]],
        }

    # финальная нормализация: не даём текущим хп превышать максимум
    try:
        base["hp"]["max"] = max(1, int(base.get("hp", {}).get("max", 10)))
        base["hp"]["current"] = max(0, min(int(base.get("hp", {}).get("current", base["hp"]["max"])), base["hp"]["max"]))
        base["hp"]["temp"] = max(0, int(base.get("hp", {}).get("temp", 0)))
    except Exception:
        base["hp"] = {"current": 10, "max": 10, "temp": 0}

    # created_at не импортируем
    base["created_at"] = base.get("created_at") or datetime.now().isoformat()

    # Long Story Short режим: лишние поля не выбрасываем, а упаковываем безопасно
    if allow_lss:
        extra = {k: v for k, v in imported.items() if k not in _ALLOWED_IMPORT_TOP_KEYS}
        if extra:
            gi = base.get("general_info") if isinstance(base.get("general_info"), dict) else {}
            lss_extra = gi.get("long_story_short_extra") if isinstance(gi.get("long_story_short_extra"), dict) else {}
            for k, v in list(extra.items())[:200]:
                lss_extra[_sanitize_text(k, 120)] = _sanitize_import_nested(v, depth=0, max_depth=12)
            gi["long_story_short_extra"] = lss_extra
            base["general_info"] = gi

    return base

@app.route('/import/<name>/json', methods=['POST'])
@require_login
def import_character_json(name):
    """Безопасный импорт персонажа из JSON файла в текущего персонажа"""
    allow_lss = (request.form.get('import_mode') == 'lss')
    if not _player_can_access_character(session.get('username') or '', name):
        flash('Доступ запрещен', 'error')
        return redirect(url_for('edit_character', name=name))

    upload = request.files.get('json_file')
    if not upload:
        flash('Файл не выбран', 'error')
        return redirect(url_for('edit_character', name=name))

    # Ограничение на размер (дополнительно к общему MAX_CONTENT_LENGTH)
    upload.stream.seek(0, os.SEEK_END)
    size = upload.stream.tell()
    upload.stream.seek(0)
    if size > 2 * 1024 * 1024:
        flash('Файл слишком большой (макс. 2MB)', 'error')
        return redirect(url_for('edit_character', name=name))

    try:
        raw = upload.read().decode('utf-8', errors='strict')
    except Exception:
        flash('Файл должен быть в UTF-8', 'error')
        return redirect(url_for('edit_character', name=name))

    try:
        imported = _json_loads_lenient(raw)
    except Exception:
        flash('Неверный JSON', 'error')
        return redirect(url_for('edit_character', name=name))

    # Авто-распознавание Long Story Short, даже если чекбокс не отмечен.
    if not allow_lss and _looks_like_lss_payload(imported):
        allow_lss = True

    if allow_lss:
        try:
            imported = _convert_lss_payload(imported)
        except Exception as e:
            flash(f'Формат Long Story Short распознан, но не разобран: {str(e)}', 'error')
            return redirect(url_for('edit_character', name=name))

    characters = load_characters()
    _ensure_owner_for_legacy(session.get('username') or '', name, characters)
    if name not in characters:
        flash('Персонаж не найден', 'error')
        return redirect(url_for('index'))

    current = characters[name]
    bonuses = get_equipment_bonuses(current)

    try:
        sanitized = _sanitize_import_character(imported, name, current, bonuses, allow_lss=allow_lss)
    except Exception as e:
        flash(f'Импорт отклонён: {str(e)}', 'error')
        return redirect(url_for('edit_character', name=name))

    characters[name] = sanitized
    save_characters(characters)
    log_action('CHARACTER_IMPORTED_JSON', session.get('username'), {'character': name, 'size': size})
    flash('JSON импортирован.', 'success')
    return redirect(url_for('edit_character', name=name))

@app.route('/export/<name>/pdf')
@require_login
def export_character_pdf(name):
    """Экспорт персонажа в PDF"""
    # Проверка доступа
    if not _player_can_access_character(session.get('username') or '', name):
        flash('Доступ запрещен', 'error')
        return redirect(url_for('index'))
    
    characters = load_characters()
    _ensure_owner_for_legacy(session.get('username') or '', name, characters)
    if name not in characters:
        flash('Персонаж не найден', 'error')
        return redirect(url_for('index'))
    
    character = characters[name]
    bonuses = get_equipment_bonuses(character)
    
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
        from reportlab.platypus.tables import LongTable
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from io import BytesIO

        def _register_unicode_font():
            """
            Регистрируем Unicode TTF, чтобы кириллица печаталась без 'квадратиков'.
            Пробуем системные шрифты Windows/Linux.
            """
            candidates = []
            # Windows
            candidates += [
                ("AppFont", r"C:\Windows\Fonts\arial.ttf"),
                ("AppFont", r"C:\Windows\Fonts\segoeui.ttf"),
            ]
            # Linux (часто на VPS)
            candidates += [
                ("AppFont", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                ("AppFont", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
            ]
            for font_name, path in candidates:
                try:
                    if os.path.exists(path):
                        pdfmetrics.registerFont(TTFont(font_name, path))
                        return font_name
                except Exception:
                    continue
            return "Helvetica"

        FONT = _register_unicode_font()

        buffer = BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=15 * mm,
            rightMargin=15 * mm,
            topMargin=15 * mm,
            bottomMargin=15 * mm,
            title=f"{character.get('name', name)}"
        )
        styles = getSampleStyleSheet()

        title_style = ParagraphStyle(
            'Title',
            parent=styles['Heading1'],
            fontName=FONT,
            fontSize=20,
            textColor=colors.HexColor('#111111'),
            spaceAfter=10
        )
        h_style = ParagraphStyle(
            'H',
            parent=styles['Heading2'],
            fontName=FONT,
            fontSize=14,
            textColor=colors.HexColor('#111111'),
            spaceAfter=6,
            spaceBefore=12
        )
        normal = ParagraphStyle(
            'N',
            parent=styles['Normal'],
            fontName=FONT,
            fontSize=10,
            leading=12
        )

        def safe(text):
            # Убираем emoji, чтобы не было квадратиков даже если шрифт не покрывает их
            if text is None:
                return ""
            t = str(text)
            # грубое удаление суррогатов/символов вне BMP
            return "".join(ch for ch in t if ord(ch) <= 0xFFFF)

        story = []

        # Страница 1: Основное
        story.append(Paragraph(safe(character.get('name', name)), title_style))
        story.append(Paragraph("Лист персонажа (экспорт)", normal))
        story.append(Spacer(1, 6 * mm))

        story.append(Paragraph("Основная информация", h_style))
        base_info = [
            ["Раса", safe(character.get('race', ''))],
            ["Уровень", str(character.get('level', 1))],
            ["ХП", f"{character.get('hp', {}).get('current', 0)}/{character.get('hp', {}).get('max', 0)}"],
            ["КД", str(character.get('ac', 10))],
            ["Скорость", str(character.get('speed', 30))],
            ["Инициатива", str(character.get('initiative', 0))],
            ["Бонус мастерства", str(character.get('proficiency_bonus', 2))],
        ]
        t = Table(base_info, colWidths=[45 * mm, 135 * mm])
        t.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, -1), FONT),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f0f0f0')),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#999999')),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        story.append(t)

        # Страница 2: Характеристики + Навыки
        story.append(PageBreak())
        story.append(Paragraph("Характеристики", h_style))
        stats_rows = [["Характеристика", "Значение", "Мод."]]
        for stat_key, stat_name in [
            ('strength', 'Сила'), ('dexterity', 'Ловкость'), ('constitution', 'Телосложение'),
            ('intelligence', 'Интеллект'), ('wisdom', 'Мудрость'), ('charisma', 'Харизма')
        ]:
            stat_value = character.get('stats', {}).get(stat_key, 10)
            stat_bonus = bonuses.get('stats', {}).get(stat_key, 0)
            total_stat = stat_value + stat_bonus
            mod = calculate_modifier(total_stat)
            stats_rows.append([stat_name, str(total_stat), f"{mod:+d}"])
        stats_table = Table(stats_rows, colWidths=[70 * mm, 30 * mm, 30 * mm])
        stats_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, -1), FONT),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f0f0f0')),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#999999')),
        ]))
        story.append(stats_table)

        story.append(Spacer(1, 8 * mm))
        story.append(Paragraph("Навыки", h_style))
        skills_rows = [["Навык", "Мод."]]
        for skill in SKILLS:
            try:
                total_mod = get_skill_modifier(character, skill)
                # плюс бонусы от экипировки (get_skill_modifier уже учитывает)
            except Exception:
                total_mod = 0
            skills_rows.append([safe(skill), f"{int(total_mod):+d}"])
        skills_table = LongTable(skills_rows, colWidths=[120 * mm, 40 * mm], repeatRows=1)
        skills_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, -1), FONT),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f0f0f0')),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#999999')),
        ]))
        story.append(skills_table)

        # Страница 3: Снаряжение + Валюта
        story.append(PageBreak())
        story.append(Paragraph("Снаряжение", h_style))
        equip = character.get('equipment', []) or []
        if equip:
            equip_rows = [["Название", "Кол-во", "Надето", "Описание"]]
            for it in equip:
                equip_rows.append([
                    safe((it or {}).get('name', '')),
                    str((it or {}).get('quantity', 1)),
                    "да" if (it or {}).get('equipped') else "нет",
                    safe((it or {}).get('description', ''))[:200],
                ])
            equip_table = LongTable(equip_rows, colWidths=[55 * mm, 20 * mm, 20 * mm, 85 * mm], repeatRows=1)
            equip_table.setStyle(TableStyle([
                ('FONTNAME', (0, 0), (-1, -1), FONT),
                ('FONTSIZE', (0, 0), (-1, -1), 8),
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f0f0f0')),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#999999')),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ]))
            story.append(equip_table)
        else:
            story.append(Paragraph("Нет предметов.", normal))

        story.append(Spacer(1, 8 * mm))
        story.append(Paragraph("Валюта", h_style))
        curr = character.get('currency', []) or []
        if curr:
            curr_rows = [["Название", "Кол-во"]]
            for c in curr:
                curr_rows.append([safe((c or {}).get('name', '')), str((c or {}).get('amount', 0))])
            curr_table = Table(curr_rows, colWidths=[90 * mm, 90 * mm])
            curr_table.setStyle(TableStyle([
                ('FONTNAME', (0, 0), (-1, -1), FONT),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f0f0f0')),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#999999')),
            ]))
            story.append(curr_table)
        else:
            story.append(Paragraph("Нет валюты.", normal))

        # Страница 4+: Заклинания
        spells = character.get('spells', []) or []
        if spells:
            story.append(PageBreak())
            story.append(Paragraph("Заклинания", h_style))
            for sp in spells:
                sp_name = safe((sp or {}).get('name', 'Без названия'))
                sp_level = (sp or {}).get('level', 0)
                sp_school = safe((sp or {}).get('school', ''))
                header = f"{sp_name} уровень {sp_level}" + (f" ({sp_school})" if sp_school else "")
                story.append(Paragraph(header, ParagraphStyle('SpellH', parent=normal, fontName=FONT, fontSize=11, spaceBefore=6, spaceAfter=2)))
                desc = safe((sp or {}).get('description', ''))
                if desc:
                    story.append(Paragraph(desc, normal))
                dmg = (sp or {}).get('damage') or {}
                if isinstance(dmg, dict) and dmg:
                    dmg_lines = ", ".join([f"ур.{k}: {safe(v)}" for k, v in dmg.items()])
                    story.append(Paragraph("Урон: " + safe(dmg_lines), normal))
                story.append(Spacer(1, 3 * mm))

        # Страница: Заметки / Состояния
        story.append(PageBreak())
        story.append(Paragraph("Заметки", h_style))
        notes = character.get('notes', []) or []
        if notes:
            for n in notes:
                story.append(Paragraph("• " + safe((n or {}).get('text', n)), normal))
        else:
            story.append(Paragraph("Нет заметок.", normal))

        story.append(Spacer(1, 8 * mm))
        story.append(Paragraph("Состояния", h_style))
        conditions = character.get('conditions', []) or []
        if conditions:
            for c in conditions:
                cname = safe((c or {}).get('name', ''))
                cdesc = safe((c or {}).get('description', ''))
                story.append(Paragraph("• " + cname, normal))
                if cdesc:
                    story.append(Paragraph(cdesc, normal))
        else:
            story.append(Paragraph("Нет состояний.", normal))

        doc.build(story)
        buffer.seek(0)

        response = make_response(buffer.getvalue())
        response.headers['Content-Type'] = 'application/pdf'
        download_name = f'{name}_character.pdf'
        response.headers['Content-Disposition'] = f"attachment; filename=character.pdf; filename*=UTF-8''{quote(download_name)}"

        log_action('CHARACTER_EXPORTED_PDF', session.get('username'), {'character': name})
        return response

    except ImportError:
        flash('Библиотека reportlab не установлена. Установите: pip install reportlab', 'error')
        return redirect(url_for('view_character', name=name))
    except Exception as e:
        flash(f'Ошибка создания PDF: {str(e)}', 'error')
        return redirect(url_for('view_character', name=name))

@app.route('/edit/<name>', methods=['GET', 'POST'])
@require_login
def edit_character(name):
    """Редактирование персонажа"""
    user = get_current_user()
    
    # Проверка доступа
    if not _player_can_access_character(session.get('username') or '', name):
        log_action('UNAUTHORIZED_EDIT_ATTEMPT', session.get('username'), {
            'attempted_character': name
        })
        flash('Доступ запрещен', 'error')
        return redirect(url_for('index'))
    
    characters = load_characters()
    _ensure_owner_for_legacy(session.get('username') or '', name, characters)
    if name not in characters:
        flash('Персонаж не найден', 'error')
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        # Обновление данных персонажа. После импорта старая открытая вкладка не имеет
        # права затереть новые списки своим автосохранением.
        character = characters[name]
        current_epoch = str((character.get('_there_meta') or {}).get('import_epoch') or '')
        submitted_epoch = str(request.form.get('_there_import_epoch', '') or '')
        if current_epoch != submitted_epoch:
            if _is_fast_save_request():
                return jsonify({"success": False, "error": "stale_page"}), 409
            return make_response('Страница устарела после импорта. Перезагрузите лист.', 409)
        old_name = name
        new_name = request.form.get('name', name).strip()
        
        # Переименование персонажа
        if new_name and new_name != old_name:
            if new_name in characters:
                flash('Персонаж с таким именем уже существует', 'error')
                return redirect(url_for('edit_character', name=old_name))
            characters[new_name] = character
            del characters[old_name]
            name = new_name
            character['name'] = new_name

            # Обновляем привязку персонажа к аккаунту (важно для доступа и админки)
            try:
                users = load_local_state()
                # Если текущий игрок, обновляем его
                if not is_admin():
                    uname = session.get('username')
                    if uname and uname in users:
                        users[uname]['character_name'] = new_name
                        save_local_state(users)
                else:
                    # Админ мог переименовать чужого персонажа, найдём владельца по старому имени
                    changed = False
                    for uname, udata in users.items():
                        if (udata or {}).get('role') != 'player':
                            continue
                        current_char = (udata or {}).get('character_name') or uname
                        if current_char == old_name:
                            users[uname]['character_name'] = new_name
                            changed = True
                    if changed:
                        save_local_state(users)
            except Exception:
                pass
        
        # Основные данные
        character['level'] = max(0, min(20, int(request.form.get('level', 0))))
        character['race'] = request.form.get('race', '')
        character['ac'] = max(0, int(request.form.get('ac', 10)))
        character['proficiency_bonus'] = max(0, int(request.form.get('proficiency_bonus', 2)))
        character['proficiencies'] = request.form.get('proficiencies', '')
        
        # Хиты - учитываем базовое значение и бонусы от экипировки
        base_max_hp = int(request.form.get('hp_max_base', request.form.get('hp_max', 10)))
        bonuses = get_equipment_bonuses(character)
        max_hp_bonus = bonuses.get('basic', {}).get('max_hp', 0)
        character['hp']['max'] = max(1, base_max_hp + max_hp_bonus)
        # Важно: берем текущие ХП из формы, но не превышаем максимум
        hp_current_from_form = int(request.form.get('hp_current', character['hp']['current']))
        character['hp']['current'] = max(0, min(hp_current_from_form, character['hp']['max']))
        # Временные хиты (не ограничиваем максимумом)
        try:
            hp_temp_from_form = int(request.form.get('hp_temp', character['hp'].get('temp', 0)))
        except Exception:
            hp_temp_from_form = int(character['hp'].get('temp', 0) or 0)
        character['hp']['temp'] = max(0, hp_temp_from_form)
        
        # Движение
        character['speed'] = max(0, int(request.form.get('speed', 30)))
        character['initiative'] = int(request.form.get('initiative', 0))
        character['passive_perception'] = max(0, int(request.form.get('passive_perception', 10)))
        
        # Характеристики
        for stat in ['strength', 'dexterity', 'constitution', 'intelligence', 'wisdom', 'charisma']:
            character['stats'][stat] = max(1, min(30, int(request.form.get(stat, 10))))
        
        # Навыки (теперь это checkbox - 1 если есть мастерство, 0 если нет)
        # Также сохраняем ручные бонусы к навыкам
        if 'skill_bonuses' not in character:
            character['skill_bonuses'] = {}
        for skill in SKILLS:
            character['skills'][skill] = 1 if request.form.get(f'skill_{skill}') == 'on' else 0
            # Сохраняем ручной бонус к навыку
            skill_bonus = request.form.get(f'skill_bonus_{skill}', '0')
            try:
                character['skill_bonuses'][skill] = int(skill_bonus)
            except:
                character['skill_bonuses'][skill] = 0
        
        # Пользовательские навыки и характеристики (сохраняются через API)
        # custom_skills, custom_stats, custom_fields сохраняются через api_update_character
        
        # Личность
        character['personality']['traits'] = request.form.get('traits', '')
        character['personality']['ideals'] = request.form.get('ideals', '')
        character['personality']['bonds'] = request.form.get('bonds', '')
        character['personality']['flaws'] = request.form.get('flaws', '')
        character['personality']['backstory'] = request.form.get('backstory', '')
        character['personality']['alignment'] = request.form.get('alignment', 'Нейтральный')

        # Внешность (appearance_*), допускаем любые пункты
        appearance = []
        i = 0
        while True:
            lab = (request.form.get(f'appearance_label_{i}', '') or '').strip()
            val = (request.form.get(f'appearance_value_{i}', '') or '').strip()
            if not lab and not val:
                break
            appearance.append({"label": lab[:60], "value": val[:500]})
            i += 1
        if not appearance:
            appearance = [
                {"label": "Рост", "value": ""},
                {"label": "Вес", "value": ""},
                {"label": "Возраст", "value": ""},
                {"label": "Глаза", "value": ""},
                {"label": "Кожа", "value": ""},
                {"label": "Волосы", "value": ""},
            ]
        character['appearance'] = appearance

        # Общая информация
        if 'general_info' not in character or not isinstance(character.get('general_info'), dict):
            character['general_info'] = {}
        character['general_info']['vision'] = request.form.get('vision', character['general_info'].get('vision', 'Обычное'))
        character['general_info']['armor'] = request.form.get('armor_info', character['general_info'].get('armor', ''))
        character['general_info']['weapons'] = request.form.get('weapons_info', character['general_info'].get('weapons', ''))
        character['general_info']['tools'] = request.form.get('tools_info', character['general_info'].get('tools', ''))
        character['general_info']['languages'] = request.form.get('languages_info', character['general_info'].get('languages', ''))
        character['general_info']['armor_extra'] = request.form.get('armor_extra', character['general_info'].get('armor_extra', ''))
        character['general_info']['weapons_extra'] = request.form.get('weapons_extra', character['general_info'].get('weapons_extra', ''))
        character['general_info']['tools_extra'] = request.form.get('tools_extra', character['general_info'].get('tools_extra', ''))
        character['general_info']['languages_extra'] = request.form.get('languages_extra', character['general_info'].get('languages_extra', ''))
        # Черты: список. Пустой промежуточный слот не обрывает чтение следующих черт.
        trait_indexes = sorted({
            int(match.group(1))
            for key in request.form.keys()
            for match in [re.fullmatch(r'gi_trait_(?:name|desc)_(\d+)', key)]
            if match
        })
        traits = []
        for i in trait_indexes:
            tname = (request.form.get(f'gi_trait_name_{i}', '') or '').strip()
            tdesc = (request.form.get(f'gi_trait_desc_{i}', '') or '').strip()
            if not tname and not tdesc:
                continue
            traits.append({"name": tname[:160], "description": tdesc[:16000]})
        character['general_info']['traits'] = _clean_trait_records(traits)
        
        # Классы
        classes = []
        i = 0
        while True:
            class_name = request.form.get(f'class_name_{i}', '').strip()
            if not class_name:
                break
            class_level = request.form.get(f'class_level_{i}', '1')
            classes.append({
                "name": class_name,
                "level": max(1, int(class_level) if class_level else 1),
                "description": request.form.get(f'class_desc_{i}', '')
            })
            i += 1
        character['classes'] = classes
        # Уровень = сумма уровней классов (если есть классы)
        if classes:
            level_sum = sum(c.get('level', 1) for c in classes)
            character['level'] = max(0, min(20, level_sum))
        
        # Снаряжение
        equipment = []
        i = 0
        while True:
            item_name = request.form.get(f'item_name_{i}', '').strip()
            if not item_name:
                break
            # Получаем бонусы из скрытого поля JSON (упрощенная система)
            bonuses_json = request.form.get(f'item_bonuses_{i}', '{}')
            try:
                if bonuses_json and bonuses_json.startswith('{'):
                    bonuses = json.loads(bonuses_json)
                else:
                    bonuses = {'basic': {}, 'stats': {}, 'skills': {}}
                # Убеждаемся что структура правильная
                if 'basic' not in bonuses:
                    bonuses['basic'] = {}
                if 'stats' not in bonuses:
                    bonuses['stats'] = {}
                if 'skills' not in bonuses:
                    bonuses['skills'] = {}
            except json.JSONDecodeError:
                bonuses = {'basic': {}, 'stats': {}, 'skills': {}}
            except Exception:
                bonuses = {'basic': {}, 'stats': {}, 'skills': {}}
            
            eq_entry = {
                "name": item_name,
                "quantity": max(1, int(request.form.get(f'item_qty_{i}', 1))),
                "type": request.form.get(f'item_type_{i}', 'consumable'),
                "description": request.form.get(f'item_desc_{i}', ''),
                "slot": request.form.get(f'item_slot_{i}', ''),
                "weapon_formula": request.form.get(f'item_weapon_formula_{i}', ''),
                "two_handed": request.form.get(f'item_two_handed_{i}') == 'on',
                "weapon_light": request.form.get(f'item_weapon_light_{i}') == 'on',
                "weapon_finesse": request.form.get(f'item_weapon_finesse_{i}') == 'on',
                "weapon_thrown": request.form.get(f'item_weapon_thrown_{i}') == 'on',
                "weapon_ranged": request.form.get(f'item_weapon_ranged_{i}') == 'on',
                "weapon_heavy": request.form.get(f'item_weapon_heavy_{i}') == 'on',
                "apply_saved_conditions": [],
                "bonuses": bonuses,
                "equipped": request.form.get(f'item_equipped_{i}') == 'on'
            }
            # Типы владения для снаряжения
            eq_entry["prof_shield"] = request.form.get(f'item_prof_shield_{i}') == 'on'
            eq_entry["prof_light_armor"] = request.form.get(f'item_prof_light_armor_{i}') == 'on'
            eq_entry["prof_medium_armor"] = request.form.get(f'item_prof_medium_armor_{i}') == 'on'
            eq_entry["prof_heavy_armor"] = request.form.get(f'item_prof_heavy_armor_{i}') == 'on'
            eq_entry["prof_simple_ranged"] = request.form.get(f'item_prof_simple_ranged_{i}') == 'on'
            eq_entry["prof_simple_melee"] = request.form.get(f'item_prof_simple_melee_{i}') == 'on'
            eq_entry["prof_martial_melee"] = request.form.get(f'item_prof_martial_melee_{i}') == 'on'
            eq_entry["prof_martial_ranged"] = request.form.get(f'item_prof_martial_ranged_{i}') == 'on'

            # V6: надевать можно только настоящий тип сайта: "Снаряжение" или "Оружие".
            # Сюжетные предметы/расходники не должны случайно оказываться в руках на манекене.
            if eq_entry.get("type") not in ("equipment", "weapon"):
                eq_entry["type"] = "consumable"
                eq_entry["slot"] = ""
                eq_entry["equipped"] = False
            elif not eq_entry.get("slot"):
                eq_entry["equipped"] = False

            equipment.append(eq_entry)
            # состояния при использовании (список имен)
            try:
                raw_apply = request.form.get(f'item_apply_conditions_{i}', '[]')
                apply_list = []
                if raw_apply:
                    parsed = json.loads(raw_apply) if isinstance(raw_apply, str) else raw_apply
                    if isinstance(parsed, list):
                        for x in parsed[:200]:
                            if x is None:
                                continue
                            s = str(x).strip()
                            if s:
                                apply_list.append(s[:120])
                equipment[-1]["apply_saved_conditions"] = apply_list
            except Exception:
                equipment[-1]["apply_saved_conditions"] = []
            i += 1
        character['equipment'] = equipment
        
        # Обновление equipped на основе снаряжения
        for slot in EQUIPMENT_SLOTS:
            character['equipped'][slot] = None
        
        for item in equipment:
            if item.get('equipped') and item.get('slot') and item.get('type') in ('equipment', 'weapon'):
                slot = item['slot']
                if slot in EQUIPMENT_SLOTS:
                    character['equipped'][slot] = item['name']
                    # Двуручное оружие занимает обе руки (или оба слота оружия)
                    if item.get('two_handed'):
                        if slot in ['left_hand', 'right_hand']:
                            character['equipped']['left_hand'] = item['name']
                            character['equipped']['right_hand'] = item['name']
                        if slot in ['left_weapon', 'right_weapon']:
                            character['equipped']['left_weapon'] = item['name']
                            character['equipped']['right_weapon'] = item['name']
        
        # Валюта
        currency = []
        i = 0
        while True:
            curr_name = request.form.get(f'curr_name_{i}', '').strip()
            if not curr_name:
                break
            currency.append({
                "name": curr_name,
                "amount": max(0, int(request.form.get(f'curr_amount_{i}', 0)))
            })
            i += 1
        if currency:
            character['currency'] = currency
        
        # Заклинания - новая структура с поддержкой школ магии
        spells_json = request.form.get('spells_json', '[]')
        spell_schools_json = request.form.get('spell_schools_json', '[]')
        favorite_spells_json = request.form.get('favorite_spells', '[]')
        
        try:
            spells = json.loads(spells_json) if spells_json else []
            spell_schools = json.loads(spell_schools_json) if spell_schools_json else []
            favorite_spells = json.loads(favorite_spells_json) if favorite_spells_json else []
            
            # Валидация и миграция старых данных
            if not isinstance(spells, list):
                spells = []
            if not isinstance(spell_schools, list):
                spell_schools = ['Некромантия', 'Эвокация', 'Ограждение', 'Преобразование', 'Прорицание', 'Очарование', 'Вызов', 'Иллюзия']
            
            # Убеждаемся что все заклинания имеют обязательные поля
            for spell in spells:
                if 'school' not in spell or not spell['school']:
                    spell['school'] = spell_schools[0] if spell_schools else 'Некромантия'
                if 'level' not in spell:
                    spell['level'] = 1
                if 'damage' not in spell:
                    spell['damage'] = {}
                if 'classes' not in spell:
                    spell['classes'] = []
            
            character['spells'] = spells
            character['spell_schools'] = spell_schools
            if isinstance(favorite_spells, list):
                character['favorite_spells'] = favorite_spells
        except json.JSONDecodeError:
            # Если ошибка парсинга, используем старый формат для обратной совместимости
            spells = []
            i = 0
            while True:
                spell_name = request.form.get(f'spell_name_{i}', '').strip()
                if not spell_name:
                    break
                damage = {}
                for level in range(1, 10):
                    dmg = request.form.get(f'spell_damage_{i}_{level}', '').strip()
                    if dmg:
                        damage[str(level)] = dmg
                spells.append({
                    "name": spell_name,
                    "description": request.form.get(f'spell_desc_{i}', ''),
                    "damage": damage,
                    "school": spell_schools[0] if spell_schools else 'Некромантия',
                    "level": 1,
                    "classes": []
                })
                i += 1
            character['spells'] = spells
            if not character.get('spell_schools'):
                character['spell_schools'] = ['Некромантия', 'Эвокация', 'Ограждение', 'Преобразование', 'Прорицание', 'Очарование', 'Вызов', 'Иллюзия']
        
        # Ячейки заклинаний (включая заговоры - уровень 0)
        for level in range(0, 10):
            slot_val = request.form.get(f'spell_slot_{level}', '0')
            used_val = request.form.get(f'spell_slot_used_{level}', '0')
            character['spell_slots'][str(level)] = max(0, int(slot_val) if slot_val else 0)
            character['spell_slots_used'][str(level)] = max(0, int(used_val) if used_val else 0)
        
        # Заметки. Полностью пустые карточки отбрасываются, заполненные после пропуска сохраняются.
        note_indexes = sorted({
            int(match.group(1))
            for key in request.form.keys()
            for match in [re.fullmatch(r'note_(?:title|content)_(\d+)', key)]
            if match
        })
        notes = []
        for i in note_indexes:
            note_title = (request.form.get(f'note_title_{i}', '') or '').strip()
            note_content = request.form.get(f'note_content_{i}', '') or ''
            if not note_title and not note_content.strip():
                continue
            notes.append({"title": note_title, "content": note_content})
        character['notes'] = _clean_note_records(notes)
        
        # Истощение и состояния (инициализируем если нет)
        if 'exhaustion_level' not in character:
            character['exhaustion_level'] = 0
        if 'conditions' not in character:
            character['conditions'] = []
        
        # Сохранение
        # Закрепляем владельца (для мультиперсов)
        if not is_admin():
            character['owner'] = session.get('username') or character.get('owner') or ''
        character['is_placeholder'] = False
        characters[name] = character
        if save_characters(characters):
            # Логирование изменений
            log_action('CHARACTER_EDITED', session.get('username'), {
                'character': name,
                'changes': 'Данные персонажа обновлены'
            })
            if _is_fast_save_request():
                meta = character.get('_there_meta') if isinstance(character.get('_there_meta'), dict) else {}
                return jsonify({
                    'success': True,
                    'name': name,
                    'revision': int(meta.get('revision', 0) or 0),
                    'updated_at': str(meta.get('updated_at') or ''),
                })
            flash('Изменения сохранены!', 'success')
            # Остаемся на странице редактирования
            return redirect(url_for('edit_character', name=name))
        else:
            if _is_fast_save_request():
                return jsonify({'success': False, 'error': 'save_failed'}), 503
            flash('Ошибка сохранения', 'error')
    
    character = characters[name]
    bonuses = get_equipment_bonuses(character)
    
    exchange_state_for_edit = _exchange_get_state(character, name)
    return render_template('edit.html', 
                         character=character, 
                         name=name,
                         races=DEFAULT_RACES,
                         classes=CLASSES,
                         skills=SKILLS,
                         alignments=ALIGNMENTS,
                         skill_to_stat=SKILL_TO_STAT,
                         equipment_slots=EQUIPMENT_SLOTS,
                         bonuses=bonuses,
                         exchange_state=exchange_state_for_edit,
                         exchange_packet=_exchange_full_packet(exchange_state_for_edit))

@app.route('/delete/<name>', methods=['POST'])
@require_login
def delete_character(name):
    """Удаление персонажа"""
    username = (session.get('username') or '').strip()
    characters = load_characters()
    # Ленивая миграция legacy-данных: сначала заполним список персонажей, потом owner.
    try:
        _ensure_user_char_list(username, characters)
    except Exception:
        pass
    try:
        _ensure_owner_for_legacy(username, name, characters)
    except Exception:
        pass

    # Админ может удалять любых. Игрок может удалять только своего персонажа.
    if not _player_can_access_character(username, name, characters):
        flash('Доступ запрещен', 'error')
        return redirect(url_for('index'))

    if name in characters:
        del characters[name]
        save_characters(characters)

        # Если удаляет игрок — убираем из списка персонажей и (legacy) активного
        if not is_admin():
            try:
                username = session.get('username')
                users = load_local_state()
                if username and username in users:
                    lst = users[username].get('character_names')
                    if isinstance(lst, list):
                        users[username]['character_names'] = [x for x in lst if x != name][:200]
                    if users[username].get('character_name') == name:
                        users[username]['character_name'] = (users[username].get('character_names') or [username])[0] if isinstance(users[username].get('character_names'), list) and users[username].get('character_names') else username
                    save_local_state(users)
            except Exception:
                pass

        log_action('CHARACTER_DELETED', session.get('username'), {'character': name})
        flash('Персонаж удален', 'success')
    return redirect(url_for('index'))

@app.route('/api/roll/<stat>/<int:modifier>')
def roll_stat(stat, modifier):
    """Бросок кубика для характеристики"""
    roll = random.randint(1, 20)
    total = roll + modifier
    return jsonify({
        'stat': stat,
        'roll': roll,
        'modifier': modifier,
        'total': total
    })

@app.route('/api/roll_dice/<dice>')
def roll_dice(dice):
    """Бросок кубика по строке (напр. 2d6+3)"""
    try:
        result = parse_dice(dice)
        return jsonify({
            'dice': dice,
            'result': result
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/roll_skill/<name>/<skill>')
def roll_skill(name, skill):
    """Бросок кубика для навыка"""
    characters = load_characters()
    if name not in characters:
        return jsonify({'error': 'Character not found'}), 404
    
    character = characters[name]
    modifier = get_skill_modifier(character, skill)
    bonuses = get_equipment_bonuses(character)
    
    # Добавляем бонусы от экипировки
    skill_bonus = bonuses.get('skills', {}).get(skill, 0)
    total_modifier = modifier + skill_bonus
    
    roll = random.randint(1, 20)
    total = roll + total_modifier
    
    return jsonify({
        'skill': skill,
        'roll': roll,
        'modifier': modifier,
        'equipment_bonus': skill_bonus,
        'total_modifier': total_modifier,
        'total': total
    })

@app.route('/api/character/<name>')
@require_login
def api_character(name):
    """API для получения данных персонажа"""
    # Проверка доступа
    if not _player_can_access_character(session.get('username') or '', name):
        log_action('UNAUTHORIZED_API_ACCESS', session.get('username'), {
            'endpoint': f'/api/character/{name}'
        })
        return jsonify({'error': 'Access denied'}), 403
    
    characters = load_characters()
    _ensure_owner_for_legacy(session.get('username') or '', name, characters)
    if name not in characters:
        return jsonify({'error': 'Character not found'}), 404
    
    character = characters[name]
    bonuses = get_equipment_bonuses(character)
    character['equipment_bonuses'] = bonuses
    return jsonify(character)

@app.route('/api/update_hp', methods=['POST'])
@require_login
def api_update_hp():
    """Обновление текущих ХП"""
    name = request.form.get('name', '').strip()
    hp_current = int(request.form.get('hp_current', 0))
    
    if not name:
        return jsonify({'error': 'Name required'}), 400
    
    if not _player_can_access_character(session.get('username') or '', name):
        return jsonify({'error': 'Access denied'}), 403
    
    characters = load_characters()
    if name not in characters:
        return jsonify({'error': 'Character not found'}), 404
    
    characters[name]['hp']['current'] = max(0, hp_current)
    save_characters(characters)
    
    return jsonify({'success': True, 'hp_current': characters[name]['hp']['current']})

@app.route('/api/update_initiative', methods=['POST'])
@require_login
def api_update_initiative():
    """Обновление инициативы"""
    name = request.form.get('name', '').strip()
    initiative = int(request.form.get('initiative', 0))
    
    if not name:
        return jsonify({'error': 'Name required'}), 400
    
    if not _player_can_access_character(session.get('username') or '', name):
        return jsonify({'error': 'Access denied'}), 403
    
    characters = load_characters()
    if name not in characters:
        return jsonify({'error': 'Character not found'}), 404
    
    characters[name]['initiative'] = initiative
    save_characters(characters)
    
    return jsonify({'success': True, 'initiative': initiative})

@app.route('/api/character/<name>', methods=['PUT'])
@require_login
def api_update_character(name):
    """API для обновления персонажа (только своего)"""
    if not _player_can_access_character(session.get('username') or '', name):
        return jsonify({'error': 'Access denied'}), 403
    
    characters = load_characters()
    if name not in characters:
        return jsonify({'error': 'Character not found'}), 404
    
    character = characters[name]
    
    # Поддержка JSON body
    if request.is_json:
        data = request.get_json()
        # Обновляем все поля из JSON
        for key, value in data.items():
            if key != 'name':  # Не позволяем менять имя через API
                character[key] = value
    else:
        # Обновление favorite_spells (старый способ через form)
        if request.form.get('favorite_spells'):
            try:
                favorite_spells = json.loads(request.form.get('favorite_spells'))
                if isinstance(favorite_spells, list):
                    character['favorite_spells'] = favorite_spells
            except:
                pass
    
    # Санитизация после обновления: убираем дубликаты заклинаний и пустые элементы.
    try:
        spells = character.get('spells') if isinstance(character.get('spells'), list) else []
        seen = set()
        deduped = []
        for sp in spells:
            if not isinstance(sp, dict):
                continue
            n = (sp.get('name') or '').strip()
            if not n:
                continue
            key = n.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(sp)
        character['spells'] = deduped
    except Exception:
        pass

    save_characters(characters)
    return jsonify({'success': True})


def _character_snapshot_storage_path(character_name):
    """Файл слотов бэкапа для персонажа (имя в хэше — безопасные пути)."""
    key = hashlib.sha256((character_name or "").encode("utf-8")).hexdigest()
    return os.path.join(CHARACTER_SNAPSHOTS_DIR, f"{key}.json")


def _load_character_snapshots_file(character_name):
    path = _character_snapshot_storage_path(character_name)
    if not os.path.exists(path):
        return {"slots": [None] * 10}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        slots = data.get("slots")
        if not isinstance(slots, list):
            return {"slots": [None] * 10}
        while len(slots) < 10:
            slots.append(None)
        return {"slots": slots[:10]}
    except Exception:
        return {"slots": [None] * 10}


def _save_character_snapshots_file(character_name, doc):
    os.makedirs(CHARACTER_SNAPSHOTS_DIR, exist_ok=True)
    path = _character_snapshot_storage_path(character_name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


@app.route('/api/character/<name>/snapshots', methods=['GET'])
@require_login
def api_character_snapshots_list(name):
    if not _player_can_access_character(session.get('username') or '', name):
        return jsonify({'error': 'Access denied'}), 403
    doc = _load_character_snapshots_file(name)
    out_slots = []
    for i, slot in enumerate(doc.get("slots") or []):
        if not isinstance(slot, dict) or not slot.get("data"):
            out_slots.append(None)
            continue
        out_slots.append({
            "index": i,
            "label": slot.get("label") or "Сохранение",
            "saved_at": slot.get("saved_at") or "",
        })
    return jsonify({"success": True, "slots": out_slots})


@app.route('/api/character/<name>/snapshots/<int:slot_idx>', methods=['PUT'])
@require_login
def api_character_snapshot_save(name, slot_idx):
    if not _player_can_access_character(session.get('username') or '', name):
        return jsonify({'error': 'Access denied'}), 403
    if slot_idx < 0 or slot_idx > 9:
        return jsonify({'error': 'Invalid slot'}), 400
    characters = load_characters()
    if name not in characters:
        return jsonify({'error': 'Character not found'}), 404
    payload = request.get_json(silent=True) or {}
    custom_label = (payload.get("label") or "").strip()
    doc = _load_character_snapshots_file(name)
    slots = doc["slots"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    default_label = f"Сохранение {now}"
    snap = deepcopy(characters[name])
    meta = snap.get('_there_meta') if isinstance(snap.get('_there_meta'), dict) else {}
    slots[slot_idx] = {
        "label": custom_label or default_label,
        "saved_at": now,
        "saved_epoch": time.time(),
        "revision": int(meta.get('revision', 0) or 0),
        "data": snap,
    }
    _save_character_snapshots_file(name, doc)
    return jsonify({"success": True, "slot": {"index": slot_idx, "label": slots[slot_idx]["label"], "saved_at": slots[slot_idx]["saved_at"]}})


@app.route('/api/character/<name>/snapshots/<int:slot_idx>', methods=['PATCH'])
@require_login
def api_character_snapshot_rename(name, slot_idx):
    if not _player_can_access_character(session.get('username') or '', name):
        return jsonify({'error': 'Access denied'}), 403
    if slot_idx < 0 or slot_idx > 9:
        return jsonify({'error': 'Invalid slot'}), 400
    payload = request.get_json(silent=True) or {}
    new_label = (payload.get("label") or "").strip()
    if not new_label:
        return jsonify({'error': 'label required'}), 400
    doc = _load_character_snapshots_file(name)
    slot = doc["slots"][slot_idx]
    if not isinstance(slot, dict) or not slot.get("data"):
        return jsonify({'error': 'Empty slot'}), 400
    slot["label"] = new_label[:200]
    _save_character_snapshots_file(name, doc)
    return jsonify({"success": True, "label": slot["label"]})


@app.route('/api/character/<name>/snapshots/<int:slot_idx>', methods=['DELETE'])
@require_login
def api_character_snapshot_delete(name, slot_idx):
    if not _player_can_access_character(session.get('username') or '', name):
        return jsonify({'error': 'Access denied'}), 403
    if slot_idx < 0 or slot_idx > 9:
        return jsonify({'error': 'Invalid slot'}), 400
    doc = _load_character_snapshots_file(name)
    doc["slots"][slot_idx] = None
    _save_character_snapshots_file(name, doc)
    return jsonify({"success": True})



def _deep_difference_count(left, right, limit=500):
    count = 0
    stack = [(left, right)]
    while stack and count < limit:
        a, b = stack.pop()
        if type(a) is not type(b):
            count += 1
            continue
        if isinstance(a, dict):
            keys = set(a) | set(b)
            for key in keys:
                if key == '_there_meta':
                    continue
                if key not in a or key not in b:
                    count += 1
                else:
                    stack.append((a[key], b[key]))
        elif isinstance(a, list):
            count += abs(len(a) - len(b))
            for idx in range(min(len(a), len(b))):
                stack.append((a[idx], b[idx]))
        elif a != b:
            count += 1
    return count


def _snapshot_qualifies_for_archivist(current, slot):
    try:
        saved_epoch = float(slot.get('saved_epoch') or 0)
    except Exception:
        saved_epoch = 0
    age_seconds = max(0.0, time.time() - saved_epoch) if saved_epoch else 0.0
    current_meta = current.get('_there_meta') if isinstance(current, dict) and isinstance(current.get('_there_meta'), dict) else {}
    try:
        current_revision = int(current_meta.get('revision', 0) or 0)
        snapshot_revision = int(slot.get('revision', 0) or 0)
    except Exception:
        current_revision = snapshot_revision = 0
    revision_gap = max(0, current_revision - snapshot_revision)
    difference_count = _deep_difference_count(current, slot.get('data'))
    return age_seconds >= 7 * 24 * 60 * 60 and (revision_gap >= 40 or difference_count >= 35)


@app.route('/api/character/<name>/snapshots/<int:slot_idx>/restore', methods=['POST'])
@require_login
def api_character_snapshot_restore(name, slot_idx):
    if not _player_can_access_character(session.get('username') or '', name):
        return jsonify({'error': 'Access denied'}), 403
    if slot_idx < 0 or slot_idx > 9:
        return jsonify({'error': 'Invalid slot'}), 400
    doc = _load_character_snapshots_file(name)
    slot = doc["slots"][slot_idx]
    if not isinstance(slot, dict) or not slot.get("data"):
        return jsonify({'error': 'Empty slot'}), 400
    characters = load_characters()
    if name not in characters:
        return jsonify({'error': 'Character not found'}), 404
    current_character = deepcopy(characters[name])
    unlock_archivist = _snapshot_qualifies_for_archivist(current_character, slot)
    restored = deepcopy(slot["data"])
    if isinstance(restored, dict):
        restored.pop("name", None)
    characters[name] = restored
    save_characters(characters)
    unlocked = _unlock_achievement("a5") if unlock_archivist else None
    return jsonify({"success": True, "unlocked": unlocked})

@app.route('/api/use_spell/<name>/<int:spell_index>/<int:level>', methods=['POST'])
@require_login
def api_use_spell(name, spell_index, level):
    """Использование заклинания"""
    # Проверка доступа
    if not _player_can_access_character(session.get('username') or '', name):
        return jsonify({'error': 'Access denied'}), 403
    
    characters = load_characters()
    if name not in characters:
        return jsonify({'error': 'Character not found'}), 404
    
    character = characters[name]
    if spell_index >= len(character.get('spells', [])):
        return jsonify({'error': 'Spell not found'}), 404
    
    spell = character['spells'][spell_index]
    total = character['spell_slots'].get(str(level), 0)
    used = character['spell_slots_used'].get(str(level), 0)
    
    if used >= total:
        return jsonify({'error': 'No spell slots available'}), 400
    
    # Используем ячейку
    character['spell_slots_used'][str(level)] = used + 1
    save_characters(characters)
    
    # Рассчитываем урон если есть
    damage = None
    if spell.get('damage') and str(level) in spell['damage']:
        try:
            damage = parse_dice(spell['damage'][str(level)])
        except:
            pass
    
    log_action('SPELL_USED', session.get('username'), {
        'character': name,
        'spell': spell['name'],
        'level': level
    })
    
    return jsonify({
        'success': True,
        'remaining': total - (used + 1),
        'damage': damage
    })

@app.route('/api/use_spell_slot/<name>/<int:level>', methods=['POST'])
@require_login
def api_use_spell_slot(name, level):
    """Использование ячейки заклинания"""
    if not _player_can_access_character(session.get('username') or '', name):
        return jsonify({'error': 'Access denied'}), 403
    
    characters = load_characters()
    if name not in characters:
        return jsonify({'error': 'Character not found'}), 404
    
    character = characters[name]
    total = character['spell_slots'].get(str(level), 0)
    used = character['spell_slots_used'].get(str(level), 0)
    
    if used >= total:
        return jsonify({'error': 'No spell slots available'}), 400
    
    character['spell_slots_used'][str(level)] = used + 1
    save_characters(characters)
    
    return jsonify({
        'success': True,
        'remaining': total - (used + 1),
        'total': total
    })

@app.route('/api/restore_spell_slot/<name>/<int:level>', methods=['POST'])
@require_login
def api_restore_spell_slot(name, level):
    """Восстановление ячейки заклинания"""
    if not _player_can_access_character(session.get('username') or '', name):
        return jsonify({'error': 'Access denied'}), 403
    
    characters = load_characters()
    if name not in characters:
        return jsonify({'error': 'Character not found'}), 404
    
    character = characters[name]
    total = character['spell_slots'].get(str(level), 0)
    used = character['spell_slots_used'].get(str(level), 0)
    
    if used <= 0:
        return jsonify({'error': 'No used slots to restore'}), 400
    
    character['spell_slots_used'][str(level)] = max(0, used - 1)
    save_characters(characters)
    
    return jsonify({
        'success': True,
        'remaining': total - (character['spell_slots_used'][str(level)]),
        'total': total
    })



# ===================== EXCHANGE DND PANEL EXTENSION =====================
# Локальная панель для персонажа: импорт/экспорт текстом, patch/state обмен с обменом,
# автологин local profile только для локального запуска из батника.
EXCHANGE_DEFAULT_CHARACTER_NAME = "Персонаж"
EXCHANGE_LOCAL_AUTOSTART_ENV = "EXCHANGE_LOCAL_AUTOLOGIN"


def _exchange_now():
    return datetime.now().isoformat()


def _exchange_is_local_request() -> bool:
    try:
        ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
              or request.remote_addr or "")
        return ip in ("127.0.0.1", "::1", "localhost") or ip.startswith("127.")
    except Exception:
        return False




def _exchange_note_content(notes, title, default=""):
    wanted = str(title).strip().lower()
    for note in notes if isinstance(notes, list) else []:
        if isinstance(note, dict) and str(note.get("title", "")).strip().lower() == wanted:
            return str(note.get("content", "") or "").strip()
    return default


def _exchange_section(text_value, start_label, next_labels=None, default=""):
    text_value = str(text_value or "")
    pos = text_value.find(start_label)
    if pos < 0:
        return default
    pos += len(start_label)
    end = len(text_value)
    for label in next_labels or []:
        p2 = text_value.find(label, pos)
        if p2 >= 0:
            end = min(end, p2)
    return text_value[pos:end].strip(" \n:\t") or default


def _exchange_json_copy(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return deepcopy(value)


def _exchange_inventory_view(equipment):
    result = []
    for index, raw in enumerate(equipment if isinstance(equipment, list) else []):
        if not isinstance(raw, dict):
            continue
        item = _exchange_json_copy(raw)
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        item.setdefault("id", str(item.get("exchange_id") or f"item_{index}"))
        item.setdefault("desc", str(item.get("description") or ""))
        item.setdefault("description", str(item.get("desc") or ""))
        item.setdefault("type", "item")
        item.setdefault("quantity", 1)
        result.append(item)
    return result


def _exchange_character_view(sheet):
    sheet = sheet if isinstance(sheet, dict) else {}
    hp = sheet.get("hp") if isinstance(sheet.get("hp"), dict) else {}
    stats = sheet.get("stats") if isinstance(sheet.get("stats"), dict) else {}
    personality = sheet.get("personality") if isinstance(sheet.get("personality"), dict) else {}
    custom = sheet.get("custom_fields") if isinstance(sheet.get("custom_fields"), dict) else {}
    meters = {}
    for key, raw in custom.items():
        if isinstance(raw, dict) and ("value" in raw or "max" in raw):
            meters[str(key)] = {
                "label": str(raw.get("name") or raw.get("label") or key),
                "value": _to_int(raw.get("value", 0), 0, -999999, 999999),
                "max": _to_int(raw.get("max", 10), 10, 1, 999999),
            }
    return {
        "name": str(sheet.get("name") or "Персонаж"),
        "concept": str(personality.get("backstory") or sheet.get("concept") or ""),
        "classes": _exchange_json_copy(sheet.get("classes") if isinstance(sheet.get("classes"), list) else []),
        "race": str(sheet.get("race") or ""),
        "level": _to_int(sheet.get("level", 0), 0, 0, 999),
        "hp": {
            "current": _to_int(hp.get("current", 10), 10, -999999, 999999),
            "max": _to_int(hp.get("max", 10), 10, 1, 999999),
            "temp": _to_int(hp.get("temp", 0), 0, 0, 999999),
        },
        "ac": _to_int(sheet.get("ac", 10), 10, -999999, 999999),
        "speed": _to_int(sheet.get("speed", 30), 30, -999999, 999999),
        "initiative": _to_int(sheet.get("initiative", 0), 0, -999999, 999999),
        "passive_perception": _to_int(sheet.get("passive_perception", 10), 10, -999999, 999999),
        "proficiency_bonus": _to_int(sheet.get("proficiency_bonus", 2), 2, -999999, 999999),
        "stats": {key: _to_int(stats.get(key, 10), 10, -999999, 999999) for key in ["strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"]},
        "skills": _exchange_json_copy(sheet.get("skills") if isinstance(sheet.get("skills"), dict) else {}),
        "meters": meters,
    }


def _exchange_make_base_state(character: dict, name: str) -> dict:
    sheet = _exchange_json_copy(character if isinstance(character, dict) else {})
    sheet.pop("exchange_panel", None)
    sheet.setdefault("name", name)
    notes = sheet.get("notes") if isinstance(sheet.get("notes"), list) else []
    scene_note = _exchange_note_content(notes, "Текущая сцена")
    location = _exchange_section(scene_note, "Локация", ["Где остановились", "Последний удар"], "")
    summary = _exchange_section(scene_note, "Где остановились", ["Последний удар"], "Сцена ещё не описана.")
    last_beat = _exchange_section(scene_note, "Последний удар", [], "")
    flags = []
    for condition in sheet.get("conditions", []) if isinstance(sheet.get("conditions"), list) else []:
        title = str(condition.get("name", "") if isinstance(condition, dict) else condition).strip()
        if title:
            flags.append(title)
    state = {
        "schema": "DND_PARTY_PANEL",
        "version": "THERE_FULL_STATE_V2",
        "updatedAt": _exchange_now(),
        "campaign": {
            "name": _exchange_note_content(notes, "Кампания", "Новая кампания"),
            "arc": _exchange_note_content(notes, "Арка", ""),
            "tone": _exchange_note_content(notes, "Тон", ""),
        },
        "character": _exchange_character_view(sheet),
        "scene": {"location": location, "summary": summary, "lastBeat": last_beat, "choices": []},
        "quest": {"active": _exchange_note_content(notes, "Активный квест", "")},
        "inventory": _exchange_inventory_view(sheet.get("equipment")),
        "flags": flags,
        "entities": _exchange_note_content(notes, "Сущности и угрозы", ""),
        "gmNotes": _exchange_note_content(notes, "Заметки мастеру", ""),
        "gmSecrets": str(sheet.get("exchange_gm_secrets") or ""),
        "secretMechanics": _exchange_json_copy(sheet.get("exchange_secret_mechanics") if isinstance(sheet.get("exchange_secret_mechanics"), dict) else {}),
        "sessionLog": [],
        "development": _exchange_note_content(notes, "Развитие без класса", ""),
        "sheet": sheet,
    }
    log_text = _exchange_note_content(notes, "Летопись", "")
    if log_text:
        state["sessionLog"] = [{"id": "log_from_note", "date": _exchange_now(), "title": "Летопись из листа", "text": log_text}]
    return state


def _exchange_rebuild_views(state):
    state = state if isinstance(state, dict) else {}
    sheet = state.get("sheet") if isinstance(state.get("sheet"), dict) else {}
    generated = _exchange_make_base_state(sheet, str(sheet.get("name") or state.get("character", {}).get("name") or "Персонаж"))
    for key in ["schema", "version", "campaign", "scene", "quest", "entities", "gmNotes", "gmSecrets", "secretMechanics", "sessionLog", "development"]:
        if key in state:
            generated[key] = _exchange_json_copy(state[key])
    generated["sheet"] = _exchange_json_copy(sheet)
    generated["character"] = _exchange_character_view(sheet)
    generated["inventory"] = _exchange_inventory_view(sheet.get("equipment"))
    generated["flags"] = []
    for condition in sheet.get("conditions", []) if isinstance(sheet.get("conditions"), list) else []:
        title = str(condition.get("name", "") if isinstance(condition, dict) else condition).strip()
        if title:
            generated["flags"].append(title)
    generated.setdefault("scene", {})["choices"] = []
    generated["updatedAt"] = _exchange_now()
    return generated


def _exchange_normalize_state(state: dict, character: dict, name: str) -> dict:
    if not isinstance(state, dict):
        return _exchange_make_base_state(character, name)
    current = _exchange_make_base_state(character, name)
    incoming_sheet = state.get("sheet") if isinstance(state.get("sheet"), dict) else None
    merged_sheet = _exchange_json_copy(character)
    if incoming_sheet is not None:
        _exchange_deep_merge(merged_sheet, incoming_sheet)

        # notes меняются только при явном корректном поле sheet.notes.
        if isinstance(incoming_sheet.get("notes"), list):
            merged_sheet["notes"] = _exchange_normalize_notes(incoming_sheet.get("notes"), strict=True)
        else:
            merged_sheet["notes"] = _exchange_normalize_notes(character.get("notes"))

        incoming_info = incoming_sheet.get("general_info") if isinstance(incoming_sheet.get("general_info"), dict) else {}
        current_info = character.get("general_info") if isinstance(character.get("general_info"), dict) else {}
        merged_info = merged_sheet.get("general_info") if isinstance(merged_sheet.get("general_info"), dict) else {}
        if isinstance(incoming_info.get("traits"), list):
            merged_info["traits"] = _exchange_normalize_traits(incoming_info.get("traits"), strict=True)
        else:
            merged_info["traits"] = _exchange_normalize_traits(current_info.get("traits"))
        merged_sheet["general_info"] = merged_info
    current["sheet"] = merged_sheet
    for key in ["schema", "version", "campaign", "scene", "quest", "entities", "gmNotes", "gmSecrets", "secretMechanics", "sessionLog", "development"]:
        if key in state:
            current[key] = _exchange_json_copy(state[key])
    if incoming_sheet is None:
        if isinstance(state.get("character"), dict):
            _exchange_apply_character_view_to_sheet(current["sheet"], state["character"])
        if isinstance(state.get("inventory"), list):
            current["sheet"]["equipment"] = [_exchange_item_to_equipment(x, index) for index, x in enumerate(state["inventory"]) if isinstance(x, dict)]
        if isinstance(state.get("flags"), list):
            _exchange_set_flags_on_sheet(current["sheet"], state["flags"])
    _exchange_normalize_sheet_collections(current["sheet"])
    return _exchange_rebuild_views(current)

def _exchange_get_state(character: dict, name: str) -> dict:
    return _exchange_make_base_state(character or {}, name)


def _exchange_item_to_equipment(item, index=0):
    obj = _exchange_json_copy(item if isinstance(item, dict) else {"name": str(item)})
    obj["name"] = str(obj.get("name") or f"Предмет {index + 1}")
    if "description" not in obj and "desc" in obj:
        obj["description"] = obj.get("desc")
    if "desc" not in obj and "description" in obj:
        obj["desc"] = obj.get("description")
    obj.setdefault("quantity", 1)
    obj.setdefault("type", "item")
    obj.setdefault("exchange_id", str(obj.get("id") or _exchange_inventory_id(obj)))
    return obj



def _exchange_normalize_notes(value, strict=False):
    out = []
    for raw in value if isinstance(value, list) else []:
        if isinstance(raw, str) and not strict:
            title, content, extra = "", raw, {}
        elif isinstance(raw, dict):
            if strict and not ({"title", "content"} & set(raw.keys())):
                continue
            title = raw.get("title", "")
            content = raw.get("content", "")
            if not strict and content in (None, "") and "text" in raw:
                content = raw.get("text", "")
            extra = {
                str(key)[:80]: _exchange_json_copy(val)
                for key, val in raw.items()
                if key not in {"title", "content", "text", "name", "label", "description"}
            }
        else:
            continue
        title = _sanitize_text(title or "", 160).strip()
        content = _sanitize_text(content or "", 32000)
        if not title and not content.strip():
            continue
        extra["title"] = title or f"Заметка {len(out) + 1}"
        extra["content"] = content
        out.append(extra)
    return out[:1000]


def _exchange_normalize_traits(value, strict=False):
    out = []
    for raw in value if isinstance(value, list) else []:
        if isinstance(raw, str) and not strict:
            name, description, extra = raw, "", {}
        elif isinstance(raw, dict):
            if strict and not ({"name", "description"} & set(raw.keys())):
                continue
            name = raw.get("name", "")
            description = raw.get("description", "")
            if not strict and not name and "label" in raw:
                name = raw.get("label", "")
            extra = {
                str(key)[:80]: _exchange_json_copy(val)
                for key, val in raw.items()
                if key not in {"name", "description", "label", "title", "content", "text"}
            }
        else:
            continue
        name = _sanitize_text(name or "", 160).strip()
        description = _sanitize_text(description or "", 16000)
        if not name and not description.strip():
            continue
        extra["name"] = name or f"Черта {len(out) + 1}"
        extra["description"] = description
        out.append(extra)
    return out[:500]

def _exchange_normalize_currency(value):
    if isinstance(value, dict):
        value = [{"name": k, "amount": v} for k, v in value.items()]
    out = []
    for index, raw in enumerate(value if isinstance(value, list) else []):
        if isinstance(raw, dict):
            name = raw.get("name") or raw.get("label") or raw.get("title") or f"Валюта {index + 1}"
            amount = raw.get("amount", raw.get("value", 0))
            item = _exchange_json_copy(raw)
        else:
            name, amount, item = f"Валюта {index + 1}", raw, {}
        name = _sanitize_text(name, 80).strip()
        if not name:
            continue
        item["name"] = name
        item["amount"] = _to_int(amount, 0, 0, 2_000_000_000)
        out.append(item)
    return out[:200]


def _exchange_normalize_spells(value, schools=None):
    schools = [str(x) for x in (schools if isinstance(schools, list) else []) if str(x).strip()]
    default_school = schools[0] if schools else "Некромантия"
    out = []
    for index, raw in enumerate(value if isinstance(value, list) else []):
        if isinstance(raw, str):
            raw = {"name": raw}
        if not isinstance(raw, dict):
            continue
        item = _exchange_json_copy(raw)
        name = _sanitize_text(item.get("name") or item.get("title") or f"Заклинание {index + 1}", 200).strip()
        if not name:
            continue
        item["name"] = name
        item["description"] = _sanitize_text(item.get("description", item.get("desc", "")), 32000)
        item["school"] = _sanitize_text(item.get("school") or default_school, 120)
        item["level"] = _to_int(item.get("level", 0), 0, 0, 9)
        classes = item.get("classes")
        item["classes"] = [_sanitize_text(x, 120) for x in classes[:100] if _sanitize_text(x, 120).strip()] if isinstance(classes, list) else []
        for field in ("damage", "heal"):
            source = item.get(field)
            normalized = {}
            if isinstance(source, dict):
                for key, formula in list(source.items())[:20]:
                    try:
                        level = str(max(0, min(9, int(key))))
                    except Exception:
                        continue
                    text = _sanitize_text(formula, 500).strip()
                    if text:
                        normalized[level] = text
            elif isinstance(source, str) and source.strip():
                normalized[str(item["level"])] = _sanitize_text(source, 500).strip()
            item[field] = normalized
        out.append(item)
    return out[:1000]


def _exchange_normalize_equipment(value):
    out = []
    allowed_types = {"weapon", "equipment", "consumable"}
    for index, raw in enumerate(value if isinstance(value, list) else []):
        if isinstance(raw, str):
            raw = {"name": raw}
        if not isinstance(raw, dict):
            continue
        item = _exchange_item_to_equipment(raw, index)
        name = _sanitize_text(item.get("name"), 200).strip()
        if not name:
            continue
        item["name"] = name
        item["description"] = _sanitize_text(item.get("description", item.get("desc", "")), 32000)
        item["desc"] = item["description"]
        item["quantity"] = _to_int(item.get("quantity", 1), 1, 1, 2_000_000_000)
        item_type = str(item.get("type") or "consumable").strip().lower()
        item["type"] = item_type if item_type in allowed_types else "consumable"
        item["slot"] = _sanitize_text(item.get("slot", ""), 80)
        item["weapon_formula"] = _sanitize_text(item.get("weapon_formula", ""), 500)
        for key in ["two_handed", "weapon_light", "weapon_finesse", "weapon_thrown", "weapon_ranged", "weapon_heavy", "equipped", "prof_shield", "prof_light_armor", "prof_medium_armor", "prof_heavy_armor", "prof_simple_ranged", "prof_simple_melee", "prof_martial_melee", "prof_martial_ranged"]:
            item[key] = bool(item.get(key, False))
        apply_conditions = item.get("apply_saved_conditions")
        item["apply_saved_conditions"] = [_sanitize_text(x, 160) for x in apply_conditions[:200] if _sanitize_text(x, 160).strip()] if isinstance(apply_conditions, list) else []
        bonuses = item.get("bonuses") if isinstance(item.get("bonuses"), dict) else {}
        item["bonuses"] = {
            "basic": _exchange_json_copy(bonuses.get("basic") if isinstance(bonuses.get("basic"), dict) else {}),
            "stats": _exchange_json_copy(bonuses.get("stats") if isinstance(bonuses.get("stats"), dict) else {}),
            "skills": _exchange_json_copy(bonuses.get("skills") if isinstance(bonuses.get("skills"), dict) else {}),
        }
        if item["type"] not in {"weapon", "equipment"}:
            item["slot"] = ""
            item["equipped"] = False
        elif not item["slot"]:
            item["equipped"] = False
        out.append(item)
    return out[:2000]


def _exchange_normalize_sheet_collections(sheet, strict_import=False):
    if not isinstance(sheet, dict):
        return sheet
    sheet["notes"] = _exchange_normalize_notes(sheet.get("notes"), strict=strict_import)
    info = sheet.get("general_info") if isinstance(sheet.get("general_info"), dict) else {}
    info["traits"] = _exchange_normalize_traits(info.get("traits"), strict=strict_import)
    sheet["general_info"] = info
    sheet["currency"] = _exchange_normalize_currency(sheet.get("currency"))
    if not sheet["currency"]:
        sheet["currency"] = [{"name": "Золото", "amount": 0}, {"name": "Серебро", "amount": 0}, {"name": "Медь", "amount": 0}]
    schools = sheet.get("spell_schools") if isinstance(sheet.get("spell_schools"), list) else []
    sheet["spells"] = _exchange_normalize_spells(sheet.get("spells"), schools)
    sheet["equipment"] = _exchange_normalize_equipment(sheet.get("equipment"))
    prepared = sheet.get("prepared_spells")
    sheet["prepared_spells"] = sorted({i for i in [_to_int(x, -1, -1, 99999) for x in (prepared if isinstance(prepared, list) else [])] if 0 <= i < len(sheet["spells"])})
    favorites = sheet.get("favorite_spells")
    sheet["favorite_spells"] = sorted({i for i in [_to_int(x, -1, -1, 99999) for x in (favorites if isinstance(favorites, list) else [])] if 0 <= i < len(sheet["spells"])})
    equipped = {slot: None for slot in EQUIPMENT_SLOTS}
    for item in sheet["equipment"]:
        if item.get("equipped") and item.get("slot") in equipped and item.get("type") in {"weapon", "equipment"}:
            equipped[item["slot"]] = item["name"]
            if item.get("two_handed"):
                if item["slot"] in {"left_hand", "right_hand"}:
                    equipped["left_hand"] = equipped["right_hand"] = item["name"]
                if item["slot"] in {"left_weapon", "right_weapon"}:
                    equipped["left_weapon"] = equipped["right_weapon"] = item["name"]
    sheet["equipped"] = equipped
    return sheet


def _exchange_import_summary(character):
    info = character.get("general_info") if isinstance(character.get("general_info"), dict) else {}
    return {
        "spells": len(character.get("spells") if isinstance(character.get("spells"), list) else []),
        "notes": len(character.get("notes") if isinstance(character.get("notes"), list) else []),
        "traits": len(info.get("traits") if isinstance(info.get("traits"), list) else []),
        "equipment": len(character.get("equipment") if isinstance(character.get("equipment"), list) else []),
        "currency": len(character.get("currency") if isinstance(character.get("currency"), list) else []),
    }


def _exchange_validate_import_payload(kind, obj):
    """Отсекает случайный JSON и не позволяет структурам заметок и черт пересекаться."""
    if not isinstance(obj, dict):
        raise ValueError("Пакет обмена должен быть JSON объектом")
    root = obj.get("patch") if kind == "patch" and isinstance(obj.get("patch"), dict) else obj

    def validate_notes(value, label):
        if value is None:
            return
        if not isinstance(value, list):
            raise ValueError(f"{label} должно быть списком заметок")
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise ValueError(f"{label}[{index}] должно содержать объект с title и content")
            if not ({"title", "content"} & set(item.keys())):
                raise ValueError(f"{label}[{index}] не является заметкой")
            if {"name", "description"} & set(item.keys()) and not ({"title", "content"} & set(item.keys())):
                raise ValueError(f"{label}[{index}] похоже на черту, а не на заметку")

    def validate_traits(value, label):
        if value is None:
            return
        if not isinstance(value, list):
            raise ValueError(f"{label} должно быть списком черт")
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise ValueError(f"{label}[{index}] должно содержать объект с name и description")
            if not ({"name", "description"} & set(item.keys())):
                raise ValueError(f"{label}[{index}] не является чертой")

    sheet = root.get("sheet") if isinstance(root.get("sheet"), dict) else None
    if sheet is not None:
        if "notes" in sheet:
            validate_notes(sheet.get("notes"), "sheet.notes")
        info = sheet.get("general_info") if isinstance(sheet.get("general_info"), dict) else None
        if info is not None and "traits" in info:
            validate_traits(info.get("traits"), "sheet.general_info.traits")
    if kind == "patch":
        for key in ("notes_add", "notes_update"):
            if key in root:
                validate_notes(root.get(key), key)
        for key in ("traits_add", "traits_update"):
            if key in root:
                validate_traits(root.get(key), key)
    return True


def _exchange_requested_collection_minimums(kind, obj):
    root = obj if isinstance(obj, dict) else {}
    if kind == "patch":
        root = root.get("patch") if isinstance(root.get("patch"), dict) else root
    sheet = root.get("sheet") if isinstance(root.get("sheet"), dict) else (root.get("sheet") if kind == "state" else {})
    if not isinstance(sheet, dict):
        sheet = {}
    info = sheet.get("general_info") if isinstance(sheet.get("general_info"), dict) else {}
    result = {}
    mapping = {"spells": sheet.get("spells"), "notes": _exchange_normalize_notes(sheet.get("notes"), strict=True), "equipment": sheet.get("equipment"), "currency": sheet.get("currency"), "traits": _exchange_normalize_traits(info.get("traits"), strict=True)}
    for key, value in mapping.items():
        if isinstance(value, list):
            result[key] = len(value)
    return result


def _exchange_validate_import_result(kind, obj, character):
    requested = _exchange_requested_collection_minimums(kind, obj)
    actual = _exchange_import_summary(character)
    missing = []
    for key, minimum in requested.items():
        if minimum > 0 and actual.get(key, 0) < minimum:
            missing.append(f"{key}: ожидалось минимум {minimum}, сохранено {actual.get(key, 0)}")
    if missing:
        raise ValueError("Импорт не прошёл внутреннюю проверку: " + "; ".join(missing))
    return actual


def _exchange_apply_character_view_to_sheet(sheet, view):
    if not isinstance(sheet, dict) or not isinstance(view, dict):
        return
    direct = ["name", "race", "level", "ac", "speed", "initiative", "passive_perception", "proficiency_bonus", "classes", "skills"]
    for key in direct:
        if key in view:
            sheet[key] = _exchange_json_copy(view[key])
    if isinstance(view.get("hp"), dict):
        sheet.setdefault("hp", {}).update(_exchange_json_copy(view["hp"]))
    if isinstance(view.get("stats"), dict):
        sheet.setdefault("stats", {}).update(_exchange_json_copy(view["stats"]))
    if "concept" in view:
        sheet.setdefault("personality", {})["backstory"] = str(view.get("concept") or "")
        sheet["concept"] = str(view.get("concept") or "")
    if isinstance(view.get("meters"), dict):
        fields = sheet.setdefault("custom_fields", {})
        for key, meter in view["meters"].items():
            if isinstance(meter, dict):
                fields[str(key)] = {
                    "name": str(meter.get("label") or meter.get("name") or key),
                    "value": meter.get("value", 0),
                    "max": meter.get("max", 10),
                }


def _exchange_set_flags_on_sheet(sheet, flags):
    sheet["conditions"] = [{"name": str(flag), "description": "", "formula": ""} for flag in flags if str(flag).strip()]


def _exchange_sync_state_to_character(character: dict, exchange_state: dict) -> dict:
    state = _exchange_normalize_state(exchange_state, character, str((exchange_state.get("character") or {}).get("name") or character.get("name") or "Персонаж"))
    result = _exchange_json_copy(state.get("sheet") if isinstance(state.get("sheet"), dict) else character)
    result.setdefault("name", character.get("name") or "Персонаж")
    result["exchange_gm_secrets"] = str(state.get("gmSecrets") or "")
    result["exchange_secret_mechanics"] = _exchange_json_copy(state.get("secretMechanics") if isinstance(state.get("secretMechanics"), dict) else {})
    result["exchange_version"] = "THERE_FULL_STATE_V3"
    _exchange_normalize_sheet_collections(result, strict_import=True)
    # Кампания, арка, тон и прочие служебные поля больше не создают заметки.
    # notes изменяются только через явное поле sheet.notes или notes_* в PATCH.
    result["notes"] = _exchange_normalize_notes(result.get("notes"), strict=True)
    return result


def _exchange_sync_notes_to_site(character: dict, exchange_state: dict) -> None:
    notes = character.setdefault("notes", [])
    if not isinstance(notes, list):
        notes = []
        character["notes"] = notes
    scene = exchange_state.get("scene", {}) if isinstance(exchange_state.get("scene"), dict) else {}
    quest = exchange_state.get("quest", {}) if isinstance(exchange_state.get("quest"), dict) else {}
    _exchange_set_note(notes, "Кампания", str(exchange_state.get("campaign", {}).get("name", "")))
    _exchange_set_note(notes, "Арка", str(exchange_state.get("campaign", {}).get("arc", "")))
    _exchange_set_note(notes, "Тон", str(exchange_state.get("campaign", {}).get("tone", "")))
    _exchange_set_note(notes, "Текущая сцена", f"Локация: {scene.get('location','')}\n\nГде остановились:\n{scene.get('summary','')}\n\nПоследний удар:\n{scene.get('lastBeat','')}".strip())
    _exchange_set_note(notes, "Активный квест", str(quest.get("active", "")))
    _exchange_set_note(notes, "Сущности и угрозы", str(exchange_state.get("entities", "")))
    _exchange_set_note(notes, "Заметки мастеру", str(exchange_state.get("gmNotes", "")))
    _exchange_set_note(notes, "Развитие без класса", str(exchange_state.get("development", "")))
    log = exchange_state.get("sessionLog") if isinstance(exchange_state.get("sessionLog"), list) else []
    if log:
        text = "\n\n".join(str(x.get("text") or "") for x in log if isinstance(x, dict) and x.get("text"))
        _exchange_set_note(notes, "Летопись", text)


def _exchange_set_note(notes, title, content):
    title = str(title or '').strip()
    content = str(content or '')
    if not title and not content.strip():
        return
    if title and not content.strip():
        notes[:] = [note for note in notes if not (isinstance(note, dict) and str(note.get('title', '')).strip().lower() == title.lower())]
        return
    for note in notes:
        if isinstance(note, dict) and str(note.get("title", "")).strip().lower() == str(title).strip().lower():
            note["title"] = title
            note["content"] = content
            return
    notes.append({"title": title, "content": content})


def _exchange_inventory_id(item: dict) -> str:
    base = str((item or {}).get("id") or (item or {}).get("exchange_id") or (item or {}).get("name") or "exchange_item").strip().lower()
    base = re.sub(r"[^a-zа-я0-9]+", "_", base, flags=re.I).strip("_")
    return base or f"exchange_item_{int(time.time())}"


def _exchange_path_parts(path):
    if isinstance(path, list):
        return [str(x) for x in path]
    return [p for p in str(path or "").strip(".").split(".") if p != ""]


def _exchange_path_parent(root, path, create=False):
    parts = _exchange_path_parts(path)
    if not parts:
        return None, None
    cur = root
    for part in parts[:-1]:
        if isinstance(cur, list):
            idx = int(part)
            if idx < 0:
                idx += len(cur)
            if idx < 0 or idx >= len(cur):
                raise ValueError(f"Индекс вне списка: {part}")
            cur = cur[idx]
        elif isinstance(cur, dict):
            if part not in cur:
                if not create:
                    raise ValueError(f"Путь не найден: {path}")
                cur[part] = {}
            cur = cur[part]
        else:
            raise ValueError(f"Нельзя пройти по пути: {path}")
    return cur, parts[-1]


def _exchange_path_get(root, path):
    parts = _exchange_path_parts(path)
    cur = root
    for part in parts:
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            raise ValueError(f"Путь не найден: {path}")
    return cur


def _exchange_deep_merge(dst, src):
    if not isinstance(dst, dict) or not isinstance(src, dict):
        return _exchange_json_copy(src)
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _exchange_deep_merge(dst[key], value)
        else:
            dst[key] = _exchange_json_copy(value)
    return dst


def _exchange_match_where(item, where):
    if not isinstance(item, dict) or not isinstance(where, dict):
        return False
    for key, value in where.items():
        try:
            if _exchange_path_get(item, key) != value:
                return False
        except Exception:
            return False
    return True


def _exchange_apply_operation(state, operation):
    if not isinstance(operation, dict):
        return
    op = str(operation.get("op") or "").lower().strip()
    path = operation.get("path")
    if op in {"set", "replace"}:
        parent, key = _exchange_path_parent(state, path, create=True)
        value = _exchange_json_copy(operation.get("value"))
        if isinstance(parent, list):
            idx = int(key)
            if idx == len(parent):
                parent.append(value)
            else:
                parent[idx] = value
        else:
            parent[key] = value
    elif op == "merge":
        target = _exchange_path_get(state, path)
        value = operation.get("value")
        if not isinstance(target, dict) or not isinstance(value, dict):
            raise ValueError("merge требует объект по указанному пути и объект value")
        _exchange_deep_merge(target, value)
    elif op in {"append", "add"}:
        target = _exchange_path_get(state, path)
        if not isinstance(target, list):
            raise ValueError("append требует список")
        value = operation.get("value")
        if isinstance(value, list) and operation.get("many"):
            target.extend(_exchange_json_copy(value))
        else:
            target.append(_exchange_json_copy(value))
    elif op == "insert":
        target = _exchange_path_get(state, path)
        if not isinstance(target, list):
            raise ValueError("insert требует список")
        target.insert(int(operation.get("index", 0)), _exchange_json_copy(operation.get("value")))
    elif op in {"remove", "unset", "delete"}:
        parent, key = _exchange_path_parent(state, path, create=False)
        if isinstance(parent, list):
            parent.pop(int(key))
        else:
            parent.pop(key, None)
    elif op == "update_where":
        target = _exchange_path_get(state, path)
        if not isinstance(target, list):
            raise ValueError("update_where требует список")
        where = operation.get("where") or {}
        value = operation.get("value") or {}
        found = False
        for item in target:
            if _exchange_match_where(item, where):
                if not isinstance(item, dict) or not isinstance(value, dict):
                    raise ValueError("update_where изменяет объекты")
                _exchange_deep_merge(item, value)
                found = True
                if not operation.get("all"):
                    break
        if not found and operation.get("upsert"):
            obj = {}
            _exchange_deep_merge(obj, where)
            _exchange_deep_merge(obj, value)
            target.append(obj)
    elif op == "remove_where":
        target = _exchange_path_get(state, path)
        if not isinstance(target, list):
            raise ValueError("remove_where требует список")
        where = operation.get("where") or {}
        target[:] = [item for item in target if not _exchange_match_where(item, where)]
    else:
        raise ValueError(f"Неизвестная операция: {op}")


def _exchange_merge_patch(state: dict, patch: dict) -> dict:
    if not isinstance(patch, dict):
        raise ValueError("PATCH должен быть JSON-объектом")
    patch = patch.get("patch") if isinstance(patch.get("patch"), dict) else patch
    state = _exchange_json_copy(state)
    state.setdefault("sheet", {})

    for key in ["campaign", "scene", "quest"]:
        if isinstance(patch.get(key), dict):
            state.setdefault(key, {})
            _exchange_deep_merge(state[key], patch[key])
    for key in ["entities", "gmNotes", "gmSecrets", "development"]:
        if key in patch:
            state[key] = _exchange_json_copy(patch[key])
    if isinstance(patch.get("secretMechanics"), dict):
        state.setdefault("secretMechanics", {})
        _exchange_deep_merge(state["secretMechanics"], patch["secretMechanics"])

    if isinstance(patch.get("sheet"), dict):
        _exchange_deep_merge(state["sheet"], patch["sheet"])
    if isinstance(patch.get("character"), dict):
        _exchange_apply_character_view_to_sheet(state["sheet"], patch["character"])

    equipment = state["sheet"].setdefault("equipment", [])
    if not isinstance(equipment, list):
        equipment = []
        state["sheet"]["equipment"] = equipment
    if isinstance(patch.get("inventory_add"), list):
        for index, item in enumerate(patch["inventory_add"]):
            equipment.append(_exchange_item_to_equipment(item if isinstance(item, dict) else {"name": str(item)}, len(equipment) + index))
    if isinstance(patch.get("inventory_update"), list):
        for update in patch["inventory_update"]:
            if not isinstance(update, dict):
                continue
            marker = str(update.get("id") or update.get("exchange_id") or update.get("name") or "")
            for item in equipment:
                if not isinstance(item, dict):
                    continue
                candidates = {str(item.get("id") or ""), str(item.get("exchange_id") or ""), str(item.get("name") or "")}
                if marker and marker in candidates:
                    _exchange_deep_merge(item, update)
                    if "desc" in update and "description" not in update:
                        item["description"] = update["desc"]
                    break
    if isinstance(patch.get("inventory_remove"), list):
        markers = {str(x.get("id") or x.get("name")) if isinstance(x, dict) else str(x) for x in patch["inventory_remove"]}
        equipment[:] = [item for item in equipment if not isinstance(item, dict) or not ({str(item.get("id") or ""), str(item.get("exchange_id") or ""), str(item.get("name") or "")} & markers)]

    # Явные алиасы коллекций. Они не обязательны, но позволяют патчу добавлять
    # разделы напрямую, не зная внутренний путь sheet.*.
    collection_specs = {
        "spells": ("spells", "name"),
        "notes": ("notes", "title"),
        "currency": ("currency", "name"),
    }
    for prefix, (sheet_key, marker_key) in collection_specs.items():
        target = state["sheet"].setdefault(sheet_key, [])
        if not isinstance(target, list):
            target = []
            state["sheet"][sheet_key] = target
        add_key, update_key, remove_key = f"{prefix}_add", f"{prefix}_update", f"{prefix}_remove"
        if isinstance(patch.get(add_key), list):
            target.extend(_exchange_json_copy(patch[add_key]))
        if isinstance(patch.get(update_key), list):
            for update in patch[update_key]:
                if not isinstance(update, dict):
                    continue
                marker = str(update.get("id") or update.get(marker_key) or "")
                for item in target:
                    if isinstance(item, dict) and marker and marker in {str(item.get("id") or ""), str(item.get(marker_key) or "")}:
                        _exchange_deep_merge(item, update)
                        break
        if isinstance(patch.get(remove_key), list):
            markers = {str(x.get("id") or x.get(marker_key)) if isinstance(x, dict) else str(x) for x in patch[remove_key]}
            target[:] = [item for item in target if not isinstance(item, dict) or not ({str(item.get("id") or ""), str(item.get(marker_key) or "")} & markers)]

    traits_target = state["sheet"].setdefault("general_info", {}).setdefault("traits", [])
    if not isinstance(traits_target, list):
        traits_target = []
        state["sheet"]["general_info"]["traits"] = traits_target
    if isinstance(patch.get("traits_add"), list):
        traits_target.extend(_exchange_json_copy(patch["traits_add"]))
    if isinstance(patch.get("traits_update"), list):
        for update in patch["traits_update"]:
            if not isinstance(update, dict):
                continue
            marker = str(update.get("id") or update.get("name") or "")
            for item in traits_target:
                if isinstance(item, dict) and marker and marker in {str(item.get("id") or ""), str(item.get("name") or "")}:
                    _exchange_deep_merge(item, update)
                    break
    if isinstance(patch.get("traits_remove"), list):
        markers = {str(x.get("id") or x.get("name")) if isinstance(x, dict) else str(x) for x in patch["traits_remove"]}
        traits_target[:] = [item for item in traits_target if not isinstance(item, dict) or not ({str(item.get("id") or ""), str(item.get("name") or "")} & markers)]

    existing_flags = [str(c.get("name", "") if isinstance(c, dict) else c) for c in state["sheet"].get("conditions", []) if str(c.get("name", "") if isinstance(c, dict) else c).strip()]
    if isinstance(patch.get("flags_add"), list):
        for flag in patch["flags_add"]:
            if str(flag) not in existing_flags:
                existing_flags.append(str(flag))
    if isinstance(patch.get("flags_remove"), list):
        remove = {str(x) for x in patch["flags_remove"]}
        existing_flags = [x for x in existing_flags if x not in remove]
    if "flags_add" in patch or "flags_remove" in patch:
        _exchange_set_flags_on_sheet(state["sheet"], existing_flags)

    if isinstance(patch.get("log_append"), list):
        log = state.setdefault("sessionLog", [])
        for entry in patch["log_append"]:
            obj = {"title": "Запись", "text": str(entry)} if isinstance(entry, str) else _exchange_json_copy(entry)
            if not isinstance(obj, dict):
                continue
            obj.setdefault("id", f"log_{len(log)+1}_{int(time.time())}")
            obj.setdefault("date", _exchange_now())
            log.append(obj)
    if isinstance(patch.get("sessionLog_replace"), list):
        state["sessionLog"] = _exchange_json_copy(patch["sessionLog_replace"])

    operations = patch.get("operations") if isinstance(patch.get("operations"), list) else patch.get("ops")
    if isinstance(operations, list):
        for operation in operations:
            _exchange_apply_operation(state, operation)

    _exchange_normalize_sheet_collections(state["sheet"], strict_import=True)
    return _exchange_rebuild_views(state)


def _exchange_full_packet(state: dict) -> str:
    return f"# ДЛЯ МАСТЕРА\n\nDND_STATE_BEGIN\n{json.dumps(state, ensure_ascii=False, indent=2)}\nDND_STATE_END"


def _exchange_extract_between(text: str, start: str, end: str):
    match = re.search(re.escape(start) + r"([\s\S]*?)" + re.escape(end), text or "", flags=re.I)
    return match.group(1).strip() if match else None


def _exchange_load_json_from_text(text: str):
    text = text or ""
    for begin, finish, kind in [("DND_STATE_BEGIN", "DND_STATE_END", "state"), ("DND_PATCH_BEGIN", "DND_PATCH_END", "patch")]:
        block = _exchange_extract_between(text, begin, finish)
        if block:
            obj = _json_loads_lenient(block)
            if not isinstance(obj, dict):
                raise ValueError("Внутри блока должен быть JSON объект")
            if kind == "state" and not (str(obj.get("schema") or "").upper() == "DND_PARTY_PANEL" and isinstance(obj.get("sheet"), dict)):
                raise ValueError("DND_STATE не содержит корректный sheet")
            if kind == "patch" and not _exchange_is_patch_payload(obj):
                raise ValueError("DND_PATCH не содержит распознанных изменений")
            _exchange_validate_import_payload(kind, obj)
            return kind, obj
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.I)
    obj = _json_loads_lenient(fenced.group(1).strip() if fenced else text.strip())
    if not isinstance(obj, dict):
        raise ValueError("Текст не содержит пакет THERE")
    is_state = str(obj.get("schema") or "").upper() == "DND_PARTY_PANEL" and isinstance(obj.get("sheet"), dict)
    is_patch = not is_state and _exchange_is_patch_payload(obj)
    if not is_state and not is_patch:
        raise ValueError("Неизвестный JSON проигнорирован. Нужен DND_STATE или DND_PATCH")
    kind = "patch" if is_patch else "state"
    _exchange_validate_import_payload(kind, obj)
    return kind, obj


def _exchange_is_patch_payload(obj):
    if not isinstance(obj, dict):
        return False
    root = obj.get("patch") if isinstance(obj.get("patch"), dict) else obj
    known = {
        "campaign", "scene", "quest", "entities", "gmNotes", "gmSecrets", "development", "secretMechanics",
        "sheet", "character", "operations", "ops", "inventory_add", "inventory_update", "inventory_remove",
        "spells_add", "spells_update", "spells_remove", "notes_add", "notes_update", "notes_remove",
        "traits_add", "traits_update", "traits_remove", "currency_add", "currency_update", "currency_remove",
        "flags_add", "flags_remove", "log_append", "sessionLog_replace"
    }
    return any(key in root for key in known)


@app.route('/exchange/<name>')
@require_login
def exchange_panel(name):
    if not _player_can_access_character(session.get('username') or '', name):
        flash('Доступ запрещен', 'error')
        return redirect(url_for('index'))
    characters = load_characters()
    if name not in characters:
        flash('Персонаж не найден', 'error')
        return redirect(url_for('index'))
    state = _exchange_get_state(characters[name], name)
    return render_template('legacy_exchange.html', name=name, packet=_exchange_full_packet(state))


@app.route('/exchange/<name>/export.txt')
@require_login
def exchange_export_text(name):
    if not _player_can_access_character(session.get('username') or '', name):
        return "Access denied", 403
    characters = load_characters()
    if name not in characters:
        return "Not found", 404
    st = _exchange_get_state(characters[name], name)
    resp = make_response(_exchange_full_packet(st))
    resp.headers['Content-Type'] = 'text/plain; charset=utf-8'
    resp.headers['Content-Disposition'] = f"attachment; filename=dnd_state.txt; filename*=UTF-8''{quote('dnd_state_' + name + '.txt')}"
    return resp


@app.route('/api/exchange/<name>', methods=['GET', 'POST'])
@require_login
def api_exchange_state(name):
    if not _player_can_access_character(session.get('username') or '', name):
        return jsonify({'error': 'Access denied'}), 403
    characters = load_characters()
    if name not in characters:
        return jsonify({'error': 'Character not found'}), 404
    character = characters[name]
    if request.method == 'GET':
        st = _exchange_get_state(character, name)
        return jsonify({'state': st, 'packet': _exchange_full_packet(st)})
    data = request.get_json(silent=True) or {}
    st = data.get('state') if isinstance(data.get('state'), dict) else data
    _exchange_validate_import_payload('state', st)
    st = _exchange_normalize_state(st, character, name)
    synced_character = _exchange_sync_state_to_character(character, st)
    synced_character.setdefault('_there_meta', {})['import_epoch'] = secrets.token_urlsafe(24)
    characters[name] = synced_character
    if save_characters(characters):
        return jsonify({'success': True, 'state': st, 'packet': _exchange_full_packet(_exchange_get_state(characters[name], name)), 'applied': _exchange_import_summary(characters[name])})
    return jsonify({'error': 'Save failed'}), 500


@app.route('/api/exchange/<name>/import-text', methods=['POST'])
@require_login
def api_exchange_import_text(name):
    if not _player_can_access_character(session.get('username') or '', name):
        return jsonify({'error': 'Access denied'}), 403
    characters = load_characters()
    if name not in characters:
        return jsonify({'error': 'Character not found'}), 404
    text_body = ''
    if request.is_json:
        text_body = (request.get_json(silent=True) or {}).get('text', '')
    else:
        text_body = request.form.get('text', '')
    try:
        typ, obj = _exchange_load_json_from_text(text_body)
        _exchange_validate_import_payload(typ, obj)
        current = _exchange_get_state(characters[name], name)
        if typ == 'patch':
            st = _exchange_merge_patch(current, obj)
        else:
            st = _exchange_normalize_state(obj, characters[name], name)
        imported_character = _exchange_sync_state_to_character(characters[name], st)
        imported_character.setdefault('_there_meta', {})['import_epoch'] = secrets.token_urlsafe(24)
        applied = _exchange_validate_import_result(typ, obj, imported_character)
        characters[name] = imported_character
        if save_characters(characters):
            saved_character = characters[name]
            applied = _exchange_import_summary(saved_character)
            return jsonify({'success': True, 'type': typ, 'state': st, 'packet': _exchange_full_packet(_exchange_get_state(saved_character, name)), 'applied': applied})
        return jsonify({'error': 'Save failed'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/local-start')
def local_start():
    return redirect(url_for('index'))

# =================== /EXCHANGE DND PANEL EXTENSION =====================


@app.route('/api/characters')
@require_login
def api_characters():
    """API для получения списка всех персонажей"""
    user = get_current_user()
    
    if is_admin():
        # Админ видит всех персонажей
        characters = load_characters()
    else:
        username = session.get('username') or ''
        aliases = _get_username_aliases(username)
        all_chars = load_characters()
        users = load_local_state()
        u = users.get(username) or {}
        legacy = (u.get("character_name") or username)
        explicit = u.get("character_names") if isinstance(u.get("character_names"), list) else []
        allowed = []
        for nm, ch in (all_chars or {}).items():
            if isinstance(ch, dict) and _char_owner(ch) in aliases:
                allowed.append(nm)
        for nm in [legacy, username] + list(explicit):
            if nm and nm in (all_chars or {}) and nm not in allowed:
                allowed.append(nm)
        characters = {nm: all_chars[nm] for nm in allowed if nm in (all_chars or {})}
    
    return jsonify(characters)


@app.route('/api/cache-version')
@require_login
def api_cache_version():
    """Текущая версия клиентского кеша для принудительных обновлений."""
    return jsonify({
        'version': _load_site_cache_version(),
        'characters_rev': str(_characters_file_mtime_int()),
    })


@app.route('/battle_manager')
@require_login
def battle_manager():
    """Страница ведения боя (общая)."""
    log_action('BATTLE_MANAGER_OPENED', session.get('username'), {})
    return render_template('battle_manager.html', is_admin=is_admin())


@app.route('/api/battles', methods=['GET', 'POST'])
@require_login
def api_battles():
    username = session.get('username') or ''
    if request.method == 'GET':
        data = load_battles_for_user(username)
        # отдаем все бои (санитизируем, чтобы старые данные с переносами строк не ломали UI)
        battles_in = data.get("battles", [])
        battles_out = []
        for b in battles_in:
            sb = _sanitize_battle(b)
            if sb:
                battles_out.append(sb)
        return jsonify({"battles": battles_out})

    # POST create
    data = load_battles_for_user(username)
    battles = data.get("battles", [])
    bid = os.urandom(12).hex()
    battle = {
        "id": bid,
        "name": "Новый бой",
        "participants": [],
        "round": 1,
        "current_turn": 0,
        "created_at": datetime.now().isoformat(),
        "notes": "",
        "journal_draft": "",
        "log": [],
    }
    battles.insert(0, battle)
    data["battles"] = battles
    save_battles_for_user(username, data)
    log_action('BATTLE_CREATED', session.get('username'), {"battle_id": bid})
    return jsonify({"id": bid})


@app.route('/api/battles/<battle_id>', methods=['GET', 'PUT', 'DELETE'])
@require_login
def api_battle_item(battle_id):
    battle_id = (battle_id or "").strip().lower()
    username = session.get('username') or ''
    data = load_battles_for_user(username)
    battles = data.get("battles", [])
    idx = next((i for i, b in enumerate(battles) if str(b.get("id", "")).lower() == battle_id), -1)
    if idx < 0:
        return jsonify({"error": "Battle not found"}), 404

    if request.method == 'GET':
        sb = _sanitize_battle(battles[idx])
        if not sb:
            return jsonify({"error": "Battle not found"}), 404
        return jsonify({"battle": sb})

    if request.method == 'DELETE':
        deleted = battles.pop(idx)
        data["battles"] = battles
        save_battles_for_user(username, data)
        log_action('BATTLE_DELETED', session.get('username'), {"battle_id": battle_id, "name": deleted.get("name")})
        return jsonify({"success": True})

    # PUT replace
    body = request.get_json(silent=True) or {}
    incoming = body.get("battle")
    sb = _sanitize_battle(incoming)
    if not sb or str(sb.get("id", "")).lower() != battle_id:
        return jsonify({"error": "Invalid battle"}), 400
    battles[idx] = sb
    data["battles"] = battles
    save_battles_for_user(username, data)
    log_action('BATTLE_UPDATED', session.get('username'), {"battle_id": battle_id})
    return jsonify({"success": True})

# Периодическая очистка старых сессий
def cleanup_sessions():
    """Очистка неактивных сессий"""
    while True:
        time.sleep(300)  # Каждые 5 минут
        now = datetime.now()
        for username, sessions in list(user_sessions.items()):
            expired = [sid for sid, last_activity in sessions.items()
                      if (now - datetime.fromisoformat(last_activity)).total_seconds() > SESSION_TIMEOUT_MINUTES * 60]
            for sid in expired:
                del sessions[sid]
            if not sessions:
                del user_sessions[username]

# Запуск очистки сессий в отдельном потоке
cleanup_thread = threading.Thread(target=cleanup_sessions, daemon=True)
cleanup_thread.start()

def _get_network_info():
    """Публичная версия работает только на loopback и не узнаёт внешний IP."""
    return "127.0.0.1", None


if __name__ == '__main__':
    try:
        from waitress import serve
        serve(app, host='127.0.0.1', port=int(os.environ.get('THERE_PORT', '33851')), threads=16, channel_timeout=60)
    except Exception:
        app.run(host='127.0.0.1', port=int(os.environ.get('THERE_PORT', '33851')), debug=False, threaded=True)
