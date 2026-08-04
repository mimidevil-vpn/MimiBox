# -*- coding: utf-8 -*-
"""Мессенджер поверх Telethon.

Запускает Telethon в отдельном asyncio-потоке, отдаёт события в JS через
колбэк on_event (api.py превращает их в window.__tgPush) и умеет:

  * вход: api_id + api_hash, номер телефона, код, облачный пароль (2FA);
  * список диалогов, историю чата;
  * отправку текста, фото и файлов;
  * приём новых сообщений (с плагинами);
  * скачивание медиа (картинка — data:URI, файл — в папку и открыть).

Всё асинхронное живёт в одном потоке с event loop'ом, а методы из api.py
просто ставят корутины через run_coroutine_threadsafe — никаких блокировок
потока интерфейса.

Безопасность: сессия хранится локально в папке данных приложения и больше
никуда не уходит. Данные api_id/api_hash — пользовательские.
"""

import os
import re
import io
import sys
import time
import base64
import asyncio
import threading

from telethon import TelegramClient, events
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneNumberInvalidError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    FloodWaitError,
    PasswordHashInvalidError,
)
from telethon.tl.types import (
    User, Chat, Channel,
    DocumentAttributeSticker, DocumentAttributeAnimated,
    DocumentAttributeAudio, DocumentAttributeVideo, DocumentAttributeFilename,
    MessageMediaContact, MessageMediaGeo, MessageMediaVenue,
    MessageMediaPoll, MessageMediaInvoice, MessageMediaGame,
    MessageMediaWebPage,
)

# Публичная пара api_id/api_hash официального десктопного клиента Telegram.
# Используется для входа «как в официальном приложении» — только телефон + код из SMS + пароль 2FA.
# Не хранится в настройках и не видна в UI (массовое распространение сборки).
# При желании можно заменить на свою пару с https://my.telegram.org/apps.
DEFAULT_API_ID = "2040"
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"

_SESSION_FILE = "tg.session"
_MEDIA_DIR = "tg_media"

# Встроить в сообщение картинку можно до ~1.5МБ base64 — дальше WebView
# начинает заметно тормозить. Больше — сохраняем файлом и открываем.
INLINE_IMG_LIMIT = 1_500_000

_THUMB_LIMIT = 15          # миниатюр на открытие чата (чтобы не качать всё подряд)
_THUMB_MAX = 4_000_000     # оригинал больше этого — миниатюру не строим


