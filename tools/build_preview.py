"""docs/ 화면 + 샘플 데이터 → 단일 HTML(미리보기용)."""
import json, re, sys, glob
from pathlib import Path
root = Path(__file__).resolve().parent.parent
sample_dir, out = Path(sys.argv[1]), Path(sys.argv[2])
days = {Path(f).stem: json.load(open(f)) for f in glob.glob(str(sample_dir / "days" / "*.json"))}
html = (root / "docs/index.html").read_text()
body = re.search(r"<!--BODY-->(.*)<!--/BODY-->", html, re.S).group(1)
fonts = re.search(r'(<link rel="stylesheet" href="https://fonts[^>]+>)', html).group(1)
data = json.dumps({"updated": "2026-09-23T07:00", "days": days}, ensure_ascii=False).replace("</", "<\\/")
page = f"""<title>Fandom & Music IP Trend News</title>
{fonts}
<style>{(root/'docs/style.css').read_text()}</style>
{body}
<script>window.__SAMPLE__ = {data};</script>
<script>{(root/'docs/app.js').read_text()}</script>
"""
out.write_text(page)
print(out, len(page))
