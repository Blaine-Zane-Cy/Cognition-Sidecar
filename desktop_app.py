"""认知掌控侧车 · 桌面挂件

双击「启动-桌面挂件.bat」后：
  1. 探活 localhost:8501，没起来就静默拉一个（pythonw 下无黑窗）
  2. 开一个视觉 400x800 的无边框置顶窗口，加载窄栏页面
  3. 顶部一条 28px 的极简边框：整条可拖动，右侧三颗按钮（置顶 / 最小化 / 关闭）
  4. 关窗时把位置尺寸记进 data/gadget.json，下次原地打开

为什么是 pywebview 而不是 Qt：复用系统 WebView2，只多 1MB 依赖，内存与 Edge 共享。
代价是无边框窗口默认不可缩放（pywebview 的 frameless 会覆盖 resizable），
所以下面用 Win32 把 WS_THICKFRAME 加回去，并自己接管 WM_NCHITTEST 算命中区。
拖动不用 CSS app-region，改由 WM_NCHITTEST 返回 HTCAPTION —— 走系统原生拖动，
带贴边吸附，且不依赖 WebView2 对 app-region 的支持。
"""

from __future__ import annotations

import atexit
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE_FILE = DATA / "gadget.json"
LOG_FILE = DATA / "gadget.log"
ST_LOG = DATA / "streamlit.log"

PORT = 8501
URL = f"http://localhost:{PORT}/?embed=true&narrow=1"
HEALTH = f"http://localhost:{PORT}/_stcore/health"
TITLE = "认知掌控侧车"

DEF_W, DEF_H = 400, 800     # 逻辑像素（用户看到的尺寸）
MIN_W, MIN_H = 380, 460     # 再窄 CSS 只剩 ~310px，窄栏的按钮会开始挤
BAR_H = 28                  # 顶部拖动条高度，CSS 像素
BTN_ZONE = 96               # 右上角按钮区宽度，CSS 像素
EDGE = 6                    # 四边缩放热区，CSS 像素


# ------------------------------------------------------------------ 日志
_LOG_FH = None


def setup_log() -> None:
    """日志始终落文件。

    pythonw 下 sys.stdout 可能是 None，也可能是被父进程接走的无效句柄，
    两种情况 print 都会失败或石沉大海。所以不指望 stdout：自己开文件，
    两边都写。排查双击启动的问题只能靠这个文件。
    """
    global _LOG_FH
    if _LOG_FH is not None:  # 幂等：main 里还会再调一次
        return
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        _LOG_FH = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    except Exception:  # noqa: BLE001
        _LOG_FH = None
    if sys.stdout is None:
        sys.stdout = _LOG_FH or open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = _LOG_FH or open(os.devnull, "w", encoding="utf-8")


def log(*parts) -> None:
    line = time.strftime("[%H:%M:%S]") + " " + " ".join(str(p) for p in parts)
    if _LOG_FH is not None:
        try:
            _LOG_FH.write(line + "\n")
        except Exception:  # noqa: BLE001
            pass
    if sys.stdout is not _LOG_FH:  # 否则同一行会被写两遍
        try:
            print(line, flush=True)
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ Win32
u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

GWL_STYLE, GWL_EXSTYLE, GWLP_WNDPROC = -16, -20, -4
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_EX_TOPMOST = 0x00000008
WS_EX_APPWINDOW = 0x00040000

WM_NCHITTEST = 0x0084
WM_NCLBUTTONDOWN = 0x00A1
WM_NCLBUTTONDBLCLK = 0x00A3
WM_SYSCOMMAND = 0x0112
SC_MOVE = 0xF010
WM_DPICHANGED = 0x02E0
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
SWP_FRAMECHANGED = 0x20
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SW_MINIMIZE, SW_RESTORE = 6, 9
ERROR_ALREADY_EXISTS = 183

HTCLIENT, HTCAPTION = 1, 2
HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT = 10, 11, 12, 13, 14
HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 15, 16, 17

