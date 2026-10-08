"""Notifications: always stored for the dashboard; optionally delivered via
Telegram or email. Bodies are redacted and never include source content."""
from __future__ import annotations

import logging
import smtplib
import ssl
import threading
from datetime import timedelta
from email.message import EmailMessage

import httpx

from ..db import Database, dumps, now_iso
from ..security.redact import get_redactor

log = logging.getLogger(__name__)


class Channel:
    name = "base"

    def send(self, level: str, title: str, body: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(self, token: str, chat_id: str, client: httpx.Client | None = None):
        self._token, self.chat_id = token, chat_id
        self.client = client or httpx.Client(timeout=15)

    def send(self, level, title, body):
        r = self.client.post(f"https://api.telegram.org/bot{self._token}/sendMessage",
                             json={"chat_id": self.chat_id, "text": f"[AEGIS {level.upper()}] {title}\n{body}"[:4000]})
        r.raise_for_status()


class EmailChannel(Channel):
    name = "email"

    def __init__(self, host, port, user, password, sender, to):
        self.host, self.port, self.user, self._password, self.sender, self.to = host, port, user, password, sender, to

    def send(self, level, title, body):
        msg = EmailMessage()
        msg["Subject"] = f"[AEGIS {level}] {title}"
        msg["From"] = self.sender
        msg["To"] = self.to
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=20) as s:
            s.starttls(context=ssl.create_default_context())
            if self.user:
                s.login(self.user, self._password)
            s.send_message(msg)


class Notifier:
    def __init__(self, db: Database, channels: list[Channel] | None = None):
        self.db = db
        self.channels = channels or []

    @classmethod
    def from_settings(cls, db: Database, settings) -> "Notifier":
        ch: list[Channel] = []
        if settings.telegram_bot_token and settings.telegram_chat_id:
            ch.append(TelegramChannel(settings.telegram_bot_token, settings.telegram_chat_id))
        if settings.smtp_host and settings.smtp_to:
            ch.append(EmailChannel(settings.smtp_host, settings.smtp_port, settings.smtp_user,
                                   settings.smtp_password, settings.smtp_from or settings.smtp_user, settings.smtp_to))
        return cls(db, ch)

    def notify(self, level: str, kind: str, title: str, body: str = "", dedupe_minutes: int = 60) -> int | None:
        r = get_redactor()
        title, body = r.text(title)[:300], r.text(body)[:2000]
        key = f"{kind}:{title}"
        recent = self.db.one("SELECT id FROM notifications WHERE dedupe_key = ? AND created_at >= ?",
                             (key, now_iso(timedelta(minutes=-dedupe_minutes))))
        if recent:
            return None
        nid = self.db.insert("notifications", {"level": level, "kind": kind, "title": title, "body": body,
                                               "dedupe_key": key, "created_at": now_iso()})
        if self.channels and (level in ("warning", "critical") or kind in ("objective_completed", "approval_required")):
            threading.Thread(target=self._deliver, args=(nid, level, title, body), daemon=True).start()
        return nid

    def _deliver(self, nid, level, title, body):
        delivered = []
        for ch in self.channels:
            try:
                ch.send(level, title, body)
                delivered.append(ch.name)
            except Exception as exc:
                log.warning("notification via %s failed: %s", ch.name, exc)
        if delivered:
            self.db.execute("UPDATE notifications SET delivered = ? WHERE id = ?", (dumps(delivered), nid))

    def recent(self, limit: int = 50, unread_only: bool = False) -> list[dict]:
        where = "WHERE read = 0 " if unread_only else ""
        return self.db.query(f"SELECT * FROM notifications {where}ORDER BY id DESC LIMIT ?", (limit,))

    def mark_read(self, nid: int | None = None) -> None:
        if nid is None:
            self.db.execute("UPDATE notifications SET read = 1")
        else:
            self.db.execute("UPDATE notifications SET read = 1 WHERE id = ?", (nid,))
