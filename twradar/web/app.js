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
const LIGHT = {go: "加碼訊號", wait: "觀察中", stop: "不宜加碼", na: "資料不足"};
const STATUS = {complete: "完成", waiting: "等待交易所公布", running: "抓取中", partial: "部分缺漏", failed: "失敗",
  holiday: "休市", not_started: "尚未開始", provisional: "暫定", pending: "等待中", ok: "完成", nodata: "尚未公布", fallback: "備援完成"};
const RULES = [["maTol","月線容許誤差（%）","低於月線但在此範圍內不算跌破"],["streakDays","法人連續買超天數 ≥",""],
  ["amplify","單日買超放大倍數 ≥","當日買超 ÷ 前 4 日平均絕對值"],["volRatio","量能門檻（倍 5 日均量）>",""],
  ["marginFlat","融資持平容許增量（張）≤",""],["divergeMargin","背離：融資增加超過（張）>",""],
  ["blackPct","長黑跌幅（%）≥",""],["blowoff","爆量倍數 ≥",""]];

/* ---------- token ---------- */
let TOKEN = "";
try { TOKEN = localStorage.getItem("twradar-token") || ""; } catch (e) {}
const m = location.hash.match(/[#&?]t=([^&]+)/);
if (m) { TOKEN = decodeURIComponent(m[1]); try { localStorage.setItem("twradar-token", TOKEN); } catch (e) {} location.hash = "#/"; }
const withTok = u => u + (u.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(TOKEN);

async function api(path, opts = {}) {
  const r = await fetch("/api/v1" + path, {...opts, headers: {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}});
  if (r.status === 401) { askToken(); throw new Error("需要 token"); }
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
function askToken() {
  if ($("#tokForm")) return;
  sheet(`<h2>輸入存取 token</h2><div class="small muted">token 在安裝完成時顯示，也存在電腦桌面的「右側加碼雷達-連線資訊.txt」。</div>
    <form id="tokForm" class="row"><input id="tok" style="flex:1" autocomplete="off" placeholder="貼上 token"><button class="primary">確定</button></form>`,
    s => s.querySelector("#tokForm").addEventListener("submit", e => { e.preventDefault(); TOKEN = s.querySelector("#tok").value.trim();
      try { localStorage.setItem("twradar-token", TOKEN); } catch (err) {} $("#layer").innerHTML = ""; route(); }));
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
   <div class="row between"><div><b>${esc(s.code)} ${esc(s.name)}</b><div class="small muted">${esc(s.group || "")}・${s.market === "TPEx" ? "上櫃" : "上市"}</div></div>${badge(g.light)}</div>
   <div class="price"><span class="c ${cls(r.chgPct)}">${fmt(r.close, pd(r.close))}</span><span class="num ${cls(r.chgPct)}">${fmtP(r.chgPct)}</span>
     <span class="small muted num">量 ${fmt(r.vol)}${isNum(g.volRatio) ? "・" + g.volRatio.toFixed(2) + "x" : ""}</span></div>
   <div class="metrics">
     <div><span class="k">外資</span><span class="v ${cls(r.foreign)}">${fmtS(r.foreign)}</span></div>
     <div><span class="k">投信</span><span class="v ${cls(r.trust)}">${fmtS(r.trust)}</span></div>
     <div><span class="k">融資</span><span class="v">${fmtS(r.marginChg)}</span></div>
     <div><span class="k">月線</span><span class="v">${fmt(r.ma20, 2)}</span></div>
     <div><span class="k">乖離</span><span class="v">${isNum(g.maDistPct) ? fmtS(g.maDistPct, 1) + "%" : "—"}</span></div>
     <div><span class="k">外/投 連續</span><span class="v">${isNum(r.foreignStreak) ? fmtS(r.foreignStreak) : "—"} / ${isNum(r.trustStreak) ? fmtS(r.trustStreak) : "—"}</span></div></div>
   <div class="checks">${ck("法人", g.checks.inst, g.instWhy)}${ck("主力", g.checks.main)}${ck("散戶", g.checks.retail)}${ck("價量", g.checks.priceVolume)}${g.warnings.map(w => `<span class="ck warn">! ${esc(w)}</span>`).join("")}</div>
   ${g.light === "wait" && g.needs.length ? `<div class="need"><b>還差：</b>${esc(g.needs.join("；"))}</div>` : ""}
   ${g.light === "go" ? `<div class="need"><b>依據：</b>${esc([g.instWhy, g.checks.retail === "pass" ? "融資減且上漲" : "", g.checks.main === "pass" ? "主力集中" : "", "價量確認"].filter(Boolean).join("、"))}；觀察價位 月線 ${fmt(r.ma20, 2)}${isNum(r.prevHigh) ? "、前高 " + fmt(r.prevHigh, pd(r.prevHigh)) : ""}</div>` : ""}
   ${g.light === "stop" ? `<div class="need"><b>原因：</b>${esc(g.warnings.join("；"))}</div>` : ""}
   ${g.keyLevelNote ? `<div class="note">${esc(g.keyLevelNote)}</div>` : ""}${s.note ? `<div class="note">${esc(s.note)}</div>` : ""}
   ${g.missing.length ? `<div class="note">未取得：${esc(g.missing.join("、"))}</div>` : ""}</article>`;
}
function tableHTML(stocks) {
  return `<div class="tablewrap"><table><thead><tr><th class="l">代號</th><th class="l">名稱</th><th>收盤</th><th>漲跌%</th><th>外資</th><th>投信</th><th>融資增減</th><th>量比</th><th>月線位置</th><th class="l">燈號</th></tr></thead><tbody>
  ${stocks.map(s => { const r = s.data, g = s.signal; const pos = g.belowMa20 === null || g.belowMa20 === undefined ? "—" : g.belowMa20 ? "月線下" : g.maDistPct < 0 ? "貼近月線" : "月線上";
    return `<tr><td class="l num">${esc(s.code)}</td><td class="l">${esc(s.name)}</td><td class="num">${fmt(r.close, pd(r.close))}</td><td class="num ${cls(r.chgPct)}">${fmtP(r.chgPct)}</td><td class="num ${cls(r.foreign)}">${fmtS(r.foreign)}</td><td class="num ${cls(r.trust)}">${fmtS(r.trust)}</td><td class="num">${fmtS(r.marginChg)}</td><td class="num">${isNum(g.volRatio) ? g.volRatio.toFixed(2) + "x" : "—"}</td><td>${pos} <span class="muted num">${isNum(g.maDistPct) ? "(" + fmtS(g.maDistPct, 1) + "%)" : ""}</span></td><td class="l">${badge(g.light)}</td></tr>`; }).join("")}
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
async function viewReport(date) {
  await loadDates();
  const e = date ? await api("/days/" + date) : await api("/latest");
  const d = e.date; if (!d) { $("#view").innerHTML = `<div class="panel empty">尚無資料</div>`; return; }
  const st = e.stocks, m = e.market;
  const withMA = st.filter(s => isNum(s.signal.maDistPct)).map(s => ({label: s.code + " " + s.name, v: s.signal.maDistPct, light: s.signal.light}));
  const sec = (k, title) => { const list = st.filter(s => s.signal.light === k); if (!list.length) return "";
    return `<div class="sec"><h2 style="color:var(--${k === "na" ? "na" : k})">${title}（${list.length}）</h2>${list.map(s => { const r = s.data, g = s.signal;
      const line = k === "go" ? "依據：" + [g.instWhy, g.checks.retail === "pass" ? "融資減且上漲" : "", g.checks.main === "pass" ? "主力集中" : "", "價量確認"].filter(Boolean).join("、") + `；觀察價位 月線 ${fmt(r.ma20, 2)}${isNum(r.prevHigh) ? "、前高 " + fmt(r.prevHigh, pd(r.prevHigh)) : ""}`
        : k === "wait" ? "還差：" + (g.needs.join("；") || "—") : k === "stop" ? "原因：" + g.warnings.join("；") : "未取得：" + g.missing.join("、");
      return `<p style="margin:8px 0"><b>${esc(s.code)} ${esc(s.name)}</b>　收 ${fmt(r.close, pd(r.close))}（<span class="${cls(r.chgPct)}">${fmtP(r.chgPct)}</span>）　外資 ${fmtS(r.foreign)}　投信 ${fmtS(r.trust)}　融資 ${fmtS(r.marginChg)}<br><span class="muted">${esc(line)}${g.keyLevelNote ? "；" + esc(g.keyLevelNote) : ""}</span></p>`; }).join("")}</div>`; };
  $("#view").innerHTML = `
   <div class="row between noprint"><div class="row"><select id="dsel" aria-label="資料日">${dateOptions(d)}</select>${e.ready ? "" : badge(e.status, e.statusText)}</div>
     <div class="row"><button class="primary" id="printBtn">列印／存成 PDF</button></div></div>
   <article class="report">
    <div><h1>右側加碼晨報｜資料日 ${d.replace(/-/g, "/")}</h1><div class="small muted">產出時間 ${new Date().toLocaleString("zh-TW", {hour12: false})}・資料狀態：${esc(e.statusText)}</div></div>
    ${marketHTML(m)}
    <div class="tiles">${["go", "wait", "stop"].map(k => `<div class="tile ${k}"><span class="n">${e.summary[k]}</span><span class="l">${LIGHT[k]}</span></div>`).join("")}</div>
    <div class="sec"><h2>訊號矩陣</h2><div class="tablewrap"><table class="matrix"><thead><tr><th class="l">個股</th><th>法人</th><th>主力</th><th>散戶</th><th>價量</th><th>燈號</th></tr></thead><tbody>
     ${st.map(s => { const c = s.signal.checks, mk = v => `<span class="mx ${v}">${{pass: "✓", fail: "✕", na: "?"}[v]}</span>`;
       return `<tr><td class="l"><b class="num">${esc(s.code)}</b> ${esc(s.name)}</td><td>${mk(c.inst)}</td><td>${mk(c.main)}</td><td>${mk(c.retail)}</td><td>${mk(c.priceVolume)}</td><td>${badge(s.signal.light)}</td></tr>`; }).join("")}
    </tbody></table></div></div>
    <div class="sec"><h2>收盤與月線乖離（%）</h2><div class="small muted">0 線為月線，右側＝站上月線；長條顏色為燈號</div>${hBars(withMA, {color: lightColor, unit: "%"})}</div>
    <div class="sec"><h2>外資／投信 當日買賣超（張）</h2><div class="legend"><span><i style="background:var(--c-foreign)"></i>外資</span><span><i style="background:var(--c-trust)"></i>投信</span></div>
     ${divBars(st.map(s => s.code), [{values: st.map(s => s.data.foreign ?? null), color: "var(--c-foreign)"}, {values: st.map(s => s.data.trust ?? null), color: "var(--c-trust)"}], {h: 220, lab: d => d, every: 1})}</div>
    ${sec("go", "加碼訊號")}${sec("wait", "觀察中")}${sec("stop", "轉弱／不宜加碼")}${sec("na", "資料不足")}
    <div class="sec"><h2>精簡表</h2>${tableHTML(st)}</div>
    <div class="small muted">資料來源：證交所、櫃買中心${st.some(s => (s.data.source || "").includes("FinMind")) ? "、FinMind" : ""}。以上為籌碼訊號整理，非投資建議。</div>
   </article>`;
  $("#dsel").addEventListener("change", ev => location.hash = "#/report/" + ev.target.value);
  $("#printBtn").addEventListener("click", () => window.print());
}

/* ---------- 頁面：設定與狀態 ---------- */
async function viewSettings() {
  const [sys, wl, rules, ev] = await Promise.all([api("/status"), api("/watchlist"), api("/rules"), api("/events?limit=80")]);
  const bf = sys.backfill;
  $("#view").innerHTML = `
   <h1>設定與狀態</h1>
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
   <section class="panel" style="display:flex;flex-direction:column;gap:10px"><div class="row between"><h2>觀察清單（${wl.length}/${sys.maxWatchlist}）</h2></div>
     <form id="addForm" class="grid2"><div class="field"><label for="a-code">代號</label><input id="a-code" required placeholder="2330"></div>
      <div class="field"><label for="a-name">名稱</label><input id="a-name" required placeholder="台積電"></div>
      <div class="field"><label for="a-mkt">市場</label><select id="a-mkt"><option value="TWSE">上市</option><option value="TPEx">上櫃</option></select></div>
      <div class="field"><label for="a-grp">分組</label><input id="a-grp"></div>
      <div class="field"><label for="a-key">關鍵價（選填）</label><input id="a-key" inputmode="decimal"></div>
      <div class="field"><label for="a-note">觀察重點</label><input id="a-note"></div>
      <div class="field" style="justify-content:flex-end"><button class="primary">加入／更新</button></div></form>
     <div class="tablewrap"><table><thead><tr><th class="l">代號</th><th class="l">名稱</th><th class="l">市場</th><th class="l">分組</th><th>關鍵價</th><th class="l">觀察重點</th><th></th></tr></thead><tbody>
      ${wl.map(w => `<tr><td class="l num">${esc(w.code)}</td><td class="l">${esc(w.name)}</td><td class="l">${w.market === "TPEx" ? "上櫃" : "上市"}</td><td class="l">${esc(w.grp)}</td><td class="num">${fmt(w.key_level)}</td><td class="l" style="white-space:normal">${esc(w.note)}</td>
        <td><button data-edit='${esc(JSON.stringify(w))}'>編輯</button> <button class="danger" data-del="${esc(w.code)}">移除</button></td></tr>`).join("")}
     </tbody></table></div></section>
   <section class="panel" style="display:flex;flex-direction:column;gap:10px"><h2>判定門檻</h2>
     <form id="rulesForm" class="grid2">${RULES.map(([k, l, h]) => `<div class="field"><label for="r-${k}">${l}</label><input id="r-${k}" name="${k}" type="number" step="any" value="${rules.rules[k]}"><span class="hint">${h}　預設 ${rules.defaults[k]}</span></div>`).join("")}
      <div class="field" style="justify-content:flex-end"><button class="primary">儲存並重新分析</button></div></form></section>
   <section class="panel" style="display:flex;flex-direction:column;gap:10px"><h2>API</h2>
     <div class="small">晨報排程與其他程式可呼叫（token 放在 <span class="num">?token=</span> 或 Authorization header）：</div>
     <div class="log">GET /api/v1/latest              最新交易日（含狀態 status / ready）
GET /api/v1/latest?ready=1      最新「抓取＋分析皆完成」的交易日
GET /api/v1/days/2026-11-11     指定日；?format=csv | radar | status
GET /api/v1/stocks/2330/history?days=120&amp;format=csv
GET /api/v1/dates  /status  /watchlist  /rules  /events
GET /api/v1/export/sqlite       下載整個資料庫</div></section>
   <section class="panel"><h2>系統紀錄</h2><div class="log">${ev.map(e => `${esc(e.at)}  [${esc(e.level)}] ${esc(e.message)}`).join("\n")}</div></section>`;
  const act = async (p, body, msg) => { try { await api(p, {method: "POST", body: JSON.stringify(body || {})}); toast(msg); setTimeout(viewSettings, 800); } catch (err) { toast(err.message); } };
  $("#crawlBtn").onclick = () => act("/crawl", {}, "已排入抓取");
  $("#anaBtn").onclick = () => act("/analyze", {}, "已重新分析");
  $("#bfBtn").onclick = () => act("/backfill", {}, "已排入歷史補抓");
  $("#addForm").addEventListener("submit", async ev => { ev.preventDefault();
    const b = {code: $("#a-code").value.trim(), name: $("#a-name").value.trim(), market: $("#a-mkt").value, group: $("#a-grp").value.trim(), note: $("#a-note").value.trim(), keyLevel: $("#a-key").value ? Number($("#a-key").value) : null};
    const old = wl.find(w => w.code === b.code.toUpperCase()); if (old) b.order = old.sort_order;
    try { await api("/watchlist", {method: "POST", body: JSON.stringify(b)}); toast("已儲存 " + b.code + "，歷史資料會在背景補齊"); viewSettings(); } catch (err) { toast(err.message); } });
  document.querySelectorAll("[data-edit]").forEach(btn => btn.onclick = () => { const w = JSON.parse(btn.dataset.edit);
    $("#a-code").value = w.code; $("#a-name").value = w.name; $("#a-mkt").value = w.market; $("#a-grp").value = w.grp || ""; $("#a-note").value = w.note || ""; $("#a-key").value = w.key_level ?? ""; $("#a-code").focus(); });
  document.querySelectorAll("[data-del]").forEach(btn => btn.onclick = () => sheet(`<h2>移除 ${esc(btn.dataset.del)}？</h2><div class="small muted">已抓取的歷史資料會保留。</div><div class="row" style="justify-content:flex-end"><button data-close>取消</button><button class="danger" id="yes">移除</button></div>`,
    s => s.querySelector("#yes").onclick = async () => { await api("/watchlist/" + btn.dataset.del, {method: "DELETE"}); $("#layer").innerHTML = ""; toast("已移除"); viewSettings(); }));
  $("#rulesForm").addEventListener("submit", async ev => { ev.preventDefault(); const b = {}; RULES.forEach(([k]) => { const v = Number(ev.target[k].value); if (isFinite(v)) b[k] = v; });
    try { await api("/rules", {method: "PUT", body: JSON.stringify(b)}); toast("已儲存並重新分析"); } catch (err) { toast(err.message); } });
}

/* ---------- 路由 ---------- */
async function route() {
  if (!TOKEN) { askToken(); return; }
  const h = location.hash.replace(/^#/, "") || "/";
  const [, page, arg] = h.split("/");
  document.querySelectorAll("#tabs a").forEach(a => a.classList.toggle("on", a.dataset.tab === (page === "report" ? "report" : page === "settings" ? "settings" : "home")));
  try {
    if (page === "report") await viewReport(arg);
    else if (page === "settings") await viewSettings();
    else if (page === "stock" && arg) await viewStock(arg);
    else await viewHome(page === "day" ? arg : null);
  } catch (err) { $("#view").innerHTML = `<div class="panel empty">無法載入：${esc(err.message)}</div>`; }
}
window.addEventListener("hashchange", route);
route();
})();
