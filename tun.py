# -*- coding: utf-8 -*-
"""Режим «Туннель» (TUN) — весь трафик системы уходит в Xray.

Схема работы:
    приложения → Wintun-адаптер → tun2socks → SOCKS Xray (127.0.0.1) → сервер

Что делает модуль:
  1. проверяет права администратора (без них Wintun-адаптер не создать);
  2. прокладывает обходной маршрут до IP прокси-сервера через физический шлюз —
     иначе трафик самого Xray ушёл бы обратно в туннель и получилась бы петля;
  3. запускает tun2socks, дожидается появления адаптера;
  4. выдаёт адаптеру адрес, шлюз и DNS, чтобы Windows отправляла в него всё.

Остановка возвращает всё как было: адаптер исчезает вместе с процессом
tun2socks (а с ним и его маршруты), обходные маршруты удаляем сами.
"""

import os
import time
import socket
import ctypes
import shutil
import threading
import subprocess

from storage import app_dir, data_dir, log

IS_WIN = os.name == "nt"
TUN2SOCKS_NAME = "tun2socks.exe" if IS_WIN else "tun2socks"

# Адресация внутри туннеля. Подсеть выбрана заведомо редкой, чтобы не пересечься
# с домашней сетью пользователя (192.168.0.x / 192.168.1.x встречаются постоянно).
TUN_ADDR = "192.168.123.1"
TUN_MASK = "255.255.255.0"
TUN_GATEWAY = "192.168.123.2"

_NO_WINDOW = 0x08000000


def is_admin() -> bool:
    """True, если процесс запущен с правами администратора."""
    if not IS_WIN:
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    """Перезапускает приложение через UAC. True — запрос принят, себя закрываем.

    Новому процессу передаём --relaunch: по этому флагу он подождёт, пока текущий
    отпустит мьютекс единственного экземпляра, вместо того чтобы сразу выйти.
    """
    if not IS_WIN:
        return False
    try:
        import sys
        if getattr(sys, "frozen", False):
            exe = sys.executable
            params = "--relaunch"
        else:
            exe = sys.executable
            params = " ".join(['"%s"' % a for a in sys.argv] + ["--relaunch"])
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params,
                                                 os.path.dirname(exe) or None, 1)
        return int(rc) > 32          # <=32 означает отказ или ошибку
    except Exception as e:
        log("[tun] не удалось запросить права администратора: %s" % e)
        return False


def find_tun2socks(custom_path: str = "") -> str:
    """Ищет tun2socks: указанный путь -> рядом с приложением -> ./core -> данные -> PATH."""
    candidates = []
    if custom_path:
        candidates.append(custom_path)
    candidates.append(os.path.join(app_dir(), TUN2SOCKS_NAME))
    candidates.append(os.path.join(app_dir(), "core", TUN2SOCKS_NAME))
    candidates.append(os.path.join(data_dir(), TUN2SOCKS_NAME))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return shutil.which(TUN2SOCKS_NAME) or ""


def _run(args, timeout=15):
    """Тихо выполняет консольную команду, возвращает (код, вывод)."""
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        p = subprocess.run(args, startupinfo=si, creationflags=_NO_WINDOW,
                           capture_output=True, text=True, errors="ignore",
                           timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return -1, str(e)


def _powershell(script: str, timeout=15):
    code, out = _run(["powershell", "-NoProfile", "-NonInteractive",
                      "-ExecutionPolicy", "Bypass", "-Command", script], timeout)
    return out.strip() if code == 0 else ""


def default_route() -> tuple:
    """(шлюз, индекс интерфейса) активного подключения — до поднятия туннеля."""
    out = _powershell(
        "$r = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |"
        " Where-Object { $_.NextHop -ne '0.0.0.0' } |"
        " Sort-Object -Property RouteMetric,ifMetric | Select-Object -First 1;"
        " if ($r) { $r.NextHop + ' ' + $r.ifIndex }")
    parts = out.split()
    if len(parts) == 2:
        return parts[0], parts[1]

    # запасной путь: узнаём свой исходящий адрес и берём шлюз через route print
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 53))
        local_ip = s.getsockname()[0]
        s.close()
        _, table = _run(["route", "print", "-4"])
        for line in table.splitlines():
            f = line.split()
            if len(f) >= 5 and f[0] == "0.0.0.0" and f[3] == local_ip:
                return f[2], ""
    except Exception:
        pass
    return "", ""


