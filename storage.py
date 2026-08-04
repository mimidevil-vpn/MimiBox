# -*- coding: utf-8 -*-
"""Пути приложения и сохранение/загрузка данных.

Запись атомарная (сначала во временный файл, затем замена) — если приложение
уронят или выключат питание в момент сохранения, старый файл остаётся целым.
Все сбои записи пишутся в app.log рядом с данными и всплывают в UI, чтобы
«ничего не сохраняется» больше не происходило молча.
"""

import os
import sys
import json
import time
import tempfile
from dataclasses import asdict, fields

from parsing import Server

APP_FOLDER = "MimiBox"          # имя папки данных. При переезде со старого имени
                                # (LDK2ray) данные переносятся автоматически —
                                # см. _migrate_legacy_data().

_last_error = ""                # последняя ошибка записи — показываем в UI


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _writable(path: str) -> bool:
    """Папка годится, только если в неё реально получается писать."""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write-test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


_data_dir_cache = ""


def _migrate_legacy_data(new_dir: str) -> None:
    """Переносит данные из старой папки данных %APPDATA%\\LDK2ray.

    Новое имя папки — MimiBox. У тех, кто уже пользуется приложением, настройки,
    серверы, Telegram-сессия и плагины лежат в LDK2ray — при первом запуске новой
    версии перекладываем их целиком, чтобы ничего не потерялось.
    """
    import shutil
    try:
        base = os.environ.get("APPDATA")
        if not base:
            return
        old = os.path.join(base, "LDK2ray")
        if old == new_dir or not os.path.isdir(old):
            return
        if os.path.isdir(new_dir):
            try:
                if os.listdir(new_dir):
                    return          # в новой папке уже есть данные — не трогаем
            except OSError:
                return
            shutil.rmtree(new_dir, ignore_errors=True)
        os.replace(old, new_dir)
        log(f"[storage] данные перенесены из {old}")
    except Exception as e:
        log(f"[storage] миграция данных не удалась: {e}")


def data_dir() -> str:
    """Пользовательские данные храним в %APPDATA%\\MimiBox — эта папка всегда
    доступна на запись, поэтому настройки/серверы сохраняются независимо от того,
    куда установлено приложение (Program Files, флешка, только-для-чтения и т.п.).
    Если по какой-то причине она недоступна — спускаемся к запасным вариантам."""
    global _data_dir_cache
    if _data_dir_cache:
        return _data_dir_cache

    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if base:
            _migrate_legacy_data(os.path.join(base, APP_FOLDER))

    candidates = []
    if os.name == "nt":
        for env in ("APPDATA", "LOCALAPPDATA", "USERPROFILE"):
            base = os.environ.get(env)
            if base:
                candidates.append(os.path.join(base, APP_FOLDER))
    candidates.append(os.path.join(app_dir(), "data"))
    candidates.append(os.path.join(tempfile.gettempdir(), APP_FOLDER))

    for c in candidates:
        if _writable(c):
            _data_dir_cache = c
            return c

    # совсем безнадёжный случай — отдаём первый вариант, ошибки уйдут в лог
    _data_dir_cache = candidates[0]
    return _data_dir_cache


def SERVERS_FILE():
    return os.path.join(data_dir(), "servers.json")


def SETTINGS_FILE():
    return os.path.join(data_dir(), "settings.json")


def LOG_FILE():
    return os.path.join(data_dir(), "app.log")


LOG_MAX_BYTES = 1_000_000        # больше мегабайта смысла не хранит


def log(msg: str) -> None:
    """Пишем в app.log — единственный источник правды, когда что-то пошло не так."""
    path = LOG_FILE()
    try:
        # чтобы файл не рос бесконечно, при переполнении оставляем свежий хвост
        if os.path.getsize(path) > LOG_MAX_BYTES:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(LOG_MAX_BYTES // 2)
                f.readline()
                tail = f.read()
            with open(path, "w", encoding="utf-8") as f:
                f.write(tail)
    except Exception:
        pass
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (stamp, msg))
    except Exception:
        pass


