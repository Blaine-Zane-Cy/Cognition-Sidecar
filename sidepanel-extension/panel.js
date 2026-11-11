// 面板的职责只有一个：确认本地服务活着，然后把整个面板导航过去。
// 为什么不用 iframe 嵌入：Streamlit 会带 X-Frame-Options / frame-ancestors，
// 而且 iframe 里的 ?embed 参数和兄弟窗口的会话是两套东西，反而更乱。
// 直接导航 = 侧栏里就是那个页面本身，CSS 断点、滚动、输入焦点都是原生的。

const PORT_DEFAULT = 8501;
const PATH = "/?embed=true&narrow=1";

const dot = document.getElementById("dot");
const statusEl = document.getElementById("status");
const hintEl = document.getElementById("hint");
const retryBtn = document.getElementById("retry");
const portInput = document.getElementById("port");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function setState(kind, text, hint = "") {
  dot.className = `dot ${kind}`;
  statusEl.textContent = text;
  hintEl.innerHTML = hint;
}

async function reachable(port) {
  // no-cors：只想知道"有没有东西在听"，不读响应体。
  // 加了这个模式，浏览器不会因为缺 CORS 头而 reject，网络层通了就算通。
  try {
    await fetch(`http://localhost:${port}/_stcore/health`, {
      mode: "no-cors",
      cache: "no-store",
      signal: AbortSignal.timeout(2500),
    });
    return true;
  } catch (e) {
    return false;
  }
}

async function boot() {
  const { port = PORT_DEFAULT } = await chrome.storage.local.get("port");
  portInput.value = port;
  retryBtn.disabled = true;
  retryBtn.textContent = "正在连接…";
  setState("wait", `正在检查 localhost:${port} …`);

  const ok = await reachable(port);
  if (ok) {
    setState("ok", "服务在跑，正在打开…");
    retryBtn.textContent = "已连上";
    location.replace(`http://localhost:${port}${PATH}`);
    return;
  }

  setState(
    "bad",
    `localhost:${port} 上没有服务`,
    '先把侧车跑起来：双击项目里的 <code>启动.bat</code>，或在项目目录执行 ' +
      '<code>python -m streamlit run app.py</code>。' +
      "<br><br>跑起来之后再点下面的「重试」——不用重装扩展。",
  );
  retryBtn.disabled = false;
  retryBtn.textContent = "重试";
}

retryBtn.addEventListener("click", async () => {
  const port = parseInt(portInput.value, 10);
  if (!Number.isFinite(port) || port < 1024 || port > 65535) {
    setState("bad", "端口不对", "填 1024–65535 之间的数字，默认 8501。");
    return;
  }
  await chrome.storage.local.set({ port });
  boot();
});

boot();