def resolve_ips(host: str) -> list:
    """Все IPv4-адреса сервера — для них проложим обход мимо туннеля."""
    if not host:
        return []
    try:
        socket.inet_aton(host)
        return [host]                 # это уже IP
    except Exception:
        pass
    ips = []
    try:
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except Exception as e:
        log(f"[tun] не удалось определить IP сервера {host}: {e}")
    return ips


def _wintun_adapter(timeout=10.0, proc=None) -> str:
    """Ждёт, пока tun2socks создаст Wintun-адаптер, и возвращает его имя.

    Если процесс успел завершиться (например, не понял аргументы) — выходим
    сразу, а не ждём весь таймаут: иначе интерфейс замирает на десяток секунд.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return ""
        name = _powershell(
            "Get-NetAdapter -ErrorAction SilentlyContinue |"
            " Where-Object { $_.InterfaceDescription -like '*Wintun*' -or"
            " $_.Name -like '*wintun*' } |"
            " Select-Object -First 1 -ExpandProperty Name")
        if name:
            return name.splitlines()[0].strip()
        time.sleep(0.6)
    return ""


class TunManager:
    """Жизненный цикл tun2socks + маршрутов. Все операции идемпотентны."""

    def __init__(self):
        self.proc = None
        self.adapter = ""
        self._routes = []        # обходные маршруты, которые мы добавили
        self._log_thread = None
        self._last_lines = []    # хвост вывода tun2socks — для текста ошибки
        self._lock = threading.Lock()

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # ------------------------------------------------------------- запуск
    def start(self, socks_port: int, server_host: str, dns="1.1.1.1",
              tun2socks_path="", on_log=None):
        """Поднимает туннель. Бросает исключение с понятным текстом при сбое."""
        with self._lock:
            if self.is_running():
                return self.adapter
            if not IS_WIN:
                raise RuntimeError("Режим туннеля доступен только в Windows.")
            if not is_admin():
                raise PermissionError(
                    "Для режима «Туннель» нужны права администратора. "
                    "Перезапустите приложение от имени администратора.")

            exe = find_tun2socks(tun2socks_path)
            if not exe:
                raise FileNotFoundError(
                    "Не найден tun2socks.exe — положите его рядом с приложением.")

            # 1) обход для трафика самого Xray, иначе получится петля
            self._add_bypass_routes(server_host, on_log)

            # 2) сам tun2socks
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            # Флаги строго с двумя дефисами: tun2socks на pflag, и «-loglevel»
            # он разбирает как набор коротких флагов и просто печатает справку.
            self.proc = subprocess.Popen(
                [exe,
                 "--device", "wintun",
                 "--proxy", "socks5://127.0.0.1:%d" % int(socks_port),
                 # warn, а не info: на info tun2socks пишет строку на КАЖДОЕ
                 # соединение — это тысячи строк в минуту впустую. Ошибки старта
                 # приходят уровнями error/fatal, их мы по-прежнему видим.
                 "--loglevel", "warn",
                 # Ограничение памяти gVisor-стека: по умолчанию TCP-буферы
                 # растут до 4 МБ на соединение (автонастройка), из-за чего
                 # tun2socks съедает сотни МБ. Фиксируем разумный размер —
                 # это и память, и троттлинг производительности.
                 # ВНИМАНИЕ: bool-флаг pflag принимает значение ТОЛЬКО через
                 # «=» в одном аргументе; два отдельных аргумента включают
                 # флаг и оставляют «false» позиционным аргументом.
                 "--tcp-auto-tuning=false",
                 "--tcp-rcvbuf", "128KiB",
                 "--tcp-sndbuf", "128KiB"],
                cwd=os.path.dirname(exe) or None,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="ignore",
                startupinfo=si, creationflags=_NO_WINDOW,
                env=dict(os.environ, GOMEMLIMIT="300MiB", GOGC="80"),
            )
            self._last_lines = []
            self._log_thread = threading.Thread(
                target=self._pump_logs, args=(self.proc, on_log), daemon=True)
            self._log_thread.start()

            # 3) ждём адаптер и настраиваем его
            self.adapter = _wintun_adapter(timeout=10.0, proc=self.proc)
            if not self.adapter:
                with self._lock:
                    tail = " ".join(self._last_lines[-3:]).strip()
                self.stop()
                raise RuntimeError(
                    "Туннель не поднялся. " + (tail[:300] if tail else
                    "Проверьте, что wintun.dll лежит рядом с tun2socks.exe "
                    "и что антивирус его не блокирует."))

            self._configure_adapter(self.adapter, dns, on_log)
            if on_log:
                on_log(f"[tun] туннель поднят на адаптере «{self.adapter}»")
            return self.adapter

    def _add_bypass_routes(self, server_host, on_log=None):
        gw, ifidx = default_route()
        if not gw:
            log("[tun] не удалось определить шлюз — обходные маршруты не добавлены")
            if on_log:
                on_log("[tun] внимание: шлюз по умолчанию не найден")
            return
        for ip in resolve_ips(server_host):
            args = ["route", "add", ip, "mask", "255.255.255.255", gw, "metric", "5"]
            if ifidx:
                args += ["if", ifidx]
            code, out = _run(args)
            if code == 0:
                self._routes.append(ip)
                if on_log:
                    on_log(f"[tun] обход для {ip} через {gw}")
            else:
                log(f"[tun] не удалось добавить маршрут для {ip}: {out.strip()}")

    def _configure_adapter(self, adapter, dns, on_log=None):
        # Безопасная валидация DNS — только IP-адрес
        import re
        dns = (dns or "").strip()
        if not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", dns):
            dns = "1.1.1.1"

        # адрес + шлюз внутри туннеля: так Windows ставит маршрут по умолчанию сюда
        code, out = _run(["netsh", "interface", "ipv4", "set", "address",
                          f"name={adapter}", "source=static",
                          f"addr={TUN_ADDR}", f"mask={TUN_MASK}",
                          f"gateway={TUN_GATEWAY}", "gwmetric=1"])
        if code != 0:
            log(f"[tun] netsh set address: {out.strip()}")

        # низкая метрика — чтобы туннель выигрывал у физического подключения
        _run(["netsh", "interface", "ipv4", "set", "interface",
              f"interface={adapter}", "metric=1"])

        if dns:
            code, out = _run(["netsh", "interface", "ipv4", "set", "dnsservers",
                              f"name={adapter}", "static", str(dns), "primary"])
            if code != 0:
                log(f"[tun] netsh set dnsservers: {out.strip()}")
        _run(["ipconfig", "/flushdns"], timeout=10)
        if on_log:
            on_log(f"[tun] адрес {TUN_ADDR}, шлюз {TUN_GATEWAY}, DNS {dns}")

    def _pump_logs(self, proc, on_log):
        try:
            for line in iter(proc.stdout.readline, ""):
                line = (line or "").rstrip()
                if not line:
                    continue
                with self._lock:
                    self._last_lines.append(line)
                    del self._last_lines[:-8]
                if on_log:
                    on_log("[tun] " + line)
        except Exception:
            pass

    # ------------------------------------------------------------ остановка
    def stop(self):
        with self._lock:
            if self.proc is not None:
                try:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=4)
                    except Exception:
                        self.proc.kill()
                except Exception:
                    pass
                self.proc = None
            for ip in self._routes:
                _run(["route", "delete", ip])
            self._routes = []
            self.adapter = ""
            _run(["ipconfig", "/flushdns"], timeout=10)
