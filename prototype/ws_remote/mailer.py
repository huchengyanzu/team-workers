"""验证码邮件发送（Resend REST API，纯标准库）。

统一发信身份：
  From:     team-workers <no-reply@notify.example.com>
  Reply-To: support@example.com

环境变量：
  RESEND_API_KEY                 Resend API key（re_...），空 = 未配置，发信直接返回 False
  TEAM_WORKERS_EMAIL_FROM        发件人，默认 team-workers <no-reply@notify.example.com>
  TEAM_WORKERS_EMAIL_REPLY_TO     回复地址，默认 support@example.com

发送失败只记录日志、返回 False，不抛异常给上层（与原实现行为一致）。
必须在工作线程/executor 中调用，避免阻塞 asyncio 事件循环。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger("team_workers.mailer")

_RESEND_API_URL = "https://api.resend.com/emails"
_DEFAULT_FROM = "team-workers <no-reply@notify.example.com>"
_DEFAULT_REPLY_TO = "support@example.com"


def email_configured() -> bool:
    """是否配置了 Resend API key。"""
    return bool(os.environ.get("RESEND_API_KEY", "").strip())


def send_email(to_email: str, subject: str, body: str) -> bool:
    """同步发送一封纯文本邮件。成功 True，否则 False（不抛异常）。"""
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        logger.warning("Resend API key not configured; skip sending email to %s", to_email)
        return False

    from_addr = os.environ.get("TEAM_WORKERS_EMAIL_FROM", "").strip() or _DEFAULT_FROM
    reply_to = os.environ.get("TEAM_WORKERS_EMAIL_REPLY_TO", "").strip() or _DEFAULT_REPLY_TO
    payload = {
        "from": from_addr,
        "to": [to_email],
        "subject": subject,
        "text": body,
        "replyTo": reply_to,
    }
    req = urllib.request.Request(
        _RESEND_API_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Mozilla/5.0 (compatible; team-workers-mailer/1.0)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if 200 <= resp.status < 300:
                return True
            logger.error("Resend returned HTTP %s for %s", resp.status, to_email)
            return False
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        logger.error("Resend HTTP error %s for %s: %s", exc.code, to_email, detail)
        return False
    except Exception as exc:  # noqa: BLE001 - 与旧实现一致，失败只记日志
        logger.error("Resend send failed for %s: %s", to_email, exc)
        return False
