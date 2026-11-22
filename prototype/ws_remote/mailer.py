"""SMTP 邮件发送（纯标准库）。

从 myclaw_service 的 _send_reset_email 移植而来，改为通用发信函数：
- 配置从环境变量读取 NEXUS_SMTP_*（避免与 myclaw 的 MYCLAW_SMTP_* 冲突）
- 465 走 SMTP_SSL，其它端口走 SMTP + STARTTLS
- 发送失败只记录日志、返回 False，不抛异常给上层（与 myclaw 行为一致）

环境变量：
  NEXUS_SMTP_HOST      SMTP 服务器（空 = 未配置，发信直接返回 False）
  NEXUS_SMTP_PORT      端口，默认 465
  NEXUS_SMTP_USER      登录账号
  NEXUS_SMTP_PASSWORD  授权码/密码
  NEXUS_SMTP_FROM      发件人地址（默认同 USER）
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.mime.text import MIMEText

logger = logging.getLogger("team_workers.mailer")


def smtp_configured() -> bool:
    """是否配置了 SMTP（host + user + password 齐全）。"""
    return bool(
        os.environ.get("NEXUS_SMTP_HOST")
        and os.environ.get("NEXUS_SMTP_USER")
        and os.environ.get("NEXUS_SMTP_PASSWORD")
    )


def send_email(to_email: str, subject: str, body: str) -> bool:
    """同步发送一封纯文本邮件。成功 True，失败 False（不抛异常）。

    必须在工作线程/executor 中调用，避免阻塞 asyncio 事件循环。
    """
    host = os.environ.get("NEXUS_SMTP_HOST", "").strip()
    user = os.environ.get("NEXUS_SMTP_USER", "").strip()
    password = os.environ.get("NEXUS_SMTP_PASSWORD", "").strip()
    from_addr = os.environ.get("NEXUS_SMTP_FROM", "").strip() or user
    try:
        port = int(os.environ.get("NEXUS_SMTP_PORT", "465"))
    except ValueError:
        port = 465

    if not (host and user and password):
        logger.warning("SMTP not configured; skip sending email to %s", to_email)
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_email

    server = None
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            server.starttls()
        server.login(user, password)
        server.sendmail(from_addr, [to_email], msg.as_string())
        return True
    except Exception as exc:  # noqa: BLE001 - 与 myclaw 一致，失败只记日志
        logger.error("Failed to send email to %s: %s", to_email, exc)
        return False
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass
