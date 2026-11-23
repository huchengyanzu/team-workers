"""回归测试：exec 输出采集不再因孙进程继承 stdout 管道而卡死。

背景（2026-11-23 家用机掉线根因）：
  旧 exec_command 用 stdout=PIPE + proc.communicate()。Windows 上 `start /b`、
  `start cmd /c` 拉起的孙进程会继承管道写端；直系 cmd 退出后孙进程仍握着管道，
  communicate() 等 EOF 永久不返回 → 同步调用冻结 device.py 的 async 主循环 →
  websocket 心跳/keepalive pong 发不出 → 服务端 20s 判 1011 离线。

修复：exec_command 改为写临时文件 + proc.wait()（只等直系进程，不等孙进程/EOF）。

本测试用一个"父进程立即退出、孙进程长时间持有 stdout fd"的最小复现：
  - 父：python -c "开一个孙进程 sleep 后自己马上 exit"
  - 孙：继承父的 stdout fd，sleep 数秒
用直系进程退出耗时判定：新实现应在远小于孙进程存活时间时返回。
测试只在 POSIX 跑（Windows 分支靠 device 层解耦 + 代码审查，不强依赖 CI）。
"""

from __future__ import annotations

import os
import sys
import time
import unittest

PROTOTYPE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROTOTYPE_DIR)

import exec_server  # noqa: E402


@unittest.skipIf(os.name == "nt", "POSIX 复现脚本；Windows 真机行为由 device 层解耦保证")
class ExecPipeInheritanceTests(unittest.TestCase):
    def setUp(self):
        self.ex = exec_server.Executor(
            allowlist={"python3", "sleep"},
            max_output_bytes=1024,
            default_timeout=8.0,
        )

    def tearDown(self):
        self.ex.shutdown()

    def _grandchild_script(self, hold_seconds: float) -> str:
        # 孙进程继承父进程 stdout（默认继承），持有 fd sleep；父进程打印标记后立刻 exit 0。
        # 旧实现：communicate() 要等孙进程退出才拿到 EOF → 约 hold_seconds 后才返回。
        # 新实现：wait() 只等直系 python 退出（几乎立刻）→ 读文件即返回。
        return (
            "import subprocess,sys,time;"
            "subprocess.Popen([sys.executable,'-c','import time;time.sleep(%d)']);"
            "sys.stdout.write('PARENT_DONE\\n');sys.stdout.flush()"
            % int(hold_seconds)
        )

    def test_returns_when_direct_child_exits_not_waiting_grandchild_eof(self):
        hold = 6
        started = time.monotonic()
        result = self.ex.exec_command(
            ["python3", "-c", self._grandchild_script(hold)], timeout=20.0)
        elapsed = time.monotonic() - started
        self.assertTrue(result["ok"], "直系进程应正常结束：%r" % result)
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("PARENT_DONE", result["stdout"])
        # 关键断言：远早于孙进程 sleep 结束就返回（旧实现会卡到 ~6s）。
        self.assertLess(elapsed, hold - 2,
                        "不应等待孙进程释放管道 EOF；耗时 %.2fs（孙进程 sleep %ds）"
                        % (elapsed, hold))

    def test_output_from_direct_child_still_captured(self):
        result = self.ex.exec_command(
            ["python3", "-c",
             "import sys;sys.stdout.write('OUT\\n');sys.stderr.write('ERR\\n')"],
            timeout=5.0)
        self.assertTrue(result["ok"])
        self.assertIn("OUT", result["stdout"])
        self.assertIn("ERR", result["stderr"])

    def test_timeout_with_grandchild_returns_promptly(self):
        # 直系进程自己也不退出时，超时强杀后应立刻返回（不再二次无超时 communicate）。
        started = time.monotonic()
        result = self.ex.exec_command(["sleep", "30"], timeout=0.3)
        elapsed = time.monotonic() - started
        self.assertTrue(result["timed_out"])
        self.assertLess(elapsed, 3.0, "超时后应立即返回：%.2fs" % elapsed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
