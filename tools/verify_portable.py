"""方案 A 验收：模拟「干净机器 + 断网 + 双击」启动便携包。

故意把环境弄脏来验证独立性：
- PATH 只留 C:\\Windows\\system32 —— 系统里的 Python / agent 环境全部不可见；
- 清掉 COGNITION_DB / PYTHONPATH / PYTHONHOME —— 不靠任何注入；
- HTTP(S)_PROXY 指向一个死端口 —— 任何联网尝试都会立刻失败，
  localhost 走 NO_PROXY，所以本地服务不受影响（等价于拔网线的关键部分）。

用法：python data/verify_portable.py <便携包目录> [--keep]
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

TITLE = "认知掌控侧车"
PORT = 8501
HEALTH = f"http://localhost:{PORT}/_stcore/health"

u32 = ctypes.windll.user32
EnumWindows = u32.EnumWindows
EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def windows() -> list[dict]:
    out: list[dict] = []

    @EnumWindowsProc
    def cb(hwnd, _):
        if not u32.IsWindowVisible(hwnd):
            return True
        n = u32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(hwnd, buf, n + 1)
        cls = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(hwnd, cls, 256)
        r = wt.RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(r))
        out.append({
            "hwnd": hwnd, "title": buf.value, "cls": cls.value,
            "w": r.right - r.left, "h": r.bottom - r.top,
        })
        return True

    EnumWindows(cb, 0)
    return out


# 本机的 shell 注入了 http_proxy，直连 localhost 也要显式绕开它
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def health() -> bool:
    try:
        with OPENER.open(HEALTH, timeout=2) as r:
            return 200 <= r.status < 400
    except Exception:  # noqa: BLE001
        return False


def clean_env() -> dict:
    env = os.environ.copy()
    for k in ("COGNITION_DB", "COGNITION_FORCE_MOCK", "PYTHONPATH", "PYTHONHOME",
              "VIRTUAL_ENV", "CONDA_PREFIX"):
        env.pop(k, None)
    env["PATH"] = r"C:\Windows\system32;C:\Windows"
    # 大小写两种写法都要清：urllib 优先读小写，漏一个 localhost 也会被代理拦住
    for k in list(env):
        if k.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            env.pop(k, None)
    dead = "http://127.0.0.1:9"
    for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        env[k] = dead
    for k in ("no_proxy", "NO_PROXY"):
        env[k] = "localhost,127.0.0.1"
    return env


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--keep", action="store_true", help="测完不关窗口")
    ap.add_argument("--timeout", type=int, default=70)
    args = ap.parse_args()
    root = Path(args.folder).resolve()
    vbs = root / "启动.vbs"
    if not vbs.exists():
        print(f"FAIL: 找不到 {vbs}")
        return 1

    fails: list[str] = []
    before_console = sum(1 for w in windows() if w["cls"] == "ConsoleWindowClass")
    log = root / "data" / "gadget.log"
    log_before = log.stat().st_size if log.exists() else 0

    print(f"[环境] PATH 只剩 system32；代理指向死端口；已清 COGNITION_DB/PYTHONPATH")
    print(f"[启动] wscript.exe {vbs.name}")
    # 用绝对路径：CreateProcess 找 exe 时用的是父进程 PATH，不是子环境块的 PATH
    subprocess.Popen(
        [r"C:\Windows\System32\wscript.exe", str(vbs)], cwd=str(root), env=clean_env()
    )

    hwnd = 0
    info = {}
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        # pywebview 在 WinForms 下会有多个同标题窗体（含隐藏辅助窗体），按面积挑主窗体
        hits = [w for w in windows() if TITLE in w["title"] and w["w"] > 100 and w["h"] > 100]
        if hits:
            info = max(hits, key=lambda w: w["w"] * w["h"])
            hwnd = info["hwnd"]
            break
        time.sleep(0.5)
    if not hwnd:
        print("FAIL: 超时没看到挂件窗口")
        if log.exists():
            print("--- gadget.log ---")
            print(log.read_text(encoding="utf-8", errors="replace")[-2000:])
        return 1
    print(f"[窗口] 标题={info['title']!r} 类={info['cls']} 物理尺寸={info['w']}x{info['h']}")

    time.sleep(1.5)
    console_now = sum(1 for w in windows() if w["cls"] == "ConsoleWindowClass")
    print(f"[黑窗] ConsoleWindowClass 数量 {before_console} -> {console_now}")
    if console_now > before_console:
        fails.append("启动过程弹出了控制台窗口")

    ok_health = health()
    print(f"[服务] /_stcore/health = {200 if ok_health else 'FAIL'}")
    if not ok_health:
        fails.append("本地服务没起来")
    else:
        try:
            html = OPENER.open(f"http://localhost:{PORT}/", timeout=5).read()
            print(f"[页面] 首页 {len(html)} 字节, 含 Streamlit 前端: {b'streamlit' in html.lower()}")
            if len(html) < 500:
                fails.append("首页内容异常")
        except Exception as exc:  # noqa: BLE001
            fails.append(f"首页取不到：{exc}")

    reused = False
    if log.exists():
        new = log.read_bytes()[log_before:].decode("utf-8", errors="replace")
        reused = "复用已在运行的服务" in new
        print("--- 本次日志 ---")
        print(new.strip()[-1200:] or "(空)")
        for bad in ("ModuleNotFoundError", "缺依赖", "FATAL", "Traceback"):
            if bad in new:
                fails.append(f"日志里出现 {bad}")
        if str(root / "runtime" / "pythonw.exe").lower() not in new.lower():
            fails.append("日志里的解释器不是包内 pythonw（可能复用了外部服务/解释器）")
    else:
        fails.append("没有生成 gadget.log")

    if not args.keep:
        u32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        for _ in range(30):
            if not any(w["hwnd"] == hwnd for w in windows()):
                break
            time.sleep(0.3)
        time.sleep(2)
        alive = health()
        mode = "复用了外部服务（不该由它负责清理）" if reused else "自起服务"
        print(f"[关闭] 窗口已关，8501 仍存活={alive}；本次={mode}")
        if alive and not reused:
            fails.append("关窗后没有停掉自己拉起的 streamlit")

    print("\n" + ("全部通过。" if not fails else "FAIL:\n  - " + "\n  - ".join(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
