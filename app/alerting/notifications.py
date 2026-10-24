"""Mattermost webhook and optional SMTP email."""
from __future__ import annotations

import asyncio
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

import httpx

from ..integration_settings import runtime_setting

logger = logging.getLogger(__name__)


async def send_mattermost_incoming_webhook(
    client: httpx.AsyncClient,
    webhook_url: str,
    *,
    text: str,
    username: str = "ScanOps",
) -> None:
    if not webhook_url:
        return
    payload: dict[str, Any] = {
        "username": username,
        "text": text,
    }
    icon = runtime_setting("MATTERMOST_ALERT_ICON_URL", "").strip()
    if icon:
        payload["icon_url"] = icon
    r = await client.post(webhook_url, json=payload, timeout=20.0)
    r.raise_for_status()


def _send_smtp_sync(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    use_tls: bool,
    mail_from: str,
    mail_to: str,
    subject: str,
    body: str,
) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg.set_content(body)

    if use_tls:
        context = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls(context=context)
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)


async def send_alert_email(subject: str, body: str) -> None:
    host = (os.getenv("ALERT_SMTP_HOST") or "").strip()
    mail_to = (os.getenv("ALERT_EMAIL_TO") or "").strip()
    if not host or not mail_to:
        return
    port = int(os.getenv("ALERT_SMTP_PORT", "587"))
    user = (os.getenv("ALERT_SMTP_USER") or "").strip()
    password = (os.getenv("ALERT_SMTP_PASSWORD") or "").strip()
    use_tls = os.getenv("ALERT_SMTP_USE_TLS", "true").lower() in ("1", "true", "yes")
    mail_from = (os.getenv("ALERT_EMAIL_FROM") or user or "noreply@localhost").strip()

    await asyncio.to_thread(
        _send_smtp_sync,
        host=host,
        port=port,
        user=user,
        password=password,
        use_tls=use_tls,
        mail_from=mail_from,
        mail_to=mail_to,
        subject=subject,
        body=body,
    )


async def notify_long_running_job(
    *,
    text: str,
    subject: str,
) -> bool:
    """Send to Mattermost and/or email. Returns True if at least one configured channel succeeded."""
    webhook = runtime_setting("MATTERMOST_WEBHOOK_URL", "").strip()
    mail_to = (os.getenv("ALERT_EMAIL_TO") or "").strip()
    smtp_host = (os.getenv("ALERT_SMTP_HOST") or "").strip()
    want_email = bool(mail_to and smtp_host)

    mm_ok = False
    email_ok = False

    async with httpx.AsyncClient() as client:
        if webhook:
            try:
                await send_mattermost_incoming_webhook(client, webhook, text=text)
                mm_ok = True
            except Exception:
                logger.exception("Mattermost webhook failed")

    if want_email:
        try:
            await send_alert_email(subject=subject, body=text)
            email_ok = True
        except Exception:
            logger.exception("Alert email failed")

    return mm_ok or email_ok
