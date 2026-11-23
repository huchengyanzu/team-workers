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


def verification_code_html(code: str, intro: str, extra: str = "") -> str:
    """生成醒目的验证码 HTML 邮件：大号加粗验证码 + 高亮卡片。

    同时保留纯文本版本（由调用方作为 text 传入）兼容纯文本客户端。
    只使用内联样式与 table 布局，兼容 QQ 邮箱 / Outlook 等主流客户端。
    """
    import html as _html

    def _p(s: str) -> str:
        return _html.escape(s or "").replace("\n", "<br>")

    extra_html = ("<p style=\"margin:16px 0 0;font-size:13px;line-height:1.7;color:#6b7280;\">"
                  + _p(extra) + "</p>") if extra else ""
    return (
        "<div style=\"margin:0;padding:24px 0;background:#f3f4f6;font-family:-apple-system,"
        "BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;\">"
        "<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" "
        "style=\"max-width:560px;margin:0 auto;background:#ffffff;border-radius:12px;"
        "box-shadow:0 2px 8px rgba(0,0,0,.06);overflow:hidden;\">"
        "<tr><td style=\"padding:28px 32px 8px;\">"
        "<div style=\"font-size:20px;font-weight:700;color:#111827;\">team-workers</div>"
        "</td></tr>"
        "<tr><td style=\"padding:8px 32px 0;font-size:15px;line-height:1.7;color:#374151;\">"
        + _p(intro) +
        "</td></tr>"
        "<tr><td style=\"padding:20px 32px;\">"
        "<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" "
        "style=\"background:#fef2f2;border:1px solid #fecaca;border-radius:10px;\">"
        "<tr><td style=\"padding:18px 16px;text-align:center;\">"
        "<div style=\"font-size:13px;color:#991b1b;margin-bottom:8px;\">您的验证码</div>"
        "<div style=\"font-size:40px;line-height:1.2;font-weight:800;letter-spacing:8px;"
        "color:#dc2626;font-family:'SFMono-Regular',Consolas,Monaco,monospace;\">"
        + _html.escape(code) +
        "</div>"
        "</td></tr></table>"
        "</td></tr>"
        "<tr><td style=\"padding:0 32px 24px;font-size:14px;line-height:1.7;color:#374151;\">"
        "验证码 <strong style=\"color:#dc2626;\">5 分钟内有效</strong>，请尽快填写。"
        "若非本人操作请忽略此邮件。"
        + extra_html +
        "</td></tr>"
        "<tr><td style=\"padding:16px 32px;background:#f9fafb;border-top:1px solid #f3f4f6;"
        "font-size:12px;color:#9ca3af;\">—— team-workers 远程控制中枢</td></tr>"
        "</table></div>"
    )


def send_email(to_email: str, subject: str, body: str, html: str | None = None) -> bool:
    """同步发送一封邮件。传 html 时同时携带 HTML 正文（验证码更醒目），
    body 作为纯文本兜底。成功 True，否则 False（不抛异常）。"""
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
    if html:
        payload["html"] = html
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
