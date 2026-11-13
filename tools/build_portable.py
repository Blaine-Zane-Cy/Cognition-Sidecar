"""方案 A 构建器：把当前项目冷冻成一个「拷过去就能双击」的文件夹。

设计要点（见 方案评估-独立运行.md）：
- 不编译、不联网（依赖目录已备好时）、不改一行业务代码 —— 本质就是精确复制。
- 只复制 requirements.txt 的干净依赖集，不带 agent 自己那 799MB 环境里的
  scipy / cv2 / playwright / sklearn。
- 入口是 .vbs + pythonw.exe：wscript 无控制台，双击全程不闪黑窗。
- 不带 secrets.json：API Key 不跟着文件夹跑到别的机器上。
- 规则库与模型设置**带过去**（用户长期资产），历史会话/结果/分支不带。

重跑：默认增量覆盖，不删目录里的其它东西；要彻底重建加 --clean。
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYHOME = Path(sys.base_prefix)
DEFAULT_DEST = ROOT / "认知掌控侧车-独立版"
SRC_FILES = ("app.py", "desktop_app.py", "requirements.txt", "README.md", ".streamlit/config.toml")

LAUNCH_VBS = """' Guardian Sidecar portable launcher. ASCII-only on purpose:
' Windows Script Host reads .vbs as ANSI, so non-ASCII bytes would corrupt it.
Option Explicit
Dim fso, sh, here, exe, script, q
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
exe = here & "\\runtime\\pythonw.exe"
script = here & "\\desktop_app.py"
q = Chr(34)
If Not fso.FileExists(exe) Then
    MsgBox "Missing runtime\\pythonw.exe. Copy the WHOLE folder, not just this file.", 16, "Guardian Sidecar"
    WScript.Quit 1
End If
If Not fso.FileExists(script) Then
    MsgBox "Missing desktop_app.py. Copy the WHOLE folder, not just this file.", 16, "Guardian Sidecar"
    WScript.Quit 1
End If
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = here
' 0 = hidden window, False = do not wait. pythonw.exe itself has no console.
sh.Run q & exe & q & " " & q & script & q, 0, False
"""

SHORTCUT_VBS = """' Create a desktop shortcut to the launcher. ASCII-only on purpose.
' Optional argument: an output folder (used by the self-test so it never touches
' the real desktop). Without arguments it writes to the user's Desktop.
Option Explicit
Dim fso, sh, here, outDir, f, selfName, launcher, lnk, path
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
If WScript.Arguments.Count > 0 Then
    outDir = WScript.Arguments(0)
Else
    outDir = sh.SpecialFolders("Desktop")
End If
If Not fso.FolderExists(outDir) Then
    MsgBox "Output folder not found: " & outDir, 16, "Guardian Sidecar"
    WScript.Quit 1
End If
' There are exactly two .vbs files in this folder. The one that is not this
' script is the launcher -- locating it by scanning avoids writing a Chinese
' filename inside an ASCII-only script.
selfName = LCase(fso.GetFileName(WScript.ScriptFullName))
For Each f In fso.GetFolder(here).Files
    If LCase(fso.GetExtensionName(f.Name)) = "vbs" And LCase(f.Name) <> selfName Then
        launcher = f.Path
        Exit For
    End If
Next
If launcher = "" Then
    MsgBox "Launcher .vbs not found.", 16, "Guardian Sidecar"
    WScript.Quit 1
End If
path = outDir & "\\Guardian Sidecar.lnk"
Set lnk = sh.CreateShortcut(path)
lnk.TargetPath = launcher
lnk.WorkingDirectory = here
lnk.IconLocation = here & "\\runtime\\pythonw.exe,0"
lnk.Description = "Guardian Sidecar"
lnk.Save
' With an explicit output folder (self-test), stay silent: a MsgBox would block.
If WScript.Arguments.Count = 0 Then
    MsgBox "Shortcut created:" & vbCrLf & path, 64, "Guardian Sidecar"
End If
"""

README_TXT = """认知掌控侧车 · 独立版
========================================

