# -*- coding: utf-8 -*-
"""MimiBox — точка входа (pywebview) c поддержкой системного трея."""

import os
import sys
import threading

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


def main():
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
