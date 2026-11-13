"""v3 窄栏审计：新增的上传器、删除按钮、自由对话、个人主页。

用挂件的真实 CSS 内宽 327px 量，而不是想当然的 400px。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PORT = sys.argv[1] if len(sys.argv) > 1 else "8501"
URL = f"http://localhost:{PORT}/?embed=true&narrow=1"
OUT = ROOT / "data" / "shots-v3"
OUT.mkdir(parents=True, exist_ok=True)

MEASURE = """() => {
  const de = document.documentElement;
  const clipped = [];
  document.querySelectorAll('pre, pre code, .stMarkdown p').forEach(e => {
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
  return {sw: de.scrollWidth, cw: de.clientWidth, clipped: clipped.slice(0,5),
          maxPerRow: Math.max(0, ...Object.values(rows))};
}"""


def nav(pg, name: str) -> bool:
    loc = pg.get_by_text(name, exact=True)
    if not loc.count():
        return False
    loc.first.click()
    pg.wait_for_timeout(1800)
    return True


def measure(pg, label: str, w: int, fails: list[str]) -> None:
    m = pg.evaluate(MEASURE)
    ok = m["sw"] <= m["cw"] + 1 and not m["clipped"] and m["maxPerRow"] <= 2
    print(f"  {label:28} sw={m['sw']} cw={m['cw']} 同行按钮={m['maxPerRow']} "
          f"裁字={len(m['clipped'])}  {'OK' if ok else 'FAIL'}")
    if not ok:
        fails.append(f"{label}: {m}")
    pg.screenshot(path=str(OUT / f"narrow-{w}-{label}.png"), full_page=True)


def main() -> int:
    # 造一个 awaiting 会话，让工作台显示新加的上传器与删除按钮；审计完删掉
    os.environ.pop("COGNITION_DB", None)
    from core import db
    db.init_db()
    sid = db.create_session("窄栏审计临时会话")
    db.update_stage(sid, "awaiting_result")
    fails: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            for w in (327, 400):
                pg = browser.new_page(viewport={"width": w, "height": 780})
                pg.goto(URL, wait_until="networkidle")
                pg.wait_for_timeout(1800)
                # 顶部会话下拉里选中临时会话
                try:
                    pg.locator('[data-testid="stSelectbox"]').first.click()
                    pg.wait_for_timeout(300)
                    pg.get_by_role("option", has_text="窄栏审计临时会话").first.click()
                    pg.wait_for_timeout(1500)
                except Exception:  # noqa: BLE001
                    pg.keyboard.press("Escape")
                print(f"\n[{w}px]")
                measure(pg, "1-workbench", w, fails)
                if nav(pg, "对话"):
                    measure(pg, "2-chat", w, fails)
                else:
                    fails.append("找不到「对话」导航")
                if nav(pg, "我的"):
                    measure(pg, "3-profile", w, fails)
                    if nav(pg, "模型与设置"):
                        measure(pg, "4-settings", w, fails)
                    else:
                        fails.append("找不到「模型与设置」分区")
                else:
                    fails.append("找不到「我的」导航")
                pg.close()
            browser.close()
    finally:
        db.delete_session(sid)
        print("\n临时会话已清理")

    print("\n" + ("0 问题。" if not fails else "FAIL:\n  " + "\n  ".join(str(f) for f in fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
