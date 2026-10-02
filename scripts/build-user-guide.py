"""Build a self-contained, offline Chinese user manual from the public Markdown guides."""
from __future__ import annotations
import argparse
import base64
import hashlib
import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = ("USER-GUIDE.md", "SETUP.md", "COMPATIBILITY.md", "KNOWN-ISSUES.md",
             "BACKUP-UPGRADE.md", "R9-TRIAL.md", "FEEDBACK.md")
SLUGS = {name: Path(name).stem.lower() for name in DOCUMENTS}

def build(root: Path, output: Path) -> dict:
    docs = root / "docs/public"
    images = set()
    def inline(source: str) -> str:
        saved = []
        def keep(fragment):
            saved.append(fragment)
            return f"@@INLINE{len(saved)-1}@@"
        def picture(match):
            alt, target = match.groups()
            path = (docs / target).resolve()
            if not path.is_relative_to((docs / "images").resolve()) or not path.is_file() or path.suffix != ".png":
                raise ValueError("manual image must be a reviewed local PNG")
            images.add(path.relative_to(root).as_posix())
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            return keep(f'<figure><img alt="{html.escape(alt, quote=True)}" src="data:image/png;base64,{encoded}" loading="lazy"></figure>')
        source = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", picture, source)
        source = re.sub(r"`([^`]+)`", lambda m: keep("<code>" + html.escape(m[1]) + "</code>"), source)
        def link(match):
            label, target = match.groups()
            filename = Path(target.split("#")[0]).name
            if filename in SLUGS:
                target = "#" + SLUGS[filename]
            elif not re.match(r"^(https?://|#|mailto:)", target):
                target = "https://github.com/xzyj50609/QQVibe/blob/preview/" + (docs / target).relative_to(root).as_posix()
            return keep(f'<a href="{html.escape(target, quote=True)}">{html.escape(label)}</a>')
        source = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link, source)
        source = html.escape(source)
        source = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", source)
        source = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", source)
        for index, fragment in enumerate(saved): source = source.replace(f"@@INLINE{index}@@", fragment)
        return source
    def convert(source: str) -> str:
        result = []; paragraph = []; list_kind = None; fence = False; code = []
        rows = source.splitlines(); index = 0
        def flush():
            if paragraph: result.append("<p>" + inline(" ".join(paragraph)) + "</p>"); paragraph.clear()
        def close_list():
            nonlocal list_kind
            if list_kind: result.append("</" + list_kind + ">"); list_kind = None
        while index < len(rows):
            line = rows[index].strip(); index += 1
            if line.startswith("```"):
                flush(); close_list()
                if fence: result.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>"); code=[]
                fence = not fence; continue
            if fence: code.append(rows[index-1]); continue
            if not line: flush(); close_list(); continue
            heading = re.match(r"^(#{1,6})\s+(.+)$", line)
            if heading:
                flush(); close_list(); level = min(len(heading[1])+1, 6)
                result.append(f"<h{level}>"+inline(heading[2])+f"</h{level}>"); continue
            if line.startswith("|") and index < len(rows) and re.match(r"^\s*\|?[\s:|\-]+\|?\s*$", rows[index]):
                flush(); close_list(); head=[x.strip() for x in line.strip("|").split("|")]; index += 1
                result.append('<div class="table-scroll"><table><thead><tr>'+"".join("<th>"+inline(x)+"</th>" for x in head)+"</tr></thead><tbody>")
                while index < len(rows) and rows[index].strip().startswith("|"):
                    cells=[x.strip() for x in rows[index].strip().strip("|").split("|")]; index += 1
                    result.append("<tr>"+"".join("<td>"+inline(x)+"</td>" for x in cells)+"</tr>")
                result.append("</tbody></table></div>"); continue
            if line.startswith(("<details", "</details", "<summary", "</summary")):
                flush(); close_list(); result.append(line); continue
            item = re.match(r"^(?:([-*])\s+|(\d+)[.)]\s+)(.+)$", line)
            if item:
                flush(); kind = "ul" if item[1] else "ol"
                if list_kind != kind:
                    close_list(); list_kind=kind
                    start = f' start="{int(item[2])}"' if kind == "ol" and item[2] != "1" else ""
                    result.append("<"+kind+start+">")
                result.append("<li>"+inline(item[3])+"</li>"); continue
            if line.startswith("> "):
                flush(); close_list(); result.append("<blockquote>"+inline(line[2:])+"</blockquote>"); continue
            if line.startswith("!["):
                flush(); close_list(); result.append(inline(line)); continue
            paragraph.append(line)
        flush(); close_list()
        if fence: raise ValueError("unterminated code block in public manual")
        return "\n".join(result)
    sections = []
    for name in DOCUMENTS:
        text = (docs / name).read_text(encoding="utf8")
        sections.append(f'<section id="{SLUGS[name]}">' + convert(text) + "</section>")
    version = json.loads((root / "package.json").read_text(encoding="utf8"))["version"]
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>QQVibe 使用说明</title><style>
:root{color-scheme:light;font-family:"Segoe UI","Microsoft YaHei",sans-serif;color:#26302e;background:#f3f6f5}*{box-sizing:border-box}body{margin:0;line-height:1.85}header{background:#fff;border-bottom:1px solid #dce5e0;padding:26px max(24px,calc((100vw - 1040px)/2))}header h1{margin:0;font-size:30px;color:#17634b}header p{margin:4px 0;color:#61736b}nav{display:flex;gap:10px 22px;flex-wrap:wrap;margin-top:16px}a{color:#136649;text-decoration:none;border-bottom:1px solid #bcdace}a:hover{text-decoration:underline}main{max-width:1040px;margin:28px auto;padding:0 24px}section{background:#fff;padding:28px 36px;margin-bottom:22px;border:1px solid #e1e9e5;border-radius:14px;scroll-margin-top:20px}h2{margin-top:0;font-size:25px;color:#17634b}h3{margin-top:30px;font-size:20px}h4{font-size:17px}p{margin:12px 0}li{margin:8px 0}figure{margin:22px 0}img{display:block;width:100%;height:auto;border:1px solid #dce5e0;border-radius:10px}blockquote{margin:16px 0;padding:12px 18px;background:#edf6f1;border-left:4px solid #318065;color:#3c6252}code{font-family:Consolas,monospace;font-size:.92em;background:#eef2ef;padding:2px 5px;border-radius:4px}pre{overflow:auto;padding:16px;background:#eef2ef}pre code{padding:0}table{border-collapse:collapse;width:100%;font-size:14px}td,th{border:1px solid #dfe7e2;padding:9px 12px;vertical-align:top;text-align:left}th{background:#edf5f0}.table-scroll{overflow:auto}footer{max-width:1040px;margin:24px auto;padding:0 24px 30px;color:#61736b;font-size:13px}summary{cursor:pointer;color:#17634b;font-weight:600}@media(max-width:650px){main{padding:0 12px}section{padding:20px 18px;border-radius:9px}header{padding:20px}h2{font-size:22px}}@media print{body{background:white}section{border:0;break-inside:auto}nav{display:none}a{color:inherit}img{max-height:18cm;object-fit:contain}}
</style><header><h1>QQVibe 使用说明</h1><p>版本 VERSION · 这份手册可以离线打开，图中聊天为演示数据。</p><nav><a href="#user-guide">开始使用</a><a href="#setup">安装与连接</a><a href="#compatibility">支持的电脑</a><a href="#known-issues">遇到问题</a><a href="#backup-upgrade">备份和升级</a><a href="#r9-trial">逐项检查</a><a href="#feedback">反馈问题</a></nav></header><main>BODY</main><footer>QQVibe · 基于 WechatVibe 开源项目。模型分析是交流参考，不是对人的事实判断。应用本体采用 Apache-2.0，完整来源和许可随程序保留。</footer></html>'''
    page = page.replace("VERSION", html.escape(version)).replace("BODY", "\n".join(sections))
    output.parent.mkdir(parents=True, exist_ok=True); output.write_text(page, encoding="utf8")
    return {"version": version, "documents": list(DOCUMENTS), "embeddedImages": sorted(images),
            "bytes": output.stat().st_size, "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "offline": True}

if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    args=parser.parse_args(); root=args.source_root.resolve()
    print(json.dumps(build(root, args.output or root / "docs/public/USER-GUIDE.html"), ensure_ascii=False))
