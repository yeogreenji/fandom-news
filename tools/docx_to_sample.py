"""위클리 docx → 사이트 샘플 데이터(일자별 JSON). 미리보기·시안용."""
import re, sys, json, glob
from collections import defaultdict
from pathlib import Path
import docx
from docx.oxml.ns import qn

SEC = {"[bemyfriends & Dreamus Company]": "own", "[Fandom & Music IP business]": "fandom_music",
       "[Entertainment & Contents IP business]": "ent_content", "[AI & Tech business]": "ai_tech"}
HEAD_RE = re.compile(r"^(.*)\s\[([^\]]+)\]\s*[–-]\s*(\d{2})\.(\d{2})\.(\d{2})\s*$")

def para_info(p, rels):
    text, bold_text, link = "", "", None
    parts = []
    for node in p:
        if node.tag == qn("w:hyperlink"):
            rid = node.get(qn("r:id"))
            if rid and rid in rels and not link:
                link = rels[rid].target_ref
            runs = node.iter(qn("w:r"))
        elif node.tag == qn("w:r"):
            runs = [node]
        else:
            continue
        for r in runs:
            t = "".join(x.text or "" for x in r.iter(qn("w:t")))
            rpr = r.find(qn("w:rPr"))
            b = rpr is not None and rpr.find(qn("w:b")) is not None and rpr.find(qn("w:b")).get(qn("w:val")) not in ("0", "false")
            parts.append((t, b))
    return parts, link

def main(src, out_dir):
    d = docx.Document(src)
    rels = d.part.rels
    section, items, pending = None, [], None
    in_issues = False
    for p in d.element.body.iter(qn("w:p")):
        parts, link = para_info(p, rels)
        text = "".join(t for t, _ in parts).replace("\xa0", " ").strip()
        if not text:
            continue
        if text == "Weekly Business Issues":
            in_issues = True; continue
        if not in_issues:
            continue
        if text in SEC:
            section = SEC[text]; continue
        m = HEAD_RE.match(text)
        if m and section:
            yy, mm, dd = m.group(3), m.group(4), m.group(5)
            pending = {"title": m.group(1).strip(), "press": m.group(2), "url": link,
                       "date": f"20{yy}-{mm}-{dd}", "date_label": f"{yy}.{mm}.{dd}", "section": section}
            items.append(pending); continue
        if pending and "summary" not in pending:
            s = ""
            for t, b in parts:
                t = t.replace("\xa0", " ")
                s += f"**{t}**" if b and t.strip() else t
            pending["summary"] = re.sub(r"\*\*\s*\*\*", "", s).strip()
    days = defaultdict(list)
    for i, it in enumerate(items):
        it.setdefault("summary", "")
        if not it["url"]:
            from urllib.parse import quote
            it["url"] = "https://search.naver.com/search.naver?where=news&query=" + quote(it["title"])
        days[it["date"]].append({
            "id": f"s{i}", "title": it["title"], "url": it["url"], "press": it["press"],
            "pub_date": it["date"] + "T09:00:00+09:00", "date_label": it["date_label"],
            "section": it["section"], "subsection": "", "importance": 3, "kind": "news",
            "summary": it["summary"], "reason": "", "tags": ["영문"] if not re.search(r"[가-힣]", it["title"]) else [],
            "series": None, "lang": "en" if not re.search(r"[가-힣]", it["title"]) else "ko", "cluster_size": 1})
    out = Path(out_dir); (out / "days").mkdir(parents=True, exist_ok=True)
    for day, its in days.items():
        json.dump({"date": day, "generated_at": day + "T07:00+09:00", "items": its, "excluded": [],
                   "stats": {"kept": len(its)}, "sample": True},
                  open(out / "days" / f"{day}.json", "w"), ensure_ascii=False, indent=1)
    print(len(items), "items", sorted(days))

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
