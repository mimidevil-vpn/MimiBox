# -*- coding: utf-8 -*-
"""MimiBox — точка входа (pywebview) c поддержкой системного трея."""

import os
import sys
import threading
import ctypes
from ctypes import wintypes

import storage

APP_TITLE = "MimiBox"

# Профиль движка WebView2 держим в папке данных. По умолчанию он создаётся рядом
# с exe — а если приложение установлено в Program Files, туда писать нельзя, и
# окно повисает белым или не открывается вовсе.
try:
    _profile = os.path.join(storage.data_dir(), "webview2")
    os.makedirs(_profile, exist_ok=True)
    os.environ.setdefault("WEBVIEW2_USER_DATA_FOLDER", _profile)
except Exception:
    _profile = ""

# Интерфейс — одна статичная страница, поэтому одного рендерера достаточно:
# без ограничения WebView2 разводит больше десятка процессов и сотни мегабайт.
# А вот лимит JS-кучи (--max-old-space-size), который стоял здесь раньше, убран
# намеренно: с ним движок уходил в бесконечную сборку мусора и подвисал на старте.
# Таймеры не душим — иначе счётчик скорости замирает, когда окно свёрнуто.
os.environ.setdefault(
    "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
    "--renderer-process-limit=1 --disable-background-timer-throttling "
    "--disable-features=RendererCodeIntegrity",
)

import webview

from api import Api, apply_priority

# Трей — опционально: если pystray/Pillow недоступны, приложение работает без него.
try:
    import pystray
    from PIL import Image
    _HAS_TRAY = True
except Exception:
    _HAS_TRAY = False


