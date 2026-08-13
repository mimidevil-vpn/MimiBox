# -*- coding: utf-8 -*-
"""Перехват завершения сеанса Windows (выключение / перезагрузка / выход).

Когда VPN включён, системный прокси указывает на локальный порт приложения.
Если Windows завершает сеанс (перезагрузка, выключение, выход из системы),
процесс приложения убивается, не успев снять прокси — после входа в систему
браузеры ходят в мёртвый порт и «интернет не работает» («ошибка прокси»).

В winforms-бэкенде pywebview закрытие окна при завершении сеанса снова
прячется в трей (on_closing возвращает False), поэтому на путь закрытия окна
полагаться нельзя. Вместо этого отдельный скрытый top-level-экземпляр со своим
циклом сообщений ловит WM_QUERYENDSESSION/WM_ENDSESSION и ДО завершения сеанса
вызывает колбэк восстановления исходных настроек прокси. Окно создаётся на
своём потоке, чтобы не зависеть от цикла сообщений UI.
"""

import ctypes
import threading
from ctypes import wintypes

_WNDPROC = None            # живая ссылка на WndProc — иначе GC сломает окно
_CB_END = lambda: None     # сеанс завершается — восстановить исходный прокси
_CB_CANCEL = lambda: None  # сеанс отменили — вернуть наш прокси, если нужен
_ENDING = False            # завершается ли сеанс прямо сейчас

WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
_CLASS_NAME = "MimiBoxShutdownGuard"


def session_ending() -> bool:
    """True, если Windows прямо сейчас завершает сеанс."""
    return _ENDING


def start(on_end=None, on_cancel=None):
    """Запускает сторожевой поток со скрытым окном (идемпотентно по факту).

    on_end вызывается, когда сеанс завершается (выключение/перезагрузка/выход),
    on_cancel — когда завершение отменили (шутдаун не прошёл).
    """
    global _CB_END, _CB_CANCEL
    _CB_END = on_end or _CB_END
    _CB_CANCEL = on_cancel or _CB_CANCEL
    threading.Thread(target=_run, daemon=True, name="win-session-guard").start()


def _safe(cb):
    try:
        cb()
    except Exception:
        pass


def _run():
    """Создаёт скрытое окно и крутит собственный цикл сообщений."""
    global _WNDPROC, _ENDING
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

        WNDPROC = ctypes.WINFUNCTYPE(
            wintypes.LPARAM, wintypes.HWND, wintypes.UINT,
            wintypes.WPARAM, wintypes.LPARAM)

        @WNDPROC
        def _wndproc(hwnd, msg, wparam, lparam):
            global _ENDING
            try:
                if msg == WM_QUERYENDSESSION:
                    _ENDING = True
                    _safe(_CB_END)
                    return 1                    # TRUE: не мешаем завершению
                if msg == WM_ENDSESSION:
                    if wparam:
                        _ENDING = True
                        _safe(_CB_END)
                    else:
                        # шутдаун отменили — возвращаемся к нормальной работе
                        _ENDING = False
                        _safe(_CB_CANCEL)
                    return 0
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
            except Exception:
                return 1

        _WNDPROC = _wndproc

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", wintypes.HANDLE),
            ]

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = _wndproc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = _CLASS_NAME

        # Класс может уже быть зарегистрирован (повторный start в тестах) —
        # тогда просто используем его; при любой ошибке CreateWindowExW
        # вернёт 0, и поток молча завершится, не ломая приложение.
        try:
            user32.RegisterClassExW.restype = wintypes.ATOM
            user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
            user32.RegisterClassExW(ctypes.byref(wc))
        except Exception:
            pass

        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, wintypes.HWND, wintypes.HANDLE,
            wintypes.HINSTANCE, ctypes.c_void_p]
        hwnd = user32.CreateWindowExW(
            0x80,                              # WS_EX_TOOLWINDOW: вне Alt-Tab
            _CLASS_NAME, _CLASS_NAME,
            0,                                 # без видимых стилей — не показываем
            0, 0, 0, 0,
            None,                              # top-level, без владельца
            None,
            kernel32.GetModuleHandleW(None),
            None)
        if not hwnd:
            return

        user32.GetMessageW.restype = ctypes.c_int
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG),
                                       wintypes.HWND, wintypes.UINT,
                                       wintypes.UINT]
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DefWindowProcW.restype = wintypes.LPARAM
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    except Exception:
        pass
