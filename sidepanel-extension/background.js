// 侧边栏的开关逻辑。
// 一条原则：扩展只做「打开一个 URL」，不做注入、不读页面内容、不监听你访问了什么。
// 这样它永远碰不到被审阅站点的 DOM，也就不需要 content_scripts 和任何网页权限。

const APP_PATH = "/?embed=true&narrow=1";

// Chrome 116+：让浏览器自己处理图标点击（不需要我们自己 open），体验最稳
chrome.runtime.onInstalled.addListener(() => {
  if (chrome.sidePanel?.setPanelBehavior) {
    chrome.sidePanel
      .setPanelBehavior({ openPanelOnActionClick: true })
      .catch((err) => console.warn("setPanelBehavior 失败：", err));
  }
});

async function appUrl() {
  const { port = 8501 } = await chrome.storage.local.get("port");
  return `http://localhost:${port}${APP_PATH}`;
}

// 兜底路径：浏览器没有 sidePanel（或上面那步没生效）时，退化成一个独立小窗口。
// 位置和宽度都按侧栏的比例给，视觉上接近，但会遮住页面——所以它只是兜底，不是首选。
chrome.action.onClicked.addListener(async (tab) => {
  if (chrome.sidePanel?.open && tab?.windowId !== undefined) {
    try {
      await chrome.sidePanel.open({ windowId: tab.windowId });
      return;
    } catch (err) {
      console.warn("sidePanel.open 失败，改用弹出窗口：", err);
    }
  }
  const url = await appUrl();
  chrome.windows.create({ url, type: "popup", width: 480, height: 920 });
});