def last_error() -> str:
    return _last_error


DEFAULT_SETTINGS = {
    "socks_port": 10808,
    "http_port": 10809,
    "system_proxy": True,       # режим «Прокси» — системный прокси Windows
    "tun_mode": False,          # режим «Туннель» — весь трафик через TUN (нужен админ)
    "xray_path": "",
    "subscription_url": "",
    "sub_info": {},             # upload/download/total/expire из заголовка панели
    "sub_updated": 0,           # когда подписка обновлялась в последний раз
    "theme": "auto",            # auto (следует теме системы) | light | dark
    "lang": "ru",
    "intro_done": False,        # интро-экран показывается только один раз
    "local_id": "",             # локальный идентификатор профиля (генерируется)
    "rating": 0,                # пользовательская оценка приложения (0..5)
    "minimize_to_tray": True,   # при закрытии сворачивать в трей, а не выходить
    "start_minimized": False,   # запускать сразу свёрнутым в трей
    "high_priority": False,     # высокий приоритет процесса (лечит вялый старт)
    "tun_dns": "1.1.1.1",       # DNS, который отдаём внутрь туннеля
    # ---- маршрутизация ----
    "route_mode": "global",     # global (всё через VPN) | rules (RU напрямую) | direct
    "direct_sites": [],         # сайты и IP в обход VPN
    "block_sites": [],          # сайты и IP, которым режем доступ
    # ---- привязанный Telegram ----
    "tg_username": "",
    "tg_name": "",
    "tg_avatar": "",            # data:image/... — храним прямо в настройках
    # ---- мессенджер (Telethon) ----
    "tg_api_id": "",            # api_id пользователя (my.telegram.org)
    "tg_api_hash": "",          # api_hash пользователя
    "plugin_enabled": {},       # имя плагина -> вкл/выкл
    # ---- эмодзи возле названия (меняется раз в час) ----
    "emoji": "",
    "emoji_ts": 0,
    # ---- кастомизация интерфейса ----
    "background_image": "",     # base64 данные пользовательского фона
    "tutorial_done": False,     # обучение пройдено
    "custom_bg": "",            # кастомный цвет фона (hex)
    "custom_text": "",          # кастомный цвет текста (hex)
    "custom_accent": "",        # кастомный цвет акцента (hex)
    "custom_surface": "",       # кастомный цвет поверхности (hex)
    "custom_font": "",          # кастомный шрифт (имя или CSS)
    "has_custom_font": False,   # загружен ли .ttf шрифт
    "last_news_id": "",         # ID последнего показанного поста
    "news_off": False,          # отключить уведомления о новых постах
    "snow_enabled": True,       # снежинки на подключённом сервере
}


def _atomic_write(path: str, text: str) -> bool:
    """Пишем через временный файл в той же папке + os.replace (атомарно на NTFS)."""
    global _last_error
    tmp = ""
    try:
        folder = os.path.dirname(path) or "."
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=folder)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _last_error = ""
        return True
    except Exception as e:
        _last_error = f"{os.path.basename(path)}: {e}"
        log(f"[storage] НЕ УДАЛОСЬ СОХРАНИТЬ {path}: {e}")
        try:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    path = SETTINGS_FILE()
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            s.update(loaded)
    except FileNotFoundError:
        log("[storage] settings.json ещё нет — стартуем с настроек по умолчанию")
    except Exception as e:
        log(f"[storage] settings.json повреждён ({e}) — беру настройки по умолчанию")
        _backup_broken(path)
    return s


def save_settings(settings: dict) -> bool:
    ok = _atomic_write(SETTINGS_FILE(),
                       json.dumps(settings, ensure_ascii=False, indent=2))
    if ok:
        log("[storage] настройки сохранены")
    return ok


