# -*- coding: utf-8 -*-
"""Плагины мессенджера — как в ExtraGram.

Пользователь кладёт в <данные>/plugins/ файл *.py. Каждый файл — один плагин:

    PLUGIN = {
        "name": "Приветик",
        "version": "1.0",
        "description": "Отвечает на приветствия",
    }

    def on_load(ctx): ...
    def on_unload(ctx): ...
    def on_message(ctx, event): ...       # при новом входящем сообщении
    def on_dialog(ctx, dialog): ...       # при открытии чата
    def on_command(ctx, event): ...       # любая команда, если нет cmd_*

    # команды: cmd_<имя> вызывается для сообщения «/<имя> аргументы»
    def cmd_hi(ctx, event):
        ctx.reply("Привет! 👋")

Событие — dict: peer_id, peer_title, text, message_id, out, date.

Контекст ctx:
    ctx.reply(text)          # ответить в тот же чат
    await ctx.reply_async(text)
    ctx.send(peer_id, text)  # отправить в любой чат
    ctx.me()                 # dict профиля
    ctx.notify(text)         # тост в интерфейсе
    ctx.ui(action, data)     # произвольный push в JS (window.__pluginPush)
"""

import os
import asyncio
import inspect
import importlib.util


def _run_hook_result(result):
    """Если хук вернул корутину — запускаем её на текущем (работающем) цикле.

    Синхронные хуки (как в доках) вызываются и читаются напрямую; async-хуки
    мы просто не теряем: планируем на работающем цикле. Для cmd_* это значит,
    что команда считается перехваченной, а текст ответа приходит через ctx.reply.
    """
    if not inspect.isawaitable(result):
        return
    try:
        loop = asyncio.get_event_loop()
        if loop and loop.is_running():
            asyncio.ensure_future(result)
    except Exception:
        pass


