"""方案 A 成功标准 3 / 4：换路径、桌面快捷方式。

把整个文件夹拷到另一个盘符/另一层目录，再各自双击一次；
然后生成快捷方式并双击它，看是否和双击原入口效果一致。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_portable as vp  # noqa: E402  复用窗口枚举与环境断言

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "认知掌控侧车-独立版"
TMP = Path(tempfile.gettempdir())
ALT = TMP / "gadget-alt-path" / "认知掌控侧车-独立版"
LNK_DIR = TMP / "gadget-shortcut-out"
LNK = LNK_DIR / "Guardian Sidecar.lnk"


def wait_window(timeout: int = 70) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        hits = [
            w for w in vp.windows()
            if vp.TITLE in w["title"] and w["w"] > 100 and w["h"] > 100
        ]
        if hits:
            return max(hits, key=lambda w: w["w"] * w["h"])
        time.sleep(0.5)
    return None


def main() -> int:
    fails: list[str] = []

    print("[1] 复制整个文件夹到另一个路径 …")
    if ALT.exists():
        shutil.rmtree(ALT)
    ALT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SRC, ALT)
    n = sum(1 for f in ALT.rglob("*") if f.is_file())
    print(f"    {ALT}  ({n} 个文件)")
    if n < 1000:
        fails.append("复制不完整")

    print("[2] 在新路径双击 启动.vbs（同样污染 PATH / 断代理）…")
    rc = subprocess.run(
        [sys.executable, str(ROOT / "data" / "verify_portable.py"), str(ALT)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=180,
    ).returncode
    print("    verify_portable rc =", rc)
    if rc:
        fails.append("换路径后启动失败")
        print(rc and "")

    print("[3] 生成桌面快捷方式（带参数=静默，输出目录不在真桌面）…")
    LNK_DIR.mkdir(exist_ok=True)
    if LNK.exists():
        LNK.unlink()
    subprocess.run(
        [r"C:\Windows\System32\wscript.exe",
         str(ALT / "创建桌面快捷方式.vbs"), str(LNK_DIR)],
        cwd=str(ALT), timeout=30, check=True,
    )
    print("    快捷方式存在:", LNK.exists())
    if not LNK.exists():
        fails.append("快捷方式没生成")
        return 1
    raw = LNK.read_bytes()
    needle = str(ALT).encode("utf-16-le")
    print("    含目标文件夹路径:", needle in raw)
    if needle not in raw:
        fails.append("快捷方式没有指向新路径的文件夹")

    print("[4] 双击快捷方式 …")
    before = [w["hwnd"] for w in vp.windows() if vp.TITLE in w["title"]]
    os.startfile(str(LNK))
    hit = wait_window()
    if not hit:
        fails.append("双击快捷方式没出现窗口")
    else:
        print(f"    窗口 {hit['w']}x{hit['h']} 类={hit['cls']}")
        if hit["hwnd"] in before:
            fails.append("看到的是上一个残留窗口，不是新起的")
        if not vp.health():
            fails.append("快捷方式启动后服务没起来")
        vp.u32.PostMessageW(hit["hwnd"], 0x0010, 0, 0)
        for _ in range(30):
            if not any(w["hwnd"] == hit["hwnd"] for w in vp.windows()):
                break
            time.sleep(0.3)
        time.sleep(2)
        print("    关窗后 8501 仍存活:", vp.health())
        if vp.health():
            fails.append("关窗后没停掉服务")

    print("\n" + ("全部通过。" if not fails else "FAIL:\n  - " + "\n  - ".join(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
