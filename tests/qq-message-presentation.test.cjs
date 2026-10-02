const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const app = fs.readFileSync(path.join(__dirname, "../chatui/app.js"), "utf8");
const start = app.indexOf("function messageBubble(");
const end = app.indexOf("function messageNode(", start);
assert.ok(start >= 0 && end > start);
function element(tag, className, text = "") {
  assert.equal(tag, "div", "presentation must never create a network/media element");
  return { tag, className, textContent: text, children: [],
    appendChild(child) { this.children.push(child); return child; },
    get childNodes() { return this.children; },
    get text() { return [this.textContent, ...this.children.map(item => item.text)].join(" "); } };
}
function render(message, product = "qq") {
  const context = { window: { ProductConfig: { key: product } }, element };
  vm.runInNewContext(app.slice(start, end) + "this.render = messageBubble;", context);
  return context.render(message);
}
const details = extra => ({ parts: [], hasQuote: false, quoteText: null, quoteTruncated: false, ...extra });

test("each supported descriptor explains that its actual media content is unread", () => {
  for (const [kind, title] of Object.entries({ image: "图片", audio: "语音", file: "文件", video: "视频", face: "表情" })) {
    const view = render({ kind: "other", text: "[未知类型]", qqDisplay: details({ parts: [kind] }) });
    assert.match(view.text, new RegExp(`\\[${title}\\].*内容未读取`));
    assert.doesNotMatch(view.text, /未知类型/);
  }
});
test("a quote is a separate display block and keeps literal HTML and newlines", () => {
  const view = render({ kind: "text", text: "这是作者自己的回复", qqDisplay: details({
    hasQuote: true, quoteText: "<img src=https://never-fetch.invalid>\n别人的话", parts: ["image"],
  }) });
  assert.equal(view.children[0].className, "qq-message-quote");
  assert.match(view.children[0].text, /<img.*\n别人的话/);
  assert.equal(view.children.at(-1).textContent, "这是作者自己的回复");
  assert.equal(view.children.at(-1).className, "qq-message-body");
});
test("missing quote content stays visibly unavailable instead of being guessed", () => {
  const view = render({ kind: "other", text: "[未知类型]", qqDisplay: details({ hasQuote: true }) });
  assert.match(view.text, /引用内容未保存在本机/);
  assert.doesNotMatch(view.text, /未知类型/);
});
test("bounded quotes preserve Unicode codepoints and state truncation", () => {
  const view = render({ kind: "text", text: "回复", qqDisplay: details({
    hasQuote: true, quoteText: "😀".repeat(4000), quoteTruncated: true,
  }) });
  const quote = view.children[0];
  assert.equal(Array.from(quote.children[1].textContent).length, 4000);
  assert.match(quote.text, /仅显示前 4000 字/);
});
test("unknown descriptors do not acquire invented media names", () => {
  const view = render({ kind: "other", text: "[未知类型]", qqDisplay: details({ parts: ["unknown", "unsupported", "__proto__"] }) });
  assert.match(view.text, /未知类型.*内容暂不支持/);
  assert.doesNotMatch(view.text, /unsupported|__proto__|图片/);
});
test("plain text, recalled rows and the WeChat renderer retain their legacy display", () => {
  assert.equal(render({ kind: "text", text: "普通文本" }).textContent, "普通文本");
  assert.equal(render({ kind: "text", text: " \n\t" }).textContent, "[空白文本]");
  assert.equal(render({ kind: "other", text: "[消息已撤回]" }).textContent, "[消息已撤回]");
  assert.equal(render({ kind: "image", text: "", qqDisplay: details({ parts: ["audio"] }) }, "wechat").textContent, "[图片]");
});