【怎么用】
双击「启动.vbs」。第一次会稍慢（约 6 秒，后台要起一个本地服务），
之后窗口会原样出现在上次的位置。关掉窗口即退出。

【换机器 / 换目录】
把整个文件夹原样拷走即可。路径随便放，不用改任何配置，
不用装 Python、Node，也不用联网。
注意：要拷「整个文件夹」，单独拷那个 .vbs 是跑不起来的。

【桌面快捷方式】
双击「创建桌面快捷方式.vbs」，会在桌面生成一个快捷方式，
效果和双击「启动.vbs」完全一样。

【什么带了、什么没带】
带：偏好规则库、模型设置、全部源码、Python 运行时和依赖。
没带：历史会话/结果卡/分支（干净起步）、API Key（data/secrets.json）。
      到新机器上想用真实模型，在「我的 → 模型与设置」里填一次 Key；
      不填也能用，会走演示替身。

【系统要求】
Windows 10 1903+ / Windows 11，64 位，系统自带 WebView2 与 .NET Framework 4.8。
这两样是 Windows 自带组件，不是需要另外安装的开发环境。
极少数精简版 / LTSC 系统可能没有 WebView2，那种情况窗口会起不来，
详见项目里的「方案评估-独立运行.md」第 3 节。

【日志】
启动出问题时看 data\\gadget.log 和 data\\streamlit.log。
"""


def ignore_pycache(dirpath: str, names: list[str]) -> set[str]:
    out = {n for n in names if n == "__pycache__" or n.endswith(".pyc")}
    if Path(dirpath).resolve() == (PYHOME / "Lib").resolve():
        out.add("site-packages")  # 本机那 799MB 环境绝对不能带过去
    return out


def copy_runtime(dest: Path) -> None:
    rt = dest / "runtime"
    rt.mkdir(parents=True, exist_ok=True)
    for name in (
        "python.exe", "pythonw.exe", "python3.dll", "python313.dll",
        "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt",
    ):
        src = PYHOME / name
        if src.is_file():
            shutil.copy2(src, rt / name)
    for sub in ("DLLs",):
        shutil.copytree(PYHOME / sub, rt / sub, dirs_exist_ok=True, ignore=ignore_pycache)
    shutil.copytree(PYHOME / "Lib", rt / "Lib", dirs_exist_ok=True, ignore=ignore_pycache)
    if not (rt / "pythonw.exe").exists():
        raise RuntimeError("CPython 运行时复制失败：缺 pythonw.exe")


def resolve_deps(arg: str | None) -> Path:
    if arg:
        p = Path(arg)
        if not p.is_dir():
            raise RuntimeError(f"依赖目录不存在：{p}")
        return p
    found = sorted(glob.glob(os.path.join(tempfile.gettempdir(), "sidecar-vol-*")))
    found = [Path(p) for p in found if (Path(p) / "streamlit").is_dir()]
    if found:
        return found[-1]
    # 没有现成的干净依赖集时，联网装一次到临时目录（构建机联网，目标机不用）
    p = Path(tempfile.mkdtemp(prefix="sidecar-vol-"))
    print(f"未找到现成依赖，正在安装到 {p} …")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--target", str(p),
         "-r", str(ROOT / "requirements.txt"), "--no-warn-script-location", "-q"],
        check=True,
    )
    return p


def copy_source(dest: Path) -> None:
    for rel in SRC_FILES:
        src = ROOT / rel
        if not src.is_file():
            continue
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
    for pkg in ("core", "ui"):
        out_dir = dest / pkg
        out_dir.mkdir(exist_ok=True)
        for f in sorted((ROOT / pkg).glob("*.py")):
            shutil.copy2(f, out_dir / f.name)


def make_db(dest: Path, py: Path) -> None:
    """用包内解释器建一个干净库，再把规则库与设置搬过去。"""
    (dest / "data").mkdir(exist_ok=True)
    env = os.environ.copy()
    env.pop("COGNITION_DB", None)      # 绝不能让 agent 注入的路径跟过来
    env.pop("COGNITION_FORCE_MOCK", None)
    subprocess.run(
        [str(py), "-c", "from core import db; db.init_db(); print('db:', db.db_file())"],
        cwd=str(dest), env=env, check=True, capture_output=True, text=True,
    )
    src = ROOT / "data" / "app.db"
    if not src.exists():
        return
    # WAL 模式要先用 backup API 取一致快照，直接读会漏掉最近写入
    snap = Path(tempfile.mkdtemp()) / "snapshot.db"
    s0, t0 = sqlite3.connect(str(src)), sqlite3.connect(str(snap))
    s0.backup(t0)
    t0.close(); s0.close()
    s = sqlite3.connect(str(snap)); s.row_factory = sqlite3.Row
    d = sqlite3.connect(str(dest / "data" / "app.db"))
    for table in ("rules", "settings", "rule_usages"):
        cols = [r[1] for r in d.execute(f"PRAGMA table_info({table})")]
        if not cols:
            continue
        try:
            rows = [tuple(r) for r in s.execute(f"SELECT {','.join(cols)} FROM {table}")]
        except sqlite3.Error:
            continue
        if table == "rule_usages":
            rows = [tuple(None if c == "review_id" else v for c, v in zip(cols, r)) for r in rows]
        marks = ",".join("?" for _ in cols)
        d.executemany(
            f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({marks})", rows
        )
        print(f"  带过去 {table}: {len(rows)} 行")
    d.commit(); d.close(); s.close()


def write_entries(dest: Path) -> None:
    # .vbs 内容纯 ASCII；文件名可以是中文（由 WScript.ScriptFullName 推导，不写死在脚本里）
    (dest / "启动.vbs").write_bytes(LAUNCH_VBS.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii"))
    (dest / "创建桌面快捷方式.vbs").write_bytes(
        SHORTCUT_VBS.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii")
    )
    # UTF-8 BOM：记事本（Win10 1903+）和编辑器预览都能正确显示中文；
    # .txt 不像 .bat 那样由 cmd 按 GBK 解析，所以不必用 GBK。
    (dest / "使用说明.txt").write_bytes(
        README_TXT.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8-sig")
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default=str(DEFAULT_DEST))
    ap.add_argument("--deps", default=None, help="干净依赖目录；默认自动查找或现装")
    ap.add_argument("--clean", action="store_true", help="先删掉目标目录再建")
    args = ap.parse_args()
    dest = Path(args.dest).resolve()

    if dest.exists() and args.clean:
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    print("1/5 复制 CPython 运行时 …")
    copy_runtime(dest)
    print("2/5 复制干净依赖集 …")
    deps = resolve_deps(args.deps)
    shutil.copytree(
        deps, dest / "runtime" / "Lib" / "site-packages", dirs_exist_ok=True,
        ignore=ignore_pycache,
    )
    print("3/5 复制源码 …")
    copy_source(dest)
    print("4/5 建干净数据库并搬规则/设置 …")
    make_db(dest, dest / "runtime" / "python.exe")
    print("5/5 写入口与说明 …")
    write_entries(dest)

    # 用包内解释器自检：这一步同时证明 sys.path 与依赖都装对了
    env = os.environ.copy()
    env.pop("COGNITION_DB", None)
    check = subprocess.run(
        [str(dest / "runtime" / "python.exe"), "-c",
         "import sys,streamlit,webview,requests; "
         "print(sys.executable); print(streamlit.__version__, webview.__version__ if hasattr(webview,'__version__') else 'ok')"],
        cwd=str(dest), env=env, capture_output=True, text=True,
    )
    print("\n包内解释器自检 rc =", check.returncode)
    print(check.stdout.strip() or check.stderr.strip()[-500:])
    if check.returncode:
        return 1

    size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
    count = sum(1 for f in dest.rglob("*") if f.is_file())
    print(f"\n产物：{dest}")
    print(f"体积：{size / 1024 / 1024:.0f} MB / {count} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
