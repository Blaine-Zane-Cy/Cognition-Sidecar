"""窄栏/宽屏全屏审计：逐屏截图 + 硬指标断言。仅本地调试用。

    python data/audit.py
"""
from __future__ import annotations

import json
import os
import sys

from playwright.sync_api import sync_playwright

PORT = sys.argv[1] if len(sys.argv) > 1 else "8501"
BASE_NARROW = f"http://localhost:{PORT}/?embed=true&narrow=1"
BASE_WIDE = f"http://localhost:{PORT}/"
OUT = "D:/新建文件夹/2026-11-08-18-46-10/data/shots"
os.makedirs(OUT, exist_ok=True)

MEASURE = """() => {
  const de = document.documentElement;
  const clipped = [];
  document.querySelectorAll('pre, pre code, .stMarkdown p').forEach(e => {
    // 折叠着的 expander 里的代码块没有真实布局，量出来是假阳性，跳过
    const det = e.closest('details');
    if (det && !det.open) return;
    if (e.scrollWidth > e.clientWidth + 1)
      clipped.push([e.tagName, e.scrollWidth, e.clientWidth, (e.innerText||'').slice(0,20)]);
  });
  const rows = {};
  document.querySelectorAll('button').forEach(e => {
    const r = e.getBoundingClientRect();
    const t = (e.innerText||'').trim();
    if (r.width > 0 && t && r.y > 120) { const k = Math.round(r.y/5)*5; rows[k] = (rows[k]||0)+1; }
  });
  return {sw: de.scrollWidth, cw: de.clientWidth, clipped: clipped.slice(0,6),
          maxPerRow: Math.max(0, ...Object.values(rows))};
}"""


def pick(pg, hint: str, idx: int = 0) -> None:
    """切会话。找不到就跳过——审计是为了量布局，不该因为数据不一样就崩。"""
    try:
        box = pg.locator('[data-testid="stSelectbox"]').nth(idx)
        box.click()
        pg.wait_for_timeout(350)
        pg.locator('[role="option"]', has_text=hint).first.click()
        pg.wait_for_timeout(2200)
    except Exception:  # noqa: BLE001
        pg.keyboard.press("Escape")
        print(f"  （跳过：下拉里没有「{hint}」）")


def audit(pg, label: str, url: str, w: int, h: int, steps) -> list[str]:
    pg.set_viewport_size({"width": w, "height": h})
    pg.goto(url, wait_until="networkidle")
    pg.wait_for_timeout(2300)
    problems = []
    for name, action in steps:
        if action:
            action()
        m = pg.evaluate(MEASURE)
        tag = f"{label}/{name}"
        flag = ""
        if m["sw"] > m["cw"] + 1:
            flag = f"  ← 横向溢出 {m['sw']}>{m['cw']}"
            problems.append(tag + " 横向溢出")
        if m["clipped"]:
            problems.append(f"{tag} 内容被裁 {m['clipped'][:2]}")
            flag += f"  ← 裁切 {m['clipped'][:2]}"
        if m["maxPerRow"] > 2 and w < 700:
            problems.append(f"{tag} 同行按钮 {m['maxPerRow']} 个")
            flag += f"  ← 同行 {m['maxPerRow']} 按钮"
        print(f"{tag:38s} h={m['maxPerRow']} {flag}")
        pg.screenshot(path=f"{OUT}/{label}-{name}.png")
    return problems


def click_nav(pg, text: str, narrow: bool = True):
    def _go():
        if narrow:
            pg.locator('[data-testid="stButtonGroup"] button', has_text=text).first.click()
        else:
            pg.locator('[data-testid="stSidebar"] label', has_text=text).first.click()
        pg.wait_for_timeout(1800)
    return _go


def open_card(pg):
    def _go():
        pg.locator("summary", has_text="展开完整结果卡").first.click()
        pg.wait_for_timeout(1500)
    return _go


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 420, "height": 900}, device_scale_factor=2)

        problems += audit(pg, "n420", BASE_NARROW, 420, 900, [
            ("01-delegation", None),
            ("02-clarify", lambda: pick(pg, "先修移动端还是先补部署步骤")),
            ("03-review", lambda: pick(pg, "帮我做个个人网站")),
            ("04-review-card", open_card(pg)),
            ("05-rules", click_nav(pg, "规则库", True)),
            ("06-settings", click_nav(pg, "设置", True)),
            ("07-reviews-list", click_nav(pg, "结果卡", True)),
        ])

        pg2 = b.new_page(viewport={"width": 480, "height": 780}, device_scale_factor=2)
        problems += audit(pg2, "n480", BASE_NARROW, 480, 780, [
            ("01-delegation", None),
            ("02-review", lambda: pick(pg2, "帮我做个个人网站")),
        ])

        pg3 = b.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=1)
        problems += audit(pg3, "w1600", BASE_WIDE, 1600, 1000, [
            ("01-delegation", None),
            ("02-clarify", lambda: pick(pg3, "先修移动端还是先补部署步骤")),
            ("03-review", lambda: pick(pg3, "帮我做个个人网站")),
            ("04-rules", click_nav(pg3, "规则库", False)),
            ("05-settings", click_nav(pg3, "设置", False)),
        ])
        b.close()

    print("\n问题清单：")
    for x in problems:
        print(" -", x)
    print("合计", len(problems), "项")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(run())
