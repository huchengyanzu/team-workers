#!/usr/bin/env python3
"""容器化设备端本地策略钩子（服务端无感）。

确定性静态规则把工具调用分为绿/黄/红三级，并按 mode 决定放行/确认：
  - off        ：一律 allow（仍返回 risk 供审计）
  - audit_only ：一律 allow（risk 照记）
  - enforce    ：绿 allow；黄 ack_required（带 ack_risk=true 则 allow）；
                 红 confirm_required（带有效一次性 confirm_token 则 allow）

高危红级令牌一次一密、60 秒过期、消费即焚。审计以 JSONL 落地到本地，
只记录 command 的 sha256 前 16 位，不落原始命令全文（避免敏感参数入日志）。
纯标准库实现，任何失败都调用方（device.py）降级 allow，不影响设备可用性。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

# 危险标志：命中即红（大小写不敏感子串匹配）
DANGER_FLAGS = (
    "rm -rf", "mkfs", "dd if=", "reboot", "shutdown", "poweroff",
    "> /dev/", "--privileged", "chmod 777 /", "chown -r",
)
# 中风险标志：命中即黄
YELLOW_FLAGS = (
    "docker start", "docker stop", "docker restart", "docker pull",
    "systemctl start", "systemctl stop", "systemctl restart",
    "apt install", "kill -9",
)
# 高危 exec 标志（容器/包管理/下载即执行）
RED_EXEC_FLAGS = ("docker rm", "docker rmi", "docker system prune")
RED_PIPE_PATTERNS = (
    r"curl\b[^\n]*\|\s*(?:ba)?sh\b",
    r"wget\b[^\n]*\|\s*(?:ba)?sh\b",
)

TOKEN_TTL = 60.0
HASH_LEN = 16


class DockerPolicy:
    def __init__(self, mode: str, audit_path: str | None = None):
        self.mode = mode if mode in ("off", "audit_only", "enforce") else "off"
        if audit_path:
            try:
                self.audit_path = Path(os.path.expanduser(str(audit_path)))
            except Exception:
                self.audit_path = Path.home() / ".team-workers" / "policy-audit.jsonl"
        else:
            self.audit_path = Path.home() / ".team-workers" / "policy-audit.jsonl"
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._lock = threading.Lock()
        # {token: (payload_hash, expire_ts)}
        self._tokens: dict[str, tuple[str, float]] = {}

    # ---------- 辅助 ----------
    @staticmethod
    def _command_text(arguments: dict) -> str | None:
        """返回命令文本用于匹配/预览；command 非 str/list 时返回 None（视为无法判断）。"""
        if not isinstance(arguments, dict):
            return None
        cmd = arguments.get("command")
        if isinstance(cmd, str):
            return cmd
        if isinstance(cmd, (list, tuple)):
            return " ".join(str(x) for x in cmd)
        return None

    @staticmethod
    def _payload_hash(tool: str, arguments: dict) -> str:
        """规范化 JSON（去掉 confirm_token）后取 sha256 前 16 位。"""
        args = dict(arguments) if isinstance(arguments, dict) else {}
        args.pop("confirm_token", None)
        canonical = json.dumps({"tool": tool, "arguments": args}, sort_keys=True,
                               separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:HASH_LEN]

    def _classify(self, tool: str, arguments: dict) -> str:
        """确定性静态分级：green / yellow / red。"""
        if tool in ("file_read", "file_list"):
            return "green"
        if tool == "file_write":
            return "yellow"
        if tool == "exec":
            text = self._command_text(arguments)
            if not text or not text.strip():
                return "red"  # 无法判断 → 红级兜底
            lower = text.lower()
            for flag in DANGER_FLAGS:
                if flag in lower:
                    return "red"
            for flag in RED_EXEC_FLAGS:
                if flag in lower:
                    return "red"
            for pat in RED_PIPE_PATTERNS:
                if re.search(pat, text, re.IGNORECASE):
                    return "red"
            for flag in YELLOW_FLAGS:
                if flag in lower:
                    return "yellow"
            return "green"
        # 其它非 exec/文件工具：无规则，默认放行为绿，避免影响可用性
        return "green"

    def _new_confirm(self, tool: str, arguments: dict) -> tuple[str, str, float]:
        """生成并登记一次性令牌。调用方须持有 self._lock。"""
        token = uuid.uuid4().hex[:16]
        payload_hash = self._payload_hash(tool, arguments)
        expires = time.time() + TOKEN_TTL
        self._tokens[token] = (payload_hash, expires)
        text = self._command_text(arguments) or ""
        preview = "将对宿主执行高危命令: %s" % text[:120]
        return token, preview, expires

    def issue_confirm_token(self, tool: str, arguments: dict, reason: str) -> dict:
        """异常兜底签发一次性确认令牌，供 device.py 策略异常时 fail-closed 使用。

        复用 check() 的令牌表（同一 TTL、消费即焚），用户重试带 confirm_token 时
        check() 能正常校验并放行，不会因异常路径陷入无限确认。
        """
        if not isinstance(arguments, dict):
            arguments = {}
        with self._lock:
            token, preview, expires = self._new_confirm(tool, arguments)
        return {"action": "confirm_required", "risk": "red", "reason": reason,
                "confirm_token": token, "preview": preview, "expires_at": expires}

    # ---------- 策略判定 ----------
    def check(self, tool: str, arguments: dict) -> dict:
        # 归一化：arguments 非 dict（如 ["echo","hi"]、字符串）时按空参处理 →
        # exec 无 command 会走「无法判断→红」兜底返回 confirm_required，
        # 畸形帧绝不被判成绿级；配合 device.py 的 fail-closed 异常兜底。
        if not isinstance(arguments, dict):
            arguments = {}
        risk = self._classify(tool, arguments)

        # off / audit_only：一律放行，风险照记
        if self.mode != "enforce":
            return {"action": "allow", "risk": risk, "reason": ""}

        with self._lock:
            if risk == "green":
                return {"action": "allow", "risk": risk, "reason": ""}

            if risk == "yellow":
                if arguments.get("ack_risk") is True:
                    return {"action": "allow", "risk": risk, "reason": ""}
                return {"action": "ack_required", "risk": risk,
                        "reason": "该操作有风险，请在 arguments 中带 ack_risk=true 确认"}

            # ---- red ----
            token = arguments.get("confirm_token")
            if token:
                key = str(token)
                entry = self._tokens.get(key)
                if (entry and entry[1] > time.time()
                        and entry[0] == self._payload_hash(tool, arguments)):
                    del self._tokens[key]  # 消费即焚
                    return {"action": "allow", "risk": risk, "reason": "",
                            "confirm_used": True}
                # 令牌无效/过期/命令被篡改 → 发新令牌，不复用旧 token
                new_token, preview, expires = self._new_confirm(tool, arguments)
                return {"action": "confirm_required", "risk": risk,
                        "reason": "确认令牌无效或已过期",
                        "confirm_token": new_token, "preview": preview, "expires_at": expires}

            new_token, preview, expires = self._new_confirm(tool, arguments)
            return {"action": "confirm_required", "risk": risk,
                    "reason": "高危操作需二次确认",
                    "confirm_token": new_token, "preview": preview, "expires_at": expires}

    # ---------- 本地审计 ----------
    def audit(self, task_id, tool: str, arguments: dict, verdict: dict, result) -> None:
        try:
            if not isinstance(arguments, dict):
                arguments = {}
            verdict = verdict or {}
            res = result if isinstance(result, dict) else {}
            ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            command_hash = hashlib.sha256(
                (self._command_text(arguments) or "").encode("utf-8")
            ).hexdigest()[:HASH_LEN]
            rec = {
                "ts": ts,
                "task_id": task_id,
                "tool": tool,
                "risk": verdict.get("risk", "green"),
                "action": verdict.get("action", "allow"),
                "confirm_used": bool(arguments.get("confirm_token")),
                "ok": res.get("ok"),
                "error_code": res.get("error_code"),
                "error": str(res.get("error") or "")[:200],
                "command_hash": command_hash,
            }
            if "duration_ms" in res:
                rec["duration_ms"] = res["duration_ms"]
            line = json.dumps(rec, ensure_ascii=False)
            with self._lock:
                with open(self.audit_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            pass  # 审计写失败静默忽略，不影响主流程