def load_servers() -> list:
    result = []
    path = SERVERS_FILE()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        allowed = {fld.name for fld in fields(Server)}
        for item in raw:
            if not isinstance(item, dict):
                continue
            clean = {k: v for k, v in item.items() if k in allowed}
            try:
                result.append(Server(**clean))
            except Exception as e:
                log(f"[storage] пропускаю битую запись сервера: {e}")
    except FileNotFoundError:
        log("[storage] servers.json ещё нет — список серверов пуст")
    except Exception as e:
        log(f"[storage] servers.json повреждён ({e}) — список серверов пуст")
        _backup_broken(path)
    return result


def save_servers(servers: list) -> bool:
    try:
        data = [asdict(s) for s in servers]
    except Exception as e:
        log(f"[storage] не смог сериализовать серверы: {e}")
        return False
    ok = _atomic_write(SERVERS_FILE(),
                       json.dumps(data, ensure_ascii=False, indent=2))
    if ok:
        log(f"[storage] сохранено серверов: {len(data)}")
    return ok


def _backup_broken(path: str) -> None:
    """Битый файл не удаляем, а отодвигаем — вдруг данные ещё можно достать."""
    try:
        if os.path.exists(path):
            os.replace(path, path + ".broken")
    except Exception:
        pass


# --------------- безопасная валидация пользовательского ввода ---------------

import re as _re

_IP_RE = _re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
_URL_SCHEMES = ("http://", "https://")


def validate_port(value) -> int:
    """Безопасно преобразует значение в порт (1..65535)."""
    try:
        port = int(value)
    except (TypeError, ValueError):
        return 0
    return port if 1 <= port <= 65535 else 0


def validate_dns(value: str) -> str:
    """Проверяет, что DNS — допустимый IP-адрес или hostname."""
    v = (value or "").strip()
    if not v:
        return "1.1.1.1"
    if _IP_RE.match(v):
        return v
    if _re.match(r"^[a-zA-Z0-9._-]+$", v) and len(v) <= 253:
        return v
    return "1.1.1.1"


_URL_JUNK_CHARS = "\ufeff\u200b\u200c\u200d\u200e\u200f"


def clean_url(url: str) -> str:
    """Убирает мусор вокруг URL из буфера (BOM, zero-width, кавычки, скобки, пунктуацию)."""
    v = (url or "").strip()
    v = "".join(ch for ch in v if ch not in _URL_JUNK_CHARS)
    v = v.strip().strip("\"'<>()[]{}").strip().strip("\u00ab\u00bb\u201c\u201d\u2018\u2019").strip()
    v = v.strip().strip(".,;:").strip()
    return v


def validate_url(url: str) -> str:
    """Проверяет, что URL использует допустимую схему (http/https)."""
    v = clean_url(url)
    if not v:
        return ""
    low = v.lower()
    if any(low.startswith(s) for s in _URL_SCHEMES):
        return v
    return ""


def validate_color(value: str) -> str:
    """Проверяет, что цвет — корректный hex (#rgb или #rrggbb)."""
    v = (value or "").strip()
    if not v:
        return ""
    if _re.match(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$", v):
        return v.lower()
    return ""


def save_background(data_b64: str) -> bool:
    """Сохраняет base64-данные изображения фона в папку данных."""
    import base64
    if not data_b64:
        return False
    try:
        if "," in data_b64:
            data_b64 = data_b64.split(",", 1)[1]
        raw = base64.b64decode(data_b64)
        if len(raw) > 5 * 1024 * 1024:
            log("[storage] фон слишком большой (>5 МБ)")
            return False
        path = os.path.join(data_dir(), "background.png")
        return _atomic_write_raw(path, raw)
    except Exception as e:
        log(f"[storage] ошибка сохранения фона: {e}")
        return False


def load_background() -> str:
    """Читает сохранённый фон и возвращает голый base64 (без data: URI)."""
    import base64
    path = os.path.join(data_dir(), "background.png")
    try:
        with open(path, "rb") as f:
            raw = f.read()
        if len(raw) > 5 * 1024 * 1024:
            return ""
        return base64.b64encode(raw).decode("ascii")
    except FileNotFoundError:
        return ""
    except Exception:
        return ""


def remove_background() -> bool:
    """Удаляет сохранённый фон."""
    path = os.path.join(data_dir(), "background.png")
    try:
        if os.path.exists(path):
            os.remove(path)
        return True
    except Exception:
        return False


def save_font(data_b64: str) -> bool:
    """Сохраняет base64-данные .ttf шрифта в папку данных."""
    import base64
    if not data_b64:
        return False
    try:
        if "," in data_b64:
            data_b64 = data_b64.split(",", 1)[1]
        raw = base64.b64decode(data_b64)
        if len(raw) > 20 * 1024 * 1024:
            log("[storage] шрифт слишком большой (>20 МБ)")
            return False
        path = os.path.join(data_dir(), "custom_font.ttf")
        return _atomic_write_raw(path, raw)
    except Exception as e:
        log(f"[storage] ошибка сохранения шрифта: {e}")
        return False


def load_font() -> str:
    """Читает сохранённый шрифт и возвращает голый base64 (без data: URI)."""
    import base64
    path = os.path.join(data_dir(), "custom_font.ttf")
    try:
        with open(path, "rb") as f:
            raw = f.read()
        if len(raw) > 20 * 1024 * 1024:
            return ""
        return base64.b64encode(raw).decode("ascii")
    except FileNotFoundError:
        return ""
    except Exception:
        return ""


def remove_font() -> bool:
    """Удаляет сохранённый шрифт."""
    path = os.path.join(data_dir(), "custom_font.ttf")
    try:
        if os.path.exists(path):
            os.remove(path)
        return True
    except Exception:
        return False


def save_ser_avatar(data_b64: str) -> bool:
    """Сохраняет своё фото для Серийчика (прозрачный PNG)."""
    import base64
    if not data_b64:
        return False
    try:
        if "," in data_b64:
            data_b64 = data_b64.split(",", 1)[1]
        raw = base64.b64decode(data_b64)
        if len(raw) > 10 * 1024 * 1024:
            log("[storage] аватар Серийчика слишком большой (>10 МБ)")
            return False
        path = os.path.join(data_dir(), "ser_avatar.png")
        return _atomic_write_raw(path, raw)
    except Exception as e:
        log(f"[storage] ошибка сохранения аватара Серийчика: {e}")
        return False


def load_ser_avatar() -> str:
    """Читает своё фото Серийчика и возвращает голый base64 (без data: URI)."""
    import base64
    path = os.path.join(data_dir(), "ser_avatar.png")
    try:
        with open(path, "rb") as f:
            raw = f.read()
        if len(raw) > 10 * 1024 * 1024:
            return ""
        return base64.b64encode(raw).decode("ascii")
    except FileNotFoundError:
        return ""
    except Exception:
        return ""


def remove_ser_avatar() -> bool:
    """Удаляет своё фото Серийчика."""
    path = os.path.join(data_dir(), "ser_avatar.png")
    try:
        if os.path.exists(path):
            os.remove(path)
        return True
    except Exception:
        return False


def ser_default_avatar() -> str:
    """Фото Серийчика по умолчанию — ui/ser_default.png из ресурсов приложения."""
    import base64
    import sys as _sys
    candidates = []
    base = getattr(_sys, "_MEIPASS", None)
    if base:
        candidates.append(os.path.join(base, "ui", "ser_default.png"))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "ui", "ser_default.png"))
    for path in candidates:
        try:
            if not os.path.exists(path):
                continue
            with open(path, "rb") as f:
                raw = f.read()
            if len(raw) > 10 * 1024 * 1024:
                return ""
            return base64.b64encode(raw).decode("ascii")
        except Exception:
            continue
    return ""


def _atomic_write_raw(path: str, data: bytes) -> bool:
    """Атомарная запись бинарных данных."""
    global _last_error
    tmp = ""
    try:
        folder = os.path.dirname(path) or "."
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=folder)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _last_error = ""
        return True
    except Exception as e:
        _last_error = f"{os.path.basename(path)}: {e}"
        log(f"[storage] НЕ УДАЛОСЬ СОХРАНИТЬ {path}: {e}")
        try:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False
