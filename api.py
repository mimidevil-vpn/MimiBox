# -*- coding: utf-8 -*-
"""API, доступный из JS через window.pywebview.api.*

Важно про имена: всё, что не начинается с подчёркивания, pywebview прокидывает
в JavaScript. Поэтому окно, менеджеры ядра и колбэки живут под приватными
именами — иначе мост лезет внутрь объекта окна и генерация API ломается.
"""

import os
import json
import time
import queue
import random
import base64
import hashlib
import secrets
import threading

from parsing import parse_many, parse_subscription, fetch_subscription
from xray_core import XrayManager, tcp_ping, find_xray, kill_orphans
import win_proxy
import win_session
import storage
import tun
import tg_link
import plugins as plugins_mod
from tg_client import TgMessenger

# Эмодзи, которое подмигивает возле названия. Меняется раз в час.
EMOJI_POOL = ["🧊", "❄️", "🐧", "🌊", "⚡", "🛡️", "🚀", "🌙", "✨", "🔒",
              "🦈", "🐳", "🧭", "🎧", "🌌", "🔥", "🍀", "🎯", "💎", "🪐"]
EMOJI_PERIOD = 3600           # секунд между сменами


def _make_hwid() -> str:
    """Аппаратный ID для заголовка x-hwid.

    Формат панели: 10-64 символа [a-zA-Z0-9=-]. Строим из MachineGuid системы
    (стабилен между переустановками приложения, уникален для каждого ПК);
    если GUID недоступен — случайная строка.
    """
    seed = ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography") as key:
            seed = str(winreg.QueryValueEx(key, "MachineGuid")[0] or "")
    except Exception:
        pass
    if not seed:
        seed = secrets.token_hex(16)
    raw = base64.urlsafe_b64encode(
        hashlib.sha256(seed.encode("utf-8", "ignore")).digest()
    ).decode("ascii")
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789=-"
    hw = "".join(c for c in raw if c in allowed)[:44]
    return hw or "HW-MIMIBOX-1"


# Репозиторий для автообновлений: проверяем последний релиз через GitHub API
GITHUB_REPO = "mimidevil-vpn/MimiBox"
GITHUB_API_LATEST = "https://api.github.com/repos/%s/releases/latest" % GITHUB_REPO


