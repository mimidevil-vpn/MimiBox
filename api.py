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
import threading

from parsing import parse_many, parse_subscription, fetch_subscription
from xray_core import XrayManager, tcp_ping, find_xray, kill_orphans
import win_proxy
import storage
import tun
import tg_link
import plugins as plugins_mod
from tg_client import TgMessenger

# Эмодзи, которое подмигивает возле названия. Меняется раз в час.
EMOJI_POOL = ["🧊", "❄️", "🐧", "🌊", "⚡", "🛡️", "🚀", "🌙", "✨", "🔒",
              "🦈", "🐳", "🧭", "🎧", "🌌", "🔥", "🍀", "🎯", "💎", "🪐"]
EMOJI_PERIOD = 3600           # секунд между сменами


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
        self._save_error = ""
        self._speed = (0, 0)
        self._total = [0, 0]
        self._last_hint = ""
        self._stats_thread = None
        self._stop_stats = threading.Event()

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
        self._roll_emoji(force=False)
        self._log_environment()

        # поиск сторонних VPN не должен тормозить открытие окна
        threading.Thread(target=self._scan_conflicts, daemon=True).start()
        threading.Thread(target=self._cleanup_orphans, daemon=True).start()

    def _cleanup_orphans(self):
        killed = kill_orphans(find_xray(self.settings.get("xray_path", "")))
        if killed:
            storage.log("[env] снято зависших ядер от прошлого запуска: %d" % killed)

    # ------------------------------------------------- окно и колбэки
    def _attach(self, window, on_quit=None, on_speed=None):
        """Вызывает main.py после создания окна."""
        self._window = window
        self._on_quit_cb = on_quit
        self._on_speed_cb = on_speed

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
        try:
            content, info = fetch_subscription(url)
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
        return {"added": len(parsed), "state": self._state()}

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

        self.selected = index
        srv = self.servers[index]
        sp = int(self.settings.get("socks_port", 10808))
        hp = int(self.settings.get("http_port", 10809))
        try:
            exe = self._xray.start(
                srv, sp, hp, self.settings.get("xray_path", ""),
                on_log=self._log,
                mode=self.settings.get("route_mode", "global"),
                direct_entries=self.settings.get("direct_sites", []),
                block_entries=self.settings.get("block_sites", []),
                high_priority=bool(self.settings.get("high_priority", False)),
            )
            self._log("[core] запущено ядро: %s" % exe)
            self._log("[core] сервер: %s (%s, %s)" % (srv.name, srv.protocol, srv.network))
        except Exception as e:
            self._log("[error] %s" % e)
            return {"error": str(e)}

        if self.settings.get("system_proxy", True):
            ok, err = win_proxy.set_proxy("127.0.0.1:%d" % hp)
            self._log("[proxy] системный прокси -> 127.0.0.1:%d" % hp if ok
                      else "[proxy] ошибка: %s" % err)

        if want_tun:
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
        self._start_stats()
        return {"ok": True, "state": self._state()}

    def disconnect(self):
        self._stop_stats_loop()
        self._tun.stop()
        self._xray.stop()
        if self.settings.get("system_proxy", True):
            win_proxy.disable_proxy()
        self.connected = False
        self._speed = (0, 0)
        self._push_speed(0, 0)
        self._log("[core] отключено")
        return {"ok": True, "state": self._state()}

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
                win_proxy.set_proxy("127.0.0.1:%d" % hp)
                self._log("[proxy] системный прокси включён")
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
            elif not want_tun and was_tun:
                self._tun.stop()
                self._log("[tun] туннель выключен")

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
        try:
            self.shutdown()
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
            self._xray.start(
                srv,
                int(self.settings.get("socks_port", 10808)),
                int(self.settings.get("http_port", 10809)),
                self.settings.get("xray_path", ""),
                on_log=self._log,
                mode=self.settings.get("route_mode", "global"),
                direct_entries=self.settings.get("direct_sites", []),
                block_entries=self.settings.get("block_sites", []),
                high_priority=bool(self.settings.get("high_priority", False)),
            )
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

    def tg_open(self, peer):
        return {"ok": self._tg.open_chat(peer)}

    def tg_refresh(self):
        return {"ok": self._tg.refresh_dialogs()}

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
        self._total = [0, 0]

    def _stats_loop(self):
        # первый опрос сбрасывает накопленное за время старта ядра
        self._xray.traffic()
        while not self._stop_stats.wait(1.0):
            if not self.connected:
                break
            up, down = self._xray.traffic()
            self._total[0] += up
            self._total[1] += down
            self._speed = (up, down)
            self._push_speed(up, down)
        self._speed = (0, 0)

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

        ok = self._save()
        if ok and "high_priority" in patch:
            apply_priority(bool(cur["high_priority"]))
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
        """Возвращает base64-данные фона для отображения в UI."""
        return {"data": storage.load_background()}

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
        return self._fetch_channel_news("mackkill")

    def get_support_news(self):
        return self._fetch_channel_news("mackkill")

    def _fetch_channel_news(self, channel: str) -> dict:
        """Получить последний пост из указанного Telegram-канала."""
        import re as _re
        import urllib.request
        import urllib.error
        import html as _html
        url = f"https://t.me/s/{channel}"
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            # обходим системный прокси — при включённом VPN t.me недоступен через локальный прокси
            _noproxy = urllib.request.ProxyHandler({})
            _opener = urllib.request.build_opener(_noproxy)
            with _opener.open(req, timeout=10) as resp:
                data = resp.read().decode("utf-8", errors="replace")
            # Ищем все посты (data-post="channel/123")
            post_blocks = _re.findall(
                r'<div[^>]*class="tgme_widget_message_wrap[^"]*"[^>]*data-post="([^"]+)"[^>]*>.*?'
                r'<div[^>]*class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>\s*'
                r'(?:<div[^>]*class="tgme_widget_message_footer)',
                data, _re.DOTALL
            )
            if not post_blocks:
                # Фоллбэк: ищем посты без footer
                post_blocks = _re.findall(
                    r'data-post="([^"]+)"[^>]*>.*?'
                    r'<div[^>]*class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                    data, _re.DOTALL
                )
            if not post_blocks:
                return {"ok": True, "html": "", "post_id": "", "date": "", "disabled": self.settings.get("news_off", False)}
            post_id, post_html = post_blocks[-1]
            # Очищаем нежелательные теги, но сохраняем форматирование TG
            html = post_html
            # Безопасность: удаляем опасные теги
            html = _re.sub(r'<(script|iframe|object|embed|form|input|button|textarea|select|style)\b[^>]*>.*?</\1>', '', html, flags=_re.DOTALL | _re.IGNORECASE)
            html = _re.sub(r'<(script|iframe|object|embed|form|input|button|textarea|select|style)\b[^>]*/>', '', html, flags=_re.IGNORECASE)
            # Удаляем обработчики событий
            html = _re.sub(r'\s+on\w+\s*=\s*["\'][^"\']*["\']', '', html, flags=_re.IGNORECASE)
            html = _re.sub(r'\s+on\w+\s*=\s*\S+', '', html, flags=_re.IGNORECASE)
            # Удаляем <span class="tgme_widget_message_wrap..."> вложенные (реклама и т.д.)
            html = _re.sub(r'<div[^>]*class="tgme_widget_message_author[^"]*".*?</div>', '', html, flags=_re.DOTALL)
            # Удаляем спойлеры (оставляем текст)
            html = _re.sub(r'<tg-spoiler[^>]*>(.*?)</tg-spoiler>', r'<span class="spoiler">\1</span>', html)
            # Удаляем <tg-emoji> теги, оставляем текст
            html = _re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', r'\1', html)
            # Превращаем <br> в переносы строк
            html = _re.sub(r'<br\s*/?>', '\n', html)
            # Убирают <div class="tgme_widget_message_reply..."> (цитаты из других постов)
            html = _re.sub(r'<div[^>]*class="tgme_widget_message_reply[^"]*".*?</div>\s*</div>', '', html, flags=_re.DOTALL)
            html = html.strip()
            # Дата
            date_match = _re.search(r'datetime="([^"]+)"', data[data.rfind(post_id):])
            date_str = date_match.group(1) if date_match else ""
            if not date_str:
                dates = _re.findall(r'datetime="([^"]+)"', data)
                date_str = dates[-1] if dates else ""
            return {
                "ok": True, "html": html, "post_id": post_id,
                "date": date_str, "disabled": self.settings.get("news_off", False)
            }
        except Exception as e:
            storage.log(f"[news] ошибка получения: {e}")
            return {"ok": False, "html": "", "post_id": "", "date": "", "disabled": self.settings.get("news_off", False)}

    def shutdown(self):
        try:
            self._stop_stats_loop()
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
            "name": str(s.get("ser_name") or "Серийчик"),
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
