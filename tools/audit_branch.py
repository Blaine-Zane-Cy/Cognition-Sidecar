"""分支页的窄栏审计：400 / 480 / 1600 逐屏量硬指标。

    python data/audit_branch.py [端口]

断言（和 data/audit.py 同一套口径）：
- 横向不溢出：scrollWidth <= clientWidth + 1
- 同一行最多两颗按钮（窄栏里三颗就挤成一行看不清）
- 点开分支、退回主干，两屏都要量；顺带留下截图

前提：库里已有一条带分支的评审（python data/seed_branch_demo.py）。
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

PORT = sys.argv[1] if len(sys.argv) > 1 else "8501"
NARROW = f"http://localhost:{PORT}/?embed=true&narrow=1"
OUT = "D:/新建文件夹/2026-11-08-18-46-10/data/shots-branch"
os.makedirs(OUT, exist_ok=True)

MEASURE = """() => {
  const de = document.documentElement;
  const clipped = [];
  document.querySelectorAll('pre, pre code, .stMarkdown p').forEach(e => {
    const det = e.closest('details');
    if (det && !det.open) return;     // 折叠区里的代码块没有真实布局
    if (e.scrollWidth > e.clientWidth + 1)
      clipped.push([e.tagName, e.scrollWidth, e.clientWidth, (e.innerText||'').slice(0,24)]);
  });
  const rows = {};
  document.querySelectorAll('button').forEach(e => {
    const r = e.getBoundingClientRect();
    const t = (e.innerText||'').trim();
    if (r.width > 0 && t && r.y > 100) { const k = Math.round(r.y/5)*5; rows[k] = (rows[k]||0)+1; }
  });
  return {sw: de.scrollWidth, cw: de.clientWidth, clipped: clipped.slice(0,6),
          maxPerRow: Math.max(0, ...Object.values(rows))};
}"""

fails: list[str] = []


def measure(pg, label: str) -> dict:
    res = pg.evaluate(MEASURE)
    ok_w = res["sw"] <= res["cw"] + 1
    ok_b = res["maxPerRow"] <= 2
    print(f"  {label}: {res['cw']}px 视口 / 内容 {res['sw']}px · 同行最多 {res['maxPerRow']} 颗按钮")
    if not ok_w:
        fails.append(f"{label} 横向溢出 {res['sw']}>{res['cw']} {res['clipped']}")
    if not ok_b:
        fails.append(f"{label} 同行 {res['maxPerRow']} 颗按钮")
    if res["clipped"]:
        print(f"    裁切嫌疑: {res['clipped']}")
    return res


def scene(pg, url: str, width: int, label: str) -> None:
    pg.set_viewport_size({"width": width, "height": 800})
    pg.goto(url, wait_until="networkidle")
    pg.wait_for_timeout(2200)
    measure(pg, f"{label} · 主干")
    pg.screenshot(path=f"{OUT}/{label}-1-trunk.png", full_page=False)

    btn = pg.get_by_role("button", name="聊这一条").first
    if btn.count() == 0:
        print(f"  {label}: 没找到「聊这一条」按钮，跳过分支页")
        fails.append(f"{label} 找不到分支入口")
        return
    btn.click()
    # 显式等那根路由条出现，别用固定 sleep：Streamlit 重跑有快有慢
    back = pg.get_by_role("button", name="← 主干").first
    try:
        back.wait_for(state="visible", timeout=10000)
    except Exception:  # noqa: BLE001
        fails.append(f"{label} 等待分支页超时")
        return
    pg.wait_for_timeout(600)
    measure(pg, f"{label} · 分支")
    pg.screenshot(path=f"{OUT}/{label}-2-branch.png", full_page=False)

    if back.count() == 0:
        fails.append(f"{label} 分支页没有「← 主干」")
        return
    back.click()
    pg.wait_for_timeout(1400)
    res = measure(pg, f"{label} · 退回主干")
    if pg.get_by_role("button", name="聊这一条").count() == 0:
        fails.append(f"{label} 退回后卡片消失")


def run() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        # 327 = 挂件默认 400 逻辑宽在 WebView2 里的真实 CSS 内宽；300 = 最小窗口 380 逻辑宽
        for width in (327, 300, 400, 480):
            print(f"\n[{width}px 窄栏]")
            pg = browser.new_page(viewport={"width": width, "height": 800})
            scene(pg, NARROW, width, f"narrow-{width}")
            pg.close()
        # 宽屏没跑：宽屏要先在侧栏选会话才进得了审阅页，用浏览器点选容易假阴性。
        # 宽屏的分支视图（两列输入框）由 data/uitest_branch.py 的 AppTest 覆盖。
        browser.close()

    print("\n" + ("0 问题。" if not fails else f"{len(fails)} 个问题："))
    for f in fails:
        print("  -", f)
    print(f"截图：{OUT}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(run())
