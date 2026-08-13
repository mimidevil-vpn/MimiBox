# -*- coding: utf-8 -*-
"""Включение/выключение системного прокси Windows (реестр + WinINet).

Перед включением своего прокси сохраняем текущие настройки (ProxyEnable /
ProxyServer / ProxyOverride) в отдельный ключ реестра, а при выключении
восстанавливаем их. Так «свои» настройки пользователя (например, прокси другой
программы или свои исключения) не теряются после сеанса MimiBox.
"""

import os

IS_WIN = os.name == "nt"
_INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
# Здесь храним настройки прокси, какими они были ДО нашего вмешательства.
# Ключ переживает перезагрузки и сбои, поэтому «зависший» прокси от прошлого
# запуска можно снять, вернув исходные значения, а не просто обнулив галочку.
_PROXY_BACKUP_KEY = r"Software\MimiBox\ProxyBackup"


def _refresh():
    """Сообщает Windows, что настройки прокси изменились."""
    try:
        import ctypes
        wininet = ctypes.windll.Wininet
        wininet.InternetSetOptionW(0, 39, 0, 0)  # SETTINGS_CHANGED
        wininet.InternetSetOptionW(0, 37, 0, 0)  # REFRESH
    except Exception:
        pass


def _read_proxy():
    """(enable, server, override) текущих настроек системного прокси."""
    import winreg
    enable, server, override = 0, "", ""
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS,
                         0, winreg.KEY_READ)
    try:
        try:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
        except OSError:
            enable = 0
        try:
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        except OSError:
            server = ""
        try:
            override, _ = winreg.QueryValueEx(key, "ProxyOverride")
        except OSError:
            override = ""
    finally:
        winreg.CloseKey(key)
    return int(enable), str(server or ""), str(override or "")


def _write_proxy(enable, server, override):
    import winreg
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS,
                         0, winreg.KEY_WRITE)
    try:
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, int(enable))
        if server is not None:
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, str(server))
        if override is not None:
            winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ,
                              str(override))
    finally:
        winreg.CloseKey(key)


def _backup_exists():
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PROXY_BACKUP_KEY,
                             0, winreg.KEY_READ)
        winreg.CloseKey(key)
        return True
    except OSError:
        return False


def _save_backup():
    """Сохраняет текущие настройки прокси как исходные, если ещё не сохраняли."""
    if _backup_exists():
        return
    import winreg
    try:
        enable, server, override = _read_proxy()
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _PROXY_BACKUP_KEY,
                                 0, winreg.KEY_WRITE)
        try:
            winreg.SetValueEx(key, "Enable", 0, winreg.REG_DWORD, int(enable))
            winreg.SetValueEx(key, "Server", 0, winreg.REG_SZ, server)
            winreg.SetValueEx(key, "Override", 0, winreg.REG_SZ, override)
        finally:
            winreg.CloseKey(key)
    except Exception:
        pass


def _restore_backup():
    """Восстанавливает исходные настройки прокси и удаляет резервную копию.

    True, если резервная копия была и её восстановили.
    """
    if not _backup_exists():
        return False
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PROXY_BACKUP_KEY,
                             0, winreg.KEY_READ)
        try:
            enable, _ = winreg.QueryValueEx(key, "Enable")
            server, _ = winreg.QueryValueEx(key, "Server")
            override, _ = winreg.QueryValueEx(key, "Override")
        finally:
            winreg.CloseKey(key)
        _write_proxy(enable, server, override)
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, _PROXY_BACKUP_KEY)
        except OSError:
            pass
        return True
    except Exception:
        return False


def port_listening(host, port, timeout=0.4):
    """True, если на host:port реально принимаются соединения (порт готов)."""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return True
        except Exception:
            return False
        finally:
            s.close()
    except Exception:
        return False


def set_proxy(host_port: str):
    """host_port вида '127.0.0.1:10809' (HTTP inbound Xray)."""
    if not IS_WIN:
        return False, "Системный прокси доступен только в Windows."
    try:
        import winreg
        enable, server, _ = _read_proxy()
        # Если прокси уже наш (повторное включение, «зависший» от прошлого
        # запуска) — исходные настройки не перезаписываем.
        if not (enable and (server or "").strip().lower() == host_port.lower()):
            _save_backup()
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
        # Сначала пробуем вернуть исходные настройки (резервная копия).
        # Только если копии нет — просто снимаем галочку «использовать прокси».
        if _restore_backup():
            _refresh()
            return True, ""
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
    Такой прокси убираем автоматически при старте приложения, восстанавливая
    исходные настройки (если их успели сохранить).

    Чужой прокси (не на 127.0.0.1/localhost и не на наш порт) и живое
    подключение (порт реально слушается, например вторая копия приложения)
    не трогаем. Возвращает (True, "") если сняли, (False, "") если делать
    было нечего, (False, err) при ошибке.
    """
    if not IS_WIN:
        return False, "Системный прокси доступен только в Windows."
    try:
        enable, server, _ = _read_proxy()
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