class Api:
    # Версия приложения — build скрипты подставляют реальную из git describe
    # Для dev-режима (из исходников) — git describe в runtime
    BUILD_VERSION = "4.0.0"
    BUILD_DATE = "2026-08-04"

    def __init__(self):
        self._window = None
        self._on_speed_cb = None      # колбэк для трея (up_bps, down_bps)
        self._on_quit_cb = None       # колбэк выхода из приложения
        self.settings = storage.load_settings()
        self.servers = storage.load_servers()
        self.selected = 0 if self.servers else -1
        self._xray = XrayManager()
        self._tun = tun.TunManager()
        self.connected = False
        self._conflicts = []
        self._conflicts_lock = threading.Lock()
        # Сериализует connect/disconnect/авто-восстановление: одновременный
        # запуск ядра из двух потоков (пользователь + _auto_resume) недопустим.
        self._conn_lock = threading.Lock()
        self._shutdown_done = False
        self._resume_in_progress = False   # идёт ли авто-восстановление сейчас
        self._resume_started = False
        self._orphans_done = threading.Event()  # _cleanup_orphans завершился
        self._save_error = ""
        self._speed = (0, 0)
        # Кумулятивный трафик (байты): продолжаем с прошлых запусков
        self._total = [int(self.settings.get("traffic_up") or 0),
                       int(self.settings.get("traffic_down") or 0)]
        self._last_hint = ""
        self._stats_thread = None
        self._stop_stats = threading.Event()
        # ---- новости ----
        self._news_result = None    # последний успешный результат get_news
        self._news_ts = 0.0         # время последнего успешного получения
        self._news_fetching = False # идёт ли фоновый скан канала
        self._news_suppressed = False  # игровой режим: новости не тянем
        # ---- игровой режим ----
        self._game_frozen = False   # заморожены ли внутренние подсистемы

        # ---- мессенджер и плагины ----
        self._plugins = plugins_mod.PluginManager(
            folder=os.path.join(storage.data_dir(), "plugins"),
            is_enabled=self._plugin_is_enabled,
            set_enabled=self._plugin_set_enabled,
            log=storage.log,
        )
        self._plugins.set_ctx_builder(self._plugin_ctx)
        self._tg = TgMessenger(
            session_dir=storage.data_dir(),
            on_event=self._tg_event,
            log=storage.log,
            get_credentials=self._tg_creds,
        )
        self._tg.set_hooks(on_before_send=self._tg_before_send,
                           on_incoming=self._tg_incoming)
        threading.Thread(target=self._plugins.reload_all, daemon=True).start()
        self._tg.start()

        # Все вызовы в JS идут через одну очередь и один поток. Раньше логи ядра,
        # статистика и пинги дёргали evaluate_js параллельно — WebView2 от этого
        # намертво вставал, потому что каждый вызов ждёт ответа от UI-потока.
        self._js_queue = queue.Queue(maxsize=500)
        self._js_thread = threading.Thread(target=self._js_pump, daemon=True)
        self._js_thread.start()

        # локальный идентификатор профиля (создаётся один раз)
        if not self.settings.get("local_id"):
            self.settings["local_id"] = "LDK-" + "".join(
                random.choice("0123456789ABCDEF") for _ in range(4))
            self._save()
        # аппаратный ID для лимита устройств (x-hwid): создаётся один раз
        if not self.settings.get("hwid_value"):
            self.settings["hwid_value"] = _make_hwid()
            self._save()
        self._roll_emoji(force=False)
        self._log_environment()

        # поиск сторонних VPN не должен тормозить открытие окна
        threading.Thread(target=self._scan_conflicts, daemon=True).start()
        threading.Thread(target=self._cleanup_orphans, daemon=True).start()
        # VPN был включён до перезагрузки/выключения ПК — восстановим его после
        # открытия окна (см. _auto_resume). Флаг снимается в connect/disconnect.
        self._resume_wanted = bool(self.settings.get("was_connected", False)) \
            and bool(self.servers) and self.selected >= 0
        if self._resume_wanted:
            storage.log("[resume] VPN был включён до перезагрузки — "
                        "восстановлю после открытия окна")
        # После жёсткой перезагрузки в реестре остаётся включённый системный
        # прокси на наш inbound-порт, а Xray уже мёртв — сайты не открываются.
        # Снимаем такой «зависший» прокси сразу (исходные настройки вернутся из
        # резервной копии). При авто-восстановлении это же разблокирует интернет
        # на время, пока мы поднимаем ядро и ждём готовности порта.
        self._cleanup_stale_proxy()
        # Скрытое окно-сторож (win_session): когда Windows завершает сеанс
        # (выключение/перезагрузка/выход), восстанавливаем системный прокси
        # ДО того, как процесс будет убит. Иначе после входа в систему
        # браузеры ходят в мёртвый локальный порт.
        win_session.start(on_end=self._on_session_end,
                          on_cancel=self._on_session_cancel)
        # Зависшее ядро от прошлого запуска может держать порт: первый проход
        # _cleanup_stale_proxy() увидел живой порт и оставил прокси. Повторяем
        # проверку уже после того, как _cleanup_orphans прибил ядро.
        threading.Thread(target=self._cleanup_stale_after_orphans,
                         daemon=True).start()
        # фон: устаревшую подписку освежаем молча, не тормозя открытие окна
        threading.Thread(target=self._auto_refresh_subscription, daemon=True).start()
        # фон: проверяем, не вышла ли новая версия на GitHub (баннер в UI)
        threading.Thread(target=self._auto_check_update, daemon=True).start()
        # игровой режим: если остался включён с прошлого запуска — замораживаем
        # свои фоновые подсистемы сразу, как раньше делал сторожевой цикл
        self._apply_game_mode()
        # автозапуск: если включён в настройках — вернуть запись в реестр
        if self.settings.get("autostart"):
            apply_autostart(True)

    def _apply_game_mode(self):
        """Включает/выключает внутреннюю заморозку по текущей настройке.

        Игровой режим больше не трогает чужие процессы системы (раньше — tasklist
        + NtSuspendProcess по Chrome/Edge/Discord и т.п.). Вместо этого паузу
        получают СВОИ фоновые подсистемы приложения: мессенджер Telethon
        (отключается и сбрасывает кэши — это самый прожорливый по памяти кусок)
        и фоновые сканы новостей. Так освобождаются и RAM, и CPU для игры, не
        трогая остальную систему.
        """
        if self.settings.get("game_mode"):
            self._game_freeze()
        else:
            self._game_thaw()

    def _game_freeze(self):
        if self._game_frozen:
            return
        self._game_frozen = True
        self._news_suppressed = True
        frozen = []
        try:
            if self._tg.set_paused(True):
                frozen.append("messenger")
        except Exception:
            pass
        self._push("window.__pushGame(%s)"
                   % json.dumps(frozen, ensure_ascii=False))

    def _game_thaw(self):
        if not self._game_frozen:
            return
        self._game_frozen = False
        self._news_suppressed = False
        try:
            self._tg.set_paused(False)
        except Exception:
            pass
        self._push("window.__pushGame(%s)" % json.dumps([]))

    def _cleanup_orphans(self):
        killed = kill_orphans(find_xray(self.settings.get("xray_path", "")))
        if killed:
            storage.log("[env] снято зависших ядер от прошлого запуска: %d" % killed)
        self._orphans_done.set()

    def _cleanup_stale_proxy(self):
        """Снимает системный прокси, оставшийся от прошлого запуска.

        Сценарий: VPN включён, ПК выключают/перезагружают без «Отключить».
        shutdown() не выполняется, ProxyEnable=1 остаётся в реестре, Xray уже
        не слушает порт — и после входа в систему сайты не открываются.
        Если прокси включён именно на наш порт и порт никто не слушает —
        снимаем (галочка убирается автоматически).
        """
        hp = int(self.settings.get("http_port", 10809))
        sp = int(self.settings.get("socks_port", 10808))
        try:
            ok, err = win_proxy.cleanup_stale_proxy(own_ports=(hp, sp))
        except Exception as e:
            storage.log("[proxy] сбой очистки прокси: %s" % e)
            return
        if ok:
            storage.log("[proxy] снят зависший системный прокси после перезагрузки")
        elif err:
            storage.log("[proxy] очистка прокси: %s" % err)

    def _cleanup_stale_after_orphans(self):
        """Повторная проверка зависшего прокси после снятия ядер прошлого запуска.

        _cleanup_stale_proxy() в __init__ мог увидеть ещё живой порт старого
        xray и решить, что прокси рабочий. После kill_orphans() порт мёртв —
        снимаем прокси. Если к этому моменту соединение уже поднято и порт
        слушается, cleanup ничего не тронет.
        """
        self._orphans_done.wait(timeout=8.0)
        if self.connected:
            return
        self._cleanup_stale_proxy()

    def _on_session_end(self):
        """Windows завершает сеанс (выключение/перезагрузка/выход из системы).

        Соединение живо, а системный прокси указывает на наш локальный порт.
        Возвращаем исходные настройки прокси заранее, пока процесс ещё может
        писать в реестр, — иначе после входа в систему браузеры «не увидят
        интернет». Флаг was_connected НЕ снимаем: после загрузки системы
        приложение само восстановит VPN. Вызывается из потока win_session.
        """
        try:
            hp = int(self.settings.get("http_port", 10809))
            sp = int(self.settings.get("socks_port", 10808))
            ok, err = win_proxy.restore_if_ours((hp, sp))
            if ok:
                storage.log("[proxy] сеанс Windows завершается — "
                            "системный прокси восстановлен")
            elif err:
                storage.log("[proxy] завершение сеанса: %s" % err)
        except Exception as e:
            storage.log("[proxy] завершение сеанса: %s" % e)

    def _on_session_cancel(self):
        """Завершение сеанса отменили (шутдаун не прошёл).

        Мы уже вернули исходный прокси — если соединение живо и локальный порт
        слушается, включаем прокси обратно, чтобы VPN продолжил работать.
        """
        try:
            if not self.connected:
                return
            if not self.settings.get("system_proxy", True):
                return
            hp = int(self.settings.get("http_port", 10809))
            if win_proxy.port_listening("127.0.0.1", hp, timeout=0.4):
                win_proxy.set_proxy("127.0.0.1:%d" % hp)
                storage.log("[proxy] шутдаун отменён — системный прокси вернул")
        except Exception:
            pass

    def _auto_refresh_subscription(self):
        """При старте молча обновляем подписку, если серверов нет или данные
        устарели (старше 12 часов). Свежие данные и живые серверы не трогаем —
        чтобы не затирать вручную добавленные серверы. Ошибки не критичны."""
        url = (self.settings.get("subscription_url") or "").strip()
        if not url:
            return
        time.sleep(2.5 + random.random() * 2.0)   # даём окну открыться
        if self.connected:
            return
        updated = int(self.settings.get("sub_updated") or 0)
        stale = (time.time() - updated) > 12 * 3600
        if self.servers and not stale:
            return
        storage.log("[sub] авто-обновление подписки (%s)"
                    % ("нет серверов" if not self.servers else "данные устарели"))
        try:
            self.refresh_subscription()
        except Exception as e:
            storage.log("[sub] авто-обновление не удалось: %s" % e)

    def _auto_check_update(self):
        """Фоновая проверка обновлений на GitHub.

        Первый заход — через несколько секунд после открытия окна, дальше раз
        в несколько часов. Баннер показываем один раз на версию; пропущенную
        через «Скрыть» версию не трогаем; сетевые ошибки игнорируются.
        """
        time.sleep(8.0 + random.random() * 2.0)
        pushed = ""
        while True:
            try:
                r = self.check_update()
                if r.get("ok") and r.get("version") and r["version"] != pushed:
                    pushed = r["version"]
                    self._push("window.__pushUpdate(%s)"
                               % json.dumps(r, ensure_ascii=False))
            except Exception as e:
                storage.log("[update] проверка обновлений: %s" % e)
            time.sleep(6 * 3600)

    @staticmethod
    def _version_tuple(v):
        """"4.2.6", "v4.2.6" или "4.2.6-1-gXXX" -> (4, 2, 6)."""
        import re as _re
        m = _re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", (v or "").lstrip("v"))
        if not m:
            return (0, 0, 0)
        return tuple(int(m.group(i) or 0) for i in (1, 2, 3))

    @staticmethod
    def _version_newer(latest, current):
        return Api._version_tuple(latest) > Api._version_tuple(current)

    def check_update(self, force=False):
        """Проверяет на GitHub последний релиз и сравнивает с текущей версией.

        Возвращает {ok: True, version, date, notes, download_url, url, current},
        если вышла более новая версия; иначе {ok: False, current: ...}.
        force=True игнорирует «Скрыть эту версию» — для ручной проверки.
        """
        import urllib.request
        cur = self._resolve_version()
        try:
            req = urllib.request.Request(GITHUB_API_LATEST, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 MimiBox",
                "Accept": "application/vnd.github+json"})
            # обходим системный прокси: проверка не должна зависеть от VPN
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with op.open(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as e:
            storage.log("[update] GitHub недоступен: %s" % e)
            return {"ok": False, "current": cur, "error": str(e)}

        tag = (data.get("tag_name") or "").strip()
        if not tag:
            return {"ok": False, "current": cur}
        latest = tag.lstrip("v")
        if not self._version_newer(latest, cur):
            return {"ok": False, "current": cur}
        if not force:
            skipped = (self.settings.get("skip_update_version") or "").strip()
            if skipped and skipped == latest:
                return {"ok": False, "current": cur, "skipped": True}

        download_url = ""
        for a in data.get("assets") or []:
            name = (a.get("name") or "").lower()
            if name.endswith((".exe", ".msi")) and "setup" in name:
                download_url = a.get("browser_download_url") or ""
                break
        if not download_url:
            for a in data.get("assets") or []:
                if (a.get("name") or "").lower().endswith(".exe"):
                    download_url = a.get("browser_download_url") or ""
                    break

        return {
            "ok": True,
            "version": latest,
            "date": (data.get("published_at") or "")[:10],
            "notes": (data.get("body") or "").strip(),
            "download_url": download_url,
            "url": data.get("html_url") or "",
            "current": cur,
        }

    def _find_live_server(self, timeout=1.5, prefer=None):
        """Индекс самого быстрого доступного сервера из текущего списка, либо None.

        prefer — (protocol, network) транспорта, к которому нужно стремиться:
        сначала ищем живой с таким же транспортом (для Reality-конфига это почти
        наверняка рабочий), и только если таких нет — любой живой.
        """
        targets = [(i, s.address, s.port) for i, s in enumerate(self.servers)]
        if not targets:
            return None
        if prefer is None and 0 <= self.selected < len(self.servers):
            srv = self.servers[self.selected]
            prefer = (srv.protocol, srv.network)
        results = {}
        lock = threading.Lock()
        sem = threading.Semaphore(12)

        def one(idx, host, port):
            ms = tcp_ping(host, port, timeout=timeout)
            with lock:
                results[idx] = ms

        threads = [threading.Thread(target=one, args=t, daemon=True)
                   for t in targets]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        live = [i for i, ms in results.items() if ms is not None and ms >= 0]
        if not live:
            return None
        pool = live
        if prefer:
            same = [i for i in live
                    if (self.servers[i].protocol, self.servers[i].network) == prefer]
            if same:
                pool = same
        pool.sort(key=lambda i: results[i])
        return pool[0]

    # ------------------------------------------------- окно и колбэки
    def _attach(self, window, on_quit=None, on_speed=None):
        """Вызывает main.py после создания окна."""
        self._window = window
        self._on_quit_cb = on_quit
        self._on_speed_cb = on_speed
        # VPN был включён до перезагрузки — восстанавливаем его в фоне.
        if self._resume_wanted and not self._resume_started:
            self._resume_started = True
            threading.Thread(target=self._auto_resume, daemon=True).start()

    def _log_environment(self):
        """Пишем в app.log, где именно приложение нашло ядро и туннель.

        Когда у пользователя «не видит tun2socks», это первое, что нужно знать:
        куда установлено приложение и что оно там нашло."""
        storage.log("[env] папка приложения: %s" % storage.app_dir())
        storage.log("[env] папка данных:     %s" % storage.data_dir())
        storage.log("[env] xray:      %s" % (find_xray(self.settings.get("xray_path", ""))
                                             or "НЕ НАЙДЕН"))
        storage.log("[env] tun2socks: %s" % (tun.find_tun2socks() or "НЕ НАЙДЕН"))
        storage.log("[env] права администратора: %s" % ("да" if tun.is_admin() else "нет"))

    # ---------------------------------------------------- вспомогательное
    def _save(self):
        ok = storage.save_settings(self.settings)
        self._save_error = "" if ok else storage.last_error()
        return ok

    def _save_servers(self):
        ok = storage.save_servers(self.servers)
        self._save_error = "" if ok else storage.last_error()
        return ok

    def _roll_emoji(self, force=False):
        """Раз в час подбирает новое эмодзи возле названия."""
        now = int(time.time())
        ts = int(self.settings.get("emoji_ts", 0) or 0)
        if force or not self.settings.get("emoji") or (now - ts) >= EMOJI_PERIOD:
            pool = [e for e in EMOJI_POOL if e != self.settings.get("emoji")]
            self.settings["emoji"] = random.choice(pool)
            self.settings["emoji_ts"] = now
            self._save()
        return self.settings["emoji"]

    # ---------------------------------------------------- сериализация
    def _srv_json(self, s):
        return {
            "name": s.name or s.address,
            "protocol": s.protocol,
            "address": s.address,
            "port": s.port,
            "network": s.network,
        }

    def _sub_info(self):
        """Лимиты и срок действия подписки — то, что панель прислала в заголовке.

        total == 0 у провайдеров означает «безлимит», expire == 0 — «бессрочно»,
        поэтому такие поля отдаём как None, а не как нули.
        """
        raw = self.settings.get("sub_info") or {}
        used = int(raw.get("upload", 0) or 0) + int(raw.get("download", 0) or 0)
        total = int(raw.get("total", 0) or 0)
        expire = int(raw.get("expire", 0) or 0)
        now = int(time.time())
        return {
            "known": bool(raw),
            "title": raw.get("title", ""),
            "announce": raw.get("announce", ""),
            "support_url": raw.get("support_url", ""),
            "used": used,
            "total": total or None,
            "left": max(0, total - used) if total else None,
            "percent": min(100, round(used * 100.0 / total, 1)) if total else None,
            "expire": expire or None,
            "days_left": max(0, (expire - now) // 86400) if expire else None,
            "expired": bool(expire and expire <= now),
            "refill": raw.get("refill") or None,
            "updated": int(self.settings.get("sub_updated", 0) or 0),
            "url": self.settings.get("subscription_url", ""),
        }

    def _state(self):
        return {
            "servers": [self._srv_json(s) for s in self.servers],
            "selected": self.selected,
            "connected": self.connected,
            "settings": self.settings,
            "xray_found": bool(find_xray(self.settings.get("xray_path", ""))),
            "tun_found": bool(tun.find_tun2socks()),
            "is_admin": tun.is_admin(),
            "tun_active": self._tun.is_running(),
            "intro_done": bool(self.settings.get("intro_done", False)),
            "tutorial_done": bool(self.settings.get("tutorial_done", False)),
            "local_id": self.settings.get("local_id", ""),
            "rating": int(self.settings.get("rating", 0) or 0),
            "emoji": self.settings.get("emoji", ""),
            "background_image": self.settings.get("background_image", ""),
            "conflicts": list(self._conflicts),
            "sub": self._sub_info(),
            "tg": self._tg.status(),
            "ser": self._ser_json(),
            "save_error": self._save_error,
            "data_dir": storage.data_dir(),
            "speed": {"up": self._speed[0], "down": self._speed[1],
                      "total_up": self._total[0], "total_down": self._total[1]},
        }

    # ---------------------------------------------------- методы для JS
    def get_state(self):
        return self._state()

    def tick_emoji(self):
        """UI дёргает раз в минуту — эмодзи само сменится, когда придёт час."""
        return {"emoji": self._roll_emoji()}

    # ------------------------------------------------------------ серверы
    def add_links(self, text):
        parsed = parse_many(text or "")
        if not parsed:
            return {"added": 0, "state": self._state()}
        self.servers.extend(parsed)
        self._save_servers()
        if self.selected < 0 and self.servers:
            self.selected = 0
        self._mark_onboarded()
        return {"added": len(parsed), "state": self._state()}

    def import_subscription(self, url):
        url = storage.validate_url((url or "").strip())
        if not url:
            return {"error": "empty_url"}
        hwid = self.settings.get("hwid_value", "") if self.settings.get(
            "hwid_enabled", True) else ""
        try:
            content, info = fetch_subscription(url, hwid=hwid)
            parsed = parse_subscription(content)
        except Exception as e:
            err = str(e)
            if "html_response" in err:
                err = "html_response"
            elif "ssl" in err.lower() or "certificate" in err.lower():
                err = "ssl_error"
            storage.log("[sub] ошибка загрузки: %s" % err)
            return {"error": err}
        if not parsed:
            return {"error": "empty_sub"}
        self.servers = parsed
        self.selected = 0
        self.settings["subscription_url"] = url
        self.settings["sub_info"] = info or {}
        self.settings["sub_updated"] = int(time.time())
        self._save_servers()
        self._mark_onboarded()
        storage.log("[sub] загружено серверов: %d" % len(parsed))
        hwid_msg = self._hwid_warning(info)
        return {"added": len(parsed), "hwid_msg": hwid_msg, "state": self._state()}

    def _hwid_warning(self, info: dict):
        """Короткий код предупреждения панели про лимит устройств, либо None.

        info — данные из заголовков ответа подписки (x-hwid-*).
        """
        if not info:
            return None
        if info.get("hwid_max_devices") or info.get("hwid_limit"):
            return "max_devices"
        if info.get("hwid_not_supported"):
            return "not_supported"
        return None

    def refresh_subscription(self):
        """Перечитать сохранённую подписку — серверы у провайдера меняются."""
        url = (self.settings.get("subscription_url") or "").strip()
        if not url:
            return {"error": "empty_url"}
        return self.import_subscription(url)

    def delete_server(self, index):
        if 0 <= index < len(self.servers):
            del self.servers[index]
            self._save_servers()
            if self.selected >= len(self.servers):
                self.selected = len(self.servers) - 1
        return self._state()

    def delete_all_servers(self):
        self.servers.clear()
        self._save_servers()
        self.selected = -1
        return self._state()

    def select_server(self, index):
        if 0 <= index < len(self.servers):
            self.selected = index
        return self._state()

    def ping_all(self):
        targets = [(i, s.address, s.port) for i, s in enumerate(self.servers)]
        threading.Thread(target=self._ping_worker, args=(targets,), daemon=True).start()
        return {"ok": True}

    def _ping_worker(self, targets):
        """Пингуем пачками: сотня одновременных потоков только мешает друг другу."""
        lock = threading.Semaphore(12)

        def one(index, host, port):
            with lock:
                ms = tcp_ping(host, port)
            self._push("window.__pushPing(%d,%d)" % (index, ms))

        threads = [threading.Thread(target=one, args=t, daemon=True) for t in targets]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    # ------------------------------------------------------------ соединение
    def connect(self, index):
        if not (0 <= index < len(self.servers)):
            return {"error": "no_server"}
        want_tun = bool(self.settings.get("tun_mode", False))
        if want_tun and not tun.is_admin():
            return {"error": "need_admin"}

        with self._conn_lock:
            # Ручное подключение отменяет фоновое авто-восстановление.
            if not self._resume_in_progress:
                self._resume_wanted = False

            self.selected = index
            srv = self.servers[index]

            # Авто-фейловер: выбранный сервер недоступен — ищем живой. Живой отвечает
            # за миллисекунды, задержка появляется только на мёртвом (таймаут ping).
            if tcp_ping(srv.address, srv.port) < 0:
                self._log("[core] сервер %s:%s недоступен, ищу живой..."
                          % (srv.address, srv.port))
                live = self._find_live_server(prefer=(srv.protocol, srv.network))
                if live is None:
                    self._log("[core] все серверы недоступны — обновите подписку")
                    return {"error": "no_live_server"}
                self.selected = live
                srv = self.servers[live]
                self._log("[core] переключаюсь на %s:%s" % (srv.address, srv.port))
            sp = int(self.settings.get("socks_port", 10808))
            hp = int(self.settings.get("http_port", 10809))
            mode, direct_entries = self._xray_route()
            try:
                exe = self._xray.start(
                    srv, sp, hp, self.settings.get("xray_path", ""),
                    on_log=self._log,
                    mode=mode,
                    direct_entries=direct_entries,
                    block_entries=self.settings.get("block_sites", []),
                    high_priority=bool(self.settings.get("high_priority", False)),
                )
                self._log("[core] запущено ядро: %s" % exe)
                self._log("[core] сервер: %s (%s, %s)"
                          % (srv.name, srv.protocol, srv.network))
            except Exception as e:
                self._log("[error] %s" % e)
                return {"error": str(e)}

            # Системный прокси включаем ТОЛЬКО после того, как локальный
            # proxy-порт реально начал принимать соединения. Иначе трафик
            # пойдёт в мёртвый порт и интернет заблокируется.
            if not self._wait_local_port(hp):
                self._log("[error] локальный proxy-порт %d не поднялся — "
                          "подключение отменено, системный прокси не включаю" % hp)
                self._xray.stop()
                if self.settings.get("system_proxy", True):
                    win_proxy.disable_proxy()
                return {"error": "core_not_ready"}

            if self.settings.get("system_proxy", True):
                ok, err = win_proxy.set_proxy("127.0.0.1:%d" % hp)
                self._log("[proxy] системный прокси -> 127.0.0.1:%d" % hp if ok
                          else "[proxy] ошибка: %s" % err)

            if want_tun:
                if not self._wait_local_port(sp):
                    self._log("[error] socks-порт %d не поднялся — "
                              "туннель не запущен" % sp)
                    self._xray.stop()
                    if self.settings.get("system_proxy", True):
                        win_proxy.disable_proxy()
                    return {"error": "core_not_ready"}
                try:
                    self._tun.start(sp, srv.address,
                                    dns=self.settings.get("tun_dns", "1.1.1.1"),
                                    on_log=self._log)
                except Exception as e:
                    self._log("[tun] ошибка: %s" % e)
                    self._xray.stop()
                    if self.settings.get("system_proxy", True):
                        win_proxy.disable_proxy()
                    return {"error": str(e)}

            self.connected = True
            # Соединение активно — запоминаем, чтобы восстановить после
            # перезагрузки ПК. Снимается в disconnect()/shutdown().
            self.settings["was_connected"] = True
            self._save()
            self._start_stats()
            return {"ok": True, "state": self._state()}

    def disconnect(self):
        with self._conn_lock:
            self._resume_wanted = False
            self._stop_stats_loop()
            self._persist_traffic()
            self._tun.stop()
            self._xray.stop()
            if self.settings.get("system_proxy", True):
                win_proxy.disable_proxy()
            self.connected = False
            self._speed = (0, 0)
            if self.settings.get("was_connected"):
                self.settings["was_connected"] = False
                self._save()
        self._push_speed(0, 0)
        self._log("[core] отключено")
        return {"ok": True, "state": self._state()}

    # ------------------------------------------------- авто-восстановление
    def _wait_local_port(self, port, timeout=10.0):
        """True, когда на 127.0.0.1:port реально принимаются соединения.

        Проверяем коннектом, а не bind() — это точное «порт готов принимать
        трафик». Опрашиваем с короткой паузой, чтобы не поймать момент, когда
        слушатель уже создан, но ещё не в accept-цикле.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if win_proxy.port_listening("127.0.0.1", port, timeout=0.4):
                return True
            time.sleep(0.15)
        return False

    def _auto_resume(self):
        """Восстанавливает VPN после перезагрузки/выключения ПК.

        Порядок: поднимаем ядро, ЖДЁМ готовности локального proxy-порта и только
        потом включаем системный прокси. Если ядро не поднялось, сервер
        недоступен или порт так и не зазвучал — системный прокси остаётся
        выключенным (исходные настройки уже восстановлены на старте), интернет
        работает как обычно. Повторяем попытки, потому что сразу после входа в
        систему сеть может быть ещё не поднята.
        """
        try:
            time.sleep(1.5)              # даём окну и системе прийти в себя
            if not self._resume_wanted:
                return
            # Ждём, пока _cleanup_orphans убьёт ядро от прошлого запуска: иначе
            # он может прибить только что поднятый нами xray (тот же путь).
            self._orphans_done.wait(timeout=5.0)
            if not self._resume_wanted:
                return
            if not (0 <= self.selected < len(self.servers)):
                self._finish_resume_failed()
                return
            if bool(self.settings.get("tun_mode", False)) and not tun.is_admin():
                self._log("[resume] туннель требует прав администратора — "
                          "авто-восстановление пропущено")
                self._finish_resume_failed()
                return
            self._resume_in_progress = True
            try:
                deadline = time.time() + 90.0
                while time.time() < deadline:
                    if not self._resume_wanted:
                        return            # пользователь подключился/отключился сам
                    self._log("[resume] восстанавливаю соединение (попытка)...")
                    try:
                        r = self.connect(self.selected)
                    except Exception as e:
                        r = {"error": str(e)}
                    if r.get("error") is None:
                        self._log("[resume] соединение восстановлено")
                        self._push("window.__pushResume()")
                        return
                    self._log("[resume] не вышло: %s — повтор через 5 с"
                              % r.get("error"))
                    time.sleep(5)
                self._log("[resume] сервер недоступен — авто-восстановление отменено")
            finally:
                self._resume_in_progress = False
            self._finish_resume_failed()
        except Exception as e:
            storage.log("[resume] сбой авто-восстановления: %s" % e)
            self._finish_resume_failed()

    def _finish_resume_failed(self):
        """Сдаёмся в авто-восстановлении: без VPN и без нашего системного прокси."""
        self._resume_wanted = False
        try:
            if self.settings.get("was_connected"):
                self.settings["was_connected"] = False
                self._save()
        except Exception:
            pass
        try:
            self._xray.stop()
        except Exception:
            pass
        try:
            if self.settings.get("system_proxy", True):
                win_proxy.disable_proxy()
        except Exception:
            pass

    def _xray_route(self):
        """(mode, direct_entries) для ядра.

        При активном туннеле любой «direct»-outbound на публичный адрес
        уходит в default-маршрут туннеля, снова попадает в Xray через
        tun2socks и зацикливается, плодя сокеты и память. Поэтому туннель
        всегда работает в режиме global и без прямых исключений (они в
        туннеле физически невыполнимы). Private/локальные адреса не идут
        через default-маршрут — они остаются в петле безопасными.
        """
        if bool(self.settings.get("tun_mode", False)):
            return "global", []
        return (self.settings.get("route_mode", "global"),
                self.settings.get("direct_sites", []))

    # ------------------------------------------------------------ режимы
    def set_modes(self, patch):
        """Тумблеры «Прокси» и «Туннель» на главном экране.

        Переключаются на лету: если соединение уже поднято, применяем сразу,
        не разрывая его.
        """
        patch = patch or {}
        want_proxy = bool(patch.get("proxy", self.settings.get("system_proxy", True)))
        want_tun = bool(patch.get("tun", self.settings.get("tun_mode", False)))

        if want_tun and not tun.is_admin():
            return {"error": "need_admin", "state": self._state()}
        if want_tun and not tun.find_tun2socks():
            return {"error": "no_tun2socks", "state": self._state()}

        was_proxy = bool(self.settings.get("system_proxy", True))
        was_tun = bool(self.settings.get("tun_mode", False))
        self.settings["system_proxy"] = want_proxy
        self.settings["tun_mode"] = want_tun
        self._save()

        if self.connected:
            hp = int(self.settings.get("http_port", 10809))
            sp = int(self.settings.get("socks_port", 10808))
            if want_proxy and not was_proxy:
                # Прокси включаем только если локальный порт реально слушается,
                # иначе весь трафик уйдёт в пустоту и интернет заблокируется.
                if win_proxy.port_listening("127.0.0.1", hp, timeout=0.4):
                    win_proxy.set_proxy("127.0.0.1:%d" % hp)
                    self._log("[proxy] системный прокси включён")
                else:
                    self.settings["system_proxy"] = False
                    self._save()
                    self._log("[proxy] proxy-порт %d не готов — "
                              "системный прокси не включён" % hp)
            elif not want_proxy and was_proxy:
                win_proxy.disable_proxy()
                self._log("[proxy] системный прокси выключен")

            if want_tun and not was_tun:
                try:
                    srv = self.servers[self.selected]
                    self._tun.start(sp, srv.address,
                                    dns=self.settings.get("tun_dns", "1.1.1.1"),
                                    on_log=self._log)
                except Exception as e:
                    self.settings["tun_mode"] = False
                    self._save()
                    self._log("[tun] ошибка: %s" % e)
                    return {"error": str(e), "state": self._state()}
                # туннель всегда работает в global без прямых исключений —
                # перезапускаем ядро, чтобы выйти из режима rules/direct,
                # иначе «direct» зациклится через туннель обратно в Xray
                self._restart_core()
            elif not want_tun and was_tun:
                self._tun.stop()
                self._log("[tun] туннель выключен")
                # возвращаем ядру сохранённый режим маршрутизации
                self._restart_core()

        return {"ok": True, "state": self._state()}

    def request_admin(self):
        """Перезапуск с правами администратора (нужно для туннеля)."""
        if tun.is_admin():
            return {"ok": True, "already": True}
        if not tun.relaunch_as_admin():
            return {"error": "denied"}
        threading.Timer(0.4, self._quit).start()
        return {"ok": True}

    def _quit(self):
        # Перезапуск с правами администратора: соединение (если было) сохраняем
        # для авто-восстановления в новом процессе, чтобы VPN не «пропадал».
        try:
            self.shutdown(keep_resume=True)
        except Exception:
            pass
        if self._on_quit_cb:
            try:
                self._on_quit_cb()
            except Exception:
                pass

    # ------------------------------------------------------------ маршруты
    def get_routing(self):
        return {
            "mode": self.settings.get("route_mode", "global"),
            "direct": self.settings.get("direct_sites", []),
            "block": self.settings.get("block_sites", []),
        }

    def save_routing(self, patch):
        """Режим маршрутизации и списки исключений.

        Списки принимаем текстом (по строке на правило) или массивом.
        """
        patch = patch or {}
        mode = patch.get("mode", self.settings.get("route_mode", "global"))
        if mode not in ("global", "rules", "direct"):
            mode = "global"
        self.settings["route_mode"] = mode
        self.settings["direct_sites"] = self._as_list(patch.get("direct"))
        self.settings["block_sites"] = self._as_list(patch.get("block"))
        self._save()
        # правила живут в конфиге ядра — на лету применяем перезапуском Xray
        restarted = False
        if self.connected and self.selected >= 0:
            restarted = self._restart_core()
        return {"ok": True, "restarted": restarted, "state": self._state()}

    @staticmethod
    def _as_list(value):
        if value is None:
            return []
        if isinstance(value, str):
            value = value.replace(",", "\n").split("\n")
        out, seen = [], set()
        for item in value:
            item = str(item).strip()
            if item and item.lower() not in seen:
                seen.add(item.lower())
                out.append(item)
        return out

    def _restart_core(self):
        """Перезапуск ядра с новым конфигом, туннель при этом не роняем."""
        try:
            srv = self.servers[self.selected]
            mode, direct_entries = self._xray_route()
            self._xray.start(
                srv,
                int(self.settings.get("socks_port", 10808)),
                int(self.settings.get("http_port", 10809)),
                self.settings.get("xray_path", ""),
                on_log=self._log,
                mode=mode,
                direct_entries=direct_entries,
                block_entries=self.settings.get("block_sites", []),
                high_priority=bool(self.settings.get("high_priority", False)),
            )
            # Ядро перезапущено — дожидаемся готовности порта. Если он не
            # поднялся, системный прокси указывает в пустоту и интернет
            # блокируется: откатываемся и выключаем прокси.
            hp = int(self.settings.get("http_port", 10809))
            if not self._wait_local_port(hp):
                self._xray.stop()
                if self.settings.get("system_proxy", True):
                    win_proxy.disable_proxy()
                self._log("[error] после перезапуска ядра порт %d не поднялся — "
                          "системный прокси выключен" % hp)
                return False
            self._log("[core] правила маршрутизации применены")
            return True
        except Exception as e:
            self._log("[error] %s" % e)
            return False

    # ------------------------------------------------------------ Telegram
    def link_telegram(self, username):
        """Привязать аккаунт: тянем имя и аватарку с публичной страницы t.me."""
        res = tg_link.fetch_profile(username or "")
        if res.get("error"):
            return {"error": res["error"]}
        self.settings["tg_username"] = res["username"]
        self.settings["tg_name"] = res["name"]
        self.settings["tg_avatar"] = res.get("avatar", "")
        self._save()
        return {"ok": True, "state": self._state()}

    def unlink_telegram(self):
        self.settings["tg_username"] = ""
        self.settings["tg_name"] = ""
        self.settings["tg_avatar"] = ""
        self._save()
        return {"ok": True, "state": self._state()}

    # ------------------------------------------------------------ мессенджер
    def _tg_creds(self):
        return {
            "api_id": self.settings.get("tg_api_id", ""),
            "api_hash": self.settings.get("tg_api_hash", ""),
        }

    def _tg_event(self, ev):
        """Каждое событие мессенджера уходит в JS одним JSON."""
        try:
            self._push("window.__tgPush(%s)" % json.dumps(ev, ensure_ascii=False))
        except Exception:
            pass

    def _tg_toast(self, text):
        try:
            self._push("window.__tgToast(%s)" % json.dumps(str(text), ensure_ascii=False))
        except Exception:
            pass

    def _tg_before_send(self, ev):
        """Перед отправкой сообщения: даём плагинам перехватить команды."""
        handled, reply = self._plugins.dispatch_command(ev.get("text", ""), ev)
        if handled and reply:
            self._tg_event({"type": "plugin_reply", "peer_id": ev.get("peer_id"),
                            "text": reply})
        return {"handled": handled, "reply": reply}

    def _tg_incoming(self, ev):
        self._plugins.dispatch_message(ev)

    def _plugin_is_enabled(self, name):
        try:
            return bool((self.settings.get("plugin_enabled") or {}).get(name, True))
        except Exception:
            return True

    def _plugin_set_enabled(self, name, on):
        cur = dict(self.settings.get("plugin_enabled") or {})
        cur[str(name)] = bool(on)
        self.settings["plugin_enabled"] = cur
        self._save()

    def _plugin_ctx(self, name):
        return plugins_mod.PluginCtx(
            name,
            send_async=self._tg.asend_text,
            me_fn=self._tg.me_json,
            notify_fn=self._tg_toast,
            ui_fn=self._plugin_ui,
        )

    def _plugin_ui(self, action, data):
        try:
            self._push("window.__pluginPush(%s,%s)"
                       % (json.dumps(action), json.dumps(data, ensure_ascii=False)))
        except Exception:
            pass

    # ---- методы для JS ----
    def tg_state(self):
        st = self._tg.status()
        st["me"] = self._tg.me_json()
        st["has_credentials"] = bool(self._tg_creds()["api_id"]) or bool(
            self._tg_creds()["api_hash"])
        return st

    def tg_login(self, phone, api_id="", api_hash=""):
        # api_id/api_hash зашиты в tg_client.py (публичные константы клиента Telegram).
        # Параметры оставлены для совместимости со старыми сборками и больше не пишутся в настройки.
        ok = self._tg.login(phone, api_id, api_hash)
        return {"ok": ok}

    def tg_code(self, code):
        return {"ok": self._tg.code(code)}

    def tg_password(self, password):
        return {"ok": self._tg.password(password)}

    def tg_logout(self):
        return {"ok": self._tg.logout()}

    def tg_set_avatar(self, data_b64=""):
        """Ставит аватар аккаунта: свой файл (base64) или иконку MimiBox."""
        data_b64 = (data_b64 or "").strip()
        if data_b64 and "," in data_b64:
            data_b64 = data_b64.split(",", 1)[1]
        return {"ok": self._tg.set_avatar(data_b64)}

    def tg_open(self, peer):
        return {"ok": self._tg.open_chat(peer)}

    def tg_refresh(self):
        return {"ok": self._tg.refresh_dialogs()}

    def tg_archive(self, peer, on):
        return {"ok": self._tg.set_archive(peer, bool(on))}

    def tg_send(self, peer, text):
        return {"ok": self._tg.send(peer, text)}

    def tg_send_file(self, peer, name, data_b64):
        """Отправка фото/файла: JS шлёт base64, пишем во временный файл."""
        import base64 as _b64
        name = str(name or "file").strip() or "file"
        if not data_b64:
            return {"ok": False}
        try:
            if "," in data_b64:
                data_b64 = data_b64.split(",", 1)[1]
            raw = _b64.b64decode(data_b64)
        except Exception as e:
            storage.log("[tg] не удалось декодировать файл: %s" % e)
            return {"ok": False}
        if not raw or len(raw) > 50 * 1024 * 1024:
            return {"ok": False, "error": "too_big"}
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_"
                       for ch in name)
        folder = os.path.join(storage.data_dir(), "tg_uploads")
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception:
            pass
        path = os.path.join(folder, "%d_%s" % (int(time.time() * 1000), safe))
        try:
            with open(path, "wb") as f:
                f.write(raw)
        except Exception as e:
            storage.log("[tg] не удалось сохранить файл: %s" % e)
            return {"ok": False}
        return {"ok": self._tg.send_file(peer, path)}

    def tg_send_url(self, peer, url):
        """Скачивает GIF/файл по ссылке и отправляет в чат (для пикера GIF)."""
        import urllib.request as _req
        import urllib.error as _err
        url = str(url or "").strip()
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "error": "bad_url"}
        try:
            _opener = _req.build_opener(_req.ProxyHandler({}))
            req = _req.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with _opener.open(req, timeout=20) as resp:
                raw = resp.read()
        except Exception as e:
            storage.log("[tg] не удалось скачать URL: %s" % e)
            return {"ok": False, "error": "download_failed"}
        if not raw or len(raw) > 50 * 1024 * 1024:
            return {"ok": False, "error": "too_big"}
        name = os.path.basename(url.split("?", 1)[0]) or "media"
        if not name.lower().endswith((".gif", ".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm")):
            name += ".gif"
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)
        folder = os.path.join(storage.data_dir(), "tg_uploads")
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception:
            pass
        path = os.path.join(folder, "%d_%s" % (int(time.time() * 1000), safe))
        try:
            with open(path, "wb") as f:
                f.write(raw)
        except Exception as e:
            storage.log("[tg] не удалось сохранить URL: %s" % e)
            return {"ok": False}
        return {"ok": self._tg.send_file(peer, path)}

    def tg_download(self, peer, msg_id):
        return {"ok": self._tg.download(peer, msg_id)}

    def tg_open_media(self, path):
        try:
            os.startfile(path)  # type: ignore[attr-defined]
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def tg_sticker_sets(self):
        return {"ok": self._tg.sticker_sets()}

    def tg_sticker_set_items(self, set_id):
        return {"ok": self._tg.sticker_set_items(set_id)}

    def tg_sticker_preview(self, doc_id):
        return {"ok": self._tg.sticker_preview(doc_id)}

    def tg_send_doc(self, peer, doc_id):
        return {"ok": self._tg.send_doc(peer, doc_id)}

    def tg_saved_gifs(self):
        return {"ok": self._tg.saved_gifs()}

    def tg_gif_search(self, query):
        return {"ok": self._tg.gif_search(query)}

    def tg_reply(self, peer, reply_to, text):
        return {"ok": self._tg.reply(peer, reply_to, text)}

    def tg_edit(self, peer, msg_id, text):
        return {"ok": self._tg.edit(peer, msg_id, text)}

    def tg_delete(self, peer, msg_id):
        return {"ok": self._tg.delete(peer, msg_id)}

    def tg_plugins_list(self):
        return {"plugins": self._plugins.list()}

    def tg_plugin_toggle(self, name, on):
        self._plugins.set_enabled(name, bool(on))
        return {"plugins": self._plugins.list()}

    def tg_plugins_reload(self):
        return {"plugins": self._plugins.reload_all()}

    def tg_plugins_open_folder(self):
        folder = os.path.join(storage.data_dir(), "plugins")
        try:
            os.makedirs(folder, exist_ok=True)
            os.startfile(folder)  # type: ignore[attr-defined]
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------ статистика
    def _start_stats(self):
        self._stop_stats.clear()
        if self._stats_thread and self._stats_thread.is_alive():
            return
        self._stats_thread = threading.Thread(target=self._stats_loop, daemon=True)
        self._stats_thread.start()

    def _stop_stats_loop(self):
        self._stop_stats.set()

    def _persist_traffic(self):
        """Сохраняем кумулятивный трафик, чтобы он не терялся при перезапуске."""
        try:
            self.settings["traffic_up"] = int(self._total[0])
            self.settings["traffic_down"] = int(self._total[1])
            storage.save_settings(self.settings)
        except Exception:
            pass

    def _stats_loop(self):
        # первый опрос сбрасывает накопленное за время старта ядра
        self._xray.traffic()
        tick = 0
        while not self._stop_stats.wait(1.0):
            if not self.connected:
                break
            up, down = self._xray.traffic()
            self._total[0] += up
            self._total[1] += down
            self._speed = (up, down)
            self._push_speed(up, down)
            tick += 1
            if tick >= 15:      # раз в ~15 секунд — в файл
                tick = 0
                self._persist_traffic()
        self._speed = (0, 0)
        self._persist_traffic()

    def _push_speed(self, up, down):
        self._push("window.__pushSpeed(%d,%d,%d,%d)"
                   % (up, down, self._total[0], self._total[1]))
        if self._on_speed_cb:
            try:
                self._on_speed_cb(up, down)
            except Exception:
                pass

    # ------------------------------------------------------------ настройки
    def save_settings(self, patch):
        patch = patch or {}
        cur = self.settings
        for key, default in (("socks_port", 10808), ("http_port", 10809)):
            cur[key] = storage.validate_port(patch.get(key, cur.get(key, default)))
            if cur[key] == 0:
                cur[key] = default
        cur["system_proxy"] = bool(patch.get("system_proxy", cur.get("system_proxy", True)))
        cur["xray_path"] = str(patch.get("xray_path", cur.get("xray_path", ""))).strip()
        cur["hwid_enabled"] = bool(patch.get("hwid_enabled", cur.get("hwid_enabled", True)))
        cur["theme"] = patch.get("theme", cur.get("theme", "auto"))
        cur["lang"] = patch.get("lang", cur.get("lang", "ru"))
        cur["tun_dns"] = storage.validate_dns(patch.get("tun_dns", cur.get("tun_dns", "1.1.1.1")))
        for key, default in (("minimize_to_tray", True), ("start_minimized", False),
                             ("high_priority", False)):
            cur[key] = bool(patch.get(key, cur.get(key, default)))
        cur["tun_mode"] = bool(patch.get("tun_mode", cur.get("tun_mode", False)))
        # ---- кастомизация ----
        cur["custom_bg"] = storage.validate_color(patch.get("custom_bg", cur.get("custom_bg", "")))
        cur["custom_text"] = storage.validate_color(patch.get("custom_text", cur.get("custom_text", "")))
        cur["custom_accent"] = storage.validate_color(patch.get("custom_accent", cur.get("custom_accent", "")))
        cur["custom_surface"] = storage.validate_color(patch.get("custom_surface", cur.get("custom_surface", "")))
        cur["custom_font"] = str(patch.get("custom_font", cur.get("custom_font", ""))).strip()[:100]
        # ---- снежинки ----
        if "snow_enabled" in patch:
            cur["snow_enabled"] = bool(patch["snow_enabled"])
        # ---- новости ----
        cur["news_off"] = bool(patch.get("news_off", cur.get("news_off", False)))
        if "last_news_id" in patch:
            cur["last_news_id"] = str(patch.get("last_news_id", cur.get("last_news_id", "")))
        # ---- автообновление: какую версию не напоминать ----
        if "skip_update_version" in patch:
            cur["skip_update_version"] = str(patch.get("skip_update_version", "") or "").strip()[:32]
        # ---- фоновая сцена и шрифт (раньше жили только в JS) ----
        if "bg_scene" in patch:
            cur["bg_scene"] = str(patch.get("bg_scene", "") or "")[:32]
        if "font_style" in patch:
            cur["font_style"] = str(patch.get("font_style", "") or "")[:32]
        # ---- автозапуск и игровой режим ----
        cur["autostart"] = bool(patch.get("autostart", cur.get("autostart", False)))
        if "game_mode" in patch:
            prev_game = bool(cur.get("game_mode", False))
            cur["game_mode"] = bool(patch.get("game_mode", prev_game))
            if bool(cur["game_mode"]) != prev_game:
                self._apply_game_mode()

        ok = self._save()
        if ok and "high_priority" in patch:
            apply_priority(bool(cur["high_priority"]))
        if "autostart" in patch:
            apply_autostart(bool(cur["autostart"]))
        state = self._state()
        state["saved"] = ok
        return state

    def check_conflicts(self):
        return {"conflicts": self._conflicts}

    # Только процессы, которые реально гоняют трафик. Фоновые службы-помощники
    # (clash-verge-service и подобные) сюда не входят: они висят в системе всегда,
    # даже когда сам клиент закрыт, и раньше из-за них баннер не гас никогда.
    CONFLICT_PROCESSES = {
        "happ.exe": "Happ", "nekoray.exe": "NekoRay", "nekobox.exe": "NekoBox",
        "v2rayn.exe": "v2rayN", "v2rayw.exe": "v2rayW", "v2ray.exe": "V2Ray",
        "clash.exe": "Clash", "clash-verge.exe": "Clash Verge",
        "clashx.exe": "ClashX", "clash party.exe": "Clash Party",
        "clash-party.exe": "Clash Party", "verge-mihomo.exe": "Clash Verge",
        "mihomo.exe": "Clash / Mihomo", "mihomo-alpha.exe": "Clash / Mihomo",
        "hiddify.exe": "Hiddify", "hiddifynext.exe": "Hiddify",
        "sing-box.exe": "sing-box", "singbox.exe": "sing-box",
        "hysteria.exe": "Hysteria", "wireguard.exe": "WireGuard",
        "openvpn.exe": "OpenVPN", "openvpn-gui.exe": "OpenVPN",
        "amneziavpn.exe": "AmneziaVPN", "outline.exe": "Outline",
        "furiousgfw.exe": "Furious", "throne.exe": "Throne",
        "invisibleman-xray.exe": "InvisibleMan",
    }

    def _scan_conflicts(self):
        """Следит за сторонними VPN-клиентами, пока приложение работает.

        Проверка повторяется: пользователь закрывает чужой клиент и ждёт, что
        предупреждение исчезнет само, а не после перезапуска приложения.
        """
        import subprocess
        while True:
            found = set()
            try:
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                out = subprocess.check_output(
                    ["tasklist", "/fo", "csv", "/nh"],
                    startupinfo=si, creationflags=0x08000000,
                    text=True, errors="ignore", timeout=8,
                )
                low = out.lower()
                for exe, label in self.CONFLICT_PROCESSES.items():
                    if '"%s"' % exe in low:
                        found.add(label)
            except Exception:
                pass

            found = sorted(found)
            with self._conflicts_lock:
                if found != self._conflicts:
                    self._conflicts = found
                    self._push("window.__pushConflicts(%s)" % json.dumps(found))
            time.sleep(15)

    def open_external(self, url):
        """Открыть ссылку во ВНЕШНЕМ браузере (не внутри окна приложения).

        Принимаем только http/https — иначе можно открыть file://, javascript: и прочие опасные схемы.
        """
        import webbrowser
        url = (url or "").strip()
        if not url:
            return {"ok": False}
        low = url.lower()
        if not low.startswith(("http://", "https://")):
            storage.log("[security] open_external заблокировал небезопасную схему: %s" % url[:80])
            return {"ok": False}
        try:
            webbrowser.open(url, new=2)
        except Exception:
            try:
                os.startfile(url)  # type: ignore[attr-defined]
            except Exception:
                pass
        return {"ok": True}

    def open_data_folder(self):
        """Показать папку с настройками и логом — для разбора проблем."""
        try:
            os.startfile(storage.data_dir())  # type: ignore[attr-defined]
            return {"ok": True}
        except Exception as e:
            return {"error": str(e)}

    def finish_intro(self):
        """Отметить, что приветственный экран пройден (показываем только раз)."""
        self.settings["intro_done"] = True
        self._save()
        return self._state()

    def _mark_onboarded(self):
        """Появились серверы — знакомство закончено, окна больше не нужны."""
        if not self.settings.get("intro_done"):
            self.settings["intro_done"] = True
        self._save()

    def set_rating(self, n):
        """Сохранить пользовательскую оценку приложения (0..5)."""
        try:
            n = max(0, min(5, int(n)))
        except Exception:
            n = 0
        self.settings["rating"] = n
        self._save()
        return self._state()

    # ------------------------------------------------------------ фон
    def upload_background(self, data_b64):
        """Сохраняет пользовательское фоновое изображение."""
        ok = storage.save_background(data_b64)
        if ok:
            self.settings["background_image"] = "custom"
            self._save()
        return {"ok": ok, "state": self._state()}

    def get_background(self):
        """Возвращает base64-данные фона для отображения в UI.

        Фон хранится/отдаётся как JPEG (см. storage.save_background) — большой
        PNG в base64 клал рендерер WebView2 (чёрный экран).
        """
        return {"data": storage.load_background(), "mime": "image/jpeg"}

    def get_scene_background(self, scene_id):
        """Возвращает base64-данные встроенного фона сцены (stars/sakura/street).

        Сцена отдаётся JPEG'ом (~150–250 КБ): полноразмерные PNG-ресурсы
        через JS-мост вешали WebView2.
        """
        return {"data": storage.scene_background(scene_id), "mime": "image/jpeg"}

    def remove_background(self):
        """Удаляет пользовательский фон."""
        storage.remove_background()
        self.settings["background_image"] = ""
        self._save()
        return {"ok": True, "state": self._state()}

    # ------------------------------------------------------------ шрифт
    def upload_font(self, data_b64):
        """Сохраняет пользовательский .ttf шрифт."""
        ok = storage.save_font(data_b64)
        if ok:
            self.settings["has_custom_font"] = True
            self._save()
        return {"ok": ok, "state": self._state()}

    def get_font(self):
        """Возвращает base64-данные шрифта для @font-face."""
        return {"data": storage.load_font()}

    def remove_font(self):
        """Удаляет пользовательский шрифт."""
        storage.remove_font()
        self.settings["has_custom_font"] = False
        self._save()
        return {"ok": True, "state": self._state()}

    # ------------------------------------------------------------ обучение
    def finish_tutorial(self):
        """Отметить, что обучение пройдено."""
        self.settings["tutorial_done"] = True
        self._save()
        return self._state()

    def reset_tutorial(self):
        """Сбросить обучение для повторного просмотра."""
        self.settings["tutorial_done"] = False
        self._save()
        return self._state()

    # ------------------------------------------------------------ билд
    def _resolve_version(self):
        import os, subprocess, sys as _sys
        if getattr(_sys, 'frozen', False):
            return self.BUILD_VERSION
        try:
            tag = subprocess.check_output(
                ["git", "describe", "--tags", "--always"],
                stderr=subprocess.DEVNULL, text=True
            ).strip()
            if tag:
                return tag.lstrip("v") if tag.startswith("v") else tag
        except Exception:
            pass
        return self.BUILD_VERSION

    def get_build_info(self):
        """Информация о сборке для отображения в настройках."""
        return {
            "version": self._resolve_version(),
            "date": self.BUILD_DATE,
            "python": __import__("sys").version.split()[0],
            "platform": __import__("platform").platform(),
        }

    def get_debug_log(self, max_lines=400):
        """Последние строки app.log для кнопки «Copy debug log» на десктопе."""
        import os
        path = storage.LOG_FILE()
        try:
            if not path or not os.path.exists(path):
                return {"log": ""}
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            return {"log": "".join(lines[-max_lines:])}
        except Exception as e:
            return {"log": "", "error": str(e)}

    def read_clipboard(self):
        """Читает текст из системного буфера обмена (Windows, через ctypes).

        Надёжнее navigator.clipboard.readText(), который в WebView2 часто
        блокируется из-за отсутствия разрешения clipboard-read.
        """
        import ctypes
        text = ""
        try:
            user32 = ctypes.windll.user32
            CF_UNICODETEXT = 13
            user32.OpenClipboard.argtypes = [ctypes.c_void_p]
            user32.OpenClipboard.restype = ctypes.c_int
            user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
            user32.IsClipboardFormatAvailable.restype = ctypes.c_int
            user32.GetClipboardData.argtypes = [ctypes.c_uint]
            user32.GetClipboardData.restype = ctypes.c_void_p
            if user32.OpenClipboard(None):
                try:
                    if user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                        h = user32.GetClipboardData(CF_UNICODETEXT)
                        if h:
                            text = ctypes.wstring_at(h) or ""
                finally:
                    user32.CloseClipboard()
        except Exception:
            pass
        return {"text": text}

    # ------------------------------------------------------------ новости
    def get_news(self):
        """Последний пост канала новостей.

        Скан канала занимает секунды, поэтому делаем его в фоне: сюда приходят
        сразу (кэш или признак загрузки), а результат приходит в JS пушем
        (window.__tgPush {type:'news'}), который фронт уже умеет показывать.
        """
        if self._news_result and (time.time() - self._news_ts) < 1800:
            return self._news_result
        if not self._news_fetching and not self._news_suppressed:
            self._news_fetching = True
            threading.Thread(target=self._news_worker, args=("mackkill",),
                             daemon=True).start()
        return {"ok": False, "html": "", "post_id": "", "date": "",
                "disabled": self.settings.get("news_off", False)}

    def _news_worker(self, channel):
        try:
            res = self._fetch_channel_news(channel)
            self._news_result = res
            self._news_ts = time.time()
            if res.get("ok") and res.get("html"):
                self._tg_event({"type": "news", "channel": channel,
                                "post_id": res.get("post_id", ""),
                                "date": res.get("date", ""),
                                "html": res.get("html", "")})
            else:
                self._tg_event({"type": "news_error"})
        except Exception as e:
            storage.log("[news] worker: %s" % e)
            self._tg_event({"type": "news_error"})
        finally:
            self._news_fetching = False

    def news_via_tg(self, channel="mackkill"):
        """Fallback новостей через Telegram-сессию, если HTTP-скрейпинг не сработал."""
        try:
            if (self._tg and self._tg.is_running()
                    and self._tg.is_authorized()):
                self._tg.news_fetch(channel)
                return {"ok": True, "async": True}
        except Exception as e:
            storage.log("[news] fallback tg: %s" % e)
        return {"ok": False, "async": False}

    def get_support_news(self):
        return self.get_news()

    def _clean_post_html(self, html):
        """Чистим HTML поста Telegram: убираем опасные теги и обработчики."""
        import re as _re
        html = _re.sub(r'<(script|iframe|object|embed|form|input|button|textarea|select|style)\b[^>]*>.*?</\1>', '', html, flags=_re.DOTALL | _re.IGNORECASE)
        html = _re.sub(r'<(script|iframe|object|embed|form|input|button|textarea|select|style)\b[^>]*/>', '', html, flags=_re.IGNORECASE)
        html = _re.sub(r'\s+on\w+\s*=\s*["\'][^"\']*["\']', '', html, flags=_re.IGNORECASE)
        html = _re.sub(r'\s+on\w+\s*=\s*\S+', '', html, flags=_re.IGNORECASE)
        html = _re.sub(r'<div[^>]*class="tgme_widget_message_author[^"]*".*?</div>', '', html, flags=_re.DOTALL)
        html = _re.sub(r'<tg-spoiler[^>]*>(.*?)</tg-spoiler>', r'<span class="spoiler">\1</span>', html)
        html = _re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', r'\1', html)
        html = _re.sub(r'<br\s*/?>', '\n', html)
        html = _re.sub(r'<div[^>]*class="tgme_widget_message_reply[^"]*".*?</div>\s*</div>', '', html, flags=_re.DOTALL)
        return html.strip()

    def _latest_post_id(self, channel, embed_exists) -> int:
        """id последнего существующего поста канала (кэш — в settings).

        data-post в embed-странице есть только у существующих постов; удалённый
        пост и «id выше последнего» выглядят одинаково (err_message), поэтому
        бинпоиск для разреженных каналов невозможен. Плотные каналы ищем
        удвоением+бинпоиском, разреженные — последовательно (с капами).
        """
        last = int(self.settings.get("news_top_id") or 0) or 0
        best = 0

        if last:
            if embed_exists(last):
                best = last
            else:
                # кэш устарел — ищем ближайший существующий ниже
                for pid in range(last - 1, max(0, last - 20), -1):
                    if embed_exists(pid):
                        best = pid
                        break
        else:
            # холодный старт: определяем плотность канала по первым постам
            try:
                dense = bool(embed_exists(1) and embed_exists(2) and embed_exists(3))
            except Exception:
                dense = False
            if dense:
                lo, hi = 1, 1
                while hi <= 5000000 and embed_exists(hi):
                    lo = hi
                    hi *= 2
                while hi - lo > 1:
                    mid = (lo + hi) // 2
                    if embed_exists(mid):
                        lo = mid
                    else:
                        hi = mid
                best = lo
            else:
                # разреженный канал (как mackkill): только последовательный скан
                for pid in range(1, 151):
                    if embed_exists(pid):
                        best = pid
        if not best:
            return 0

        # дешёвый скан вверх: новые посты обычно идут подряд
        pid = best + 1
        misses = 0
        while pid <= best + 40 and misses < 12:
            if embed_exists(pid):
                best = pid
                misses = 0
            else:
                misses += 1
            pid += 1

        # раз в 6 часов — глубокий скан, чтобы догнать разреженные выбросы
        changed = best != last
        deep_at = float(self.settings.get("news_deep_at") or 0)
        if time.time() - deep_at > 21600:
            pid = best + 1
            cap = best + 150
            while pid <= cap:
                if embed_exists(pid):
                    best = pid
                pid += 1
            self.settings["news_deep_at"] = time.time()
            changed = True

        if changed:
            try:
                self.settings["news_top_id"] = best
                storage.save_settings(self.settings)
            except Exception:
                pass
        return best

    def _fetch_channel_news(self, channel: str) -> dict:
        """Последний пост канала.

        Старый путь t.me/s/{channel} больше не отдаёт разметку постов (JS-shell),
        а текст постов каналов с меткой [SCAM] скрыт в embed-виджете. Поэтому:
          1. id последнего поста находим сканом embed-страниц (_latest_post_id);
          2. дату берём из embed (datetime);
          3. текст — из разметки поста, а если скрыт — из og:description поста.
        """
        import re as _re
        import html as _html
        import urllib.request

        def _get(url):
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36"})
            # обходим системный прокси — при включённом VPN t.me недоступен через него
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with op.open(req, timeout=8) as resp:
                return resp.read().decode("utf-8", errors="replace")

        def _embed_exists(pid):
            try:
                return 'data-post="' in _get(f"https://t.me/{channel}/{pid}?embed=1")
            except Exception:
                return False

        top = self._latest_post_id(channel, _embed_exists)
        if not top:
            # постов нет/скан сорвался — показываем хотя бы описание канала
            try:
                og = _re.search(r'property="og:description"[^>]*content="([^"]*)"',
                                _get(f"https://t.me/{channel}"))
                if og and og.group(1).strip():
                    text = _html.unescape(og.group(1)).strip()
                    html = _html.escape(text).replace("\n", "<br>")
                    return {"ok": True, "html": html, "post_id": "",
                            "date": "", "disabled": self.settings.get("news_off", False)}
            except Exception:
                pass
            return {"ok": False, "html": "", "post_id": "", "date": "",
                    "disabled": self.settings.get("news_off", False)}

        date_str = ""
        html = ""
        try:
            emb = _get(f"https://t.me/{channel}/{top}?embed=1")
            dm = _re.search(r'datetime="([^"]+)"', emb)
            if dm:
                date_str = dm.group(1)
            blocks = _re.findall(
                r'<div[^>]*class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>\s*'
                r'(?:<div[^>]*class="tgme_widget_message_footer)',
                emb, _re.DOTALL)
            if blocks:
                html = self._clean_post_html(blocks[-1])
        except Exception:
            pass
        if not html:
            # текст в embed скрыт (SCAM/возрастное) — берём og:description поста
            try:
                og = _re.search(r'property="og:description"[^>]*content="([^"]*)"',
                                _get(f"https://t.me/{channel}/{top}"))
                if og and og.group(1).strip():
                    text = _html.unescape(og.group(1)).strip()
                    html = _html.escape(text).replace("\n", "<br>")
            except Exception:
                pass
        if not html:
            return {"ok": False, "html": "", "post_id": "", "date": "",
                    "disabled": self.settings.get("news_off", False)}
        return {"ok": True, "html": html, "post_id": "%s/%d" % (channel, top),
                "date": date_str, "disabled": self.settings.get("news_off", False)}

    def shutdown(self, keep_resume=False):
        # Повторные вызовы (main.py зовёт и при закрытии окна, и при выходе из
        # трея; _quit тоже) не должны повторно снимать флаг was_connected.
        if self._shutdown_done:
            return
        self._shutdown_done = True
        try:
            self._stop_stats_loop()
            self._persist_traffic()
            # Обычный выход = пользователь выключил VPN. При перезапуске с
            # правами администратора (keep_resume) соединение сохраняем.
            if not keep_resume:
                self._resume_wanted = False
                if self.settings.get("was_connected"):
                    self.settings["was_connected"] = False
                    self._save()
            self._tun.stop()
            self._xray.stop()
            if self.settings.get("system_proxy", True):
                win_proxy.disable_proxy()
        except Exception:
            pass

    # ---------------------------------------------------- push в JS
    # Построчный вывод ядра идёт в окно, но в файл попадает не весь: при активном
    # сёрфинге Xray пишет строку на каждое соединение, и app.log распухал бы до
    # десятков мегабайт за вечер. В файл — только то, что пригодится для разбора.
    # Xray пишет «accepted ...», tun2socks — JSON вида {"level":"info", ...};
    # и то и другое появляется на каждое соединение.
    _NOISE = ("accepted", "[info]", '"level":"info"', '"level":"debug"',
              "tunneling request")

    # Ядро пишет об этом по-английски и невнятно, а причина почти всегда одна:
    # трафик перехватывает другой VPN, поднявший TUN-адаптер.
    _HINTS = (
        ("received real certificate",
         "[!] Соединение с сервером перехватывает другая программа. "
         "Почти всегда это сторонний VPN с включённым режимом туннеля — "
         "закройте его полностью и попробуйте снова."),
        ("failed to listen tcp",
         "[!] Порт занят другой программой. Смените порты в настройках "
         "или закройте программу, которая их держит."),
    )

    # Рабочий шум, который пользователю видеть не нужно: он пугает «ошибками»
    # и забивает окно лога. REALITY-строку выше подменяем понятной подсказкой,
    # остальное просто не показываем вовсе.
    _QUIET = ("failed to read response",       # Telegram: рвётся несущее соединение
              "wsasend: connection aborted",   # окно, закрытое сопером
              "received real certificate",     # REALITY-строка → заменяем hint'ом
              "accepted udp", "tunneling request to udp")
    _QUIET_UDP_PORTS = (":137", ":138", ":139", ":1900", ":5353")

    def _is_quiet(self, low):
        if any(n in low for n in self._QUIET):
            return True
        if "udp:" in low and any(p in low for p in self._QUIET_UDP_PORTS):
            return True
        return False

    def _log(self, line):
        line = str(line)
        low = line.lower()
        for needle, hint in self._HINTS:
            if needle in low and hint != self._last_hint:
                self._last_hint = hint
                storage.log(hint)
                self._push("window.__pushLog(%s)" % json.dumps(hint))
                self._push("window.__pushHint(%s)" % json.dumps(hint))
                break
        if self._is_quiet(low):
            return
        if not any(n in low for n in self._NOISE):
            storage.log(line)
        self._push("window.__pushLog(%s)" % json.dumps(line))

    def _push(self, js):
        """Ставит вызов в очередь. Никогда не блокирует вызывающий поток."""
        try:
            self._js_queue.put_nowait(js)
        except queue.Full:
            pass          # UI не успевает — лучше потерять строку лога, чем встать

    def _js_pump(self):
        """Единственный поток, которому позволено дёргать evaluate_js."""
        while True:
            js = self._js_queue.get()
            window = self._window
            if window is None:
                continue
            try:
                window.evaluate_js(js)
            except Exception:
                pass

    # ------------------------------------------------------------ Серийчик
    def _ser_json(self):
        """Питомец-аниме-девушка. Кормится VPN-трафиком: всё, что прокачано
        сверх прошлой точки, падает в «запас еды» (ser_bank)."""
        s = self.settings
        level = max(1, int(s.get("ser_level") or 1))
        xp = int(s.get("ser_xp") or 0)
        xp_next = level * 100
        last_feed = int(s.get("ser_last_feed") or 0)
        hunger = 100
        if last_feed:
            hunger = max(0, 100 - int((time.time() - last_feed) // 900))  # -4 в час
        total = int(self._total[0] or 0) + int(self._total[1] or 0)
        baseline = int(s.get("ser_baseline") or 0)
        bank = int(s.get("ser_bank") or 0)
        if total > baseline:
            bank += total - baseline
            s["ser_baseline"] = total
            s["ser_bank"] = bank
            self._save()
        if hunger <= 25:
            mood = "hungry"
        elif self.connected:
            mood = "happy"
        else:
            mood = "neutral"
        return {
            "name": str(s.get("ser_name") or "Kitagawa"),
            "level": level,
            "xp": xp,
            "xp_next": xp_next,
            "hunger": int(hunger),
            "bank": int(bank),
            "mood": mood,
            "connected": bool(self.connected),
        }

    def ser_feed(self, mb=50):
        mb = max(1, int(mb or 50))
        amount = mb * 1024 * 1024
        ser = self._ser_json()
        if ser["bank"] < amount:
            return {"ok": False, "state": self._state(), "error": "not_enough"}
        s = self.settings
        s["ser_bank"] = ser["bank"] - amount
        level, xp = ser["level"], ser["xp"] + mb
        while xp >= level * 100:
            xp -= level * 100
            level += 1
        s["ser_level"] = level
        s["ser_xp"] = xp
        s["ser_last_feed"] = int(time.time())
        self._save()
        return {"ok": True, "state": self._state()}

    def ser_set_name(self, name):
        name = str(name or "").strip()
        if not name:
            return {"ok": False, "error": "empty"}
        self.settings["ser_name"] = name[:24]
        self._save()
        return {"ok": True, "state": self._state()}

    def ser_set_avatar(self, data_b64):
        """Ставит своё фото Серийчика (прозрачный PNG)."""
        if not data_b64:
            return {"ok": False, "error": "empty"}
        ok = storage.save_ser_avatar(data_b64)
        return {"ok": ok, "state": self._state()}

    def ser_get_avatar(self, level=None):
        """Возвращает фото Серийчика: своё, если загружено, иначе — по уровню.

        Дефолт растёт вместе с питомцем: 1–50 → первое фото, 51–100 → второе,
        101–150 → третье, 151+ → четвёртое.
        """
        data = storage.load_ser_avatar()
        custom = bool(data)
        if not data:
            data = storage.ser_level_avatar(int(level or 1))
        return {"data": data, "custom": custom}

    def ser_remove_avatar(self):
        """Убирает своё фото — возвращается плейсхолдер."""
        storage.remove_ser_avatar()
        return {"ok": True, "state": self._state()}


def apply_priority(high: bool) -> bool:
    """Переключает класс приоритета своего процесса.

    «Выше обычного» вместо «высокого»: реального выигрыша столько же, но
    приложение не начинает конкурировать с системными службами.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        ABOVE_NORMAL, NORMAL = 0x00008000, 0x00000020
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        return bool(ctypes.windll.kernel32.SetPriorityClass(
            handle, ABOVE_NORMAL if high else NORMAL))
    except Exception:
        return False


# Тяжёлые фоновые приложения, которые «игровой режим» замораживает ради FPS.
# Ключ — имя процесса в нижнем регистре, значение — человеческое имя для UI.
# Свой список можно расширить прямо здесь: добавьте "имя.exe": "Название".
def apply_autostart(enabled: bool) -> bool:
    """Включает/выключает автозапуск приложения при входе в Windows (HKCU Run).

    Пишем только в реестр текущего пользователя — не нужны права администратора.
    """
    if os.name != "nt":
        return False
    import sys as _sys
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE)
        try:
            if enabled:
                exe = _sys.executable if getattr(_sys, "frozen", False) \
                    else os.path.abspath(_sys.argv[0])
                winreg.SetValueEx(key, "MimiBox", 0, winreg.REG_SZ, '"%s"' % exe)
            else:
                try:
                    winreg.DeleteValue(key, "MimiBox")
                except FileNotFoundError:
                    pass
            return True
        finally:
            winreg.CloseKey(key)
    except Exception as e:
        storage.log("[autostart] ошибка: %s" % e)
        return False
