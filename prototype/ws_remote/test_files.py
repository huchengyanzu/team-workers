#!/usr/bin/env python3
"""文件工具单测：路径白名单、穿越拒绝、覆盖确认、base64 往返。"""

from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from file_tools import FileTools, MAX_BYTES


class TestFileTools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "sub").mkdir()
        (self.root / "hello.txt").write_text("hello", encoding="utf-8")
        (self.root / "sub" / "n.txt").write_text("nested", encoding="utf-8")
        self.tools = FileTools([str(self.root)])

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_ok(self):
        r = self.tools.read("hello.txt")
        self.assertTrue(r["ok"], r)
        self.assertEqual(base64.b64decode(r["content"]).decode(), "hello")
        self.assertEqual(r["size"], 5)

    def test_read_outside_root(self):
        r = self.tools.read("/etc/passwd")
        self.assertFalse(r["ok"])  # 无论软链检查还是白名单检查，越界必被拒
        self.assertTrue(r["error"])

    def test_path_traversal(self):
        r = self.tools.read("../outside.txt")
        self.assertFalse(r["ok"])

    def test_write_no_overwrite(self):
        data = base64.b64encode(b"new").decode()
        r = self.tools.write("hello.txt", data, overwrite=False)
        self.assertFalse(r["ok"])
        self.assertIn("overwrite", r["error"])

    def test_write_with_overwrite_and_roundtrip(self):
        data = base64.b64encode(b"changed").decode()
        r = self.tools.write("hello.txt", data, overwrite=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual((self.root / "hello.txt").read_bytes(), b"changed")
        self.assertEqual(base64.b64decode(self.tools.read("hello.txt")["content"]), b"changed")

    def test_write_new_nested(self):
        data = base64.b64encode(b"made").decode()
        r = self.tools.write("sub/new/deep.txt", data)
        self.assertTrue(r["ok"], r)
        self.assertEqual((self.root / "sub" / "new" / "deep.txt").read_bytes(), b"made")

    def test_list_dir(self):
        r = self.tools.list_dir("sub")
        self.assertTrue(r["ok"])
        self.assertIn("n.txt", [e["name"] for e in r["entries"]])

    def test_no_roots_is_unrestricted(self):
        """留空 = 不限制模式：任意绝对路径可读，不受白名单约束。"""
        tools = FileTools([])
        self.assertTrue(tools.unrestricted)
        r = tools.read(str(self.root / "hello.txt"))
        self.assertTrue(r["ok"], r)
        self.assertEqual(base64.b64decode(r["content"]).decode(), "hello")
        # 不限制模式下 /etc/passwd 这类系统路径也可读（这正是"不限制"的预期）
        self.assertTrue(tools.read("/etc/passwd")["ok"])

    def test_unrestricted_still_confirms_overwrite(self):
        """不限制模式仍保留覆盖确认与大小上限保护。"""
        tools = FileTools([])
        data = base64.b64encode(b"new").decode()
        r = tools.write(str(self.root / "hello.txt"), data, overwrite=False)
        self.assertFalse(r["ok"])
        self.assertIn("overwrite", r["error"])

    def test_symlink_rejected(self):
        link = self.root / "link.txt"
        try:
            link.symlink_to(self.root / "hello.txt")
        except (OSError, NotImplementedError):
            self.skipTest("symlink unavailable")
        r = self.tools.read("link.txt")
        self.assertFalse(r["ok"])

    def test_oversize_rejected(self):
        data = base64.b64encode(b"x" * (MAX_BYTES + 1)).decode()
        r = self.tools.write("big.bin", data)
        self.assertFalse(r["ok"])
        self.assertIn("上限", r["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