u32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
u32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]
u32.SetWindowLongPtrW.restype = ctypes.c_void_p
u32.CallWindowProcW.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.CallWindowProcW.restype = ctypes.c_ssize_t
u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
u32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, wt.UINT]
u32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.SendMessageW.restype = ctypes.c_ssize_t
u32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.ReleaseCapture.argtypes = []
u32.SetCapture.argtypes = [wt.HWND]
u32.SetCapture.restype = wt.HWND
u32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
u32.GetAsyncKeyState.restype = ctypes.c_short
VK_LBUTTON = 0x01
# 不显式声明这些，64 位下字符串缓冲区和窗口句柄会被按 32 位截断
u32.GetWindowTextLengthW.argtypes = [wt.HWND]
u32.GetWindowTextW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
u32.GetClassNameW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
u32.IsWindowVisible.argtypes = [wt.HWND]
u32.IsIconic.argtypes = [wt.HWND]
u32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
u32.SetForegroundWindow.argtypes = [wt.HWND]
u32.MessageBoxW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_wchar_p, wt.UINT]
u32.SystemParametersInfoW.argtypes = [wt.UINT, wt.UINT, ctypes.c_void_p, wt.UINT]

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


def dpi_scale(hwnd) -> float:
    """窗口所在显示器的缩放系数（192 → 2.0）。"""
    try:
        return u32.GetDpiForWindow(hwnd) / 96.0
    except Exception:  # noqa: BLE001
        hdc = u32.GetDC(0)
        try:
            return ctypes.WinDLL("gdi32").GetDeviceCaps(hdc, 88) / 96.0
        finally:
            u32.ReleaseDC(0, hdc)


def work_area() -> tuple[int, int, int, int]:
    """工作区，物理像素。"""
    r = wt.RECT()
    u32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
    return r.left, r.top, r.right, r.bottom


def window_rect(hwnd) -> tuple[int, int, int, int]:
    r = wt.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def find_windows(pid: int | None = None, title: str | None = None) -> list[int]:
    """找可见顶层窗口。按 pid 或标题模糊匹配 —— 不依赖 pywebview 内部属性。"""
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if not u32.IsWindowVisible(hwnd):
            return True
        if pid is not None:
            p = wt.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            if p.value != pid:
                return True
        if title is not None:
            n = u32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(hwnd, buf, n + 1)
            if title not in buf.value:
                return True
        found.append(hwnd)
        return True

    u32.EnumWindows(cb, 0)
    return found


def pick_form_window(pid: int) -> int | None:
    """挑出真正那个主窗体。

    pywebview 在 WinForms 里不止创建一个窗体，同进程可能还有隐藏的辅助窗体。
    最小化/隐藏窗口的坐标是 -32000，尺寸也接近 0 —— 用这两个特征把它们筛掉，
    再按面积取最大，避免抓到一个看不见的窗体然后把位置全设到屏幕外。
    """
    best, best_area = None, 0
    for h in find_windows(pid=pid):
        buf = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(h, buf, 256)
        if "WindowsForms" not in buf.value:
            continue
        l, t, r, b = window_rect(h)
        if l < -10000 or t < -10000:
            continue
        area = (r - l) * (b - t)
        if area > best_area and (r - l) > 100 and (b - t) > 100:
            best, best_area = h, area
    return best


# ------------------------------------------------------------------ 服务
def healthy(timeout: float = 1.2) -> bool:
    try:
        with urllib.request.urlopen(HEALTH, timeout=timeout) as r:
            return 200 <= r.status < 400
    except Exception:  # noqa: BLE001
        return False


def console_python() -> str:
    """pythonw 起 streamlit 会因为 sys.stdout 为 None 直接崩，必须换回 python.exe。"""
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        cand = exe[: -len("pythonw.exe")] + "python.exe"
        if os.path.exists(cand):
            return cand
    return exe


def ensure_server() -> subprocess.Popen | None:
    """已经在跑就复用（不重启、不动别人的会话）；否则静默拉一个。"""
    if healthy():
        log("复用已在运行的服务")
        return None

    DATA.mkdir(parents=True, exist_ok=True)
    out = open(ST_LOG, "ab")
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    proc = subprocess.Popen(
        [console_python(), "-m", "streamlit", "run", "app.py",
         "--server.port", str(PORT),
         "--server.headless", "true",
         "--server.runOnSave", "false"],
        cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT,
        creationflags=flags,
    )
    log("启动 streamlit, pid =", proc.pid)

    deadline = time.time() + 50
    while time.time() < deadline:
        if proc.poll() is not None:
            log("streamlit 提前退出, code =", proc.returncode)
            return proc
        if healthy():
            log("streamlit 就绪, 用时 %.1fs" % (50 - (deadline - time.time())))
            return proc
        time.sleep(0.4)
    log("streamlit 等待超时")
    return proc


