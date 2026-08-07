# -*- coding: utf-8 -*-
"""Включение/выключение системного прокси Windows (реестр + WinINet)."""

import os

IS_WIN = os.name == "nt"
_INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


def _refresh():
    """Сообщает Windows, что настройки прокси изменились."""
    try:
        import ctypes
        wininet = ctypes.windll.Wininet
        wininet.InternetSetOptionW(0, 39, 0, 0)  # SETTINGS_CHANGED
        wininet.InternetSetOptionW(0, 37, 0, 0)  # REFRESH
    except Exception:
        pass


def set_proxy(host_port: str):
    """host_port вида '127.0.0.1:10809' (HTTP inbound Xray)."""
    if not IS_WIN:
        return False, "Системный прокси доступен только в Windows."
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS,
                             0, winreg.KEY_WRITE)
        try:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, host_port)
            winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ,
                              "localhost;127.*;10.*;172.16.*;192.168.*;<local>")
        finally:
            winreg.CloseKey(key)
        _refresh()
        return True, ""
    except Exception as e:
        return False, str(e)


def disable_proxy():
    if not IS_WIN:
        return False, "Системный прокси доступен только в Windows."
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS,
                             0, winreg.KEY_WRITE)
        try:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        finally:
            winreg.CloseKey(key)
        _refresh()
        return True, ""
    except Exception as e:
        return False, str(e)


def _port_bound(host, port):
    """True, если адрес host:port уже занят (наши inbound-порты слушаются).

    Попытка подключиться ненадёжна: к живому сокету с полным/нулевым backlog
    connect может честно истечь по таймауту. А bind() либо проходит (порт
    свободен), либо даёт EADDRINUSE — детерминированно.
    """
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((host, port))
        finally:
            s.close()
        return False
    except OSError as e:
        # Windows: WSAEADDRINUSE = 10048, *nix: EADDRINUSE = 98
        return getattr(e, "winerror", None) == 10048 or getattr(e, "errno", None) in (98, 10048)
    except Exception:
        return False


def cleanup_stale_proxy(own_ports=()):
    """Снимает «зависший» системный прокси после жёсткой перезагрузки.

    Если ПК выключили или перезагрузили, не отключив VPN, в реестре остаётся
    включённый прокси на наш inbound-порт, а локального слушателя нет — сайты
    не открываются, пока пользователь сам не снимет галочку в настройках сети.
    Такой прокси убираем автоматически при старте приложения.

    Чужой прокси (не на 127.0.0.1/localhost и не на наш порт) и живое
    подключение (порт реально слушается, например вторая копия приложения)
    не трогаем. Возвращает (True, "") если сняли, (False, "") если делать
    было нечего, (False, err) при ошибке.
    """
    if not IS_WIN:
        return False, "Системный прокси доступен только в Windows."
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS,
                             0, winreg.KEY_READ)
        try:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        except OSError:
            enable, server = 0, ""
        finally:
            winreg.CloseKey(key)
    except Exception as e:
        return False, str(e)

    if not enable:
        return False, ""
    server = (server or "").strip().lower()
    if "127.0.0.1" not in server and "localhost" not in server:
        return False, ""                       # прокси не наш — не лезем
    if own_ports and not any(":%d" % p in server for p in own_ports):
        return False, ""                       # не наш порт — не лезем
    # Наш порт реально слушается (живой Xray, вторая копия) — не трогаем.
    for p in own_ports:
        if _port_bound("127.0.0.1", p):
            return False, ""
    return disable_proxy()