def _norm_phone(raw: str) -> str:
    """Из «+7 900 123-45-67» и «89001234567» делает «+79001234567»."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    return "+" + digits


class TgMessenger:
    def __init__(self, session_dir, on_event, log=None,
                 get_credentials=None):
        self._dir = session_dir
        self._on_event = on_event or (lambda ev: None)
        self._log = log or (lambda *a: None)
        self._get_credentials = get_credentials  # fn() -> {"api_id":..,"api_hash":..}

        self._loop = None
        self._thread = None
        self._started = False

        self._client = None
        self._authorized = False
        self._me = None
        self._phone = ""
        self._phone_code_hash = None
        self._handler_registered = False
        self._offline = False          # сеть/прокси недоступны, сессия не потеряна
        self._logging_in = False       # идёт ручной вход — сторож не мешает
        self._auth_emitted = False     # not_authorized уже показан один раз
        self._keepalive_task = None

        # хуки, которые вешает api.py: плагины и рассылка в UI
        self._on_before_send = None   # fn(ev) -> {"handled": bool, "reply": str}
        self._on_incoming = None      # fn(ev)

        # кэши (все — только в потоке event loop'а)
        self._entities = {}           # "u123"/"c123" -> entity
        self._dialogs_cache = {}      # peer_key -> dialog dict (в порядке списка)
        self._sender_cache = {}       # user_id -> имя
        self._messages = {}           # peer_key -> [msg dict]
        self._pushed = set()          # (peer_key, msg_id) — уже отдали в UI

    # ------------------------------------------------------------- lifecycle
    def start(self):
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name="tg-async")
        self._thread.start()

    def _run_loop(self):
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._bootstrap())
            self._loop.run_forever()
        except Exception as e:
            self._log("[tg] авария цикла: %s" % e)
        finally:
            try:
                if self._loop:
                    self._loop.close()
            except Exception:
                pass
            self._loop = None

    async def _bootstrap(self):
        """При старте: пробуем восстановить сессию.

        Если Telegram сейчас недоступен (нет VPN/сети) — сессия НЕ теряется и
        экран входа НЕ показывается: уходим в фоновый keepalive и, как только
        сеть вернётся, восстанавливаемся автоматически.
        """
        api_id, api_hash = self._resolve_creds()
        if not api_id or not api_hash:
            self._auth_emitted = True
            self._emit({"type": "need_credentials"})
            return
        try:
            self._client = self._new_client(api_id, api_hash)
            await self._client.connect()
        except Exception as e:
            self._log("[tg] подключение не удалось: %s" % e)
            self._offline = True
            self._emit({"type": "tg_offline"})
            self._schedule_keepalive()
            return
        try:
            ok = await self._client.is_user_authorized()
        except Exception as e:
            self._emit_error("auth", str(e))
            self._schedule_keepalive()
            return
        if ok:
            await self._after_login()
        else:
            self._auth_emitted = True
            self._emit({"type": "not_authorized"})
        self._schedule_keepalive()

    def _schedule_keepalive(self):
        """Один фоновый сторож на всё время жизни клиента."""
        if self._keepalive_task is not None or self._loop is None:
            return
        self._keepalive_task = self._loop.create_task(self._keepalive())

    async def _keepalive(self):
        """Держит соединение с Telegram живым.

        При сбросе VPN-соединения TCP-связь Telethon рвётся. Вместо того чтобы
        «закрыть сессию» (показать вход заново), переподключаемся в фоне —
        как только сеть поднимется, работа продолжается на той же сессии.
        """
        while True:
            try:
                await asyncio.sleep(12)
                c = self._client
                if c is None:
                    continue
                if not c.is_connected():
                    if self._logging_in:
                        continue
                    try:
                        await c.connect()
                    except Exception:
                        continue
                    if self._offline:
                        self._offline = False
                        self._emit({"type": "tg_online"})
                if self._authorized or self._logging_in:
                    continue
                try:
                    ok = await c.is_user_authorized()
                except Exception:
                    continue
                if ok:
                    await self._after_login()
                elif not self._auth_emitted:
                    self._auth_emitted = True
                    self._emit({"type": "not_authorized"})
            except Exception as e:
                self._log("[tg] keepalive: %s" % e)

    # ------------------------------------------------------------- helpers
    def _session_path(self):
        return os.path.join(self._dir, _SESSION_FILE)

    def _new_client(self, api_id, api_hash):
        """Клиент Telethon с брендированным устройством в списке сессий."""
        try:
            return TelegramClient(
                self._session_path(), api_id, api_hash,
                device_model="MimiBox",
                system_version="Windows",
                app_version="4.0.0",
                lang_code="en",
                system_lang_code="en",
                connection_retries=2,
                retry_delay=3,
            )
        except TypeError:
            try:
                return TelegramClient(
                    self._session_path(), api_id, api_hash,
                    connection_retries=2,
                    retry_delay=3,
                )
            except TypeError:
                return TelegramClient(self._session_path(), api_id, api_hash)

    def _media_dir(self):
        p = os.path.join(self._dir, _MEDIA_DIR)
        try:
            os.makedirs(p, exist_ok=True)
        except Exception:
            pass
        return p

    def _resolve_creds(self, api_id="", api_hash=""):
        """Берёт переданные, потом из настроек, потом константы."""
        api_id = str(api_id or "").strip()
        api_hash = str(api_hash or "").strip()
        if not api_id or not api_hash:
            try:
                d = self._get_credentials() if self._get_credentials else {}
                d = d or {}
                api_id = str(d.get("api_id", "") or "").strip()
                api_hash = str(d.get("api_hash", "") or "").strip()
            except Exception:
                pass
        if not api_id or not api_hash:
            api_id = str(DEFAULT_API_ID or "").strip()
            api_hash = str(DEFAULT_API_HASH or "").strip()
        try:
            api_id = int(api_id)
        except (TypeError, ValueError):
            return 0, ""
        return api_id, api_hash

    def _schedule(self, coro):
        if self._loop is None:
            return False
        try:
            asyncio.run_coroutine_threadsafe(coro, self._loop)
            return True
        except Exception:
            return False

    def is_running(self):
        return self._loop is not None

    def is_authorized(self):
        return self._authorized

    def status(self):
        return {
            "authorized": self._authorized,
            "phone": self._phone,
            "running": self.is_running(),
            "offline": self._offline,
        }

    def me_json(self):
        if not self._me:
            return {}
        m = self._me
        return {
            "id": getattr(m, "id", 0),
            "first": getattr(m, "first_name", "") or "",
            "last": getattr(m, "last_name", "") or "",
            "username": getattr(m, "username", "") or "",
            "title": self._entity_title(m),
        }

    # ------------------------------------------------------- async dispatch
    async def _call_hook(self, fn, ev):
        if fn is None:
            return None
        try:
            r = fn(ev)
            if asyncio.iscoroutine(r):
                r = await r
            return r
        except Exception as e:
            self._log("[tg] хук: %s" % e)
            return None

    def set_hooks(self, on_before_send=None, on_incoming=None):
        self._on_before_send = on_before_send
        self._on_incoming = on_incoming

    # -------------------------------------------------------------- emit
    def _emit(self, ev):
        try:
            self._on_event(ev)
        except Exception as e:
            self._log("[tg] push: %s" % e)

    def _emit_error(self, op, text):
        self._log("[tg] %s: %s" % (op, text))
        self._emit({"type": "error", "op": op, "text": str(text)})

    # ------------------------------------------------------------- entities
    @staticmethod
    def _peer_key(ent):
        if isinstance(ent, Channel):
            return "c%d" % ent.id
        if isinstance(ent, Chat):
            return "g%d" % ent.id
        if isinstance(ent, User):
            return "u%d" % ent.id
        return "x%d" % getattr(ent, "id", 0)

    @staticmethod
    def _peer_type(ent):
        if isinstance(ent, Channel):
            return "group" if (getattr(ent, "megagroup", False)
                               or getattr(ent, "gigagroup", False)) else "channel"
        if isinstance(ent, Chat):
            return "group"
        if isinstance(ent, User):
            return "bot" if getattr(ent, "bot", False) else "user"
        return "chat"

    @staticmethod
    def _entity_title(ent):
        if ent is None:
            return ""
        t = getattr(ent, "first_name", "") or ""
        if getattr(ent, "last_name", ""):
            t = (t + " " + ent.last_name).strip()
        if not t:
            t = getattr(ent, "title", "") or ""
        if not t:
            t = getattr(ent, "username", "") or ("id" + str(getattr(ent, "id", "")))
        return t

    def _get_entity(self, key):
        if key == "me":
            return self._me
        return self._entities.get(key)

    def _sender_name(self, sender_id):
        if sender_id is None:
            return ""
        name = self._sender_cache.get(sender_id)
        if name:
            return name
        ent = self._entities.get("u%d" % sender_id)
        return self._entity_title(ent) if ent else ""

    # ------------------------------------------------------------- message json
    def _media_info(self, m):
        if getattr(m, "photo", None) is not None:
            return {"kind": "photo", "mime": "image/jpeg", "size": 0, "file": ""}
        media = getattr(m, "media", None)
        if media is None:
            return None
        doc = getattr(media, "document", None)
        if doc is not None:
            kind, fname = "file", ""
            for a in (doc.attributes or []):
                if isinstance(a, DocumentAttributeSticker):
                    kind = "sticker"
                    break
                if isinstance(a, DocumentAttributeAnimated):
                    kind = "gif"
                elif isinstance(a, DocumentAttributeAudio) and getattr(a, "voice", False):
                    kind = "voice"
                elif isinstance(a, DocumentAttributeAudio):
                    kind = "audio"
                elif isinstance(a, DocumentAttributeVideo):
                    kind = "video"
                elif isinstance(a, DocumentAttributeFilename) and a.file_name:
                    fname = a.file_name
            return {"kind": kind, "mime": getattr(doc, "mime_type", "") or "",
                    "size": int(getattr(doc, "size", 0) or 0), "file": fname}
        for t, kind in ((MessageMediaContact, "contact"), (MessageMediaGeo, "location"),
                        (MessageMediaVenue, "location"), (MessageMediaPoll, "poll"),
                        (MessageMediaInvoice, "invoice"), (MessageMediaGame, "game")):
            if isinstance(media, t):
                return {"kind": kind, "mime": "", "size": 0, "file": ""}
        if isinstance(media, MessageMediaWebPage):
            return None  # текст уже в message, отдельная карточка не нужна
        return {"kind": "file", "mime": "", "size": 0, "file": ""}

    def _msg_json(self, m, chat=None):
        out = bool(getattr(m, "out", False))
        sender_id = getattr(m, "sender_id", None)
        sender_name = ""
        if out:
            sender_name = self._entity_title(self._me)
        elif sender_id is not None:
            sender_name = self._sender_name(sender_id)
        if not sender_name and chat is not None:
            sender_name = self._entity_title(chat)
        return {
            "id": int(m.id),
            "out": out,
            "text": getattr(m, "message", "") or "",
            "date": int(m.date.timestamp()) if getattr(m, "date", None) else 0,
            "sender": sender_name,
            "sender_id": sender_id,
            "media": self._media_info(m),
            "reply_to": int(m.reply_to_msg_id) if getattr(m, "reply_to_msg_id", None) else None,
        }

    def _summary_text(self, ev):
        if ev.get("text"):
            return ev["text"][:140]
        mi = ev.get("media")
        if mi:
            labels = {"photo": "Фото", "gif": "GIF", "sticker": "Стикер",
                      "voice": "Голосовое", "video": "Видео", "audio": "Аудио",
                      "contact": "Контакт", "location": "Геолокация",
                      "poll": "Опрос", "invoice": "Платёж", "game": "Игра"}
            label = labels.get(mi.get("kind"), "Файл")
            if mi.get("file"):
                label += ": " + mi["file"]
            return label
        return ""

    # ------------------------------------------------------------- dialogs
    async def _refresh_dialogs(self):
        if not self._client:
            return
        try:
            async for d in self._client.iter_dialogs(limit=200):
                ent = d.entity
                key = self._peer_key(ent)
                self._entities[key] = ent
                if isinstance(ent, User):
                    self._sender_cache[ent.id] = self._entity_title(ent)
                self._dialogs_cache[key] = self._dialog_json(d, ent)
        except Exception as e:
            self._emit_error("dialogs", str(e))
            return
        self._emit({"type": "dialogs", "dialogs": self._dialogs_list()})
        self._schedule(self._dialogs_avatars())

    async def _dialogs_avatars(self):
        """Подтягиваем аватарки всех диалогов сразу (без открытия чата)."""
        keys = list(self._dialogs_cache.keys())
        for key in keys[:50]:
            await self._avatar_flow(key)

    @staticmethod
    def _chat_muted(notify) -> bool:
        """Чат с выключенными уведомлениями.

        У Telegram «замьючен навсегда» — это mute_until = 2^31-1, временный мьют —
        будущая метка времени, снятый мьют — 0/None. Сравниваем с текущим временем,
        чтобы протухший мьют не считался активным.
        """
        try:
            until = int(getattr(notify, "mute_until", 0) or 0)
        except (TypeError, ValueError):
            return False
        return until > 0 and until > int(time.time())

    def _dialog_json(self, d, ent):
        last = ""
        if d.message is not None:
            last = self._summary_text(self._msg_json(d.message, ent))
        raw = getattr(d, "dialog", None) or d
        notify = getattr(raw, "notify_settings", None)
        return {
            "id": self._peer_key(ent),
            "title": self._entity_title(ent),
            "type": self._peer_type(ent),
            "unread": int(getattr(d, "unread_count", 0) or 0),
            "last": last,
            "date": int(d.date.timestamp()) if getattr(d, "date", None) else 0,
            "username": getattr(ent, "username", "") or "",
            "archived": bool(getattr(d, "folder_id", None) == 1),
            "muted": self._chat_muted(notify),
        }

    def _dialogs_list(self):
        return list(self._dialogs_cache.values())

    def _bump_dialog(self, peer_key, ev):
        d = self._dialogs_cache.get(peer_key)
        if d is None:
            return
        d["last"] = self._summary_text(ev)
        d["date"] = ev.get("date") or d.get("date") or 0
        d["unread"] = int(d.get("unread", 0) or 0) + (0 if ev.get("out") else 1)
        self._dialogs_cache.pop(peer_key)
        self._dialogs_cache[peer_key] = d
        self._emit({"type": "dialogs", "dialogs": self._dialogs_list()})

    # ------------------------------------------------------------- chat
    async def _open_chat(self, peer_key):
        entity = self._get_entity(peer_key)
        if entity is None:
            self._emit_error("open", "no_entity")
            return
        msgs = []
        try:
            async for m in self._client.iter_messages(entity, limit=100, reverse=True):
                msgs.append(self._msg_json(m, entity))
        except Exception as e:
            self._emit_error("messages", str(e))
            return
        # метаданные о тех, кого нет в кэше — добьём в фоне
        unknown = [m["sender_id"] for m in msgs
                   if not m["out"] and m["sender_id"] and not self._sender_cache.get(m["sender_id"])]
        self._messages[peer_key] = msgs
        self._emit({"type": "messages", "peer_id": peer_key,
                    "messages": msgs, "peer": self._peer_json(entity)})
        # миниатюры фото (до лимита)
        count = 0
        for m in msgs:
            if m.get("media") and m["media"].get("kind") == "photo" and not m["out"]:
                count += 1
                if count > _THUMB_LIMIT:
                    break
                self._schedule(self._thumb_flow(peer_key, m["id"]))
        if unknown:
            self._schedule(self._enrich_senders(peer_key, unknown))
        self._schedule(self._avatar_flow(peer_key))

    def _peer_json(self, ent):
        return {
            "id": self._peer_key(ent),
            "title": self._entity_title(ent),
            "type": self._peer_type(ent),
        }

    async def _enrich_senders(self, peer_key, ids):
        out = {}
        for sid in ids[:40]:
            try:
                ent = await self._client.get_entity(sid)
                name = self._entity_title(ent)
                self._sender_cache[sid] = name
                out[str(sid)] = name
            except Exception:
                continue
        if out:
            self._emit({"type": "senders", "peer_id": peer_key, "senders": out})

    # ------------------------------------------------------------- send
    async def _send_flow(self, peer_key, text):
        entity = self._get_entity(peer_key)
        if entity is None:
            self._emit_error("send", "no_entity")
            return
        ev = {"peer_id": peer_key, "peer_title": self._entity_title(entity),
              "text": text or ""}
        res = await self._call_hook(self._on_before_send, ev)
        if res and res.get("handled"):
            return
        try:
            msg = await self._client.send_message(entity, text)
        except Exception as e:
            self._emit_error("send", str(e))
            return
        self._push_message(peer_key, self._msg_json(msg), "sent")

    async def asend_text(self, peer_key, text):
        """Прямая отправка (без перехвата команд) — для плагинов."""
        entity = self._get_entity(peer_key)
        if entity is None:
            return {"error": "no_entity"}
        try:
            msg = await self._client.send_message(entity, text)
        except Exception as e:
            return {"error": str(e)}
        self._push_message(peer_key, self._msg_json(msg), "sent")
        return {"ok": True}

    async def _send_file_flow(self, peer_key, path, caption=""):
        entity = self._get_entity(peer_key)
        if entity is None:
            self._emit_error("send_file", "no_entity")
            return
        try:
            msg = await self._client.send_file(entity, path, caption=caption or "")
        except Exception as e:
            self._emit_error("send_file", str(e))
            return
        finally:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
        self._push_message(peer_key, self._msg_json(msg), "sent")

    def _push_message(self, peer_key, ev, ev_type):
        key = (peer_key, ev["id"])
        if key in self._pushed:
            return
        if len(self._pushed) > 5000:
            self._pushed.clear()
        self._pushed.add(key)
        lst = self._messages.get(peer_key)
        if lst is not None:
            lst.append(ev)
        # флаг «уведомления выключены» едет вместе с сообщением, чтобы JS
        # не показывал тост для замьюченных чатов (в т.ч. если чата ещё нет в списке)
        dlg = self._dialogs_cache.get(peer_key)
        ev["muted"] = bool(dlg.get("muted", False)) if dlg else False
        self._bump_dialog(peer_key, ev)
        self._emit({"type": ev_type, "peer_id": peer_key, **ev})

    # ------------------------------------------------------------- incoming
    def _register_handler(self):
        if self._handler_registered or not self._client:
            return
        self._handler_registered = True
        try:
            self._client.add_event_handler(self._on_new_message,
                                           events.NewMessage())
        except Exception as e:
            self._log("[tg] не удалось повесить приём: %s" % e)

    async def _on_new_message(self, event):
        try:
            chat = await event.get_chat()
        except Exception:
            chat = None
        if chat is None:
            return
        msg = event.message
        if msg is None:
            return
        peer_key = self._peer_key(chat)
        self._entities[peer_key] = chat
        if isinstance(chat, User):
            self._sender_cache[chat.id] = self._entity_title(chat)
        ev = self._msg_json(msg, chat)
        ev["peer_title"] = self._entity_title(chat)

        if not ev["out"]:
            await self._call_hook(self._on_incoming, ev)
        self._push_message(peer_key, ev, "message")

    # ------------------------------------------------------------- media
    async def _thumb_flow(self, peer_key, msg_id):
        entity = self._get_entity(peer_key)
        if entity is None:
            return
        try:
            msg = await self._client.get_messages(entity, ids=msg_id)
        except Exception:
            return
        if msg is None or msg.photo is None:
            return
        try:
            data = await self._client.download_media(msg, file=bytes)
        except Exception:
            return
        if not data or len(data) > _THUMB_MAX:
            return
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(data)).convert("RGB")
            img.thumbnail((256, 256), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=78)
            thumb = base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            return
        self._emit({"type": "thumb", "peer_id": peer_key, "msg_id": msg_id,
                    "data": "data:image/jpeg;base64," + thumb})

    async def _media_flow(self, peer_key, msg_id):
        entity = self._get_entity(peer_key)
        if entity is None:
            self._emit_error("media", "no_entity")
            return
        try:
            msg = await self._client.get_messages(entity, ids=msg_id)
        except Exception as e:
            self._emit_error("media", str(e))
            return
        if msg is None or msg.media is None:
            self._emit({"type": "media", "peer_id": peer_key, "msg_id": msg_id,
                        "error": "no_media"})
            return
        mi = self._media_info(msg) or {}
        if mi.get("kind") == "voice":
            # голосовое — сразу в чат проигрывателем (data:URI), без сохранения
            try:
                data = await self._client.download_media(msg, file=bytes)
            except Exception as e:
                self._emit_error("media", str(e))
                return
            if data:
                mime = mi.get("mime") or "audio/ogg"
                self._emit({"type": "media", "peer_id": peer_key, "msg_id": msg_id,
                            "kind": "voice", "mime": mime,
                            "data": "data:%s;base64,%s"
                                    % (mime, base64.b64encode(data).decode("ascii"))})
                return
        if mi.get("kind") == "photo":
            try:
                data = await self._client.download_media(msg, file=bytes)
            except Exception as e:
                self._emit_error("media", str(e))
                return
            if data and len(data) < INLINE_IMG_LIMIT:
                self._emit({"type": "media", "peer_id": peer_key, "msg_id": msg_id,
                            "mime": "image/jpeg",
                            "data": "data:image/jpeg;base64," +
                                    base64.b64encode(data).decode("ascii")})
                return
            # большая картинка — сохраняем и открываем файлом
            path = self._save_msg_file(peer_key, msg_id, mi.get("file") or "photo.jpg")
            try:
                p = await self._client.download_media(msg, file=path)
            except Exception as e:
                self._emit_error("media", str(e))
                return
            self._emit({"type": "media", "peer_id": peer_key, "msg_id": msg_id,
                        "path": p or path, "name": mi.get("file") or "photo.jpg"})
            return
        # всё остальное — файлом
        name = mi.get("file") or ("media_%d" % msg_id)
        path = self._save_msg_file(peer_key, msg_id, name)
        try:
            p = await self._client.download_media(msg, file=path)
        except Exception as e:
            self._emit_error("media", str(e))
            return
        self._emit({"type": "media", "peer_id": peer_key, "msg_id": msg_id,
                    "path": p or path, "name": name})

    def _save_msg_file(self, peer_key, msg_id, name):
        safe = re.sub(r"[^\w.\- ]", "_", str(name))
        return os.path.join(self._media_dir(), "%s_%d_%s" % (peer_key, msg_id, safe))

    async def _avatar_flow(self, peer_key):
        ent = self._get_entity(peer_key)
        if ent is None:
            return
        try:
            data = await self._client.download_profile_photo(ent, file=bytes)
        except Exception:
            data = None
        if not data:
            self._emit({"type": "avatar", "peer_id": peer_key, "data": ""})
            return
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(data)).convert("RGB")
            img.thumbnail((120, 120), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=82)
            data = base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            data = base64.b64encode(data).decode("ascii")
        self._emit({"type": "avatar", "peer_id": peer_key,
                    "data": "data:image/jpeg;base64," + data})

    def _brand_icon(self):
        """Фирменная иконка MimiBox (ui/app_icon.png) из ресурсов приложения."""
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(base, "ui", "app_icon.png")
        if not os.path.exists(path):
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "ui", "app_icon.png")
        try:
            with open(path, "rb") as f:
                return f.read()
        except Exception:
            return b""

    async def _set_avatar_flow(self, image_b64=""):
        """Ставит аватар аккаунта: свой файл или фирменную иконку MimiBox."""
        if not self._client or not self._client.is_connected():
            self._emit_error("set_avatar", "not_authorized")
            return
        raw = b""
        if image_b64:
            try:
                if "," in image_b64:
                    image_b64 = image_b64.split(",", 1)[1]
                raw = base64.b64decode(image_b64)
            except Exception as e:
                self._emit_error("set_avatar", str(e))
                return
        if not raw:
            raw = self._brand_icon()
        if not raw:
            self._emit_error("set_avatar", "no_icon")
            return
        try:
            from telethon.tl.functions.photos import UploadProfilePhotoRequest
            file = await self._client.upload_file(io.BytesIO(raw),
                                                  file_name="avatar.png")
            await self._client(UploadProfilePhotoRequest(file=file))
        except Exception as e:
            self._log("[tg] set_avatar: %s" % e)
            self._emit_error("set_avatar", str(e))
            return
        self._emit({"type": "avatar_set"})

    # ------------------------------------------------------------- login
    async def _login_start(self, phone, api_id="", api_hash=""):
        api_id, api_hash = self._resolve_creds(api_id, api_hash)
        if not api_id or not api_hash:
            self._emit({"type": "need_credentials"})
            return
        self._logging_in = True
        self._offline = False
        if self._client is None:
            self._client = self._new_client(api_id, api_hash)
        if not self._client.is_connected():
            try:
                await self._client.connect()
            except Exception as e:
                self._emit_error("connect", str(e))
                return
        self._phone = _norm_phone(phone)
        if not self._phone:
            self._emit_error("login", "bad_phone")
            return
        try:
            sent = await self._client.send_code_request(self._phone)
        except PhoneNumberInvalidError:
            self._emit_error("login", "bad_phone")
            return
        except FloodWaitError as e:
            self._emit_error("login", "flood_%ds" % getattr(e, "seconds", 0))
            return
        except Exception as e:
            self._emit_error("login", str(e))
            return
        self._phone_code_hash = getattr(sent, "phone_code_hash", None)
        self._emit({"type": "login_code", "phone": self._phone})

    async def _login_code(self, code):
        if not self._client:
            return
        code = (code or "").strip()
        try:
            me = await self._client.sign_in(
                self._phone, code, phone_code_hash=self._phone_code_hash)
        except SessionPasswordNeededError:
            self._emit({"type": "login_password"})
            return
        except PhoneCodeInvalidError:
            self._emit_error("login", "bad_code")
            return
        except PhoneCodeExpiredError:
            self._emit_error("login", "code_expired")
            return
        except FloodWaitError as e:
            self._emit_error("login", "flood_%ds" % getattr(e, "seconds", 0))
            return
        except Exception as e:
            self._emit_error("login", str(e))
            return
        await self._after_login(me)

    async def _login_password(self, password):
        if not self._client:
            return
        try:
            me = await self._client.sign_in(password=password or "")
        except PasswordHashInvalidError:
            self._emit_error("login", "bad_password")
            return
        except FloodWaitError as e:
            self._emit_error("login", "flood_%ds" % getattr(e, "seconds", 0))
            return
        except Exception as e:
            self._emit_error("login", str(e))
            return
        await self._after_login(me)

    async def _after_login(self, me=None):
        try:
            if me is None:
                me = await self._client.get_me()
        except Exception as e:
            self._emit_error("login", str(e))
            return
        self._me = me
        self._authorized = True
        self._logging_in = False
        self._offline = False
        self._auth_emitted = False
        self._register_handler()
        self._emit({"type": "ready", "me": self.me_json()})
        await self._refresh_dialogs()
        self._schedule(self._avatar_flow("me"))

    async def _logout(self):
        self._authorized = False
        self._me = None
        self._phone = ""
        self._dialogs_cache.clear()
        self._entities.clear()
        self._sender_cache.clear()
        self._messages.clear()
        self._pushed.clear()
        try:
            if self._client and self._client.is_connected():
                await self._client.log_out()
        except Exception as e:
            self._log("[tg] logout: %s" % e)
        self._emit({"type": "not_authorized"})

    # ------------------------------------------------------------- public API
    def login(self, phone, api_id="", api_hash=""):
        return self._schedule(self._login_start(phone, api_id, api_hash))

    def code(self, code):
        return self._schedule(self._login_code(code))

    def password(self, pwd):
        return self._schedule(self._login_password(pwd))

    def logout(self):
        return self._schedule(self._logout())

    def set_avatar(self, image_b64=""):
        return self._schedule(self._set_avatar_flow(image_b64 or ""))

    def news_fetch(self, channel):
        return self._schedule(self._news_flow(channel))

    async def _news_flow(self, channel):
        """Fallback новостей: тянем последний пост канала через Telegram-сессию."""
        if not self._client or not self._client.is_connected():
            self._emit({"type": "news_error"})
            return
        try:
            msgs = await self._client.get_messages(channel, limit=1)
        except Exception as e:
            self._log("[tg] news fallback: %s" % e)
            self._emit({"type": "news_error"})
            return
        m = msgs[0] if msgs else None
        if m is None:
            self._emit({"type": "news_error"})
            return
        import html as _html
        body = _html.escape(m.text or "")
        body = re.sub(r"&#x27;", "'", body)
        body = re.sub(r"(https?://[^\s<]+)", r'<a href="\1" target="_blank" rel="noopener">\1</a>', body)
        body = body.replace("\n", "<br>")
        date = ""
        try:
            if m.date:
                date = m.date.isoformat()
        except Exception:
            pass
        self._emit({
            "type": "news",
            "channel": channel,
            "post_id": "tg_%d" % (getattr(m, "id", 0) or 0),
            "date": date,
            "html": body,
        })

    def refresh_dialogs(self):
        return self._schedule(self._refresh_dialogs())

    async def _archive_flow(self, peer_key, on):
        entity = self._get_entity(peer_key)
        if entity is None:
            self._emit_error("archive", "no_entity")
            return
        try:
            await self._client.edit_folder(entity, 1 if on else 0)
        except Exception as e:
            self._emit_error("archive", str(e))
            return
        await self._refresh_dialogs()

    def set_archive(self, peer_key, on):
        return self._schedule(self._archive_flow(peer_key, bool(on)))

    def open_chat(self, peer_key):
        return self._schedule(self._open_chat(peer_key))

    def send(self, peer_key, text):
        return self._schedule(self._send_flow(peer_key, text))

    def send_file(self, peer_key, path, caption=""):
        return self._schedule(self._send_file_flow(peer_key, path, caption))

    def download(self, peer_key, msg_id):
        return self._schedule(self._media_flow(peer_key, msg_id))

    def avatar(self, peer_key):
        return self._schedule(self._avatar_flow(peer_key))
