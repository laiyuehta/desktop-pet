// 遙控頁 script 冒煙測試（node，假 DOM／假 fetch）：script 要能整段執行，螢幕畫面要真的設定圖片來源。
// 用法：node tests/js/remote_smoke.js pet/web/remote.html [mjpeg|nomjpeg]
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const mode = process.argv[3] || "mjpeg";
const js = html.split("<script>")[1].split("</script>")[0];
const els = {};
function el(sel) {
  if (!els[sel]) {
    const e = { sel, style: {}, textContent: "", innerHTML: "", value: sel === "#scr-fps" ? "10" : "", children: [],
      classList: { add() {}, remove() {}, toggle() {} }, addEventListener() {}, appendChild(c) { this.children.push(c); },
      setAttribute() {}, querySelectorAll() { return []; }, querySelector(s) { return el(sel + " " + s); },
      removeAttribute(k) { if (k === "src") { this._src = undefined; } }, srcLog: [] };
    Object.defineProperty(e, "src", { get() { return this._src; }, set(v) {
      this._src = v; this.srcLog.push(v);
      // 模擬瀏覽器：MJPEG 收到第一張會觸發 onload；nomjpeg 模式什麼都不觸發（像不支援 MJPEG 的瀏覽器）
      if (String(v).startsWith("/screen.mjpg") && mode === "mjpeg") setTimeout(() => this.onload && this.onload(), 50);
      if (String(v).startsWith("blob:")) setTimeout(() => this.onload && this.onload(), 10);
    } });
    els[sel] = e;
  }
  return els[sel];
}
const fetched = [];
global.document = { querySelector: el, querySelectorAll: () => [], getElementById: (i) => el("#" + i), createElement: (t) => el("new-" + t + Math.random()),
  addEventListener() {}, hidden: false };
global.window = global;
global.navigator = { userAgent: "node" };
global.location = { host: "192.0.2.43:8080", protocol: "http:" };
global.URL.createObjectURL = () => "blob:" + Math.random();
global.URL.revokeObjectURL = () => {};
global.fetch = async (url) => {
  fetched.push(String(url));
  let body = {};
  if (String(url).startsWith("/screen.info")) body = { ok: true, mode: "pack", pack: "seed", fps: 20, state: "idle", clip: "idle/base", frame: 1, viewers: 0, max_viewers: 3 };
  return { ok: true, status: 200, json: async () => body, blob: async () => ({}) };
};
global.alert = () => {}; global.confirm = () => true;
try {
  new Function(js)();
} catch (e) {
  console.log(JSON.stringify({ ok: false, error: String(e) })); process.exit(1);
}
setTimeout(() => {
  const img = els["#scr-img"] || { srcLog: [] };
  console.log(JSON.stringify({ ok: true, srcLog: img.srcLog.map(s => String(s).split("?")[0]), info: (els["#scr-info"] || {}).textContent,
    screenJpgFetches: fetched.filter(u => u.startsWith("/screen.jpg")).length, status: (els["#st-state"] || {}).textContent }));
  process.exit(0);
}, mode === "mjpeg" ? 1500 : 6000);
