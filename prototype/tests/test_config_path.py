"""回归测试：默认配置路径必须是绝对路径，不随 CWD 漂移。

复现 2026-11-24 家里电脑"重启后配置丢了"：Windows exe 的默认配置是相对路径
"device.json"，双击/自启项工作目录不同就找不到旧配置，配置页三个框全空、设备离线。

修复：resolve_default_config() 固定在 exe 所在目录，并向后兼容 CWD 下已有的旧配置。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PROTOTYPE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROTOTYPE_DIR, "ws_remote"))
sys.path.insert(0, PROTOTYPE_DIR)

import device  # noqa: E402


class ConfigPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.exe_dir = self.root / "app"
        self.exe_dir.mkdir()
        self.exe = str(self.exe_dir / "team-workers-device-win.exe")
        self.cwd = self.root / "some-cwd"
        self.cwd.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_is_absolute_in_exe_dir(self):
        # exe 旁无配置、CWD 也无配置 → 新配置应写到 exe 目录，且是绝对路径
        got = device.resolve_default_config(self.exe, app_mode=False, cwd=self.cwd)
        self.assertTrue(os.path.isabs(got), "必须是绝对路径，不能随 CWD 漂移：%s" % got)
        self.assertEqual(Path(got).resolve(), (self.exe_dir / "device.json").resolve())

    def test_exe_dir_config_takes_priority(self):
        # exe 旁已有配置 → 用它（正常安装场景）
        cfg = self.exe_dir / "device.json"
        cfg.write_text("{}", encoding="utf-8")
        got = device.resolve_default_config(self.exe, app_mode=False, cwd=self.cwd)
        self.assertEqual(Path(got).resolve(), cfg.resolve())

    def test_falls_back_to_cwd_legacy_config(self):
        # 向后兼容：exe 旁没有，但 CWD 有旧配置 → 用旧的绝对路径，不"丢配置"
        legacy = self.cwd / "device.json"
        legacy.write_text('{"device_id":"dev-old"}', encoding="utf-8")
        got = device.resolve_default_config(self.exe, app_mode=False, cwd=self.cwd)
        self.assertTrue(os.path.isabs(got))
        self.assertEqual(Path(got), legacy.resolve())

    def test_cwd_change_does_not_move_exe_dir_config(self):
        # 核心回归：从两个不同 CWD 启动，只要 exe 配置存在，结果一致
        (self.exe_dir / "device.json").write_text("{}", encoding="utf-8")
        other_cwd = self.root / "another"
        other_cwd.mkdir()
        g1 = device.resolve_default_config(self.exe, app_mode=False, cwd=self.cwd)
        g2 = device.resolve_default_config(self.exe, app_mode=False, cwd=other_cwd)
        self.assertEqual(g1, g2, "不同工作目录应解析到同一配置路径")

    def test_legacy_team_workers_name_in_exe_dir(self):
        # 家里电脑实际场景：exe 在 Downloads，配置叫 team-workers-device.json（非 device.json）
        cfg = self.exe_dir / "team-workers-device.json"
        cfg.write_text('{"device_id":"dev-home"}', encoding="utf-8")
        got = device.resolve_default_config(self.exe, app_mode=False, cwd=self.cwd)
        self.assertEqual(Path(got).resolve(), cfg.resolve())

    def test_app_mode_uses_user_dir(self):
        # macOS .app 行为不变：固定用户目录，与 exe 位置无关
        got = device.resolve_default_config("/Applications/team-workers-device.app/x", app_mode=True)
        self.assertTrue(os.path.isabs(got))
        self.assertIn("team-workers", got)
        self.assertTrue(got.endswith("device.json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
