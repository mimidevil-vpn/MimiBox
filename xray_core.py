# -*- coding: utf-8 -*-
"""Генерация конфигурации Xray, маршрутизация и управление процессом ядра."""

import os
import re
import json
import time
import socket
import shutil
import threading
import subprocess

from storage import app_dir, data_dir

IS_WIN = os.name == "nt"
XRAY_NAME = "xray.exe" if IS_WIN else "xray"
API_PORT = 10853              # запасной порт статистики, если свободный не нашёлся


def free_port(default=API_PORT) -> int:
    """Свободный локальный порт для служебного API статистики.

    Раньше порт был жёстко зашит, и если от прошлого запуска оставалось висеть
    ядро, новое уже не поднималось — порт занят.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port
    except Exception:
        return default

_NO_WINDOW = 0x08000000 if IS_WIN else 0

# Домены первого уровня, которые в режиме «по правилам» идут напрямую.
RU_DOMAINS = ["domain:ru", "domain:su", "domain:xn--p1ai", "domain:by", "domain:kz"]


def find_xray(custom_path: str = "") -> str:
    """Ищет ядро: указанный путь -> рядом с exe -> ./core -> PATH."""
    candidates = []
    if custom_path:
        candidates.append(custom_path)
    candidates.append(os.path.join(app_dir(), XRAY_NAME))
    candidates.append(os.path.join(app_dir(), "core", XRAY_NAME))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    found = shutil.which(XRAY_NAME) or shutil.which("xray")
    return found or ""


def build_stream_settings(s) -> dict:
    ss = {"network": s.network or "tcp"}
    sec = s.security or "none"
    ss["security"] = sec

    if sec == "tls":
        tls = {
            "serverName": s.sni or s.host or s.address,
            "allowInsecure": bool(s.allow_insecure),
        }
        if s.fingerprint:
            tls["fingerprint"] = s.fingerprint
        if s.alpn:
            tls["alpn"] = [a for a in s.alpn.split(",") if a]
        ss["tlsSettings"] = tls
    elif sec == "reality":
        reality = {
            "serverName": s.sni or s.address,
            "publicKey": s.public_key,
            "shortId": s.short_id,
            "fingerprint": s.fingerprint or "chrome",
        }
        if getattr(s, "spider_x", ""):
            reality["spiderX"] = s.spider_x
        ss["realitySettings"] = reality

    net = s.network or "tcp"
    if net == "ws":
        headers = {"Host": s.host} if s.host else {}
        ss["wsSettings"] = {"path": s.path or "/", "headers": headers}
    elif net == "grpc":
        ss["grpcSettings"] = {"serviceName": s.path or ""}
    elif net in ("h2", "http"):
        hosts = [h for h in (s.host or s.sni or s.address).split(",") if h]
        ss["httpSettings"] = {"path": s.path or "/", "host": hosts}
    elif net == "httpupgrade":
        ss["httpupgradeSettings"] = {"path": s.path or "/", "host": s.host or ""}
    elif net in ("xhttp", "splithttp"):
        # XHTTP — основной транспорт современных панелей. Параметр extra приходит
        # в ссылке как JSON-строка; Xray ждёт именно объект, поэтому разбираем.
        ss["network"] = net
        xh = {"path": s.path or "/", "mode": s.mode or "auto"}
        if s.host:
            xh["host"] = s.host
        extra = (getattr(s, "extra", "") or "").strip()
        if extra:
            try:
                parsed = json.loads(extra)
                if isinstance(parsed, dict):
                    xh["extra"] = parsed
            except Exception:
                pass          # кривой extra не должен ронять всё подключение
        ss[("splithttp" if net == "splithttp" else "xhttp") + "Settings"] = xh
    return ss


def build_outbound(s) -> dict:
    stream = build_stream_settings(s)
    if s.protocol == "vless":
        user = {"id": s.uuid, "encryption": "none"}
        if s.flow:
            user["flow"] = s.flow
        out = {"protocol": "vless", "settings": {
            "vnext": [{"address": s.address, "port": s.port, "users": [user]}]}}
    elif s.protocol == "vmess":
        user = {"id": s.uuid, "alterId": s.alter_id, "security": "auto"}
        out = {"protocol": "vmess", "settings": {
            "vnext": [{"address": s.address, "port": s.port, "users": [user]}]}}
    elif s.protocol == "trojan":
        out = {"protocol": "trojan", "settings": {
            "servers": [{"address": s.address, "port": s.port, "password": s.password}]}}
    elif s.protocol == "shadowsocks":
        out = {"protocol": "shadowsocks", "settings": {
            "servers": [{"address": s.address, "port": s.port,
                         "method": s.method, "password": s.password}]}}
    else:
        raise ValueError(f"Неподдерживаемый протокол: {s.protocol}")

    out["tag"] = "proxy"
    out["streamSettings"] = stream
    return out


# ------------------------------------------------------------------ маршруты
_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?$")


def split_rules(entries) -> tuple:
    """Разбирает пользовательские исключения на списки доменов и IP.

    Понимает: example.com, *.example.com, https://example.com/page,
    1.2.3.4, 10.0.0.0/8, а также готовые geosite:/geoip:/regexp: правила.
    """
    domains, ips = [], []
    for raw in (entries or []):
        item = str(raw).strip().lower()
        if not item or item.startswith("#"):
            continue
        if item.startswith(("geosite:", "geoip:", "regexp:", "domain:", "full:", "ext:")):
            (ips if item.startswith("geoip:") else domains).append(item)
            continue
        # вырезаем схему и ведущую звёздочку
        item = re.sub(r"^[a-z0-9+.-]+://", "", item).strip()
        # подсеть проверяем до обрезки пути, иначе от 10.0.0.0/8 останется 10.0.0.0
        if _IP_RE.match(item):
            ips.append(item)
            continue
        item = item.split("/")[0].split("?")[0]
        item = item.lstrip("*.").strip(".").split(":")[0]
        if not item:
            continue
        if _IP_RE.match(item):
            ips.append(item)
        else:
            domains.append("domain:" + item)
    return domains, ips


# Служебные UDP-порты Windows, которые при поднятом туннеле начинают сыпаться в
# логи как «accepted udp:127.0.0.1:* accepted udp:192.168.123.255:137»:
# NetBIOS (137-139), mDNS (5353), SSDP (1900), DHCP (67/68). Это локальный сетевой
# шум, а не ошибки — туннелировать его нельзя (и не нужно), отправляем напрямую.
LAN_UDP_PORTS = [67, 68, 137, 138, 139, 5353, 1900]

# Xray в поле routing.port принимает число или строку вида "67,68,137-139",
# но НЕ массив — иначе «invalid port» и ядро не стартует вовсе.
LAN_UDP_PORTS_STR = ",".join(str(p) for p in LAN_UDP_PORTS)


def build_routing(mode: str, direct_entries=None, block_entries=None) -> dict:
    """mode: global (всё через VPN) | rules (RU и локальные напрямую) | direct."""
    rules = [
        # служебный порт статистики никуда не отправляем
        {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
        # локальная сеть и петля всегда напрямую, иначе отвалится доступ к роутеру
        {"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"},
        {"type": "field", "domain": ["domain:localhost"], "outboundTag": "direct"},
        # широковещательный UDP-шум Windows — напрямую, иначе он топит логи
        {"type": "field", "network": "udp", "port": LAN_UDP_PORTS_STR,
         "outboundTag": "direct"},
    ]

    b_domains, b_ips = split_rules(block_entries)
    if b_domains:
        rules.append({"type": "field", "domain": b_domains, "outboundTag": "block"})
    if b_ips:
        rules.append({"type": "field", "ip": b_ips, "outboundTag": "block"})

    # пользовательские исключения — сильнее любых общих правил ниже
    d_domains, d_ips = split_rules(direct_entries)
    if d_domains:
        rules.append({"type": "field", "domain": d_domains, "outboundTag": "direct"})
    if d_ips:
        rules.append({"type": "field", "ip": d_ips, "outboundTag": "direct"})

    if mode == "direct":
        rules.append({"type": "field", "network": "tcp,udp", "outboundTag": "direct"})
    elif mode == "rules":
        rules.append({"type": "field", "domain": RU_DOMAINS, "outboundTag": "direct"})
        rules.append({"type": "field", "ip": ["geoip:ru"], "outboundTag": "direct"})

    # AsIs — сопоставляем по имени домена и НЕ резолвим его самостоятельно.
    # С IPIfNonMatch ядро спрашивает системный DNS о каждом домене, и если в
    # системе живёт другой VPN с fake-ip (Clash/Mihomo отдают 198.18.x.x), адрес
    # попадает под «локальная сеть» и трафик уходит мимо VPN. Резолв включаем
    # только там, где без него не работают правила по странам.
    strategy = "IPIfNonMatch" if mode == "rules" else "AsIs"
    return {"domainStrategy": strategy, "rules": rules}


def build_config(s, socks_port: int, http_port: int, mode="global",
                 direct_entries=None, block_entries=None, api_port=API_PORT) -> dict:
    # ip нужен для UDP-ассоциации SOCKS5 — без него tun2socks не прокачает UDP,
    # а это игры, звонки и QUIC-трафик браузеров.
    socks_settings = {"udp": True, "ip": "127.0.0.1"}

    return {
        "log": {"loglevel": "warning"},
        # счётчики трафика — из них считается скорость в окне и в трее
        "stats": {},
        "api": {"tag": "api", "services": ["StatsService"]},
        "policy": {
            "system": {
                "statsOutboundUplink": True,
                "statsOutboundDownlink": True,
            }
        },
        "inbounds": [
            {"tag": "socks", "listen": "127.0.0.1", "port": socks_port,
             "protocol": "socks", "settings": socks_settings,
             "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]}},
            {"tag": "http", "listen": "127.0.0.1", "port": http_port,
             "protocol": "http", "settings": {},
             "sniffing": {"enabled": True, "destOverride": ["http", "tls"]}},
            {"tag": "api", "listen": "127.0.0.1", "port": api_port,
             "protocol": "dokodemo-door", "settings": {"address": "127.0.0.1"}},
        ],
        "outbounds": [
            build_outbound(s),
            {"tag": "direct", "protocol": "freedom", "settings": {}},
            {"tag": "block", "protocol": "blackhole", "settings": {}},
        ],
        "routing": build_routing(mode, direct_entries, block_entries),
    }


def kill_orphans(exe_path: str = "") -> int:
    """Убивает ядра, оставшиеся от прошлого запуска приложения.

    Если приложение завершили жёстко (диспетчер задач, сбой, перезапуск с правами
    администратора), дочерний xray.exe продолжает жить и держать порты — новое
    ядро после этого не стартует вовсе. Бьём строго по своему пути, чужие
    установки Xray не трогаем.
    """
    if not IS_WIN:
        return 0
    exe = exe_path or find_xray()
    if not exe:
        return 0
    # Безопасная передача пути в PowerShell: используем -EncodedCommand с UTF-16LE
    import base64 as _b64
    safe_path = exe.replace("'", "''")
    script = (
        "$p='%s';"
        "$k=Get-CimInstance Win32_Process -Filter \"Name='xray.exe'\" -ErrorAction SilentlyContinue |"
        " Where-Object { $_.ExecutablePath -eq $p -and $_.ProcessId -ne $PID };"
        "$n=0; foreach($x in $k){ try{ Stop-Process -Id $x.ProcessId -Force -ErrorAction Stop; $n++ }catch{} };"
        "$n" % safe_path
    )
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-ExecutionPolicy", "Bypass", "-Command", script],
                           startupinfo=si, creationflags=_NO_WINDOW,
                           capture_output=True, text=True, errors="ignore", timeout=20)
        return int((p.stdout or "0").strip() or 0)
    except Exception:
        return 0


def tcp_ping(host: str, port, timeout: float = 2.5) -> int:
    """Задержка TCP-хендшейка в мс, либо -1 при ошибке."""
    try:
        start = time.perf_counter()
        with socket.create_connection((host, int(port)), timeout=timeout):
            return int((time.perf_counter() - start) * 1000)
    except Exception:
        return -1


class XrayManager:
    def __init__(self):
        self.proc = None
        self.exe = ""
        self._log_thread = None
        self._last_lines = []      # хвост вывода ядра — для текста ошибки
        self._ll_lock = threading.Lock()
        self.api_port = API_PORT

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, server, socks_port, http_port, xray_path="", on_log=None,
              mode="global", direct_entries=None, block_entries=None,
              high_priority=False):
        """Запускает ядро. Бросает исключение, если xray не найден."""
        self.stop()
        exe = find_xray(xray_path)
        if not exe:
            raise FileNotFoundError(
                "Не найден xray.exe. Скачайте Xray-core и положите xray.exe "
                "рядом с приложением (или укажите путь в настройках)."
            )
        self.exe = exe
        self.api_port = free_port()

        cfg = build_config(server, socks_port, http_port, mode,
                           direct_entries, block_entries, self.api_port)
        cfg_path = os.path.join(data_dir(), "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

        kwargs = {}
        if IS_WIN:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kwargs["startupinfo"] = si
            flags = _NO_WINDOW
            if high_priority:
                flags |= 0x00000080      # HIGH_PRIORITY_CLASS
            kwargs["creationflags"] = flags

        # Go-сборщик мусора упирается в память ядра неохотно: GOMEMLIMIT
        # заставляет его держать heap под лимитом (иначе RSS растёт до
        # пиковых значений и отдаётся ОС только когда упрётся в потолок).
        kwargs["env"] = dict(os.environ)
        kwargs["env"]["GOMEMLIMIT"] = "300MiB"
        kwargs["env"]["GOGC"] = "80"

        self.proc = subprocess.Popen(
            [exe, "run", "-c", cfg_path],
            cwd=os.path.dirname(exe) or None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            **kwargs,
        )

        self._last_lines = []
        self._log_thread = threading.Thread(
            target=self._pump_logs, args=(self.proc, on_log), daemon=True
        )
        self._log_thread.start()

        # Ядро падает мгновенно, если порт уже занят (например, остался висеть
        # прошлый экземпляр). Без этой проверки приложение бодро рапортовало
        # «Подключено», а трафик никуда не шёл.
        for _ in range(20):
            if self.proc.poll() is not None:
                break
            time.sleep(0.05)
        if self.proc.poll() is not None:
            with self._ll_lock:
                tail = " ".join(self._last_lines[-4:]).strip()
            self.proc = None
            raise RuntimeError(
                "Ядро не запустилось. " + (tail[:300] if tail else
                "Возможно, порты %d/%d уже заняты другой программой."
                % (socks_port, http_port)))
        return exe

    # ------------------------------------------------------------ статистика
    def traffic(self) -> tuple:
        """Байты (вверх, вниз), пришедшие с прошлого опроса.

        Запрашиваем счётчики с флагом reset — Xray отдаёт значение и обнуляет
        его, поэтому полученное число уже является приростом за интервал.
        """
        if not self.is_running() or not self.exe:
            return 0, 0
        try:
            si = None
            if IS_WIN:
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            p = subprocess.run(
                [self.exe, "api", "statsquery", "--server=127.0.0.1:%d" % self.api_port,
                 "-reset", "outbound>>>proxy>>>traffic"],
                startupinfo=si, creationflags=_NO_WINDOW,
                capture_output=True, text=True, errors="ignore", timeout=4,
            )
            if p.returncode != 0 or not p.stdout.strip():
                return 0, 0
            data = json.loads(p.stdout)
            up = down = 0
            for st in data.get("stat", []) or []:
                name = st.get("name", "")
                val = int(st.get("value", 0) or 0)
                if name.endswith("uplink"):
                    up += val
                elif name.endswith("downlink"):
                    down += val
            return up, down
        except Exception:
            return 0, 0

    def _pump_logs(self, proc, on_log):
        try:
            for line in iter(proc.stdout.readline, ""):
                line = (line or "").rstrip()
                if not line:
                    continue
                with self._ll_lock:
                    self._last_lines.append(line)
                    del self._last_lines[:-10]
                if on_log:
                    on_log(line)
        except Exception:
            pass

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except Exception:
                    self.proc.kill()
            except Exception:
                pass
            self.proc = None
