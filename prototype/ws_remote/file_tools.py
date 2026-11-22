#!/usr/bin/env python3
"""设备端文件工具：读取 / 写入 / 列目录，经 WebSocket base64 传输。

访问范围（两种模式）：
  - 不限制模式（allowed_roots 留空）：可读写本机任意路径，不做根目录白名单校验。
    适合工作区很多、无法逐一列举的机器；安全性由「写入需确认覆盖 + 大小上限」保障。
  - 限制模式（配置了 allowed_roots）：路径必须在某个根目录内，阻止 ../ 穿越与绝对路径逃逸。

两种模式都保留的保护：
  - 写入默认拒绝覆盖（overwrite=false 时目标存在即报错），符合"覆盖需确认"口径
  - 单文件大小上限 MAX_BYTES（默认 8MB），防内存耗尽
  - 只操作普通文件/目录，拒绝直接操作符号链接本身（跟随到真实目标）
  - 返回字节数 + 完整路径，供三段式回执

限制模式下越界、经符号链接逃逸、超大、非普通文件 → 返回 ok=False + 明确 error，不抛异常给协议层。
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

MAX_BYTES = 8 * 1024 * 1024


class FileTools:
    def __init__(self, allowed_roots: list[str] | None = None):
        roots = []
        for r in (allowed_roots or []):
            try:
                p = Path(os.path.expanduser(str(r))).resolve()
                roots.append(str(p))
            except Exception:
                continue
        self.roots = roots

    # ---------- 路径安全 ----------
    @property
    def unrestricted(self) -> bool:
        """未配置任何允许根目录 = 不限制模式（可访问本机任意路径）。"""
        return not self.roots

    def _resolve(self, path: str) -> tuple[Path | None, str]:
        if not path or not isinstance(path, str):
            return None, "路径为空"
        try:
            raw = Path(os.path.expanduser(path))
            # 不限制模式：任意路径（含相对路径按 cwd）都放行，仅 resolve 归一化。
            # 相对路径在限制模式下按第一个允许根目录解析（而非进程 cwd）。
            if not raw.is_absolute() and self.roots:
                raw = Path(self.roots[0]) / raw
            target = raw.resolve(strict=False)
            if self.unrestricted:
                return target, ""
            # ---- 以下仅限制模式 ----
            # 逐级检查 symlink 组件（防止父目录是软链逃逸出允许根目录）。
            # macOS 的 /var -> /private/var 系统级链接：roots 里是 resolve 后的真实路径，
            # 这里用 target（已 resolve）判断归属，避免因系统软链误拒。
            for parent in [raw] + list(raw.parents):
                if parent.is_symlink() and str(parent) != str(raw):
                    real = str(parent.resolve(strict=False))
                    if not any(real == r or real.startswith(r + os.sep) for r in self.roots):
                        return None, "拒绝经符号链接逃逸: %s" % parent
            # 拒绝直接操作符号链接本身（跟随到目标由上面白名单校验）
            if raw.is_symlink():
                return None, "拒绝直接操作符号链接路径: %s" % path
            for root in self.roots:
                try:
                    if os.path.commonpath([root, str(target)]) == root:
                        return target, ""
                except ValueError:
                    continue
            return None, "路径不在允许目录内: " + ", ".join(self.roots)
        except (OSError, RuntimeError) as exc:
            return None, "路径解析失败: %s" % exc

    # ---------- 三个操作 ----------
    def read(self, path: str) -> dict:
        target, err = self._resolve(path)
        if err:
            return {"ok": False, "error": err}
        try:
            if not target.is_file():
                return {"ok": False, "error": "不是普通文件: %s" % path}
            size = target.stat().st_size
            if size > MAX_BYTES:
                return {"ok": False, "error": "文件超过 %d 字节上限（%d）" % (MAX_BYTES, size)}
            data = target.read_bytes()
            return {
                "ok": True,
                "path": str(target),
                "size": len(data),
                "encoding": "base64",
                "content": base64.b64encode(data).decode("ascii"),
            }
        except OSError as exc:
            return {"ok": False, "error": "读取失败: %s" % exc}

    def write(self, path: str, content_b64: str, overwrite: bool = False) -> dict:
        target, err = self._resolve(path)
        if err:
            return {"ok": False, "error": err}
        try:
            if target.exists() and not overwrite:
                return {"ok": False, "error": "目标已存在，需 overwrite=true 确认覆盖: %s" % path}
            if target.exists() and not target.is_file():
                return {"ok": False, "error": "目标不是普通文件: %s" % path}
            data = base64.b64decode(content_b64 or "", validate=True)
            if len(data) > MAX_BYTES:
                return {"ok": False, "error": "内容超过 %d 字节上限" % MAX_BYTES}
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp-%d" % os.getpid())
            tmp.write_bytes(data)
            os.replace(str(tmp), str(target))
            return {"ok": True, "path": str(target), "size": len(data), "overwrite": overwrite}
        except (ValueError, OSError) as exc:
            return {"ok": False, "error": "写入失败: %s" % exc}

    def list_dir(self, path: str) -> dict:
        target, err = self._resolve(path)
        if err:
            return {"ok": False, "error": err}
        try:
            if not target.is_dir():
                return {"ok": False, "error": "不是目录: %s" % path}
            entries = []
            for e in sorted(target.iterdir(), key=lambda x: x.name)[:1000]:
                try:
                    st = e.lstat()
                    entries.append({
                        "name": e.name,
                        "type": "dir" if e.is_dir() else ("link" if e.is_symlink() else "file"),
                        "size": st.st_size if e.is_file() else 0,
                    })
                except OSError:
                    continue
            return {"ok": True, "path": str(target), "entries": entries, "count": len(entries)}
        except OSError as exc:
            return {"ok": False, "error": "列目录失败: %s" % exc}
