(function () {
"use strict";
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const isNum = v => typeof v === "number" && isFinite(v);
const fmt = (v, d = 0) => isNum(v) ? v.toLocaleString("en-US", {minimumFractionDigits: d, maximumFractionDigits: d}) : "—";
const fmtS = (v, d = 0) => isNum(v) ? (v > 0 ? "+" : "") + fmt(v, d) : "—";
const fmtP = v => isNum(v) ? (v > 0 ? "+" : "") + v.toFixed(2) + "%" : "—";
const pd = v => isNum(v) ? (v < 200 ? 2 : (v % 1 ? 1 : 0)) : 0;
const cls = v => isNum(v) ? (v > 0 ? "up" : v < 0 ? "down" : "") : "";
const LIGHT = {go: "上車／加碼", wait: "觀察中", stop: "不宜加碼", na: "資料不足", keep: "續抱", reduce: "減碼", exit: "退場"};
const holdLine = h => [
  [h.rule && h.rule !== LIGHT[h.light] ? h.rule : "", ...(h.reasons || [])].filter(Boolean).join("、"),
  h.weak && h.weak.length ? "留意：" + h.weak.join("、") : "",
  isNum(h.profitPct) ? `報酬 ${fmtS(h.profitPct, 1)}%` : "",
  h.pending ? `待確認轉為「${LIGHT[h.pending] || h.pending}」` : ""].filter(Boolean).join("；");
const STATUS = {complete: "完成", waiting: "等待交易所公布", running: "抓取中", partial: "部分缺漏", failed: "失敗",
  holiday: "休市", not_started: "尚未開始", provisional: "暫定", pending: "等待中", ok: "完成", nodata: "尚未公布", fallback: "備援完成"};

/* ---------- token ---------- */
let TOKEN = "";
try { TOKEN = localStorage.getItem("twradar-token") || ""; } catch (e) {}
const m = location.hash.match(/[#&?]t=([^&]+)/);
if (m) { TOKEN = decodeURIComponent(m[1]); try { localStorage.setItem("twradar-token", TOKEN); } catch (e) {} location.hash = "#/"; }
const withTok = u => u + (u.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(TOKEN);

async function api(path, opts = {}) {
  const headers = {"Content-Type": "application/json"};
  if (TOKEN) headers.Authorization = "Bearer " + TOKEN;
  const r = await fetch("/api/v1" + path, {...opts, headers, credentials: "same-origin"});
  if (r.status === 401) { askToken(); throw new Error("需要登入"); }
  if (r.status === 403) { askToken("這個動作需要管理者權限，請用管理密碼登入。"); throw new Error("需要管理者權限"); }
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}
function toast(msg) { const t = document.createElement("div"); t.className = "toast"; t.textContent = msg; document.body.appendChild(t); setTimeout(() => t.remove(), 2800); }
function sheet(html, mount) {
  $("#layer").innerHTML = `<div class="overlay" id="ov"><div class="sheet" role="dialog" aria-modal="true">${html}</div></div>`;
  $("#ov").addEventListener("click", e => { if (e.target.id === "ov" || e.target.closest("[data-close]")) $("#layer").innerHTML = ""; });
  mount && mount($("#ov .sheet"));
}
function askToken(msg) {
  if ($("#tokForm")) return;
  sheet(`<h2>登入</h2>${msg ? `<div class="banner warn">${esc(msg)}</div>` : ""}
    <h3>管理者</h3><div class="small muted">可修改設定。密碼在安裝完成時顯示，也存在電腦桌面的「右側加碼雷達-連線資訊.txt」。</div>
    <form id="pwForm" class="row"><input id="pw" type="password" style="flex:1" autocomplete="current-password" placeholder="管理密碼"><button class="primary">登入</button></form>
    <h3>唯讀</h3><div class="small muted">只能查看。輸入唯讀 token（管理者可在「設定 → 權限」查到）。</div>
    <form id="tokForm" class="row"><input id="tok" style="flex:1" autocomplete="off" placeholder="貼上唯讀 token"><button>確定</button></form>`,
    s => {
      s.querySelector("#tokForm").addEventListener("submit", e => { e.preventDefault(); TOKEN = s.querySelector("#tok").value.trim();
        try { localStorage.setItem("twradar-token", TOKEN); } catch (err) {} $("#layer").innerHTML = ""; route(); });
      s.querySelector("#pwForm").addEventListener("submit", async e => { e.preventDefault();
        const r = await fetch("/api/v1/auth/login", {method: "POST", headers: {"Content-Type": "application/json"}, credentials: "same-origin",
          body: JSON.stringify({password: s.querySelector("#pw").value})});
        const j = await r.json().catch(() => ({}));
        if (!r.ok) { toast(j.error || "登入失敗"); return; }
        $("#layer").innerHTML = ""; toast("已用管理者身分登入"); route(); });
    });
}

/* ---------- 共用 ---------- */
const badge = (k, text) => `<span class="badge b-${esc(k)}"><i></i>${esc(text || STATUS[k] || LIGHT[k] || k)}</span>`;
let DATES = [];
async function loadDates() { DATES = await api("/dates"); return DATES; }
function dateOptions(sel) {
  return DATES.filter(d => d.status !== "holiday").map(d => `<option value="${d.date}"${d.date === sel ? " selected" : ""}>${d.date.replace(/-/g, "/")}　${STATUS[d.status] || d.status}</option>`).join("");
}
function statusBanner(e) {
  const k = e.ready ? "ok" : (e.status === "failed" || e.status === "partial") ? "bad" : "warn";
  const steps = Object.entries(e.crawl.steps || {}).map(([, s]) => `<span class="badge b-${esc(s.status)}" title="${esc(s.error || s.source || "")}"><i></i>${esc(s.label)}：${esc(STATUS[s.status] || s.status)}</span>`).join("");
  return `<div class="banner ${k}"><div class="row between"><b>${esc(e.statusText)}</b>
    <span class="row">${badge(e.status)}<span class="small muted">分析：${esc(STATUS[e.analysis.status] || e.analysis.status)}${e.analysis.analyzedAt ? "（" + esc(e.analysis.analyzedAt.replace("T", " ").slice(0, 16)) + "）" : ""}</span></span></div>
    ${steps ? `<div class="steps">${steps}</div>` : ""}${e.crawl.message ? `<div class="small muted">${esc(e.crawl.message)}</div>` : ""}</div>`;
}
function marketHTML(m) {
  return `<div class="market">
    <div class="kv"><span class="k">加權指數</span><span class="v num">${fmt(m.taiex, 2)} <span class="small ${cls(m.taiexChgPct)}">${fmtP(m.taiexChgPct)}</span></span></div>
    <div class="kv"><span class="k">外資買賣超（億）</span><span class="v num ${cls(m.foreignAmt)}">${fmtS(m.foreignAmt, 2)}</span></div>
    <div class="kv"><span class="k">投信買賣超（億）</span><span class="v num ${cls(m.trustAmt)}">${fmtS(m.trustAmt, 2)}</span></div>
    <div class="kv"><span class="k">自營商買賣超（億）</span><span class="v num ${cls(m.dealerAmt)}">${fmtS(m.dealerAmt, 2)}</span></div></div>`;
}
const ck = (label, st, why) => `<span class="ck ${st}" title="${esc(why || "")}">${{pass: "✓", fail: "✕", na: "?"}[st] || "?"} ${label}</span>`;
function cardHTML(s) {
  const r = s.data, g = s.signal;
  return `<article class="card ${g.light}" data-code="${esc(s.code)}" tabindex="0">
   <div class="row between"><div><b>${esc(s.code)} ${esc(s.name)}</b><div class="small muted">${s.market === "TPEx" ? "上櫃" : "上市"} ${chips(s.labels)}</div></div>${badge(g.light, g.lightLabel)}</div>
   <div class="price"><span class="c ${cls(r.chgPct)}">${fmt(r.close, pd(r.close))}</span><span class="num ${cls(r.chgPct)}">${fmtP(r.chgPct)}</span>
     <span class="small muted num">量 ${fmt(r.vol)}${isNum(g.volRatio) ? "・" + g.volRatio.toFixed(2) + "x" : ""}</span></div>
   <div class="metrics">
     <div><span class="k">外資</span><span class="v ${cls(r.foreign)}">${fmtS(r.foreign)}</span></div>
     <div><span class="k">投信</span><span class="v ${cls(r.trust)}">${fmtS(r.trust)}</span></div>
     <div><span class="k">融資</span><span class="v">${fmtS(r.marginChg)}</span></div>
     <div><span class="k">月線</span><span class="v">${fmt(r.ma20, 2)}</span></div>
     <div><span class="k">乖離</span><span class="v">${isNum(g.maDistPct) ? fmtS(g.maDistPct, 1) + "%" : "—"}</span></div>
     <div><span class="k">外/投 連續</span><span class="v">${isNum(r.foreignStreak) ? fmtS(r.foreignStreak) : "—"} / ${isNum(r.trustStreak) ? fmtS(r.trustStreak) : "—"}</span></div>
     ${isNum(r.whalePct) ? `<div><span class="k">${fmt(r.whaleLots)} 張大戶</span><span class="v">${r.whalePct.toFixed(1)}%${isNum(r.whaleChg) ? ` <span class="${cls(r.whaleChg)}">${fmtS(r.whaleChg, 2)}</span>` : ""}</span></div>
       <div><span class="k">散戶人數週增</span><span class="v ${cls(-(r.retailChg || 0))}">${isNum(r.retailChg) ? fmtS(r.retailChg) : "—"}</span></div>` : ""}</div>
   <div class="checks">${ck("法人", g.checks.inst, g.instWhy)}${ck("主力", g.checks.main)}${ck("散戶", g.checks.retail)}${ck("價量", g.checks.priceVolume)}${g.warnings.map(w => `<span class="ck warn">! ${esc(w)}</span>`).join("")}</div>
   ${g.light === "wait" && g.needs.length ? `<div class="need"><b>還差：</b>${esc(g.needs.join("；"))}</div>` : ""}
   ${g.light === "wait" && g.rule && g.rule !== "觀察" ? `<div class="need"><b>${esc(g.rule)}：</b>${esc(g.warnings.join("；"))}</div>` : ""}
   ${g.light === "go" ? `<div class="need"><b>依據：</b>${esc(g.lightLabel || "")}・${esc([g.instWhy, g.checks.retail === "pass" ? "融資健康" : "", g.checks.main === "pass" ? "主力集中" : "", "價量確認"].filter(Boolean).join("、"))}；觀察價位 月線 ${fmt(r.ma20, 2)}${isNum(r.prevHigh) ? "、前高 " + fmt(r.prevHigh, pd(r.prevHigh)) : ""}</div>` : ""}
   ${g.light === "stop" ? `<div class="need"><b>原因：</b>${esc(g.warnings.join("；"))}</div>` : ""}
   ${g.hold ? `<div class="need row" style="gap:6px;align-items:baseline"><b>持有：</b>${badge(g.hold.light)}<span>${esc(holdLine(g.hold))}</span></div>` : ""}
   ${g.keyLevelNote ? `<div class="note">${esc(g.keyLevelNote)}</div>` : ""}${s.note ? `<div class="note">${esc(s.note)}</div>` : ""}
   ${g.missing.length ? `<div class="note">未取得：${esc(g.missing.join("、"))}</div>` : ""}</article>`;
}
function tableHTML(stocks) {
  const ord = {go: 0, wait: 1, stop: 2, na: 3};
  return `<div class="tablewrap"><table class="sortable" data-sort="main"><thead><tr><th class="l">代號</th><th class="l">名稱</th><th>收盤</th><th>漲跌%</th><th>外資</th><th>投信</th><th>融資增減</th><th>量比</th><th>月線位置</th><th class="l">燈號</th></tr></thead><tbody>
  ${stocks.map(s => { const r = s.data, g = s.signal; const pos = g.belowMa20 === null || g.belowMa20 === undefined ? "—" : g.belowMa20 ? "月線下" : g.maDistPct < 0 ? "貼近月線" : "月線上";
    return `<tr><td class="l num">${esc(s.code)}</td><td class="l">${esc(s.name)}</td><td class="num">${fmt(r.close, pd(r.close))}</td><td class="num ${cls(r.chgPct)}">${fmtP(r.chgPct)}</td><td class="num ${cls(r.foreign)}">${fmtS(r.foreign)}</td><td class="num ${cls(r.trust)}">${fmtS(r.trust)}</td><td class="num">${fmtS(r.marginChg)}</td><td class="num">${isNum(g.volRatio) ? g.volRatio.toFixed(2) + "x" : "—"}</td><td data-v="${isNum(g.maDistPct) ? g.maDistPct : ""}">${pos} <span class="muted num">${isNum(g.maDistPct) ? "(" + fmtS(g.maDistPct, 1) + "%)" : ""}</span></td><td class="l" data-v="${ord[g.light] ?? 9}">${badge(g.light, g.lightLabel)}</td></tr>`; }).join("")}
  </tbody></table></div>`;
}

/* ---------- SVG 圖表 ---------- */
function niceTicks(min, max, n = 4) {
  if (min === max) { min -= 1; max += 1; }
  const step0 = (max - min) / n, mag = Math.pow(10, Math.floor(Math.log10(step0))), f = step0 / mag;
  const step = (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * mag; const out = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}
function lineChart(labels, series, {h = 220, bars = null} = {}) {
  const W = 760, L = 56, R = 12, T = 10, B = 24, ch = h - T - B - (bars ? 50 : 0);
  const all = series.flatMap(s => s.values).filter(isNum); if (!all.length) return `<div class="empty">沒有資料</div>`;
  let lo = Math.min(...all), hi = Math.max(...all); const pad = (hi - lo) * .06 || 1; lo -= pad; hi += pad;
  const x = i => L + (labels.length < 2 ? 0 : i * (W - L - R) / (labels.length - 1)), y = v => T + (hi - v) / (hi - lo) * ch;
  let g = niceTicks(lo, hi).map(v => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--line)" stroke-width="1"/><text x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${fmt(v, v < 200 ? 1 : 0)}</text>`).join("");
  const step = Math.max(1, Math.ceil(labels.length / 6));
  g += labels.map((d, i) => i % step === 0 ? `<text x="${x(i)}" y="${h - 6}" text-anchor="middle">${d.slice(5)}</text>` : "").join("");
  for (const s of series) {
    let p = "", on = false; s.values.forEach((v, i) => { if (isNum(v)) { p += (on ? "L" : "M") + x(i).toFixed(1) + "," + y(v).toFixed(1); on = true; } else on = false; });
    g += `<path d="${p}" fill="none" stroke="${s.color}" stroke-width="${s.width || 1.6}"/>`;
    const li = s.values.map(isNum).lastIndexOf(true); if (li >= 0 && s.dot) g += `<circle cx="${x(li)}" cy="${y(s.values[li])}" r="3.5" fill="${s.color}"/>`;
  }
  if (bars) {
    const top = T + ch + 10, bh = 40, mx = Math.max(...bars.filter(isNum), 1), bw = Math.max(1, (W - L - R) / labels.length * .7);
    g += bars.map((v, i) => isNum(v) ? `<rect x="${x(i) - bw / 2}" y="${top + bh - v / mx * bh}" width="${bw}" height="${v / mx * bh}" fill="var(--na)" opacity=".55"/>` : "").join("");
    g += `<text x="${L - 6}" y="${top + 10}" text-anchor="end">量</text>`;
  }
  return `<svg class="chart" viewBox="0 0 ${W} ${h}" role="img">${g}</svg>`;
}
function divBars(labels, sets, {h = 200, lab = d => d.slice(5), every = 0} = {}) {
  const W = 760, L = 56, R = 12, T = 10, B = 24, ch = h - T - B;
  const all = sets.flatMap(s => s.values).filter(isNum); if (!all.length) return `<div class="empty">沒有資料</div>`;
  const mx = Math.max(...all.map(Math.abs), 1), y0 = T + ch / 2, sc = (ch / 2) / mx;
  const slot = (W - L - R) / labels.length, bw = Math.max(1, slot * .8 / sets.length);
  let g = `<line x1="${L}" x2="${W - R}" y1="${y0}" y2="${y0}" stroke="var(--muted)"/>`;
  g += [mx, -mx].map(v => `<text x="${L - 6}" y="${y0 - v * sc + 4}" text-anchor="end">${fmtS(Math.round(v))}</text>`).join("");
  sets.forEach((s, k) => s.values.forEach((v, i) => { if (!isNum(v)) return; const bx = L + i * slot + slot * .1 + k * bw;
    g += `<rect x="${bx}" y="${v >= 0 ? y0 - v * sc : y0}" width="${bw}" height="${Math.max(1, Math.abs(v) * sc)}" fill="${s.color}"/>`; }));
  const step = every || Math.max(1, Math.ceil(labels.length / 6));
  g += labels.map((d, i) => i % step === 0 ? `<text x="${L + i * slot + slot / 2}" y="${h - 6}" text-anchor="middle">${esc(lab(d))}</text>` : "").join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${h}" role="img">${g}</svg>`;
}
function hBars(items, {color = s => "var(--accent)", unit = ""} = {}) {
  const W = 760, L = 150, R = 70, rh = 26, h = items.length * rh + 26;
  const mx = Math.max(...items.map(i => Math.abs(i.v)), 1), cx = L + (W - L - R) / 2, sc = ((W - L - R) / 2) / mx;
  let g = `<line x1="${cx}" x2="${cx}" y1="0" y2="${items.length * rh}" stroke="var(--muted)"/>`;
  items.forEach((it, i) => { const y = i * rh, w = it.v * sc;
    g += `<text x="0" y="${y + 17}" style="fill:var(--fg)">${esc(it.label)}</text><rect x="${w >= 0 ? cx : cx + w}" y="${y + 5}" width="${Math.max(2, Math.abs(w))}" height="${rh - 10}" fill="${color(it)}" rx="2"/>
      <text x="${w >= 0 ? cx + w + 6 : cx + w - 6}" y="${y + 17}" text-anchor="${w >= 0 ? "start" : "end"}" style="fill:var(--fg)">${fmtS(it.v, 1)}${unit}</text>`; });
  return `<svg class="chart" viewBox="0 0 ${W} ${h}" role="img">${g}</svg>`;
}
const lightColor = it => ({go: "var(--go)", wait: "var(--wait)", stop: "var(--stop)", na: "var(--na)"})[it.light];

/* ---------- 頁面：總覽 ---------- */
let FILTER = "all";
async function viewHome(date) {
  await loadDates();
  const e = date ? await api("/days/" + date) : await api("/latest");
  const d = e.date;
  if (!d) { $("#view").innerHTML = `<div class="panel empty">${esc(e.statusText || "尚無資料")}。系統首次啟動會先補抓歷史資料，可到「設定與狀態」查看進度。</div>`; return; }
  const cnt = e.summary;
  const shown = e.stocks.filter(s => FILTER === "all" || s.signal.light === FILTER);
  $("#view").innerHTML = `
   <div class="row between"><h1>右側加碼晨報｜資料日 ${d.replace(/-/g, "/")}</h1>
     <div class="row"><select id="dsel" aria-label="資料日">${dateOptions(d)}</select><a class="btn primary" href="#/report/${d}">產出報表</a></div></div>
   ${statusBanner(e)}
   <section class="panel">${marketHTML(e.market)}</section>
   <section class="tiles">${["go", "wait", "stop"].map(k => `<div class="tile ${k}"><span class="n">${cnt[k]}</span><span class="l">${LIGHT[k]}</span></div>`).join("")}</section>
   <div class="row between"><h2>觀察清單</h2><div class="row">${[["all", "全部"], ["go", "加碼"], ["wait", "觀察"], ["stop", "轉弱"]].map(([k, l]) => `<button data-f="${k}" class="${FILTER === k ? "primary" : ""}">${l}${k !== "all" ? " " + cnt[k] : ""}</button>`).join("")}</div></div>
   <section class="cards">${shown.map(cardHTML).join("") || `<div class="panel empty">沒有符合的個股</div>`}</section>
   <h2>精簡表</h2>${tableHTML(e.stocks)}
   <section class="panel row between"><div><b>匯出資料</b><div class="small muted">CSV 可直接用 Excel 開啟；雷達格式可匯入 claude.ai 上的「右側加碼雷達」</div></div>
    <div class="row"><a class="btn" href="${withTok("/api/v1/days/" + d + "?format=csv")}">CSV</a><a class="btn" href="${withTok("/api/v1/days/" + d)}" target="_blank">JSON</a>
    <a class="btn" href="${withTok("/api/v1/days/" + d + "?format=radar")}" target="_blank">雷達格式 JSON</a><a class="btn" href="${withTok("/api/v1/export/sqlite")}">整個資料庫</a></div></section>`;
  $("#dsel").addEventListener("change", ev => location.hash = "#/day/" + ev.target.value);
  document.querySelectorAll("[data-f]").forEach(b => b.addEventListener("click", () => { FILTER = b.dataset.f; viewHome(d); }));
  document.querySelectorAll(".card").forEach(c => { const go = () => location.hash = "#/stock/" + c.dataset.code; c.addEventListener("click", go); c.addEventListener("keydown", ev => { if (ev.key === "Enter") go(); }); });
}

/* ---------- 頁面：個股 ---------- */
async function viewStock(code) {
  const days = 120;
  const h = await api(`/stocks/${code}/history?days=${days}`);
  const rows = h.rows, L = rows.map(r => r.date), last = rows[rows.length - 1] || {};
  const r60 = rows.slice(-60), L60 = r60.map(r => r.date);
  $("#view").innerHTML = `
   <div class="row between"><h1>${esc(code)} ${esc(h.name || "")}</h1><div class="row"><a class="btn" href="#/">← 總覽</a><a class="btn" href="${withTok(`/api/v1/stocks/${code}/history?days=250&format=csv`)}">下載歷史 CSV</a></div></div>
   <section class="panel"><div class="row between"><h2>收盤價與均線（近 ${rows.length} 日）</h2><div class="legend"><span><i style="background:var(--fg)"></i>收盤</span><span><i style="background:var(--c-ma20)"></i>月線 MA20</span><span><i style="background:var(--c-ma60)"></i>季線 MA60</span></div></div>
    ${lineChart(L, [{values: rows.map(r => r.close), color: "var(--fg)", width: 2, dot: true}, {values: rows.map(r => r.ma20), color: "var(--c-ma20)"}, {values: rows.map(r => r.ma60), color: "var(--c-ma60)"}], {h: 300, bars: rows.map(r => r.volume)})}</section>
   <section class="panel"><div class="row between"><h2>外資／投信買賣超（張，近 60 日）</h2><div class="legend"><span><i style="background:var(--c-foreign)"></i>外資</span><span><i style="background:var(--c-trust)"></i>投信</span></div></div>
    ${divBars(L60, [{values: r60.map(r => r.foreign), color: "var(--c-foreign)"}, {values: r60.map(r => r.trust), color: "var(--c-trust)"}])}</section>
   <section class="panel"><h2>融資餘額（張，近 60 日）</h2>${lineChart(L60, [{values: r60.map(r => r.marginBal), color: "var(--accent)", dot: true}], {h: 180})}</section>
   <h2>近 20 日明細</h2><div class="tablewrap"><table><thead><tr><th class="l">日期</th><th>收盤</th><th>漲跌</th><th>成交量</th><th>月線</th><th>季線</th><th>外資</th><th>投信</th><th>自營</th><th>融資增減</th><th>融資餘額</th></tr></thead><tbody>
   ${rows.slice(-20).reverse().map(r => `<tr><td class="l num">${r.date}</td><td class="num">${fmt(r.close, pd(r.close))}</td><td class="num ${cls(r.change)}">${fmtS(r.change, pd(r.close))}</td><td class="num">${fmt(r.volume)}</td><td class="num">${fmt(r.ma20, 2)}</td><td class="num">${fmt(r.ma60, 2)}</td><td class="num ${cls(r.foreign)}">${fmtS(r.foreign)}</td><td class="num ${cls(r.trust)}">${fmtS(r.trust)}</td><td class="num">${fmtS(r.dealer)}</td><td class="num">${fmtS(r.marginChg)}</td><td class="num">${fmt(r.marginBal)}</td></tr>`).join("")}
   </tbody></table></div>`;
}

/* ---------- 頁面：報表 ---------- */
function holdSec(st) {
  const list = st.filter(s => s.signal.hold).sort((a, b) => ["exit", "reduce", "keep", "na"].indexOf(a.signal.hold.light) - ["exit", "reduce", "keep", "na"].indexOf(b.signal.hold.light));
  if (!list.length) return "";
  return `<div class="sec"><h2>持有部位（${list.length}）</h2>${list.map(s => { const r = s.data, h = s.signal.hold;
    return `<p style="margin:8px 0">${badge(h.light)} <b>${esc(s.code)} ${esc(s.name)}</b>　收 ${fmt(r.close, pd(r.close))}（<span class="${cls(r.chgPct)}">${fmtP(r.chgPct)}</span>）<br><span class="muted">${esc(holdLine(h) || "—")}</span></p>`; }).join("")}</div>`;
}
async function viewReport(date) {
  await loadDates();
  const e = date ? await api("/days/" + date) : await api("/latest");
  const d = e.date; if (!d) { $("#view").innerHTML = `<div class="panel empty">尚無資料</div>`; return; }
  const st = e.stocks, m = e.market;
  const withMA = st.filter(s => isNum(s.signal.maDistPct)).map(s => ({label: s.code + " " + s.name, v: s.signal.maDistPct, light: s.signal.light}));
  const sec = (k, title) => { const list = st.filter(s => s.signal.light === k); if (!list.length) return "";
    return `<div class="sec"><h2 style="color:var(--${k === "na" ? "na" : k})">${title}（${list.length}）</h2>${list.map(s => { const r = s.data, g = s.signal;
      const line = k === "go" ? (g.lightLabel || "") + "：" + [g.instWhy, g.checks.retail === "pass" ? "融資健康" : "", g.checks.main === "pass" ? "主力集中" : "", "價量確認"].filter(Boolean).join("、") + `；觀察價位 月線 ${fmt(r.ma20, 2)}${isNum(r.prevHigh) ? "、前高 " + fmt(r.prevHigh, pd(r.prevHigh)) : ""}`
        : k === "wait" ? (g.rule && g.rule !== "觀察" ? g.rule + "；" : "") + "還差：" + (g.needs.join("；") || "—") : k === "stop" ? "原因：" + g.warnings.join("；") : "未取得：" + g.missing.join("、");
      return `<p style="margin:8px 0"><b>${esc(s.code)} ${esc(s.name)}</b>　收 ${fmt(r.close, pd(r.close))}（<span class="${cls(r.chgPct)}">${fmtP(r.chgPct)}</span>）　外資 ${fmtS(r.foreign)}　投信 ${fmtS(r.trust)}　融資 ${fmtS(r.marginChg)}<br><span class="muted">${esc(line)}${g.keyLevelNote ? "；" + esc(g.keyLevelNote) : ""}</span></p>`; }).join("")}</div>`; };
  $("#view").innerHTML = `
   <div class="row between noprint"><div class="row"><select id="dsel" aria-label="資料日">${dateOptions(d)}</select>${e.ready ? "" : badge(e.status, e.statusText)}</div>
     <div class="row"><button class="primary" id="printBtn">列印／存成 PDF</button></div></div>
   <article class="report">
    <div><h1>右側加碼晨報｜資料日 ${d.replace(/-/g, "/")}</h1><div class="small muted">產出時間 ${new Date().toLocaleString("zh-TW", {hour12: false})}・資料狀態：${esc(e.statusText)}</div></div>
    ${marketHTML(m)}
    <div class="tiles">${["go", "wait", "stop"].map(k => `<div class="tile ${k}"><span class="n">${e.summary[k]}</span><span class="l">${LIGHT[k]}</span></div>`).join("")}</div>
    <div class="sec"><h2>訊號矩陣</h2><div class="tablewrap"><table class="matrix"><thead><tr><th class="l">個股</th><th>法人</th><th>主力</th><th>散戶</th><th>價量</th><th>燈號</th>${st.some(s => s.signal.hold) ? "<th>持有面</th>" : ""}</tr></thead><tbody>
     ${st.map(s => { const c = s.signal.checks, mk = v => `<span class="mx ${v}">${{pass: "✓", fail: "✕", na: "?"}[v]}</span>`;
       return `<tr><td class="l"><b class="num">${esc(s.code)}</b> ${esc(s.name)}</td><td>${mk(c.inst)}</td><td>${mk(c.main)}</td><td>${mk(c.retail)}</td><td>${mk(c.priceVolume)}</td><td>${badge(s.signal.light, s.signal.lightLabel)}</td>${st.some(x => x.signal.hold) ? `<td>${s.signal.hold ? badge(s.signal.hold.light) : ""}</td>` : ""}</tr>`; }).join("")}
    </tbody></table></div></div>
    <div class="sec"><h2>收盤與月線乖離（%）</h2><div class="small muted">0 線為月線，右側＝站上月線；長條顏色為燈號</div>${hBars(withMA, {color: lightColor, unit: "%"})}</div>
    <div class="sec"><h2>外資／投信 當日買賣超（張）</h2><div class="legend"><span><i style="background:var(--c-foreign)"></i>外資</span><span><i style="background:var(--c-trust)"></i>投信</span></div>
     ${divBars(st.map(s => s.code), [{values: st.map(s => s.data.foreign ?? null), color: "var(--c-foreign)"}, {values: st.map(s => s.data.trust ?? null), color: "var(--c-trust)"}], {h: 220, lab: d => d, every: 1})}</div>
    ${holdSec(st)}${sec("go", "上車／加碼訊號")}${sec("wait", "觀察中")}${sec("stop", "轉弱／不宜加碼")}${sec("na", "資料不足")}
    <div class="sec"><h2>精簡表</h2>${tableHTML(st)}</div>
    ${e.model ? `<div class="small ${e.model.status === "偏離" ? "" : "muted"}" style="margin:8px 0">${e.model.status === "偏離" ? "⚠ " : ""}模型追蹤：${esc(e.model.status)}。${esc(e.model.text)}</div>` : ""}
    <div class="small muted">資料來源：證交所、櫃買中心${st.some(s => (s.data.source || "").includes("FinMind")) ? "、FinMind" : ""}。以上為籌碼訊號整理，非投資建議。</div>
   </article>`;
  $("#dsel").addEventListener("change", ev => location.hash = "#/report/" + ev.target.value);
  $("#printBtn").addEventListener("click", () => window.print());
}

/* ---------- 頁面：設定與狀態 ---------- */
async function authPanel() {
  const me = await fetch("/api/v1/auth/me", {credentials: "same-origin", headers: TOKEN ? {Authorization: "Bearer " + TOKEN} : {}}).then(r => r.json());
  if (me.role !== "admin") return `<section class="panel" style="display:flex;flex-direction:column;gap:10px"><div class="row between"><h2>權限</h2>${badge("na", "唯讀")}</div>
    <div class="small muted">目前只能查看。要修改觀察清單、門檻或設定，請用管理密碼登入。</div><div class="row"><button class="primary" id="loginBtn">管理者登入</button></div></section>`;
  const t = await api("/auth/tokens");
  const tokRow = (k, label, v, hint) => `<div class="field"><label>${label}</label><div class="row"><input readonly value="${esc(v)}" id="tok-${k}" style="flex:1;font-family:var(--mono,monospace)">
    <button data-copy="tok-${k}">複製</button><button class="danger" data-rotate="${k}">重設</button></div><span class="hint">${hint}</span></div>`;
  return `<section class="panel" style="display:flex;flex-direction:column;gap:10px"><div class="row between"><h2>權限</h2>${badge("ok", "管理者")}</div>
    ${me.initialPassword ? `<div class="banner warn">目前用的是安裝時產生的密碼，建議改成自己好記的密碼。</div>` : ""}
    ${tokRow("read", "唯讀 token（給晨報）", t.readToken, "可放在網址 ?token=；重設後晨報要換成新 token")}
    ${tokRow("admin", "管理 token（給管理腳本）", t.adminToken, "只能放在 Authorization 標頭")}
    <form id="pwChange" class="grid2"><div class="field"><label for="pw-cur">目前密碼</label><input id="pw-cur" type="password" autocomplete="current-password" required></div>
      <div class="field"><label for="pw-new">新密碼（至少 8 字元）</label><input id="pw-new" type="password" autocomplete="new-password" minlength="8" required></div>
      <div class="field" style="justify-content:flex-end"><button class="primary">變更密碼</button></div></form>
    <div class="small muted">變更密碼後，其他裝置需要重新登入。忘記密碼：在主機上執行 <span class="num">twradar admin reset-password</span>。</div>
    ${me.via === "session" ? `<div class="row"><button id="logoutBtn">登出</button></div>` : ""}</section>`;
}
function bindAuthPanel() {
  $("#loginBtn") && ($("#loginBtn").onclick = () => askToken());
  document.querySelectorAll("[data-copy]").forEach(b => b.onclick = async () => { try { await navigator.clipboard.writeText($("#" + b.dataset.copy).value); toast("已複製"); } catch (e) { $("#" + b.dataset.copy).select(); } });
  document.querySelectorAll("[data-rotate]").forEach(b => b.onclick = () => sheet(`<h2>重設${b.dataset.rotate === "read" ? "唯讀" : "管理"} token？</h2><div class="small muted">舊的 token 會立刻失效，使用它的程式（例如晨報）要換成新的。</div><div class="row" style="justify-content:flex-end"><button data-close>取消</button><button class="danger" id="yes">重設</button></div>`,
    s => s.querySelector("#yes").onclick = async () => { const j = await api("/auth/rotate", {method: "POST", body: JSON.stringify({kind: b.dataset.rotate})});
      if (b.dataset.rotate === "read" && TOKEN) { TOKEN = j.token; try { localStorage.setItem("twradar-token", TOKEN); } catch (e) {} }
      $("#layer").innerHTML = ""; toast("已重設"); viewSettings(); }));
  $("#pwChange") && $("#pwChange").addEventListener("submit", async ev => { ev.preventDefault();
    try { await api("/auth/password", {method: "POST", body: JSON.stringify({current: $("#pw-cur").value, new: $("#pw-new").value})}); toast("密碼已變更"); viewSettings(); } catch (err) { toast(err.message); } });
  $("#logoutBtn") && ($("#logoutBtn").onclick = async () => { await fetch("/api/v1/auth/logout", {method: "POST", credentials: "same-origin"}); toast("已登出"); viewSettings(); });
}

async function viewSettings() {
  const [sys, wl, rules, ev, authHTML, labels] = await Promise.all([api("/status"), api("/watchlist"), api("/rules"), api("/events?limit=80"), authPanel(), loadLabels()]);
  const bf = sys.backfill;
  $("#view").innerHTML = `
   <h1>設定與狀態</h1>${authHTML}
   <section class="panel" style="display:flex;flex-direction:column;gap:10px"><div class="row between"><h2>系統狀態</h2><span class="small muted">版本 ${esc(sys.version)}・${esc(sys.now.replace("T", " ").slice(0, 16))}</span></div>
     <div class="grid2"><div class="kv"><span class="k">目前工作</span><span>${esc(sys.worker.current || "閒置")}</span></div>
      <div class="kv"><span class="k">排隊中</span><span>${esc(sys.worker.pending.join("、") || "無")}</span></div>
      <div class="kv"><span class="k">已存交易日</span><span class="num">${sys.tradingDaysStored}</span></div>
      <div class="kv"><span class="k">下次排程</span><span class="num small">${sys.nextRuns.map(t => t.replace("T", " ").slice(5, 16)).join("<br>") || "—"}</span></div></div>
     ${bf ? `<div><div class="small muted">歷史補抓：${bf.status === "done" ? "完成" : "進行中"}（${bf.done}/${bf.target} 個交易日${bf.current ? "，目前 " + bf.current : ""}）</div><div class="progress"><div style="width:${Math.min(100, bf.done / bf.target * 100)}%"></div></div></div>` : ""}
     <div class="row"><button id="crawlBtn">立即抓取最新交易日</button><button id="anaBtn">重新分析</button><button id="bfBtn">重新補抓歷史</button></div>
     <div class="tablewrap"><table><thead><tr><th class="l">資料日</th><th class="l">狀態</th><th>次數</th><th class="l">完成時間</th><th class="l">說明</th></tr></thead><tbody>
      ${sys.recentRuns.map(r => `<tr><td class="l num"><a href="#/day/${r.date.slice(0, 4)}-${r.date.slice(4, 6)}-${r.date.slice(6)}">${r.date}</a></td><td class="l">${badge(r.status)}</td><td class="num">${r.attempts}</td><td class="l num">${esc((r.finished_at || "").replace("T", " ").slice(0, 16))}</td><td class="l" style="white-space:normal">${esc(r.message)}</td></tr>`).join("")}
     </tbody></table></div></section>
   ${watchPanelHTML(wl, labels, sys)}
   <section class="panel" style="display:flex;flex-direction:column;gap:10px"><h2>判定門檻</h2>
     <form id="rulesForm" class="grid2">${rules.params.map(p => `<div class="field"><label for="r-${esc(p.id)}">${esc(p.label)}${p.unit ? "（" + esc(p.unit) + "）" : ""}</label><input id="r-${esc(p.id)}" name="${esc(p.id)}" type="number" step="any" value="${p.value}"${isNum(p.min) ? ` min="${p.min}"` : ""}${isNum(p.max) ? ` max="${p.max}"` : ""}><span class="hint">${esc(p.note || "")}${p.id in rules.defaults ? "　預設 " + rules.defaults[p.id] : "　自訂參數"}</span></div>`).join("")}
      <div class="field" style="justify-content:flex-end"><button class="primary">儲存並重新分析</button></div></form></section>
   <section class="panel" style="display:flex;flex-direction:column;gap:10px"><h2>API</h2>
     <div class="small">讀取用唯讀 token（放在 <span class="num">?token=</span> 或 Authorization header）；修改設定、觸發抓取、匯出資料庫需要管理 token（只能放在 Authorization header）：</div>
     <div class="log">GET /api/v1/latest              最新交易日（含狀態 status / ready）
GET /api/v1/latest?ready=1      最新「抓取＋分析皆完成」的交易日
GET /api/v1/days/2026-11-11     指定日；?format=csv | radar | status
GET /api/v1/stocks/2330/history?days=120&amp;format=csv
GET /api/v1/dates  /status  /watchlist  /rules  /events  /labels
GET /api/v1/stocks/search?q=奇鋐   用代號或名稱找股票
GET /api/v1/watchlist/3017/rules | trial   這檔套用的規則／試算
GET /api/v1/export/sqlite       下載整個資料庫（管理）
GET/PUT /api/v1/settings        全部設定（管理）；/settings/history、/restore、/reset、/export</div></section>
   <section class="panel"><h2>系統紀錄</h2><div class="log">${ev.map(e => `${esc(e.at)}  [${esc(e.level)}] ${esc(e.message)}`).join("\n")}</div></section>`;
  bindAuthPanel();
  const act = async (p, body, msg) => { try { await api(p, {method: "POST", body: JSON.stringify(body || {})}); toast(msg); setTimeout(viewSettings, 800); } catch (err) { toast(err.message); } };
  $("#crawlBtn").onclick = () => act("/crawl", {}, "已排入抓取");
  $("#anaBtn").onclick = () => act("/analyze", {}, "已重新分析");
  $("#bfBtn").onclick = () => act("/backfill", {}, "已排入歷史補抓");
  bindWatchPanel(wl, labels, rules);
  applySorts();
  $("#rulesForm").addEventListener("submit", async ev => { ev.preventDefault(); const b = {}; rules.params.forEach(p => { const v = Number(ev.target[p.id].value); if (isFinite(v)) b[p.id] = v; });
    try { await api("/rules", {method: "PUT", body: JSON.stringify(b)}); toast("已儲存並重新分析"); } catch (err) { toast(err.message); } });
}

/* ---------- 標籤、排序 ---------- */
let LABELS = {};
async function loadLabels() { try { const ls = await api("/labels"); LABELS = Object.fromEntries(ls.map(l => [l.name, l])); return ls; } catch (e) { return []; } }
const chip = (name, extra = "") => { const i = name.indexOf("::"), c = (LABELS[name] || {}).color || "#5c6b7a";
  return `<span class="lbl" style="--c:${esc(c)}" data-label="${esc(name)}">${i > 0 ? `<span>${esc(name.slice(0, i))}</span><b>${esc(name.slice(i + 2))}</b>` : `<span>${esc(name)}</span>`}${extra}</span>`; };
const chips = names => (names || []).map(n => chip(n)).join(" ");
const sortVal = c => { const v = c.dataset.v !== undefined ? c.dataset.v : c.textContent.trim(); const t = v.replace(/[,+%x]/g, ""), n = Number(t); return t !== "" && isFinite(n) ? n : v; };
function sortTable(tb, col, dir) {
  const body = tb.tBodies[0]; if (!body) return;
  const rows = [...body.rows].map(r => [sortVal(r.cells[col] || r.cells[0]), r]);
  rows.sort(([a], [b]) => { const na = typeof a === "number", nb = typeof b === "number";
    if (na !== nb) return na ? -1 : 1;                      // 沒有數值的排最後
    return (na ? a - b : String(a).localeCompare(String(b), "zh-Hant")) * dir; });
  rows.forEach(([, r]) => body.appendChild(r));
  [...tb.tHead.rows[0].cells].forEach((th, i) => i === col ? th.setAttribute("aria-sort", dir > 0 ? "ascending" : "descending") : th.removeAttribute("aria-sort"));
}
function applySorts() { document.querySelectorAll("table.sortable[data-sort]").forEach(tb => { try { const s = JSON.parse(localStorage.getItem("twradar-sort-" + tb.dataset.sort) || "null"); if (s) sortTable(tb, s.col, s.dir); } catch (e) {} }); }
document.addEventListener("click", ev => { const th = ev.target.closest("table.sortable thead th"); if (!th || th.dataset.nosort !== undefined) return;
  const tb = th.closest("table"), col = th.cellIndex, dir = th.getAttribute("aria-sort") === "ascending" ? -1 : 1;
  sortTable(tb, col, dir); try { localStorage.setItem("twradar-sort-" + tb.dataset.sort, JSON.stringify({col, dir})); } catch (e) {} });

/* ---------- 觀察清單、標籤 ---------- */
const mktName = m => m === "TPEx" ? "上櫃" : "上市";
function watchPanelHTML(wl, labels, sys) {
  return `<section class="panel" style="display:flex;flex-direction:column;gap:10px"><div class="row between"><h2>觀察清單（${wl.length}/${sys.maxWatchlist}）</h2><span class="small muted">點欄位標題可排序</span></div>
     <div class="suggest"><input id="addQ" autocomplete="off" placeholder="新增：輸入代號或名稱，例如 3017 或 奇鋐" aria-label="新增標的"><div id="addSug" class="sugbox" hidden></div></div>
     ${labels.length ? `<div class="row" id="lblFilter"><span class="small muted">篩選：</span>${labels.map(l => `<button type="button" class="lblbtn" data-f="${esc(l.name)}">${chip(l.name)}<span class="small muted">${l.count}</span></button>`).join("")}</div>` : ""}
     <div class="tablewrap"><table class="sortable" data-sort="wl"><thead><tr><th class="l">代號</th><th class="l">名稱</th><th class="l">市場</th><th class="l">標籤</th><th>關鍵價</th><th class="l">持有</th><th>成本</th><th class="l">觀察重點</th><th data-nosort></th></tr></thead><tbody id="wlBody">
      ${wl.map(w => `<tr data-labels="${esc(JSON.stringify(w.labels))}"><td class="l num">${esc(w.code)}</td><td class="l">${esc(w.name)}</td><td class="l">${mktName(w.market)}</td><td class="l" data-v="${esc(w.labels.join(" "))}">${chips(w.labels)}</td><td class="num">${fmt(w.keyLevel)}</td><td class="l" data-v="${w.holding ? 0 : 1}">${w.holding ? "是" : ""}</td><td class="num">${isNum(w.cost) ? fmt(w.cost, 2) : ""}</td><td class="l" style="white-space:normal">${esc(w.note)}</td>
        <td><button data-edit="${esc(w.code)}">編輯</button> <button class="danger" data-del="${esc(w.code)}">移除</button></td></tr>`).join("")}
     </tbody></table></div></section>
   <section class="panel" style="display:flex;flex-direction:column;gap:10px"><div class="row between"><h2>標籤（${labels.length}）</h2><span class="small muted">「策略::名稱」可以帶規則；同一個範圍（:: 前面相同）一檔只能貼一個</span></div>
     <form id="lblAdd" class="row"><input id="lblNew" style="flex:1" placeholder="新增標籤，例如 散熱 或 策略::中小飆股" maxlength="30"><button class="primary">新增</button></form>
     ${labels.length ? `<div class="tablewrap"><table class="sortable" data-sort="labels"><thead><tr><th class="l">標籤</th><th>檔數</th><th class="l">規則</th><th class="l" data-nosort>顏色</th><th data-nosort></th></tr></thead><tbody>
      ${labels.map(l => { const n = Object.keys(l.params || {}).length; return `<tr><td class="l" data-v="${esc(l.name)}">${chip(l.name)}</td><td class="num">${l.count}</td><td class="l">${l.strategy ? esc([n ? `覆寫 ${n} 個參數` : "", l.allowAdd === false ? "不加碼" : ""].filter(Boolean).join("、") || "與預設相同") : '<span class="muted">—</span>'}</td>
        <td class="l"><input type="color" value="${esc(l.color)}" data-color="${esc(l.name)}" aria-label="顏色"></td>
        <td>${l.strategy ? `<button data-strat="${esc(l.name)}">編輯規則</button> ` : ""}<button data-ren="${esc(l.name)}">改名</button> <button class="danger" data-ldel="${esc(l.name)}">刪除</button></td></tr>`; }).join("")}
     </tbody></table></div>` : `<div class="small muted">還沒有標籤。在個股的「編輯」裡直接輸入也會自動建立。</div>`}</section>`;
}
function bindWatchPanel(wl, labels, rules) {
  const reload = () => viewSettings();
  const putLabels = async (list, renames, msg) => { try { await api("/labels", {method: "PUT", body: JSON.stringify({labels: list, renames: renames || {}})}); $("#layer").innerHTML = ""; toast(msg); reload(); } catch (err) { toast(err.message); } };
  const plain = l => ({name: l.name, color: l.color, params: l.params || {}, allowAdd: l.allowAdd !== false});
  const addStock = async body => { try { const j = await api("/watchlist", {method: "POST", body: JSON.stringify(body)}); toast(`已加入 ${j.stock.code} ${j.stock.name}，歷史資料會在背景補齊`); reload(); } catch (err) { toast(err.message); } };
  /* 新增：輸入代號或名稱 */
  const q = $("#addQ"), sug = $("#addSug"); let timer = 0, seq = 0;
  q.addEventListener("input", () => { clearTimeout(timer); const v = q.value.trim(); if (!v) { sug.hidden = true; return; }
    timer = setTimeout(async () => { const my = ++seq; let hits = []; try { hits = await api("/stocks/search?q=" + encodeURIComponent(v)); } catch (e) {} if (my !== seq) return;
      sug.innerHTML = hits.length ? hits.map(h => `<button type="button" data-code="${esc(h.code)}"${h.inWatchlist ? " disabled" : ""}><b class="num">${esc(h.code)}</b> ${esc(h.name)} <span class="small muted">${mktName(h.market)}${h.inWatchlist ? "・已在清單" : ""}</span></button>`).join("")
        : `<div class="small muted" style="padding:8px 10px">找不到「${esc(v)}」。資料庫裡沒有這檔的行情時，按 Enter 也無法加入。</div>`;
      sug.hidden = false; }, 180); });
  q.addEventListener("keydown", ev => { if (ev.key === "Enter") { ev.preventDefault(); const first = sug.querySelector("button:not([disabled])"); if (!sug.hidden && first && sug.querySelectorAll("button").length === 1) addStock({code: first.dataset.code}); else if (q.value.trim()) addStock({q: q.value.trim()}); } else if (ev.key === "Escape") sug.hidden = true; });
  sug.addEventListener("click", ev => { const b = ev.target.closest("button[data-code]"); if (b) addStock({code: b.dataset.code}); });
  /* 標籤篩選 */
  const active = new Set();
  document.querySelectorAll("#lblFilter [data-f]").forEach(b => b.onclick = () => { active.has(b.dataset.f) ? active.delete(b.dataset.f) : active.add(b.dataset.f); b.classList.toggle("on");
    document.querySelectorAll("#wlBody tr").forEach(tr => { const ls = JSON.parse(tr.dataset.labels); tr.hidden = [...active].some(a => !ls.includes(a)); }); });
  /* 編輯、移除 */
  document.querySelectorAll("[data-edit]").forEach(btn => btn.onclick = () => editStock(wl.find(w => w.code === btn.dataset.edit), labels, rules, reload));
  document.querySelectorAll("[data-del]").forEach(btn => btn.onclick = () => sheet(`<h2>移除 ${esc(btn.dataset.del)}？</h2><div class="small muted">已抓取的歷史資料會保留。</div><div class="row" style="justify-content:flex-end"><button data-close>取消</button><button class="danger" id="yes">移除</button></div>`,
    s => s.querySelector("#yes").onclick = async () => { await api("/watchlist/" + btn.dataset.del, {method: "DELETE"}); $("#layer").innerHTML = ""; toast("已移除"); reload(); }));
  /* 標籤管理 */
  $("#lblAdd").addEventListener("submit", ev => { ev.preventDefault(); const name = $("#lblNew").value.trim(); if (!name) return;
    if (labels.some(l => l.name === name)) { toast("已經有這個標籤"); return; }
    putLabels([...labels.map(plain), {name, color: "#5c6b7a", params: {}, allowAdd: true}], null, "已新增標籤"); });
  document.querySelectorAll("[data-color]").forEach(inp => inp.onchange = () => putLabels(labels.map(l => ({...plain(l), color: l.name === inp.dataset.color ? inp.value : l.color})), null, "已更新顏色"));
  document.querySelectorAll("[data-ldel]").forEach(btn => btn.onclick = () => { const l = labels.find(x => x.name === btn.dataset.ldel);
    sheet(`<h2>刪除標籤？</h2><div>${chip(l.name)}</div><div class="small muted">${l.count ? `會從 ${l.count} 檔股票拿掉這個標籤` : "目前沒有股票使用"}${l.strategy && l.count ? "，這些股票改用全域預設規則" : ""}。</div><div class="row" style="justify-content:flex-end"><button data-close>取消</button><button class="danger" id="yes">刪除</button></div>`,
      s => s.querySelector("#yes").onclick = () => putLabels(labels.filter(x => x.name !== l.name).map(plain), null, "已刪除標籤")); });
  document.querySelectorAll("[data-ren]").forEach(btn => btn.onclick = () => { const old = btn.dataset.ren;
    sheet(`<h2>標籤改名</h2><form id="renForm" class="row"><input id="renName" style="flex:1" maxlength="30" value="${esc(old)}"><button class="primary">儲存</button></form><div class="small muted">已貼這個標籤的股票會一起改。</div>`,
      s => { s.querySelector("#renName").focus(); s.querySelector("#renForm").addEventListener("submit", ev => { ev.preventDefault(); const name = s.querySelector("#renName").value.trim(); if (!name || name === old) { $("#layer").innerHTML = ""; return; }
        if (labels.some(l => l.name === name)) { toast("已經有這個標籤"); return; }
        putLabels(labels.map(l => ({...plain(l), name: l.name === old ? name : l.name})), {[old]: name}, "已改名"); }); }); });
  document.querySelectorAll("[data-strat]").forEach(btn => btn.onclick = () => editStrategy(labels.find(l => l.name === btn.dataset.strat), labels, rules, putLabels, plain));
}
const NO_OVERRIDE = ["pullbackDays", "whaleLots", "whaleAmount", "retailMaxLots"];
function editStrategy(l, labels, rules, putLabels, plain) {
  const ov = {...(l.params || {})}; const ps = rules.params.filter(p => !NO_OVERRIDE.includes(p.id)); const byId = Object.fromEntries(ps.map(p => [p.id, p]));
  sheet(`<h2>策略規則</h2><div>${chip(l.name)} <span class="small muted">影響 ${l.count} 檔</span></div>
    <div class="small muted">只寫和全域預設不一樣的地方；沒列出的參數都跟著預設走。</div>
    <label class="row"><input type="checkbox" id="stAdd"${l.allowAdd === false ? "" : " checked"}> 允許加碼（取消勾選＝只上車、不加碼）</label>
    <div id="stRows" style="display:flex;flex-direction:column;gap:6px"></div>
    <div class="row"><select id="stPick" style="flex:1"></select><button type="button" id="stPlus">加入覆寫</button></div>
    <div class="row" style="justify-content:flex-end"><button data-close>取消</button><button class="primary" id="stSave">儲存並重新分析</button></div>`,
    s => { const draw = () => {
        s.querySelector("#stRows").innerHTML = Object.keys(ov).map(k => { const p = byId[k]; return p ? `<div class="row"><span style="flex:1">${esc(p.label)}${p.unit ? "（" + esc(p.unit) + "）" : ""}<span class="small muted">　預設 ${p.value}</span></span><input type="number" step="any" style="width:90px" data-ov="${esc(k)}" value="${ov[k]}"${isNum(p.min) ? ` min="${p.min}"` : ""}${isNum(p.max) ? ` max="${p.max}"` : ""}><button type="button" data-rm="${esc(k)}" aria-label="移除">✕</button></div>` : ""; }).join("") || `<div class="small muted">目前沒有覆寫任何參數。</div>`;
        s.querySelector("#stPick").innerHTML = ps.filter(p => !(p.id in ov)).map(p => `<option value="${esc(p.id)}">${esc(p.label)}（預設 ${p.value}${esc(p.unit || "")}）</option>`).join("");
        s.querySelectorAll("[data-ov]").forEach(i => i.oninput = () => { ov[i.dataset.ov] = Number(i.value); });
        s.querySelectorAll("[data-rm]").forEach(b => b.onclick = () => { delete ov[b.dataset.rm]; draw(); }); };
      draw();
      s.querySelector("#stPlus").onclick = () => { const k = s.querySelector("#stPick").value; if (k) { ov[k] = byId[k].value; draw(); } };
      s.querySelector("#stSave").onclick = () => { if (Object.values(ov).some(v => !isFinite(v))) { toast("覆寫的值必須是數字"); return; }
        putLabels(labels.map(x => x.name === l.name ? {...plain(x), params: ov, allowAdd: s.querySelector("#stAdd").checked} : plain(x)), null, "已儲存策略並重新分析"); }; });
}
function editStock(w, labels, rules, reload) {
  let cur = [...w.labels];
  sheet(`<div class="row between"><h2><span class="num">${esc(w.code)}</span> ${esc(w.name)}</h2><span class="small muted">${mktName(w.market)}</span></div>
    <div class="tabs2" role="tablist"><button type="button" role="tab" data-tab="base" class="on">基本</button><button type="button" role="tab" data-tab="rules">規則</button><button type="button" role="tab" data-tab="trial">試算</button></div>
    <form id="pane-base" style="display:flex;flex-direction:column;gap:10px">
      <div class="field"><label>標籤</label><div id="eLabels" class="row"></div>
        <input id="eLabelIn" list="eLabelList" placeholder="輸入後按 Enter；策略::名稱 會套用那個策略的規則" maxlength="30"><datalist id="eLabelList">${labels.map(l => `<option value="${esc(l.name)}">`).join("")}</datalist></div>
      <div class="field"><label for="e-note">觀察重點</label><input id="e-note" value="${esc(w.note || "")}"></div>
      <div class="grid2"><div class="field"><label for="e-key">關鍵價（選填）</label><input id="e-key" inputmode="decimal" value="${w.keyLevel ?? ""}"></div>
       <div class="field"><label for="e-hold">持有中</label><select id="e-hold"><option value="0">否</option><option value="1"${w.holding ? " selected" : ""}>是</option></select></div>
       <div class="field"><label for="e-cost">成本價</label><input id="e-cost" inputmode="decimal" value="${w.cost ?? ""}"></div>
       <div class="field"><label for="e-shares">股數</label><input id="e-shares" inputmode="numeric" value="${w.shares ?? ""}"></div>
       <div class="field"><label for="e-buy">買進日期（保本線用）</label><input id="e-buy" type="date" value="${w.buyDate ? esc(w.buyDate.replace(/^(\d{4})-?(\d{2})-?(\d{2})$/, "$1-$2-$3")) : ""}"></div>
       <div class="field"><label for="e-name">名稱</label><input id="e-name" value="${esc(w.name)}"></div></div>
      <div class="small muted">持有中要填成本，停損與出場的提示才會啟動。</div>
      <div class="row" style="justify-content:flex-end"><button type="button" data-close>取消</button><button class="primary">儲存</button></div></form>
    <div id="pane-rules" hidden></div><div id="pane-trial" hidden></div>`,
    s => { const q = x => s.querySelector(x);
      const draw = () => { q("#eLabels").innerHTML = cur.map(n => chip(n, `<button type="button" class="x" data-x="${esc(n)}" aria-label="移除 ${esc(n)}">×</button>`)).join(" ") || `<span class="small muted">沒有標籤</span>`;
        q("#eLabels").querySelectorAll("[data-x]").forEach(b => b.onclick = () => { cur = cur.filter(n => n !== b.dataset.x); draw(); }); };
      const add = v => { v = v.trim(); if (!v) return; const i = v.indexOf("::"); if (i > 0) cur = cur.filter(n => !n.startsWith(v.slice(0, i + 2))); if (!cur.includes(v)) cur.push(v); draw(); };
      draw();
      q("#eLabelIn").addEventListener("keydown", ev => { if (ev.key === "Enter") { ev.preventDefault(); add(ev.target.value); ev.target.value = ""; } });
      q("#eLabelIn").addEventListener("change", ev => { if (labels.some(l => l.name === ev.target.value)) { add(ev.target.value); ev.target.value = ""; } });
      q("#pane-base").addEventListener("submit", async ev => { ev.preventDefault(); add(q("#eLabelIn").value);
        const b = {code: w.code, name: q("#e-name").value.trim() || w.name, market: w.market, labels: cur, note: q("#e-note").value.trim(), keyLevel: q("#e-key").value ? Number(q("#e-key").value) : null,
          holding: q("#e-hold").value === "1", cost: q("#e-cost").value ? Number(q("#e-cost").value) : null, shares: q("#e-shares").value ? parseInt(q("#e-shares").value, 10) : null, buyDate: q("#e-buy").value || null};
        try { await api("/watchlist", {method: "POST", body: JSON.stringify(b)}); await api("/analyze", {method: "POST", body: "{}"}).catch(() => {}); $("#layer").innerHTML = ""; toast("已儲存 " + w.code); reload(); } catch (err) { toast(err.message); } });
      const dirty = () => JSON.stringify(cur) !== JSON.stringify(w.labels) ? `<div class="banner warn small">標籤有變更但還沒儲存；這裡顯示的是儲存前的狀態。</div>` : "";
      const st = {pass: "✓", fail: "✕", na: "?"};
      const load = {
        rules: async el => { const r = await api(`/watchlist/${w.code}/rules`); const ov = r.params.filter(p => p.source === "label"), rest = r.params.filter(p => p.source !== "label");
          el.innerHTML = `${dirty()}<div>${r.strategy ? `套用 ${chip(r.strategy)} <span class="small muted">共 ${r.affected} 檔使用</span>` : `套用<b>全域預設</b> <span class="small muted">（沒有貼「策略::」標籤）</span>`}${r.allowAdd ? "" : "　<b>不加碼</b>"}</div>
            <div class="small muted">規則跟著標籤走，個股本身不放規則。要調整請編輯策略標籤（設定頁「標籤」），會影響所有貼了它的股票。</div>
            <div class="tablewrap"><table><thead><tr><th class="l">參數</th><th>套用值</th><th class="l">來源</th></tr></thead><tbody>
            ${[...ov, ...rest].map(p => `<tr><td class="l" style="white-space:normal">${esc(p.label)}</td><td class="num">${p.value}${esc(p.unit || "")}</td><td class="l">${p.source === "label" ? `<b>策略標籤</b> <span class="small muted">預設 ${p.default}</span>` : `<span class="muted">預設</span>`}</td></tr>`).join("")}</tbody></table></div>`; },
        trial: async el => { const t = await api(`/watchlist/${w.code}/trial`); const h = t.hold;
          el.innerHTML = `${dirty()}<div class="row">${badge(t.light, t.lightLabel)}${h ? badge(h.light) : ""}<span class="small muted">資料日 ${esc(t.date)}・${t.strategy ? esc(t.strategy) : "全域預設"}${t.rule ? "・觸發規則：" + esc(t.rule) : ""}</span></div>
            ${t.needs.length ? `<div class="small"><b>還差：</b>${esc(t.needs.join("；"))}</div>` : ""}${t.warnings.length ? `<div class="small"><b>原因：</b>${esc(t.warnings.join("；"))}</div>` : ""}
            ${h ? `<div class="small"><b>持有：</b>${esc(holdLine(h))}</div>` : ""}${t.missing.length ? `<div class="small"><b>未取得：</b>${esc(t.missing.join("、"))}</div>` : ""}
            <div class="tablewrap"><table class="sortable" data-sort="trial"><thead><tr><th class="l">條件</th><th>結果</th><th class="l">說明</th></tr></thead><tbody>
            ${t.trace.map(x => `<tr><td class="l">${esc(x.label)}</td><td data-v="${{pass: 0, fail: 1, na: 2}[x.state]}"><span class="mx ${esc(x.state)}">${st[x.state] || "?"}</span></td><td class="l" style="white-space:normal">${esc(x.text || "")}</td></tr>`).join("")}</tbody></table></div>
            <div class="small muted">✓ 成立　✕ 不成立　? 資料不足。只列出這次判定用到的條件。</div>`; }};
      s.querySelectorAll("[data-tab]").forEach(b => b.onclick = async () => { s.querySelectorAll("[data-tab]").forEach(x => x.classList.toggle("on", x === b));
        ["base", "rules", "trial"].forEach(k => { q("#pane-" + k).hidden = k !== b.dataset.tab; });
        const el = q("#pane-" + b.dataset.tab); if (load[b.dataset.tab]) { el.innerHTML = `<div class="small muted">載入中…</div>`; try { await load[b.dataset.tab](el); } catch (err) { el.innerHTML = `<div class="banner bad small">${esc(err.message)}</div>`; } } }); });
}

/* ---------- 路由 ---------- */
async function route() {
  const h = location.hash.replace(/^#/, "") || "/";
  const [, page, arg] = h.split("/");
  document.querySelectorAll("#tabs a").forEach(a => a.classList.toggle("on", a.dataset.tab === (page === "report" ? "report" : page === "settings" ? "settings" : "home")));
  try {
    if (page !== "settings" && !Object.keys(LABELS).length) await loadLabels();
    if (page === "report") await viewReport(arg);
    else if (page === "settings") await viewSettings();
    else if (page === "stock" && arg) await viewStock(arg);
    else await viewHome(page === "day" ? arg : null);
    applySorts();
  } catch (err) { $("#view").innerHTML = `<div class="panel empty">無法載入：${esc(err.message)}</div>`; }
}
window.addEventListener("hashchange", route);
route();
})();