def resource(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def human_speed(bps: int) -> str:
    """Байты/с в короткую подпись для трея."""
    v = float(max(0, bps))
    for unit in ("Б/с", "КБ/с", "МБ/с", "ГБ/с"):
        if v < 1024 or unit == "ГБ/с":
            return (f"{v:.0f} {unit}" if v >= 100 or unit == "Б/с"
                    else f"{v:.1f} {unit}")
        v /= 1024
    return "0 Б/с"


# ------------------------------------------------------- один экземпляр
# Если приложение уже запущено, повторный запуск не должен открывать новый
# сеанс: находим окно первого экземпляра и показываем/поднимаем его (в т.ч.
# если оно свёрнуто в трей), а сами выходим. Делаем это до создания окна,
# чтобы второй экземпляр не тратил время на инициализацию мессенджера и т.п.
_MUTEX_HANDLE = None


def _acquire_single_instance(name="Local\\MimiBox"):
    """True, если мы первый (единственный) экземпляр, False — если уже работает."""
    global _MUTEX_HANDLE
    kernel32 = ctypes.windll.kernel32
    h = kernel32.CreateMutexW(None, False, name)
    if kernel32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(h)
        return False
    _MUTEX_HANDLE = h
    return True


def _mimibox_pids():
    """PID всех процессов MimiBox.exe (поиск окна без привязки к заголовку)."""
    pids = set()
    TH32CS_SNAPPROCESS = 0x2

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return pids
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if kernel32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                if pe.szExeFile.lower() == "mimibox.exe":
                    pids.add(pe.th32ProcessID)
                if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                    break
    finally:
        kernel32.CloseHandle(snap)
    return pids


def _find_window_by_pids(pids):
    """Первое top-level окно, принадлежащее одному из PID."""
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            found.append(hwnd)
        return True

    user32.EnumWindows(_cb, 0)
    return found[0] if found else 0


def _activate_existing_window(title):
    """Показывает и поднимает уже открытое окно приложения."""
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        hwnd = _find_window_by_pids(_mimibox_pids())
    if not hwnd:
        return False
    # снимаем блокировку переднего плана, иначе SetForegroundWindow
    # из фонового процесса может проигнорироваться
    user32.keybd_event(0x12, 0, 0, 0)       # ALT вниз
    user32.keybd_event(0x12, 0, 2, 0)       # ALT вверх
    user32.ShowWindow(hwnd, 9)              # SW_RESTORE: показать/развернуть
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    return True


def main():
    # Один экземпляр: повторный запуск не создаёт новое окно, а показывает уже
    # открытое (в т.ч. свёрнутое в трей), после чего мы выходим.
    # Исключение — перезапуск с правами администратора (--relaunch): старый
    # экземпляр ещё жив и держит мьютекс, поэтому ждём его выхода до 15 секунд.
    relaunch = "--relaunch" in sys.argv
    if relaunch:
        import time
        deadline = time.time() + 15.0
        while not _acquire_single_instance():
            if time.time() >= deadline:
                storage.log("[env] не дождались выхода старого экземпляра")
                return
            _activate_existing_window(APP_TITLE)
            time.sleep(0.25)
        storage.log("[env] перезапуск с правами администратора")
    elif not _acquire_single_instance():
        storage.log("[env] повторный запуск — активирую существующее окно")
        _activate_existing_window(APP_TITLE)
        return

    with open(resource(os.path.join("ui", "index.html")), "r", encoding="utf-8") as f:
        html = f.read()

    api = Api()
    apply_priority(bool(api.settings.get("high_priority", False)))

    use_tray = _HAS_TRAY and bool(api.settings.get("minimize_to_tray", True))
    start_hidden = use_tray and bool(api.settings.get("start_minimized", False))

    window = webview.create_window(
        APP_TITLE,
        html=html,
        js_api=api,
        width=1060,
        height=752,
        min_size=(920, 640),
        background_color="#0F0F10",
        hidden=start_hidden,
    )

    state = {"tray": None, "quitting": False, "speed": (0, 0)}

    def do_quit():
        state["quitting"] = True
        try:
            api.shutdown()
        except Exception:
            pass
        try:
            if state["tray"] is not None:
                state["tray"].stop()
        except Exception:
            pass
        try:
            window.destroy()
        except Exception:
            pass

    def on_closing():
        # Если включён трей — прячем окно вместо выхода.
        if not state["quitting"] and _HAS_TRAY and state["tray"] is not None \
                and api.settings.get("minimize_to_tray", True):
            try:
                window.hide()
            except Exception:
                pass
            return False  # отменяем закрытие
        api.shutdown()
        return True

    try:
        window.events.closing += on_closing
    except Exception:
        pass

    speed_cb = None

    # ---- системный трей ----
    if use_tray:
        def speed_line():
            up, down = state["speed"]
            if not api.connected:
                return "Отключено"
            return f"↓ {human_speed(down)}   ↑ {human_speed(up)}"

        def on_speed(up, down):
            state["speed"] = (up, down)
            icon = state["tray"]
            if icon is None:
                return
            try:
                emoji = api.settings.get("emoji", "")
                icon.title = f"{emoji} {APP_TITLE}\n{speed_line()}".strip()
            except Exception:
                pass

        speed_cb = on_speed

        def run_tray():
            try:
                img = Image.open(resource(os.path.join("ui", "app.ico")))
            except Exception:
                return

            def act_show(icon, item):
                try:
                    window.show()
                except Exception:
                    pass

            menu = pystray.Menu(
                pystray.MenuItem(lambda item: speed_line(), None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(f"Открыть {APP_TITLE}", act_show, default=True),
                pystray.MenuItem("Выход", lambda icon, item: do_quit()),
            )
            icon = pystray.Icon(APP_TITLE, img, APP_TITLE, menu)
            state["tray"] = icon
            try:
                icon.run()
            except Exception:
                # Если трей не поднялся — сбрасываем, чтобы закрытие окна
                # снова означало выход (а не «запирало» пользователя без иконки).
                state["tray"] = None

        threading.Thread(target=run_tray, daemon=True).start()

    # Окно и колбэки отдаём одним методом: если присвоить их как обычные поля,
    # pywebview примет их за часть JS-API и полезет внутрь объекта окна.
    api._attach(window, on_quit=do_quit, on_speed=speed_cb)

    kwargs = {"debug": False}
    if _profile:
        kwargs.update(private_mode=False, storage_path=_profile)
    try:
        webview.start(**kwargs)
    except TypeError:
        webview.start(debug=False)


if __name__ == "__main__":
    main()