class PluginCtx:
    """Контекст, который видит плагин во время вызова хука."""

    def __init__(self, name, send_async, me_fn, notify_fn, ui_fn):
        self._name = name
        self._send_async = send_async     # async fn(peer_id, text) -> dict
        self._me_fn = me_fn
        self._notify_fn = notify_fn
        self._ui_fn = ui_fn
        self._peer = None
        self._event = None

    def bind(self, peer, event):
        self._peer = peer
        self._event = event
        return self

    @property
    def name(self):
        return self._name

    @property
    def event(self):
        return dict(self._event or {})

    def _fire(self, coro):
        """Запускает корутину из синхронного кода: на работающем цикле — в фоне,
        снаружи — до конца."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(coro)
            else:
                loop.run_until_complete(coro)
        except Exception:
            pass

    def reply(self, text):
        if self._peer is None:
            return {"error": "no_peer"}
        return self._fire(self.reply_async(text))

    async def reply_async(self, text):
        if self._peer is None:
            return {"error": "no_peer"}
        return await self._send_async(self._peer, text)

    def send(self, peer_id, text):
        return self._fire(self.send_async(peer_id, text))

    async def send_async(self, peer_id, text):
        return await self._send_async(peer_id, text)

    def me(self):
        try:
            return self._me_fn() or {}
        except Exception:
            return {}

    def notify(self, text):
        try:
            self._notify_fn(str(text))
        except Exception:
            pass

    def ui(self, action, data=None):
        try:
            self._ui_fn(action, data)
        except Exception:
            pass


class PluginManager:
    """Сканирует папку плагинов, грузит .py и диспатчит события."""

    def __init__(self, folder, is_enabled, set_enabled, log=None):
        self._folder = folder
        self._is_enabled = is_enabled     # fn(name) -> bool
        self._set_enabled = set_enabled   # fn(name, bool)
        self._log = log or (lambda *a: None)
        self._modules = {}                # имя файла -> module
        self._meta = {}                   # имя файла -> PLUGIN dict
        self._errors = {}                 # имя файла -> текст ошибки
        self._ctx_builder = None          # fn(name) -> PluginCtx

    def folder_path(self):
        try:
            os.makedirs(self._folder, exist_ok=True)
        except Exception:
            pass
        return self._folder

    def set_ctx_builder(self, cb):
        self._ctx_builder = cb

    # ------------------------------------------------------------- сканирование
    def _files(self):
        names = []
        try:
            for fn in sorted(os.listdir(self.folder_path())):
                if fn.endswith(".py") and not fn.startswith("_"):
                    names.append(fn[:-3])
        except Exception:
            pass
        return names

    def list(self):
        out = []
        for name in self._files():
            meta = self._meta.get(name) or {"name": name, "version": "", "description": ""}
            err = self._errors.get(name) or ""
            out.append({
                "file": name,
                "name": meta.get("name") or name,
                "version": meta.get("version") or "",
                "description": meta.get("description") or "",
                "enabled": bool(self._is_enabled(name)),
                "error": err,
            })
        return out

    def reload_all(self):
        self._modules.clear()
        self._meta.clear()
        self._errors.clear()
        for name in self._files():
            try:
                self._load_one(name)
            except Exception as e:
                self._errors[name] = str(e)
                self._log("[plugins] %s: %s" % (name, e))
        # новые плагины по умолчанию включаем
        for name in self._files():
            if name not in self._enabled_set():
                try:
                    self._set_enabled(name, True)
                except Exception:
                    pass
        return self.list()

    def _enabled_set(self):
        return {item["file"] for item in self.list() if item["enabled"]}

    def _load_one(self, name):
        path = os.path.join(self._folder, name + ".py")
        spec = importlib.util.spec_from_file_location("mimibox_plugins." + name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self._modules[name] = mod
        self._meta[name] = dict(getattr(mod, "PLUGIN", {}) or {})

    def set_enabled(self, name, on):
        self._set_enabled(name, bool(on))
        return self.list()

    def _for_each(self, hook_name):
        for name, mod in list(self._modules.items()):
            if not self._is_enabled(name):
                continue
            hook = getattr(mod, hook_name, None)
            if callable(hook):
                yield name, hook

    def _plugin_ctx(self, name, peer=None, event=None):
        ctx = None
        if self._ctx_builder:
            try:
                ctx = self._ctx_builder(name)
            except Exception:
                ctx = None
        if ctx is None:
            ctx = PluginCtx(name, lambda *a: {}, lambda: {}, lambda t: None, lambda a, d: None)
        return ctx.bind(peer, event)

    # ------------------------------------------------------------- диспетчеризация
    def dispatch_message(self, event):
        for name, hook in self._for_each("on_message"):
            ctx = self._plugin_ctx(name, event.get("peer_id"), event)
            try:
                _run_hook_result(hook(ctx, dict(event)))
            except Exception as e:
                self._errors[name] = "on_message: %s" % e
                self._log("[plugins] %s: on_message: %s" % (name, e))

    def dispatch_dialog(self, dialog):
        for name, hook in self._for_each("on_dialog"):
            ctx = self._plugin_ctx(name, dialog.get("id"))
            try:
                _run_hook_result(hook(ctx, dict(dialog)))
            except Exception as e:
                self._errors[name] = "on_dialog: %s" % e
                self._log("[plugins] %s: on_dialog: %s" % (name, e))

    def dispatch_command(self, text, event):
        """Возвращает (handled, reply_text). Если плагин перехватил команду —
        исходное сообщение в чат не уходит, отвечает плагин."""
        parts = (text or "").split()
        if not parts or not parts[0].startswith("/"):
            return False, None
        cmd = parts[0][1:].split("@")[0].lower()
        for name, mod in list(self._modules.items()):
            if not self._is_enabled(name):
                continue
            hook = getattr(mod, "cmd_" + cmd, None)
            if callable(hook):
                ctx = self._plugin_ctx(name, event.get("peer_id"), event)
                r = None
                try:
                    r = hook(ctx, dict(event))
                except Exception as e:
                    self._errors[name] = "cmd_%s: %s" % (cmd, e)
                    self._log("[plugins] %s: cmd_%s: %s" % (name, cmd, e))
                if inspect.isawaitable(r):
                    _run_hook_result(r)
                    return True, None
                return True, (str(r) if r else None)
        for name, hook in self._for_each("on_command"):
            ctx = self._plugin_ctx(name, event.get("peer_id"), event)
            try:
                r = hook(ctx, dict(event))
                if inspect.isawaitable(r):
                    _run_hook_result(r)
                    return True, None
                if r:
                    return True, str(r)
            except Exception as e:
                self._errors[name] = "on_command: %s" % e
                self._log("[plugins] %s: on_command: %s" % (name, e))
        return False, None
