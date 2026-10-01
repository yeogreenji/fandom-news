/* Fandom & Music IP Business Trend News — 데일리 모니터링 화면 */
(() => {
  "use strict";

  const SECTIONS = [
    ["own", "bemyfriends & Dreamus Company", "자사"],
    ["fandom_music", "Fandom & Music IP business", "팬덤 & 뮤직 IP 비즈니스"],
    ["ent_content", "Entertainment & Contents IP business", "엔터테인먼트 & 콘텐츠 IP 비즈니스"],
    ["ai_tech", "AI & Tech business", "AI & 테크"],
  ];
  const SUB_ORDER = {
    own: ["own"],
    fandom_music: ["fan_platform", "music_platform", "global_music", "music_ai_rights"],
    ent_content: ["fandom_commerce", "ent_biz", "ent_market", "live_ticket", "virtual_ai", "video_platform"],
    ai_tech: ["ai_tech"],
  };
  const SAMPLE = window.__SAMPLE__ || null;
  const REVIEW = location.hash.replace("#", "") === "review";

  const $ = (s, el = document) => el.querySelector(s);
  const state = {
    index: null, cache: {}, mode: "day", day: null, from: null, to: null,
    q: "", withHeader: true, picked: new Set(), tab: "news",
  };

  // ── 저장(개인 브라우저 편의 기능) ──────────────
  const store = {
    get(k, d) { try { const v = localStorage.getItem("fmn:" + k); return v ? JSON.parse(v) : d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem("fmn:" + k, JSON.stringify(v)); } catch { /* 무시 */ } },
  };
  state.picked = new Set(store.get("picked", []));
  state.withHeader = store.get("withHeader", true);

  // ── 데이터 ───────────────────────────────────
  async function loadIndex() {
    if (SAMPLE) return { dates: Object.keys(SAMPLE.days).sort().reverse(), updated: SAMPLE.updated };
    const r = await fetch("data/index.json", { cache: "no-store" });
    if (!r.ok) throw new Error("index.json을 불러오지 못했습니다");
    return r.json();
  }
  async function loadDay(d) {
    if (state.cache[d]) return state.cache[d];
    let data;
    if (SAMPLE) data = SAMPLE.days[d];
    else {
      const r = await fetch(`data/days/${d}.json`, { cache: "no-store" });
      data = r.ok ? await r.json() : { date: d, items: [], excluded: [] };
    }
    state.cache[d] = data;
    return data;
  }
  function datesInView() {
    const all = state.index.dates;
    if (state.mode === "day") return [state.day];
    return all.filter(d => d >= state.from && d <= state.to);
  }

  // ── 포맷 ─────────────────────────────────────
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const md = s => esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  const plain = s => String(s ?? "").replace(/\*\*(.+?)\*\*/g, "$1");
  const dotDate = d => { const [y, m, dd] = d.split("-"); return `${+y}.${+m}.${+dd}`; };
  const fmtW = iso => { const d = iso.slice(0, 10); return `${+d.slice(5, 7)}.${+d.slice(8, 10)}(${shortDow(d)}) ${iso.slice(11, 16)}`; };
  const shortDow = d => "일월화수목금토"[new Date(d + "T00:00:00+09:00").getDay()];

  function sortItems(items) {
    return items.slice().sort((a, b) => {
      const sa = SUB_ORDER[a.section] || [], sb = SUB_ORDER[b.section] || [];
      return (sa.indexOf(a.subsection) - sb.indexOf(b.subsection))
        || ((b.importance || 3) - (a.importance || 3))
        || String(a.pub_date).localeCompare(String(b.pub_date));
    });
  }

  function matchQ(it) {
    if (!state.q) return true;
    const q = state.q.toLowerCase();
    return (it.title + " " + it.press + " " + it.summary).toLowerCase().includes(q);
  }

  // 같은 시리즈 기획이 2건 이상이면 [추가] 블록으로 모음
  function splitSeries(items) {
    const groups = {};
    for (const it of items) if (it.series) (groups[it.series.name] ||= []).push(it);
    const series = Object.entries(groups).filter(([, v]) => v.length >= 2)
      .map(([name, v]) => ({ name, press: v[0].press, items: v.sort((a, b) => a.series.no - b.series.no) }));
    const inSeries = new Set(series.flatMap(s => s.items.map(i => i.url)));
    return { rest: items.filter(i => !inSeries.has(i.url)), series };
  }

  // ── 렌더링 ───────────────────────────────────
  async function render() {
    const dates = datesInView();
    const days = await Promise.all(dates.map(loadDay));
    const seen = new Set();
    let items = [];
    for (const d of days) for (const it of d.items || []) {
      if (seen.has(it.url)) continue;
      seen.add(it.url); items.push(it);
    }
    items = items.filter(matchQ);
    const excluded = days.flatMap(d => d.excluded || []);
    const { rest, series } = splitSeries(items);
    state.view = { rest, series, items, excluded, dates };

    renderRail();
    renderPeriod(dates);
    const main = $("#main");
    main.innerHTML = "";
    if (state.tab === "excluded") { main.append(renderExcluded(excluded)); updateCopy(); return; }

    for (const [key, en, ko] of SECTIONS) {
      const list = sortItems(rest.filter(i => i.section === key));
      const sec = document.createElement("section");
      sec.className = "sec " + key; sec.id = "sec-" + key;
      sec.innerHTML = `<header><h2>[${esc(en)}]</h2><span class="ko">${esc(ko)}</span><span class="count">${list.length}건</span></header>`;
      if (!list.length) sec.insertAdjacentHTML("beforeend", `<p class="empty">해당 기간에 클리핑된 기사가 없습니다.</p>`);
      for (const it of list) sec.append(renderItem(it));
      main.append(sec);
    }
    if (series.length) {
      const sec = document.createElement("section");
      sec.className = "sec series"; sec.id = "sec-series";
      sec.innerHTML = `<header><h2>[추가]</h2><span class="ko">시리즈 기획</span><span class="count">${series.length}개 시리즈</span></header>`;
      for (const s of series) {
        const box = document.createElement("div");
        box.className = "item";
        const allPicked = s.items.every(i => state.picked.has(i.url));
        box.innerHTML = `<input type="checkbox" id="pk-series-${esc(s.name)}" aria-label="시리즈 선택" ${allPicked ? "checked" : ""}>
          <div><h3>${esc(s.press)} 시리즈 기획 · ${esc(s.name)}</h3>
          <ol>${s.items.map(i => `<li><a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.title)}</a></li>`).join("")}</ol></div>`;
        box.querySelector("input").addEventListener("change", e => {
          s.items.forEach(i => e.target.checked ? state.picked.add(i.url) : state.picked.delete(i.url));
          savePicked(); box.classList.toggle("picked", e.target.checked); updateCopy();
        });
        if (allPicked) box.classList.add("picked");
        sec.append(box);
      }
      main.append(sec);
    }
    updateCopy();
  }

  function renderItem(it) {
    const el = document.createElement("article");
    el.className = "item" + (state.picked.has(it.url) ? " picked" : "");
    const tags = (it.tags || []).map(t => `<span class="chip${/보도자료|미확보/.test(t) ? " warn" : ""}">${esc(t)}</span>`).join("");
    el.innerHTML = `
      <input type="checkbox" id="pk-${esc(it.id)}" aria-label="메일에 넣기" ${state.picked.has(it.url) ? "checked" : ""}>
      <div>
        <h3><a href="${esc(it.url)}" target="_blank" rel="noopener">${esc(it.title)}</a></h3>
        <div class="meta"><span>[${esc(it.press)}]</span><span>–</span><time>${esc(it.date_label)}</time>${tags}</div>
        <p class="sum">${md(it.summary)}</p>
        ${REVIEW && it.reason ? `<p class="why">판정: ${esc(it.reason)} · 중요도 ${it.importance}</p>` : ""}
      </div>`;
    el.querySelector("input").addEventListener("change", e => {
      e.target.checked ? state.picked.add(it.url) : state.picked.delete(it.url);
      el.classList.toggle("picked", e.target.checked);
      savePicked(); updateCopy();
    });
    return el;
  }

  function renderExcluded(rows) {
    const box = document.createElement("section");
    box.className = "sec";
    box.innerHTML = `<header><h2>제외된 기사</h2><span class="ko">AI가 걸러낸 기사와 사유 · 필터 점검용</span><span class="count">${rows.length}건</span></header>`;
    if (!rows.length) { box.insertAdjacentHTML("beforeend", `<p class="empty">제외 기록이 없습니다.</p>`); return box; }
    const name = Object.fromEntries(SECTIONS.map(([k, , ko]) => [k, ko]));
    box.insertAdjacentHTML("beforeend", `<div class="ex-table-wrap"><table class="ex-table">
      <thead><tr><th>단계</th><th>분류</th><th>기사</th><th>사유</th></tr></thead><tbody>
      ${rows.filter(matchQ2).map(r => `<tr><td class="mono">${esc(r.stage)}</td><td>${esc(name[r.section] || "")}</td>
        <td><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title)}</a>${r.press ? ` <span class="chip">${esc(r.press)}</span>` : ""}</td>
        <td>${esc(r.reason)}</td></tr>`).join("")}
      </tbody></table></div>`);
    return box;
  }
  const matchQ2 = r => !state.q || (r.title + " " + r.reason).toLowerCase().includes(state.q.toLowerCase());

  function renderRail() {
    const ul = $("#days");
    ul.innerHTML = "";
    for (const d of state.index.dates.slice(0, 30)) {
      const li = document.createElement("li");
      const n = state.cache[d] ? state.cache[d].items.length : "";
      const cur = state.mode === "day" ? d === state.day : (d >= state.from && d <= state.to);
      li.innerHTML = `<button type="button" ${cur ? 'aria-current="true"' : ""}><span>${d.slice(5).replace("-", ".")} (${shortDow(d)})</span><span class="n">${n}</span></button>`;
      li.firstElementChild.addEventListener("click", () => { setMode("day"); state.day = d; render(); });
      ul.append(li);
    }
    const counts = Object.fromEntries(SECTIONS.map(([k]) => [k, state.view.rest.filter(i => i.section === k).length]));
    $("#toc").innerHTML = SECTIONS.map(([k, , ko]) => `<li><a href="#sec-${k}">${esc(ko)}<span class="n">${counts[k]}</span></a></li>`).join("")
      + (state.view.series.length ? `<li><a href="#sec-series">시리즈 기획<span class="n">${state.view.series.length}</span></a></li>` : "");
    $("#toc").querySelectorAll("a").forEach(a => a.addEventListener("click", e => {
      e.preventDefault(); document.getElementById(a.getAttribute("href").slice(1))?.scrollIntoView({ behavior: "smooth", block: "start" });
    }));
  }

  function renderPeriod(dates) {
    const s = dates[dates.length - 1], e = dates[0];
    const n = state.view.items.length;
    const w = state.mode === "day" && state.cache[state.day] && state.cache[state.day].window;
    const span = w ? ` (${fmtW(w.from)} ~ ${fmtW(w.to)})` : "";
    $("#period").textContent = state.mode === "day"
      ? `${dotDate(state.day)} (${shortDow(state.day)}) 발행분 · ${n}건${span}`
      : `${dotDate(state.from)} ~ ${dotDate(state.to)} · ${dates.length}일 · ${n}건`;
  }

  function setMode(m) {
    state.mode = m;
    document.querySelectorAll("#mode button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.mode === m)));
    $("#rangeBox").hidden = m !== "range";
  }

  // ── 선택 & 복사 ──────────────────────────────
  function savePicked() {
    const keep = new Set(state.view ? state.view.items.map(i => i.url) : []);
    // 화면 밖 선택도 유지하되 너무 쌓이지 않게 최근 500건만
    store.set("picked", [...state.picked].slice(-500));
    return keep;
  }
  function pickedInView() {
    const v = state.view;
    return {
      rest: v.rest.filter(i => state.picked.has(i.url)),
      series: v.series.map(s => ({ ...s, items: s.items.filter(i => state.picked.has(i.url)) })).filter(s => s.items.length),
    };
  }
  function updateCopy() {
    const p = pickedInView();
    const n = p.rest.length + p.series.reduce((a, s) => a + s.items.length, 0);
    const b = $("#copyBtn");
    b.disabled = n === 0;
    b.textContent = n ? `선택 ${n}건 메일 형식으로 복사` : "기사를 체크하면 복사할 수 있어요";
  }

  function buildMail() {
    const p = pickedInView();
    const dates = state.view.dates;
    const s = dates[dates.length - 1], e = dates[0];
    const F = "font-family:'맑은 고딕','Malgun Gothic',sans-serif;font-size:10pt;line-height:1.6;";
    let html = `<div style="${F}">`, text = "";
    if (state.withHeader) {
      html += `<p style="margin:0 0 4px"><b>Fandom &amp; Music IP Business Trend News – ${dotDate(e)}</b></p>
        <p style="margin:0 0 14px">- 모니터링 기간: ${dotDate(s)} ~ ${dotDate(e)}<br>- 모니터링 대상: 국내 매체 국/영문 기사, 해외 매체 영문 기사</p>`;
      text += `Fandom & Music IP Business Trend News – ${dotDate(e)}\n- 모니터링 기간: ${dotDate(s)} ~ ${dotDate(e)}\n- 모니터링 대상: 국내 매체 국/영문 기사, 해외 매체 영문 기사\n\n`;
    }
    for (const [key, en] of SECTIONS) {
      const list = sortItems(p.rest.filter(i => i.section === key));
      if (!list.length) continue;
      html += `<p style="margin:14px 0 8px"><b>[${esc(en)}]</b></p>`;
      text += `[${en}]\n`;
      for (const it of list) {
        html += `<p style="margin:0 0 4px"><b><a href="${esc(it.url)}">${esc(it.title)}</a> [${esc(it.press)}] – ${esc(it.date_label)}</b></p>
          <p style="margin:0 0 14px">${md(it.summary)}</p>`;
        text += `${it.title} [${it.press}] – ${it.date_label}\n${it.url}\n${plain(it.summary)}\n\n`;
      }
    }
    if (p.series.length) {
      html += `<p style="margin:14px 0 8px"><b>[추가]</b></p>`;
      text += `[추가]\n`;
      for (const s of p.series) {
        html += `<p style="margin:0 0 4px">${esc(s.press)} 시리즈 기획</p><p style="margin:0 0 14px">` +
          s.items.map(i => `<a href="${esc(i.url)}">${esc(i.title)}</a>`).join("<br>") + `</p>`;
        text += `${s.press} 시리즈 기획\n` + s.items.map(i => `${i.title} ${i.url}`).join("\n") + "\n\n";
      }
    }
    return { html: html + "</div>", text: text.trim() };
  }

  async function copyMail() {
    const { html, text } = buildMail();
    try {
      if (window.ClipboardItem && navigator.clipboard?.write) {
        await navigator.clipboard.write([new ClipboardItem({
          "text/html": new Blob([html], { type: "text/html" }),
          "text/plain": new Blob([text], { type: "text/plain" }),
        })]);
        return toast("복사했습니다. 메일 본문에 붙여넣으면 링크·굵게 표시가 유지됩니다.");
      }
      throw new Error("no rich clipboard");
    } catch {
      // 대체: 화면 밖에 서식 있는 영역을 만들어 선택 후 복사
      const box = document.createElement("div");
      box.contentEditable = "true"; box.innerHTML = html;
      Object.assign(box.style, { position: "fixed", left: "-9999px", top: "0" });
      document.body.append(box);
      const range = document.createRange(); range.selectNodeContents(box);
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
      let ok = false;
      try { ok = document.execCommand("copy"); } catch { ok = false; }
      sel.removeAllRanges(); box.remove();
      if (ok) return toast("복사했습니다. 메일 본문에 붙여넣으세요.");
      try { await navigator.clipboard.writeText(text); toast("서식 없이 텍스트로 복사했습니다."); }
      catch { toast("이 브라우저에서는 복사가 막혀 있습니다. 다른 브라우저에서 열어주세요."); }
    }
  }

  let toastTimer;
  function toast(msg) {
    let t = $("#toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 2600);
  }

  // ── 시작 ─────────────────────────────────────
  async function start() {
    try { state.index = await loadIndex(); }
    catch (e) { $("#main").innerHTML = `<p class="empty">${esc(e.message)}. 첫 수집이 끝난 뒤 다시 열어주세요.</p>`; return; }
    const ds = state.index.dates;
    if (!ds.length) { $("#main").innerHTML = `<p class="empty">아직 수집된 날짜가 없습니다.</p>`; return; }
    state.day = ds[0];
    state.to = ds[0];
    state.from = ds[Math.min(ds.length - 1, 6)];
    $("#from").min = $("#to").min = ds[ds.length - 1];
    $("#from").max = $("#to").max = ds[0];
    $("#from").value = state.from; $("#to").value = state.to;
    $("#updated").textContent = state.index.updated ? "업데이트 " + state.index.updated.replace("T", " ").slice(0, 16) : "";
    $("#withHeader").checked = state.withHeader;
    if (SAMPLE) $("#sampleNote").hidden = false;
    if (REVIEW) $("#tabs").hidden = false;
    // 최근 30일 건수 표시용 미리 읽기
    await Promise.all(ds.slice(0, 30).map(loadDay));

    $("#mode").addEventListener("click", e => {
      const m = e.target.closest("button")?.dataset.mode; if (!m) return;
      setMode(m); render();
    });
    $("#tabs").addEventListener("click", e => {
      const t = e.target.closest("button")?.dataset.tab; if (!t) return;
      state.tab = t;
      document.querySelectorAll("#tabs button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.tab === t)));
      render();
    });
    for (const id of ["from", "to"]) $("#" + id).addEventListener("change", () => {
      let f = $("#from").value, t = $("#to").value;
      if (f && t && f > t) [f, t] = [t, f];
      state.from = f || state.from; state.to = t || state.to; render();
    });
    let qTimer;
    $("#q").addEventListener("input", e => { clearTimeout(qTimer); qTimer = setTimeout(() => { state.q = e.target.value.trim(); render(); }, 180); });
    $("#withHeader").addEventListener("change", e => { state.withHeader = e.target.checked; store.set("withHeader", state.withHeader); });
    $("#pickAll").addEventListener("click", () => { state.view.items.forEach(i => state.picked.add(i.url)); savePicked(); render(); });
    $("#pickNone").addEventListener("click", () => { state.view.items.forEach(i => state.picked.delete(i.url)); savePicked(); render(); });
    $("#copyBtn").addEventListener("click", copyMail);
    setMode("day");
    render();
  }
  start();
})();