def stop_server(proc: subprocess.Popen | None) -> None:
    """只杀自己拉起来的那个。复用别人起的实例时绝不动它。"""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=6)
        log("已停止 streamlit")
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ 状态
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_state(data: dict) -> None:
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log("保存状态失败:", e)


def initial_geometry(hwnd) -> tuple[int, int, int, int]:
    """优先恢复上次的位置；越界或没有就贴屏幕右侧。返回值是逻辑像素。"""
    s = dpi_scale(hwnd)
    wl, wt_, wr, wb = work_area()
    lo, to = wl / s, wt_ / s
    lw, lh = (wr - wl) / s, (wb - wt_) / s

    st = load_state()
    x, y = st.get("x"), st.get("y")
    w, h = st.get("w", DEF_W), st.get("h", DEF_H)

    # 存档里可能是脏数据（比如最小化时存成 157x25），那种直接回默认尺寸，
    # 而不是 clamp 到最小值 —— 否则用户会觉得"窗口怎么自己缩成这样了"
    if not isinstance(w, (int, float)) or not isinstance(h, (int, float)) \
            or w < MIN_W or h < MIN_H:
        w, h = DEF_W, DEF_H
    w = min(int(w), int(lw))
    h = min(int(h), int(lh))

    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        # 上次的窗口可能落在已拔掉的显示器上，落到任何屏幕外就重置
        if lo - 40 <= x and x + w <= lo + lw + 40 and to - 40 <= y and y + h <= to + lh + 40:
            return int(x), int(y), w, h

    x = int(lo + lw - w - 12)
    y = int(to + 14)
    return x, y, w, h


# ------------------------------------------------------------------ 钢壳
class Shell:
    """无边框窗口的 Win32 钢壳：可缩放、顶部可拖、右上按钮区不吃拖动。"""

    def __init__(self) -> None:
        self.hwnd: int | None = None
        self.dragging = False
        self.px_per_css = 2.0   # 物理像素 / CSS 像素，实测校准
        self.dpi_ratio = 2.0    # 窗口 DPI / 96
        self.base = 1.0         # px_per_css / dpi_ratio，跨屏 DPI 变化时按比例保持
        self._orig = None
        self._proc = None

    # ---- 命中测试 ----
    def _hit(self, hwnd, sx: int, sy: int) -> int:
        l, t, r, b = window_rect(hwnd)
        x, y = sx - l, sy - t
        w, h = r - l, b - t
        p = self.px_per_css
        edge = max(3, round(EDGE * p))
        bar = round(BAR_H * p)
        zone = round(BTN_ZONE * p)

        left, right = x < edge, x >= w - edge
        top, bottom = y < edge, y >= h - edge

        if top and left:
            return HTTOPLEFT
        if top and right:
            return HTTOPRIGHT
        if bottom and left:
            return HTBOTTOMLEFT
        if bottom and right:
            return HTBOTTOMRIGHT
        if left:
            return HTLEFT
        if right:
            return HTRIGHT
        if top:
            return HTTOP
        if bottom:
            return HTBOTTOM

        if y < bar:
            # 右上角留出来给按钮，否则按钮永远点不到（整条都会被当成标题栏）
            return HTCLIENT if x >= w - zone else HTCAPTION
        return HTCLIENT

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_NCHITTEST:
            sx = ctypes.c_short(lparam & 0xFFFF).value
            sy = ctypes.c_short((lparam >> 16) & 0xFFFF).value
            return self._hit(hwnd, sx, sy)
        if msg == WM_DPICHANGED:
            # 拖到另一块缩放不同的屏：按新 DPI 等比重算，别让命中区错位
            new_dpi = (wparam >> 16) & 0xFFFF
            if new_dpi:
                self.dpi_ratio = new_dpi / 96.0
                self.px_per_css = self.base * self.dpi_ratio
            return 0
        return u32.CallWindowProcW(self._orig, hwnd, msg, wparam, lparam)

    # ---- 安装 ----
    def attach(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self.dpi_ratio = dpi_scale(hwnd)

        style = u32.GetWindowLongPtrW(hwnd, GWL_STYLE)
        style &= ~(WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX)
        style |= WS_THICKFRAME
        u32.SetWindowLongPtrW(hwnd, GWL_STYLE, ctypes.c_void_p(style))

        ex = u32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        u32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ctypes.c_void_p(ex | WS_EX_APPWINDOW))

        u32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                         SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)

        self._proc = WNDPROC(self._wndproc)
        self._orig = u32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, self._proc)
        log("钢壳已挂载, dpi =", self.dpi_ratio)

    def calibrate(self, css_width: int) -> None:
        """用真实 innerWidth 反推 物理/CSS 比。

        WebView2 的 devicePixelRatio 不一定等于窗口 DPI（这里是 2.38 vs 2.0），
        所以命中区不能按 DPI 硬算，必须实测一次。
        """
        if not self.hwnd or css_width <= 0:
            return
        l, _, r, _ = window_rect(self.hwnd)
        self.px_per_css = (r - l) / css_width
        self.base = self.px_per_css / self.dpi_ratio
        log("校准 px_per_css = %.3f (base %.3f)" % (self.px_per_css, self.base))

    def place(self, x: int, y: int, w: int, h: int) -> None:
        """按逻辑像素精确摆位。pywebview 自己的 scale 有约 3% 偏差，这里绕开它。"""
        if not self.hwnd:
            return
        s = dpi_scale(self.hwnd)
        u32.SetWindowPos(self.hwnd, None, round(x * s), round(y * s),
                         round(w * s), round(h * s), SWP_NOZORDER | SWP_NOACTIVATE)

    def size(self) -> tuple[int, int, int, int] | None:
        """逻辑像素的位置尺寸，用于存档。最小化/异常状态返回 None，别把脏数据写进档案。"""
        if not self.hwnd:
            return None
        if u32.IsIconic(self.hwnd):
            return None  # 最小化时 rect 是 -32000，存下去下次就找不到窗口了
        s = dpi_scale(self.hwnd)
        l, t, r, b = window_rect(self.hwnd)
        w, h = round((r - l) / s), round((b - t) / s)
        if l < -10000 or t < -10000 or w < 200 or h < 200:
            return None
        return round(l / s), round(t / s), w, h

    def set_top(self, on: bool) -> bool:
        if not self.hwnd:
            return on
        u32.SetWindowPos(self.hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST,
                         0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        return on

    def is_top(self) -> bool:
        if not self.hwnd:
            return False
        return bool(u32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)

    def minimize(self) -> None:
        if self.hwnd:
            u32.ShowWindow(self.hwnd, SW_MINIMIZE)

    def focus(self) -> None:
        if self.hwnd:
            if u32.IsIconic(self.hwnd):
                u32.ShowWindow(self.hwnd, SW_RESTORE)
            u32.SetForegroundWindow(self.hwnd)


SHELL = Shell()


def _drag_loop(hwnd: int) -> None:
    """跟着光标搬窗口，直到左键松开。

    不用系统的 SC_MOVE：那是非客户区消息，前提是鼠标按下时窗口持有捕获，
    而我们的 mousedown 发生在 WebView2 子窗口里，拿不到那个状态。
    """
    u32.SetCapture(hwnd)
    try:
        pt0 = wt.POINT()
        u32.GetCursorPos(ctypes.byref(pt0))
        l, t, _, _ = window_rect(hwnd)
        while u32.GetAsyncKeyState(VK_LBUTTON) & 0x8000:
            pt = wt.POINT()
            u32.GetCursorPos(ctypes.byref(pt))
            if pt.x != pt0.x or pt.y != pt0.y:
                u32.SetWindowPos(hwnd, None,
                                 l + pt.x - pt0.x, t + pt.y - pt0.y,
                                 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            time.sleep(0.012)
    except Exception as e:  # noqa: BLE001
        log("拖动出错:", e)
    finally:
        u32.ReleaseCapture()
        SHELL.dragging = False


# ------------------------------------------------------------------ 注入
BAR_JS = """
(function () {
  if (window.__gadgetBar) { return 'exists'; }
  window.__gadgetBar = true;

  var css = document.createElement('style');
  css.id = 'gadget-bar-style';
  css.textContent = `
  #gadget-bar {
    position: fixed; top: 0; left: 0; right: 0; height: __BAR__px;
    z-index: 2147483000; box-sizing: border-box;
    display: flex; align-items: center; justify-content: flex-end;
    padding: 0 4px 0 10px; gap: 2px;
    background: #FFFFFF; border-bottom: 1px solid #ECEAE3;
    font: 11px/1 -apple-system, "Segoe UI", system-ui, sans-serif;
    color: #8A8880; user-select: none; -webkit-user-select: none;
  }
  #gadget-bar .grip {
    position: absolute; left: 50%; top: 5px; transform: translateX(-50%);
    width: 30px; height: 3px; border-radius: 2px; background: #D3D1C7;
  }
  #gadget-bar button {
    width: 24px; height: 24px; padding: 0; border: 0; border-radius: 6px;
    background: transparent; color: #8A8880; cursor: pointer;
    display: grid; place-items: center; transition: background .12s, color .12s;
  }
  #gadget-bar button:hover { background: rgba(44,44,42,.08); color: #2C2C2A; }
  #gadget-bar button.on { background: #185FA5; color: #FFFFFF; }
  #gadget-bar button.on:hover { background: #134B84; color: #FFFFFF; }
  #gadget-bar button[data-act="close"]:hover { background: #FCEBEB; color: #A32D2D; }
  #gadget-bar svg { display: block; }
  `;
  document.head.appendChild(css);

  var bar = document.createElement('div');
  bar.id = 'gadget-bar';
  bar.innerHTML = [
    '<div class="grip"></div>',
    '<button data-act="top" title="置顶（让窗口一直在最前）">',
      '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">',
      '<path d="M6 2h4l-.7 4.2 2.7 2.3H4l2.7-2.3L6 2z"/><path d="M8 8.5V14"/></svg>',
    '</button>',
    '<button data-act="min" title="最小化">',
      '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round">',
      '<path d="M3.5 11.5h9"/></svg>',
    '</button>',
    '<button data-act="close" title="关闭挂件">',
      '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round">',
      '<path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/></svg>',
    '</button>',
  ].join('');
  document.body.appendChild(bar);

  var topBtn = bar.querySelector('[data-act="top"]');

  function apply(on) { topBtn.classList.toggle('on', !!on); }

  // 拖动只能从页面侧发起：WebView2 是独立子窗口，窗口内部按下不经过窗体 WndProc。
  // 按下就把控制权交给系统的移动循环，手感跟原生标题栏一致（含贴边吸附）。
  bar.addEventListener('mousedown', function (ev) {
    if (ev.button !== 0) { return; }
    if (ev.target.closest('button')) { return; }   // 按钮区不拖
    var api = window.pywebview && window.pywebview.api;
    if (api && typeof api.start_drag === 'function') { api.start_drag(); }
  });

  bar.addEventListener('click', function (ev) {
    var btn = ev.target.closest('button');
    if (!btn) { return; }
    var api = window.pywebview && window.pywebview.api;
    if (!api || typeof api.toggle_top !== 'function') { return; }
    var act = btn.getAttribute('data-act');
    if (act === 'top') {
      api.toggle_top().then(apply).catch(function () {});
    } else if (act === 'min') {
      api.minimize().catch(function () {});
    } else if (act === 'close') {
      api.close_window().catch(function () {});
    }
  });

  // 顶部留白：Streamlit 自己那段窄栏样式是塞在 body 里的，源顺序比 head 靠后，
  // 普通 !important 压不住。只能用 inline + important（最高优先级），
  // 再定期补一刀 —— Streamlit rerun 有可能重建这个容器，inline 会跟着丢。
  function pad() {
    var els = document.querySelectorAll('[data-testid="stMainBlockContainer"], .block-container');
    var want = 'calc(0.5rem + __BAR__px)';
    for (var i = 0; i < els.length; i++) {
      if (els[i].style.getPropertyValue('padding-top') !== want) {
        els[i].style.setProperty('padding-top', want, 'important');
      }
    }
  }
  pad();
  setInterval(pad, 800);

  // JS 桥不会在页面一加载就把方法挂好，轮询等它（实测首次注入时确实还没有）
  function syncTop(tries) {
    var api = window.pywebview && window.pywebview.api;
    if (api && typeof api.is_top === 'function') {
      api.is_top().then(apply).catch(function () {});
    } else if (tries > 0) {
      setTimeout(function () { syncTop(tries - 1); }, 150);
    }
  }
  syncTop(40);

  return 'ok';
})();
"""


def inject_bar(win) -> None:
    try:
        r = win.evaluate_js(BAR_JS.replace("__BAR__", str(BAR_H)))
        log("注入标题条:", r)
    except Exception as e:  # noqa: BLE001
        log("注入失败:", e)


def read_css_width(win) -> int:
    try:
        return int(win.evaluate_js("window.innerWidth") or 0)
    except Exception:  # noqa: BLE001
        return 0


# ------------------------------------------------------------------ JS 桥
class Api:
    def __init__(self, win_holder: dict, proc_holder: dict) -> None:
        self._w = win_holder
        self._p = proc_holder

    def is_top(self) -> bool:
        return SHELL.is_top()

    def toggle_top(self) -> bool:
        return SHELL.set_top(not SHELL.is_top())

    def minimize(self) -> bool:
        SHELL.minimize()
        return True

    def start_drag(self) -> bool:
        """由页面里的 mousedown 触发。

        WebView2 是独立子窗口且铺满客户区，窗口内部的鼠标消息根本不经过本进程
        窗体的 WndProc；而 js_api 回调又不在 GUI 线程上，直接发 SC_MOVE 系统不认
        （实测一秒内就返回、窗口纹丝不动）。所以自己驱动：SetCapture 把鼠标消息
        从 WebView2 抢过来（顺带避免拖动时莫名选中页面文字），再按光标位移搬窗口。
        """
        if not SHELL.hwnd or SHELL.dragging:
            return False
        SHELL.dragging = True
        threading.Thread(target=_drag_loop, args=(SHELL.hwnd,), daemon=True).start()
        return True

    def close_window(self) -> bool:
        shel = self._w.get("win")
        if shel is not None:
            shel.destroy()
        return True


# ------------------------------------------------------------------ 主流程
def already_running() -> bool:
    k32.CreateMutexW(None, False, "Local\\CognitionGadgetMutex")
    if k32.GetLastError() == ERROR_ALREADY_EXISTS:
        # 把已有窗口顶到前面，而不是无声退出（用户会以为双击没反应）
        for hwnd in find_windows(title=TITLE) + find_windows(title="侧车"):
            SHELL.hwnd = hwnd
            SHELL.focus()
            return True
        return True
    return False


def fatal(msg: str) -> None:
    log("FATAL:", msg)
    try:
        u32.MessageBoxW(None, msg, "桌面挂件启动失败", 0x10)
    except Exception:  # noqa: BLE001
        pass


def check_deps() -> bool:
    """缺依赖时给人话，而不是甩一个 ModuleNotFoundError 堆栈。

    最常见的起因不是没装，而是启动脚本挑错了 Python：本机 PATH 里躺着一个
    家目录下的 Python 3.12（py 启动器的默认目标），它既没有 streamlit 也没有
    pywebview。所以这里把「当前到底哪个解释器」和「怎么修」一起说清楚。
    """
    import importlib.util

    need = (("streamlit", "streamlit"), ("webview", "pywebview"), ("requests", "requests"))
    missing = [pkg for mod, pkg in need if importlib.util.find_spec(mod) is None]
    if not missing:
        return True

    log("缺依赖:", missing, "| 解释器:", sys.executable)
    fatal(
        "这个 Python 里缺依赖：" + "、".join(missing) + "\n\n"
        "解释器：\n" + sys.executable + "\n\n"
        "多半是启动脚本挑错了 Python 环境。修法二选一：\n\n"
        "  1) 直接双击「启动-桌面挂件.bat」\n"
        "     它按「能不能 import streamlit + webview」来挑解释器，\n"
        "     不看 PATH 里谁在前。\n\n"
        "  2) 手动装一次：\n"
        '     "' + console_python() + '" -m pip install -r requirements.txt'
    )
    return False


def main() -> int:
    setup_log()
    log("解释器:", sys.executable, "|", sys.version.split()[0])
    if not check_deps():
        return 1
    try:
        u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except Exception as e:  # noqa: BLE001
        log("DPI 感知设置失败（老系统），继续:", e)

    if already_running():
        log("已有实例在运行")
        return 0

    proc = ensure_server()
    if proc is not None and proc.poll() is not None:
        fatal("Streamlit 没起来。\n\n看 data/streamlit.log 里的报错，"
              "或先在命令行跑一次 python -m streamlit run app.py。")
        return 1
    if not healthy():
        fatal("等不到 http://localhost:8501 响应。\n\n看 data/streamlit.log。")
        stop_server(proc)
        return 1

    holder: dict = {"win": None}
    proc_holder: dict = {"proc": proc}
    atexit.register(lambda: stop_server(proc_holder.get("proc")))

    import webview

    api = Api(holder, proc_holder)
    win = webview.create_window(
        TITLE, URL,
        width=DEF_W, height=DEF_H,
        min_size=(MIN_W, MIN_H),
        frameless=True,
        easy_drag=False,          # 必须关：开着会把页面里的点击全吃掉
        on_top=True,
        resizable=True,
        background_color="#F7F6F2",
        js_api=api,
    )
    holder["win"] = win

    state = {"done": False}

    def on_shown():
        if state["done"]:
            return
        state["done"] = True
        pid = k32.GetCurrentProcessId()
        hwnd = None
        for _ in range(30):  # shown 可能早于主窗体可见，给 4.5 秒
            hwnd = pick_form_window(pid)
            if hwnd:
                break
            time.sleep(0.15)
        if not hwnd:
            log("找不到自己的窗口句柄，跳过钢壳")
            return
        SHELL.attach(hwnd)
        x, y, w, h = initial_geometry(hwnd)
        u32.ShowWindow(hwnd, SW_RESTORE)  # 万一进来时是最小化态，先恢复再摆位
        SHELL.place(x, y, w, h)
        time.sleep(0.05)
        SHELL.place(x, y, w, h)  # DWM 调整边框后偶有 1~2px 漂移，二次校准
        log("摆位:", x, y, w, h)

    def on_loaded():
        # 等 Streamlit 把首屏渲染出来，innerWidth 才是稳定值
        time.sleep(1.6)
        cw = read_css_width(win)
        SHELL.calibrate(cw)
        inject_bar(win)
        # 注入后再补一刀：Streamlit 首屏可能晚一步重排，二次注入是幂等的
        time.sleep(1.2)
        inject_bar(win)

    win.events.shown += on_shown
    win.events.loaded += on_loaded

    def on_closing():
        try:
            res = SHELL.size()
            if res:
                x, y, w, h = res
                save_state({"x": x, "y": y, "w": w, "h": h})
                log("记住位置:", x, y, w, h)
            else:
                log("窗口状态无效（可能已最小化），这次不记位置")
        except Exception as e:  # noqa: BLE001
            log("保存位置失败:", e)
        return True

    def on_closed():
        stop_server(proc_holder.get("proc"))

    win.events.closing += on_closing
    win.events.closed += on_closed

    try:
        webview.start(gui="edgechromium", debug=False)
    except Exception as e:  # noqa: BLE001
        fatal(f"WebView2 起不来：{e}\n\n多半是 Edge WebView2 Runtime 没装。")
        return 1
    finally:
        stop_server(proc_holder.get("proc"))
    return 0


if __name__ == "__main__":
    setup_log()
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        # pythonw 没有控制台，未捕获异常会静默消失，只留下"双击没反应"
        import traceback

        detail = traceback.format_exc()
        log("未捕获异常:\n" + detail)
        fatal("桌面挂件启动时崩了。\n\n详情已写入 data/gadget.log。\n\n"
              + detail.strip().splitlines()[-1][:200])
        sys.exit(1)
