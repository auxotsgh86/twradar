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
  isNum(h.costAdj) ? `成本依除權息調整為 ${fmt(h.costAdj, 2)}` : "",
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
const TIPS = {
  taiex: "台股上市大盤指數，旁邊是今天的漲跌幅。",
  foreign: "外國法人今天在上市市場「買進減賣出」的金額。正數＝買超，負數＝賣超。外資是台股最大的資金來源，連續大賣通常會壓低大盤。",
  trust: "國內基金公司（含 ETF）今天的買賣超。ETF 有人申購，投信就得買，所以有一部分是被動買盤。",
  dealer: "證券商用自有資金的買賣超，包含發行權證的避險交易；進出很快，對方向的參考性較低。",
  attitude: "看外資近 5 日在三個地方是否同時撤出：現貨賣超、台指期空單增加、新台幣貶值。成立項數達到設定值（預設 3 項）時，系統暫停新的上車訊號。",
  spot5: "外資最近 5 個交易日買賣超的加總。負數代表外資這週整體在賣。",
  fut: "外資在台指期貨「空單比多單多出幾口」，也就是淨空單；數字越大，外資押注下跌或避險的部位越大（新聞常說的「外資淨空單 10 萬口」就是這個）。旁邊是 5 日變化：空單減少（回補）偏多，空單增加偏空。",
  fx: "1 美元可以換多少新台幣。數字變大＝新台幣貶值，常伴隨外資把錢匯出；數字變小＝新台幣升值、資金流入。",
  breadth: "全部上市櫃普通股中，收盤價站在自己月線（20 日均價）之上的比例，看短線強弱。指數在漲但廣度很低，代表只有少數權值股在撐盤。價格已做除權息還原，除息不會被當成跌破。",
  breadth200: "全部上市櫃普通股中，收盤價站在自己 200 日均線之上的比例，看長期結構。短期廣度很低、長期廣度還過半，通常是多頭中的修正；長期廣度也跌破一半，代表長期趨勢轉弱。",
};
const kv = (tip, k, v) => `<div class="kv" tabindex="0" data-tip="${esc(TIPS[tip] || "")}"><span class="k">${k}</span><span class="v">${v}</span></div>`;
function marketHTML(m) {
  return `<div class="market">
    ${kv("taiex", "加權指數", `<span class="num">${fmt(m.taiex, 2)}</span> <span class="small ${cls(m.taiexChgPct)}">${fmtP(m.taiexChgPct)}</span>`)}
    ${kv("foreign", "外資買賣超（億）", `<span class="num ${cls(m.foreignAmt)}">${fmtS(m.foreignAmt, 2)}</span>`)}
    ${kv("trust", "投信買賣超（億）", `<span class="num ${cls(m.trustAmt)}">${fmtS(m.trustAmt, 2)}</span>`)}
    ${kv("dealer", "自營商買賣超（億）", `<span class="num ${cls(m.dealerAmt)}">${fmtS(m.dealerAmt, 2)}</span>`)}</div>
    ${climateHTML(m.climate)}`;
}
/* 大盤環境：外資態度三項（近 5 日）、市場廣度，以及白話解讀 */
function climateHTML(c, {reading = true} = {}) {
  if (!c) return "";
  const st = c.foreignState, txt = {exit: "外資撤出：暫停上車", ok: "外資未撤出", na: "外資態度資料不足"}[st] || "";
  const b = `<span class="badge b-${st === "exit" ? "stop" : st === "ok" ? "go" : "na"}">${txt}（${c.foreignHit}/${c.foreignHave} 項）</span>`;
  const P = v => v === undefined || v === null;
  const rd = reading && (c.reading || []).length ? `<div class="reading"><div class="k">目前情境（現況、一般解讀、過去資料；非預測）</div><ul>${c.reading.map(t => typeof t === "string" ? `<li>${esc(t)}</li>` :
    `<li><div class="rt">${esc(t.text)}</div>${t.concept ? `<div class="rc"><b>一般解讀</b>　${esc(t.concept)}</div>` : ""}${t.evidence ? `<div class="re"><b>過去資料</b>　${esc(t.evidence.replace(/^過去資料/, ""))}</div>` : ""}</li>`).join("")}</ul></div>` : "";
  return `<div class="market climate">
    ${kv("attitude", "外資態度（近 5 日）", b)}
    ${kv("spot5", "外資現貨 5 日（億）", `<span class="num ${cls(c.foreignSpot5d)}">${fmtS(c.foreignSpot5d, 0)}</span>`)}
    ${kv("fut", P(c.futForeign) || c.futForeign <= 0 ? "外資台指期淨空單（口）" : "外資台指期淨多單（口）", `<span class="num ${cls(c.futForeign)}">${P(c.futForeign) ? "—" : fmt(Math.abs(c.futForeign), 0)}</span><span class="small num ${cls(c.futForeignChg5)}"> ${P(c.futForeignChg5) || c.futForeignChg5 === 0 ? "" : `5日空單${c.futForeignChg5 > 0 ? "減少" : "增加"} ${fmt(Math.abs(c.futForeignChg5), 0)}`}</span>`)}
    ${kv("fx", "美元兌新台幣", `<span class="num">${fmt(c.usdTwd, 3)}</span><span class="small num ${cls(-(c.usdTwdChg5Pct || 0))}"> ${P(c.usdTwdChg5Pct) ? "" : (c.usdTwdChg5Pct > 0 ? "5日貶 " : "5日升 ") + Math.abs(c.usdTwdChg5Pct).toFixed(2) + "%"}</span>`)}
    ${kv("breadth", "市場廣度（月線／200 日線）", `<span class="num">${P(c.breadth20) ? "—" : c.breadth20.toFixed(0) + "%"}</span><span class="small num muted">${P(c.breadth200) ? "" : "／" + c.breadth200.toFixed(0) + "%"}</span>`)}</div>${rd}`;
}
const ck = (label, st, why) => `<span class="ck ${st}" title="${esc(why || "")}">${{pass: "✓", fail: "✕", na: "?"}[st] || "?"} ${label}</span>`;
/* 注意股、處置股標示 */
const mmdd = d => d ? `${+d.slice(4, 6)}/${+d.slice(6, 8)}` : "";
function alertBadge(a) {
  if (!a) return "";
  const out = [];
  if (a.disposal) { const x = a.disposal;
    out.push(`<span class="badge b-stop" title="${esc((x.reason || "") + (x.measure ? "；" + x.measure : ""))}">處置中${x.end ? " 至 " + mmdd(x.end) : ""}${x.measure ? "・" + esc(x.measure.replace("撮合", "")) : ""}</span>`); }
  if (a.notice || a.notices10) { const why = a.notice ? a.notice.reason : "";
    out.push(`<span class="badge b-wait" title="${esc(why)}">注意${a.notices10 ? `（近10日 ${a.notices10} 次）` : ""}</span>`); }
  return out.join(" ");
}
function cardHTML(s) {
  const r = s.data, g = s.signal;
  return `<article class="card ${g.light}" data-code="${esc(s.code)}" tabindex="0">
   <div class="row between"><div><b>${esc(s.code)} ${esc(s.name)}</b><div class="small muted">${s.market === "TPEx" ? "上櫃" : "上市"} ${chips(s.labels)} ${alertBadge(s.alert)}</div></div>${badge(g.light, g.lightLabel)}</div>
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
   ${g.hold ? `<div class="need row" style="gap:6px;align-items:baseline"><b>持有：</b><button type="button" class="linkish" data-hold="${esc(s.code)}" title="調整持股">${badge(g.hold.light)}</button><span>${esc(holdLine(g.hold))}</span><button type="button" class="linkish small no-print" data-hold="${esc(s.code)}">調整持股</button></div>` : ""}
   ${g.keyLevelNote ? `<div class="note">${esc(g.keyLevelNote)}</div>` : ""}${s.note ? `<div class="note">${esc(s.note)}</div>` : ""}
   ${g.missing.length ? `<div class="note">未取得：${esc(g.missing.join("、"))}</div>` : ""}</article>`;
}
function tableHTML(stocks) {
  const ord = {go: 0, wait: 1, stop: 2, na: 3}, hord = {exit: 0, reduce: 1, keep: 2, na: 3};
  return `<div class="tablewrap"><table class="sortable compact" data-sort="main"><thead><tr><th class="l">代號</th><th class="l">名稱</th><th class="opt4">收盤</th><th>漲跌%</th><th class="opt2">外資</th><th class="opt2">投信</th><th class="opt1">融資<br>增減</th><th class="opt1">量比</th><th class="opt3">月線<br>位置</th><th class="l">燈號</th><th class="l">持有</th><th data-nosort class="no-print"></th></tr></thead><tbody>
  ${stocks.map(s => { const r = s.data, g = s.signal; const pos = g.belowMa20 === null || g.belowMa20 === undefined ? "—" : g.belowMa20 ? "月線下" : g.maDistPct < 0 ? "貼近月線" : "月線上";
    return `<tr><td class="l num">${esc(s.code)}</td><td class="l wrapc">${esc(s.name)}${s.alert ? " " + alertBadge(s.alert) : ""}</td><td class="num opt4">${fmt(r.close, pd(r.close))}</td><td class="num ${cls(r.chgPct)}">${fmtP(r.chgPct)}</td><td class="num opt2 ${cls(r.foreign)}">${fmtS(r.foreign)}</td><td class="num opt2 ${cls(r.trust)}">${fmtS(r.trust)}</td><td class="num opt1">${fmtS(r.marginChg)}</td><td class="num opt1">${isNum(g.volRatio) ? g.volRatio.toFixed(2) + "x" : "—"}</td><td data-v="${isNum(g.maDistPct) ? g.maDistPct : ""}" class="wrapc opt3">${pos}<br><span class="muted num small">${isNum(g.maDistPct) ? fmtS(g.maDistPct, 1) + "%" : ""}</span></td><td class="l" data-v="${ord[g.light] ?? 9}">${badge(g.light, g.lightLabel)}</td>
      <td class="l wrapc" data-v="${g.hold ? hord[g.hold.light] ?? 9 : 9}">${g.hold ? `${badge(g.hold.light)} <span class="small muted num holdtxt">${s.shares ? fmt(s.shares) + " 股" : ""}${isNum(g.hold.profitPct) ? `・<span class="${cls(g.hold.profitPct)}">${fmtS(g.hold.profitPct, 1)}%</span>` : ""}</span>` : `<span class="muted">—</span>`}</td>
      <td class="no-print"><button type="button" class="small" data-hold="${esc(s.code)}" title="${s.holding ? "增減持股" : "買進"}">${s.holding ? `<span class="long">增減持股</span><span class="short">增減</span>` : "買進"}</button></td></tr>`; }).join("")}
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

/* ---------- 頁面：資金流向 ---------- */
let FLOW_DAYS = 60;
let FW = 1200;   // 依畫面寬度決定，讓圖上的字維持原本大小
function syncChart(dates, vals, {h = 130, bar = false, title = "", unit = "", ref = null, digits = 0, delta = false, goodUp = true, sub = "", extra = []} = {}) {
  const W = FW, L = 64, R = 12, T = 18, B = 6, n = dates.length, band = delta ? Math.round((h - T - B) * .34) : 0, ch = h - T - B - band - (delta ? 8 : 0);
  const xs = i => L + (n < 2 ? 0 : i * (W - L - R) / (n - 1));
  const nums = [...vals, ...extra.flatMap(e => e.values)].filter(isNum); let lo = Math.min(...nums, ...(bar || ref !== null ? [ref ?? 0] : [])), hi = Math.max(...nums, ...(bar || ref !== null ? [ref ?? 0] : []));
  let g = `<text x="0" y="12" style="fill:var(--fg);font-weight:700">${esc(title)}</text>${sub ? `<text x="${W - R}" y="12" text-anchor="end" style="fill:var(--fg)">${esc(sub)}</text>` : ""}`;
  if (!nums.length) return `<svg class="chart sync" viewBox="0 0 ${W} ${h}" data-l="${L}" data-r="${R}" data-n="${n}">${g}<text x="${W / 2}" y="${h / 2}" text-anchor="middle">沒有資料</text><line class="xh" y1="${T}" y2="${h - B}" stroke="var(--muted)" stroke-dasharray="3 3" visibility="hidden"/></svg>`;
  const pad = (hi - lo) * .08 || 1; lo -= pad; hi += pad;
  const y = v => T + (hi - v) / (hi - lo) * ch;
  g += niceTicks(lo, hi, digits ? 4 : 3).map(v => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--line)"/><text x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${fmt(v, Math.abs(v) < 100 && digits ? digits : 0)}</text>`).join("");
  if (ref !== null) g += `<line x1="${L}" x2="${W - R}" y1="${y(ref)}" y2="${y(ref)}" stroke="var(--muted)" stroke-dasharray="4 3"/>`;
  if (bar) {
    const bw = Math.max(1, (W - L - R) / Math.max(n, 1) * .7);
    g += vals.map((v, i) => isNum(v) ? `<rect x="${Math.max(L, xs(i) - bw / 2)}" y="${Math.min(y(v), y(0))}" width="${i === 0 ? bw / 2 : i === n - 1 ? bw / 2 : bw}" height="${Math.max(1, Math.abs(y(v) - y(0)))}" fill="var(${v >= 0 ? "--up" : "--down"})"/>` : "").join("");
  } else {
    let p = "", on = false; vals.forEach((v, i) => { if (isNum(v)) { p += (on ? "L" : "M") + xs(i).toFixed(1) + "," + y(v).toFixed(1); on = true; } else on = false; });
    g += `<path d="${p}" fill="none" stroke="var(--accent)" stroke-width="1.8"/>`;
    for (const e of extra) {
      let p2 = "", on2 = false; e.values.forEach((v, i) => { if (isNum(v)) { p2 += (on2 ? "L" : "M") + xs(i).toFixed(1) + "," + y(v).toFixed(1); on2 = true; } else on2 = false; });
      g += `<path d="${p2}" fill="none" stroke="${e.color}" stroke-width="1.8" stroke-dasharray="${e.dash || ""}"/>`;
    }
  }
  if (delta) {   // 下方：每日變化（顏色：偏多＝紅、偏空＝綠）
    const dv = vals.map((v, i) => i > 0 && isNum(v) && isNum(vals[i - 1]) ? v - vals[i - 1] : null), m = Math.max(...dv.filter(isNum).map(Math.abs), 1e-9);
    const top = T + ch + 8, mid = top + band / 2, bw = Math.max(1, (W - L - R) / Math.max(n, 1) * .7);
    g += `<line x1="${L}" x2="${W - R}" y1="${mid}" y2="${mid}" stroke="var(--line)"/><text x="${L - 6}" y="${mid + 4}" text-anchor="end">每日</text>`;
    g += dv.map((v, i) => isNum(v) && v !== 0 ? `<rect x="${Math.max(L, xs(i) - bw / 2)}" y="${v > 0 ? mid - v / m * band / 2 : mid}" width="${bw}" height="${Math.max(1, Math.abs(v) / m * band / 2)}" fill="var(${(v > 0) === goodUp ? "--up" : "--down"})" opacity=".8"/>` : "").join("");
  }
  g += `<line class="xh" y1="${T}" y2="${h - B}" stroke="var(--muted)" stroke-dasharray="3 3" visibility="hidden"/>`;
  return `<svg class="chart sync" viewBox="0 0 ${W} ${h}" data-l="${L}" data-r="${R}" data-n="${n}" role="img" aria-label="${esc(title)}">${g}</svg>`;
}
function axisRow(dates) {
  const W = FW, L = 64, R = 12, n = dates.length, step = Math.max(1, Math.ceil(n / 7));
  const xs = i => L + (n < 2 ? 0 : i * (W - L - R) / (n - 1));
  return `<svg class="chart" viewBox="0 0 ${W} 18">${dates.map((d, i) => i % step === 0 ? `<text x="${xs(i)}" y="13" text-anchor="middle">${d.slice(2, 4)}/${d.slice(4, 6)}/${d.slice(6)}</text>` : "").join("")}</svg>`;
}
function bubbleSVG(items, center = "台股現貨") {
  const W = FW, maxR = Math.min(90, FW * .11), cx = FW / 2, xl = FW * .25, xr = FW * .75, mx = Math.max(...items.map(i => Math.abs(i.v)), 1);
  const r = v => Math.max(16, maxR * Math.sqrt(Math.abs(v) / mx));
  const cols = {sell: items.filter(i => i.v < 0).sort((a, b) => a.v - b.v), buy: items.filter(i => i.v >= 0).sort((a, b) => b.v - a.v)};
  const place = (arr, x) => { let yy = 14; return arr.map(it => { const rr = r(it.v); const o = {...it, x, y: yy + rr, r: rr}; yy += rr * 2 + 26; return o; }); };
  const P = [...place(cols.sell, xl), ...place(cols.buy, xr)];
  const H = Math.max(200, ...P.map(p => p.y + p.r + 22)), cy = H / 2;
  let g = `<rect x="${cx - 50}" y="${cy - 22}" width="100" height="44" rx="8" fill="var(--surface2)" stroke="var(--line)"/><text x="${cx}" y="${cy + 5}" text-anchor="middle" style="fill:var(--fg);font-weight:700">${esc(center)}</text>`;
  for (const p of P) {
    const buy = p.v >= 0, col = buy ? "var(--up)" : "var(--down)";
    const [x1, y1, x2, y2] = buy ? [cx + 50, cy, p.x - p.r - 4, p.y] : [p.x + p.r + 4, p.y, cx - 50, cy];
    if (!p.side) g += `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${col}" stroke-width="${1 + 4 * Math.abs(p.v) / mx}" opacity=".45"/>`;
    g += `<circle cx="${p.x}" cy="${p.y}" r="${p.r}" fill="${col}" fill-opacity="${p.side ? .06 : .16}" stroke="${col}" ${p.side ? 'stroke-dasharray="5 4"' : ""}/>`;
    const out = p.r < 48 && FW > 700, tx = out ? (buy ? p.x + p.r + 8 : p.x - p.r - 8) : p.x, an = out ? (buy ? "start" : "end") : "middle";
    g += `<text x="${tx}" y="${p.y - 2}" text-anchor="${an}" style="fill:var(--fg);font-weight:700;font-size:12px">${esc(p.label)}</text>`;
    g += `<text x="${tx}" y="${p.y + 14}" text-anchor="${an}" style="fill:${col};font-size:12px">${p.side ? (buy ? "增加" : "減少") : buy ? "買超" : "賣超"} ${fmt(Math.abs(p.v), 0)} 億</text>`;
  }
  g += `<text x="${xl}" y="${H - 4}" text-anchor="middle">賣方</text><text x="${xr}" y="${H - 4}" text-anchor="middle">買方</text>`;
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="近期資金流向氣泡圖">${g}</svg>`;
}
/* 圖右上角：最新值與 5 日、期間變化 */
function chgTxt(arr, f, upWord, downWord, pct = false, plain = false) {
  const v = (arr || []).map((x, i) => [x, i]).filter(([x]) => isNum(x)); if (v.length < 2) return "";
  const last = v[v.length - 1][0], p5 = v[Math.max(0, v.length - 6)][0], first = v[0][0];
  const c = (a, b) => { const d = b - a; return `${d >= 0 ? upWord : downWord} ${pct ? Math.abs(d / a * 100).toFixed(2) + "%" : f(Math.abs(d)).replace(/^[+−-]/, "")}`; };
  return `最新 ${pct ? fmt(last, 3) : plain ? fmt(last, 0) : fmtS(last, 0)}　5 日 ${c(p5, last)}　期間 ${c(first, last)}`;
}
/* 資金流向的圖（資金流向頁與報表共用） */
function flowCharts(f) {
  const S = f.series || {}, D = f.dates || [];
  const SHORT = (S.futForeign || []).map(v => isNum(v) ? -v : null);
  return `${syncChart(D, S.taiex || [], {title: "加權指數"})}
      ${syncChart(D, S.foreign || [], {title: "外資現貨買賣超（億）", bar: true})}
      ${syncChart(D, SHORT, {title: "外資台指期淨空單（口，越高越看空；負數＝淨多單）", h: 170, delta: true, goodUp: false, sub: chgTxt(SHORT, v => fmt(v, 0) + " 口", "增加", "減少", false, true) + "　長條：紅＝空單減少　綠＝空單增加"})}
      ${syncChart(D, S.usdTwd || [], {title: "美元兌新台幣（往上＝新台幣貶）", h: 170, digits: 2, delta: true, goodUp: false, sub: chgTxt(S.usdTwd, v => fmtS(v, 3), "新台幣貶", "新台幣升", true)})}
      ${syncChart(D, S.breadth || [], {title: "市場廣度 %：實線＝站上月線（短期）、虛線＝站上 200 日線（長期）", ref: 50, extra: (S.breadth200 || []).some(isNum) ? [{values: S.breadth200, color: "var(--c-ma60)", dash: "6 4"}] : []})}
      ${axisRow(D)}`;
}
function flowBubble(f, days) {
  const S = f.series || {}, D = f.dates || [], FLOW_DAYS = days;
  const sum = k => (S[k] || []).reduce((a, v) => a + (isNum(v) ? v : 0), 0);
  const fo = sum("foreign"), tr = sum("trust"), de = sum("dealer"), mg = sum("margin");
  const items = [{label: "外資", v: fo}, {label: "投信", v: tr}, {label: "自營商", v: de}, {label: "其他本土（差額，身分不明）", v: -(fo + tr + de)}];
  if ((S.margin || []).some(isNum)) items.push({label: "融資增減", v: mg, side: true});
  const fut = (S.futForeign || []).filter(isNum), fut0 = fut[0], fut1 = fut[fut.length - 1];
  return `${D.length ? bubbleSVG(items) : `<div class="empty">沒有資料</div>`}
      <p class="small muted">圓面積與金額成正比。「其他本土」＝三大法人合計的反向，是推算出來的，證交所沒有公布這些人是誰；
      推測可能包括公股行庫與國安基金（國家隊）、壽險、企業與大股東，以及用現股買賣的散戶。虛線圓「融資增減」是用融資買股的散戶（槓桿），減少代表散戶槓桿在退場，不計入買賣平衡。
      ${isNum(fut1) ? `外資台指期${fut1 <= 0 ? `淨空單 ${fmt(-fut1, 0)}` : `淨多單 ${fmt(fut1, 0)}`} 口` : "外資台指期：沒有資料"}${isNum(fut0) && isNum(fut1) && fut1 !== fut0 ? `（這段期間空單${fut1 < fut0 ? "增加" : "減少"} ${fmt(Math.abs(fut1 - fut0), 0)} 口）` : ""}，屬於押注方向的部位，不是現貨資金，所以不畫成氣泡。</p>`;
}
async function viewFlows() {
  FW = Math.max(340, Math.min(1200, ($("#view").clientWidth || 1200) - 34));
  const f = await api("/flows?days=" + FLOW_DAYS), S = f.series || {}, D = f.dates || [];
  const sum = k => (S[k] || []).reduce((a, v) => a + (isNum(v) ? v : 0), 0);
  const SHORT = (S.futForeign || []).map(v => isNum(v) ? -v : null);
  const fo = sum("foreign"), tr = sum("trust"), de = sum("dealer"), mg = sum("margin");
  const items = [{label: "外資", v: fo}, {label: "投信", v: tr}, {label: "自營商", v: de}, {label: "其他本土（差額，身分不明）", v: -(fo + tr + de)}];
  if ((S.margin || []).some(isNum)) items.push({label: "融資增減", v: mg, side: true});
  const fut = (S.futForeign || []).filter(isNum), fut0 = fut[0], fut1 = fut[fut.length - 1];
  const ymd = d => d ? `${d.slice(0, 4)}/${d.slice(4, 6)}/${d.slice(6)}` : "—";
  $("#view").innerHTML = `
    <div class="head"><h1>資金流向｜資料日 ${ymd(f.date)}</h1>
      <div class="row">${[20, 60, 120, 250].map(n => `<button type="button" class="${n === FLOW_DAYS ? "primary" : ""}" data-days="${n}">近 ${n} 日</button>`).join("")}</div></div>
    <div class="panel flowclim">${climateHTML(f.climate) || `<div class="empty">沒有外資態度資料</div>`}</div>
    <div class="panel"><h2>趨勢（共用時間軸，滑鼠移到圖上看同一天的數值）</h2>
      <div id="xread" class="small muted">　</div>
      ${flowCharts(f)}</div>
    <div class="panel"><h2>錢流到哪裡（近 ${FLOW_DAYS} 日累計）</h2>
      ${flowBubble(f, FLOW_DAYS)}</div>`;
  document.querySelectorAll("[data-days]").forEach(b => b.onclick = () => { FLOW_DAYS = +b.dataset.days; viewFlows(); });
  const svgs = [...document.querySelectorAll("svg.sync")], read = $("#xread");
  const show = i => {
    if (i < 0) { svgs.forEach(s => s.querySelector(".xh").setAttribute("visibility", "hidden")); read.textContent = "　"; return; }
    svgs.forEach(s => { const L = +s.dataset.l, R = +s.dataset.r, n = +s.dataset.n, x = L + (n < 2 ? 0 : i * (FW - L - R) / (n - 1));
      const l = s.querySelector(".xh"); l.setAttribute("x1", x); l.setAttribute("x2", x); l.setAttribute("visibility", "visible"); });
    const v = (k, dg = 0) => isNum((S[k] || [])[i]) ? fmt(S[k][i], dg) : "—";
    read.innerHTML = `<b>${ymd(D[i])}</b>　加權 ${v("taiex", 0)}　外資現貨 ${isNum(S.foreign[i]) ? fmtS(S.foreign[i], 0) : "—"} 億　台指期淨空單 ${isNum(SHORT[i]) ? fmt(SHORT[i], 0) : "—"} 口　美元 ${v("usdTwd", 3)}　廣度 ${v("breadth", 0)}%／長期 ${v("breadth200", 0)}%`;
  };
  svgs.forEach(s => {
    s.addEventListener("pointermove", ev => { const b = s.getBoundingClientRect(), L = +s.dataset.l, R = +s.dataset.r, n = +s.dataset.n;
      const x = (ev.clientX - b.left) * FW / b.width; show(Math.max(0, Math.min(n - 1, Math.round((x - L) / (FW - L - R) * (n - 1))))); });
    s.addEventListener("pointerleave", () => show(-1));
  });
  if (D.length) show(D.length - 1);
}

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
  const r60 = rows.slice(-60), L60 = r60.map(r => r.date), g = h.signal;
  const back = (document.referrer || "").includes("#/scan") || sessionStorage.getItem("twradar-from") === "scan" ? "#/scan" : "#/";
  $("#view").innerHTML = `
   <div class="row between"><div><h1>${esc(code)} ${esc(h.name || "")}</h1><div class="small muted">${h.market ? mktName(h.market) : ""} ${alertBadge(h.alert)}</div></div>
     <div class="row"><a class="btn" href="${back}">← ${back === "#/scan" ? "全市場" : "總覽"}</a>${h.inWatchlist ? "" : `<button class="primary" id="addThis">加入觀察</button>`}<a class="btn" href="${withTok(`/api/v1/stocks/${code}/history?days=250&format=csv`)}">下載歷史 CSV</a></div></div>
   ${g ? `<section class="panel" style="display:flex;flex-direction:column;gap:6px"><div class="row">${badge(g.light, g.lightLabel)}${g.hold ? `<button type="button" class="linkish" data-hold="${esc(code)}" title="調整持股">${badge(g.hold.light)}</button>` : ""}<span class="small muted">雷達判定（資料日 ${esc(h.date || "")}${h.inWatchlist ? "" : "，不在觀察清單，以未持有、全域預設規則判定"}${g.rule ? "・" + esc(g.rule) : ""}）</span></div>
     ${g.needs && g.needs.length ? `<div class="small"><b>還差：</b>${esc(g.needs.join("；"))}</div>` : ""}${g.warnings && g.warnings.length ? `<div class="small"><b>原因：</b>${esc(g.warnings.join("；"))}</div>` : ""}
     ${g.missing && g.missing.length ? `<div class="small muted">未取得：${esc(g.missing.join("、"))}${h.inWatchlist ? "" : "（集保大戶、散戶資料只抓觀察清單的股票）"}</div>` : ""}</section>` : ""}
   <section class="panel"><div class="row between"><h2>收盤價與均線（近 ${rows.length} 日）</h2><div class="legend"><span><i style="background:var(--fg)"></i>收盤</span><span><i style="background:var(--c-ma20)"></i>月線 MA20</span><span><i style="background:var(--c-ma60)"></i>季線 MA60</span></div></div>
    ${lineChart(L, [{values: rows.map(r => r.close), color: "var(--fg)", width: 2, dot: true}, {values: rows.map(r => r.ma20), color: "var(--c-ma20)"}, {values: rows.map(r => r.ma60), color: "var(--c-ma60)"}], {h: 300, bars: rows.map(r => r.volume)})}</section>
   <section class="panel"><div class="row between"><h2>外資／投信買賣超（張，近 60 日）</h2><div class="legend"><span><i style="background:var(--c-foreign)"></i>外資</span><span><i style="background:var(--c-trust)"></i>投信</span></div></div>
    ${divBars(L60, [{values: r60.map(r => r.foreign), color: "var(--c-foreign)"}, {values: r60.map(r => r.trust), color: "var(--c-trust)"}])}</section>
   <section class="panel"><h2>融資餘額（張，近 60 日）</h2>${lineChart(L60, [{values: r60.map(r => r.marginBal), color: "var(--accent)", dot: true}], {h: 180})}</section>
   <h2>近 20 日明細</h2><div class="tablewrap"><table><thead><tr><th class="l">日期</th><th>收盤</th><th>漲跌</th><th>成交量</th><th>月線</th><th>季線</th><th>外資</th><th>投信</th><th>自營</th><th>融資增減</th><th>融資餘額</th></tr></thead><tbody>
   ${rows.slice(-20).reverse().map(r => `<tr><td class="l num">${r.date}</td><td class="num">${fmt(r.close, pd(r.close))}</td><td class="num ${cls(r.change)}">${fmtS(r.change, pd(r.close))}</td><td class="num">${fmt(r.volume)}</td><td class="num">${fmt(r.ma20, 2)}</td><td class="num">${fmt(r.ma60, 2)}</td><td class="num ${cls(r.foreign)}">${fmtS(r.foreign)}</td><td class="num ${cls(r.trust)}">${fmtS(r.trust)}</td><td class="num">${fmtS(r.dealer)}</td><td class="num">${fmtS(r.marginChg)}</td><td class="num">${fmt(r.marginBal)}</td></tr>`).join("")}
   </tbody></table></div>`;
  const add = $("#addThis");
  if (add) add.onclick = () => addStockDialog({code, name: h.name, market: h.market}, () => viewStock(code));
}

/* ---------- 頁面：報表 ---------- */
function holdSec(st) {
  const list = st.filter(s => s.signal.hold).sort((a, b) => ["exit", "reduce", "keep", "na"].indexOf(a.signal.hold.light) - ["exit", "reduce", "keep", "na"].indexOf(b.signal.hold.light));
  if (!list.length) return "";
  return `<div class="sec"><h2>持有部位（${list.length}）</h2>${list.map(s => { const r = s.data, h = s.signal.hold;
    return `<p style="margin:8px 0"><button type="button" class="linkish" data-hold="${esc(s.code)}" title="調整持股">${badge(h.light)}</button> <b>${esc(s.code)} ${esc(s.name)}</b> <button type="button" class="linkish small no-print" data-hold="${esc(s.code)}">調整持股</button>　收 ${fmt(r.close, pd(r.close))}（<span class="${cls(r.chgPct)}">${fmtP(r.chgPct)}</span>）<br><span class="muted">${esc(holdLine(h) || "—")}</span></p>`; }).join("")}</div>`;
}
const REPORT_DAYS = 60;
/* 離線報表：把目前的報表連同樣式存成一個 HTML 檔，沒有網路也能打開 */
async function downloadOffline(d) {
  const css = await fetch("app.css", {cache: "no-store"}).then(r => r.text()).catch(() => "");
  const art = $("article.report").cloneNode(true);
  art.querySelectorAll(".no-print,.noprint,button").forEach(x => x.remove());
  const html = `<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>右側加碼晨報 ${d}</title><style>${css}\n.no-print,.noprint{display:none!important}body{padding:16px}</style></head>
<body><main class="wrap">${art.outerHTML}</main><footer class="wrap foot">離線版：由右側加碼雷達產生；滑鼠移到大盤數字上可看說明。以上為籌碼訊號整理，非投資建議。</footer></body></html>`;
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([html], {type: "text/html"}));
  a.download = `twradar-report-${d.replace(/-/g, "")}.html`;
  document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}
async function viewReport(date) {
  await loadDates();
  const e = date ? await api("/days/" + date) : await api("/latest");
  const d = e.date; if (!d) { $("#view").innerHTML = `<div class="panel empty">尚無資料</div>`; return; }
  const st = e.stocks, m = e.market;
  FW = 1000;   // 報表的圖用固定寬度，列印與離線檔案都一致
  const fl = await api(`/flows?days=${REPORT_DAYS}&date=${d.replace(/-/g, "")}`).catch(() => null);
  const withMA = st.filter(s => isNum(s.signal.maDistPct)).map(s => ({label: s.code + " " + s.name, v: s.signal.maDistPct, light: s.signal.light}));
  const sec = (k, title) => { const list = st.filter(s => s.signal.light === k); if (!list.length) return "";
    return `<div class="sec"><h2 style="color:var(--${k === "na" ? "na" : k})">${title}（${list.length}）</h2>${list.map(s => { const r = s.data, g = s.signal;
      const line = k === "go" ? (g.lightLabel || "") + "：" + [g.instWhy, g.checks.retail === "pass" ? "融資健康" : "", g.checks.main === "pass" ? "主力集中" : "", "價量確認"].filter(Boolean).join("、") + `；觀察價位 月線 ${fmt(r.ma20, 2)}${isNum(r.prevHigh) ? "、前高 " + fmt(r.prevHigh, pd(r.prevHigh)) : ""}`
        : k === "wait" ? (g.rule && g.rule !== "觀察" ? g.rule + "；" : "") + "還差：" + (g.needs.join("；") || "—") : k === "stop" ? "原因：" + g.warnings.join("；") : "未取得：" + g.missing.join("、");
      return `<p style="margin:8px 0"><b>${esc(s.code)} ${esc(s.name)}</b>　收 ${fmt(r.close, pd(r.close))}（<span class="${cls(r.chgPct)}">${fmtP(r.chgPct)}</span>）　外資 ${fmtS(r.foreign)}　投信 ${fmtS(r.trust)}　融資 ${fmtS(r.marginChg)}<br><span class="muted">${esc(line)}${g.keyLevelNote ? "；" + esc(g.keyLevelNote) : ""}</span></p>`; }).join("")}</div>`; };
  $("#view").innerHTML = `
   <div class="row between noprint"><div class="row"><select id="dsel" aria-label="資料日">${dateOptions(d)}</select>${e.ready ? "" : badge(e.status, e.statusText)}</div>
     <div class="row"><button id="offBtn" title="存成一個 HTML 檔，傳給別人用瀏覽器打開，不需要網路">下載離線報表</button><button class="primary" id="printBtn">列印／存成 PDF</button></div></div>
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
    ${fl && (fl.dates || []).length ? `<div class="sec"><h2>大盤環境與資金流向（近 ${REPORT_DAYS} 個交易日）</h2>
      <div class="small muted">五張圖共用同一條時間軸；台指期與匯率下方是每日變化。</div>${flowCharts(fl)}</div>
      <div class="sec"><h2>錢流到哪裡（近 ${REPORT_DAYS} 個交易日累計）</h2>${flowBubble(fl, REPORT_DAYS)}</div>` : ""}
    ${holdSec(st)}${sec("go", "上車／加碼訊號")}${sec("wait", "觀察中")}${sec("stop", "轉弱／不宜加碼")}${sec("na", "資料不足")}
    <div class="sec"><h2>精簡表</h2>${tableHTML(st)}</div>
    ${e.model ? `<div class="small ${e.model.status === "偏離" ? "" : "muted"}" style="margin:8px 0">${e.model.status === "偏離" ? "⚠ " : ""}模型追蹤：${esc(e.model.status)}。${esc(e.model.text)}</div>` : ""}
    <div class="sec glossary"><h2>名詞說明</h2><dl>${[["外資買賣超", "foreign"], ["投信買賣超", "trust"], ["自營商買賣超", "dealer"], ["外資態度", "attitude"],
      ["外資現貨 5 日", "spot5"], ["外資台指期淨空單", "fut"], ["美元兌新台幣", "fx"], ["市場廣度（短期）", "breadth"], ["長期廣度", "breadth200"]].map(([k, t]) => `<dt>${k}</dt><dd>${esc(TIPS[t])}</dd>`).join("")}
      <dt>其他本土（差額）</dt><dd>三大法人合計的反向，是推算出來的；證交所沒有公布是誰，推測包括公股行庫與國安基金、壽險、企業與大股東，以及用現股買賣的散戶。</dd>
      <dt>燈號</dt><dd>綠＝上車／加碼訊號，黃＝觀察中（還差哪些條件寫在下方），紅＝轉弱／不宜加碼。</dd></dl></div>
    <div class="small muted">資料來源：證交所、櫃買中心、期貨交易所${st.some(s => (s.data.source || "").includes("FinMind")) ? "、FinMind" : ""}。以上為籌碼訊號整理，非投資建議。</div>
   </article>`;
  $("#dsel").addEventListener("change", ev => location.hash = "#/report/" + ev.target.value);
  $("#printBtn").addEventListener("click", () => window.print());
  $("#offBtn").addEventListener("click", () => downloadOffline(d).catch(err => toast("下載失敗：" + err.message)));
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
GET /api/v1/scan   全市場強勢注意清單（各排序方法與回溯驗證）
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
      sug.innerHTML = hits.length ? hits.map(h => `<button type="button" data-code="${esc(h.code)}" data-name="${esc(h.name)}" data-market="${esc(h.market)}"${h.inWatchlist ? " disabled" : ""}><b class="num">${esc(h.code)}</b> ${esc(h.name)} <span class="small muted">${mktName(h.market)}${h.inWatchlist ? "・已在清單" : ""}</span></button>`).join("")
        : `<div class="small muted" style="padding:8px 10px">找不到「${esc(v)}」。資料庫裡沒有這檔的行情時，按 Enter 也無法加入。</div>`;
      sug.hidden = false; }, 180); });
  q.addEventListener("keydown", ev => { if (ev.key === "Enter") { ev.preventDefault(); const first = sug.querySelector("button:not([disabled])"); if (!sug.hidden && first && sug.querySelectorAll("button").length === 1) addStock({code: first.dataset.code}); else if (q.value.trim()) addStock({q: q.value.trim()}); } else if (ev.key === "Escape") sug.hidden = true; });
  sug.addEventListener("click", ev => { const b = ev.target.closest("button[data-code]"); if (!b) return;
    sug.hidden = true; addStockDialog({code: b.dataset.code, name: b.dataset.name, market: b.dataset.market}, reload); });
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
/* 加入觀察：和設定頁的編輯視窗相同，可直接填標籤、持有、成本等 */
async function addStockDialog(base, onDone) {
  const labels = await loadLabels();
  editStock({code: base.code, name: base.name || "", market: base.market || "TWSE", labels: [], note: "", keyLevel: null, holding: false,
    cost: null, shares: null, buyDate: null, isNew: true}, labels, null, onDone || (() => {}));
}
function editStock(w, labels, rules, reload) {
  let cur = [...w.labels];
  sheet(`<div class="row between"><h2><span class="num">${esc(w.code)}</span> ${esc(w.name)}</h2><span class="small muted">${mktName(w.market)}</span></div>
    ${w.isNew ? `<div class="small muted">加入觀察清單：以下都可以之後再改；標籤裡貼「策略::名稱」會套用那個策略的規則。</div>` : ""}
    <div class="tabs2" role="tablist"><button type="button" role="tab" data-tab="base" class="on">基本</button><button type="button" role="tab" data-tab="rules">規則</button><button type="button" role="tab" data-tab="trial">試算</button></div>
    <form id="pane-base" style="display:flex;flex-direction:column;gap:10px">
      <div class="field"><label>標籤</label><div id="eLabels" class="row"></div>
        <input id="eLabelIn" list="eLabelList" placeholder="輸入後按 Enter；策略::名稱 會套用那個策略的規則" maxlength="30"><datalist id="eLabelList">${labels.map(l => `<option value="${esc(l.name)}">`).join("")}</datalist></div>
      <div class="field"><label for="e-note">觀察重點</label><input id="e-note" value="${esc(w.note || "")}"></div>
      <div class="grid2"><div class="field"><label for="e-key">關鍵價（選填）</label><input id="e-key" inputmode="decimal" value="${w.keyLevel ?? ""}"></div>
       <div class="field"><label for="e-hold">持有中</label><select id="e-hold"><option value="0">否</option><option value="1"${w.holding ? " selected" : ""}>是</option></select></div>
       <div class="field"><label for="e-cost">成本價</label><input id="e-cost" inputmode="decimal" value="${w.cost ?? ""}"></div>
       <div class="field"><label for="e-shares">股數</label><input id="e-shares" inputmode="numeric" value="${w.shares ?? ""}"></div>
       <div class="field"><label for="e-buy">買進日期（保本線、除權息調整成本用）</label><input id="e-buy" type="date" value="${w.buyDate ? esc(w.buyDate.replace(/^(\d{4})-?(\d{2})-?(\d{2})$/, "$1-$2-$3")) : ""}"></div>
       <div class="field"><label for="e-name">名稱</label><input id="e-name" value="${esc(w.name)}"></div></div>
      <div class="small muted">持有中要填成本，停損與出場的提示才會啟動；填了買進日期，之後的除權息、減資、分割會自動調整成本。</div>
      <div class="row" style="justify-content:flex-end"><button type="button" data-close>取消</button><button class="primary">${w.isNew ? "加入觀察" : "儲存"}</button></div></form>
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
        try { await api("/watchlist", {method: "POST", body: JSON.stringify(b)}); await api("/analyze", {method: "POST", body: "{}"}).catch(() => {}); $("#layer").innerHTML = ""; toast((w.isNew ? "已加入 " : "已儲存 ") + w.code + (w.isNew ? "，歷史資料會在背景補齊" : "")); reload(); } catch (err) { toast(err.message); } });
      const dirty = () => w.isNew ? `<div class="small muted">還沒加入觀察清單：以目前選的標籤試算，持有、成本等要加入後才會納入。</div>`
        : JSON.stringify(cur) !== JSON.stringify(w.labels) ? `<div class="small muted">以目前選的標籤（還沒儲存）計算。</div>` : "";
      const lq = () => "?labels=" + encodeURIComponent(cur.join(","));
      const st = {pass: "✓", fail: "✕", na: "?"};
      const load = {
        rules: async el => { const r = await api(`/watchlist/${w.code}/rules` + lq()); const ov = r.params.filter(p => p.source === "label"), rest = r.params.filter(p => p.source !== "label");
          el.innerHTML = `${dirty()}<div>${r.strategy ? `套用 ${chip(r.strategy)} <span class="small muted">共 ${r.affected} 檔使用</span>` : `套用<b>全域預設</b> <span class="small muted">（沒有貼「策略::」標籤）</span>`}${r.allowAdd ? "" : "　<b>不加碼</b>"}</div>
            <div class="small muted">規則跟著標籤走，個股本身不放規則。要調整請編輯策略標籤（設定頁「標籤」），會影響所有貼了它的股票。</div>
            <div class="tablewrap"><table><thead><tr><th class="l">參數</th><th>套用值</th><th class="l">來源</th></tr></thead><tbody>
            ${[...ov, ...rest].map(p => `<tr><td class="l" style="white-space:normal">${esc(p.label)}</td><td class="num">${p.value}${esc(p.unit || "")}</td><td class="l">${p.source === "label" ? `<b>策略標籤</b> <span class="small muted">預設 ${p.default}</span>` : `<span class="muted">預設</span>`}</td></tr>`).join("")}</tbody></table></div>`; },
        trial: async el => { const t = await api(`/watchlist/${w.code}/trial` + lq()); const h = t.hold;
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

/* ---------- 全市場強勢注意清單 ---------- */
function liveLine(live, m) {
  if (!live || !live.since) return `<b>上線後追蹤：</b>從下一個交易日起每天記錄，滿 10 個交易日後開始顯示實際表現。`;
  const x = ((live.methods || {})[m] || {})["全市場"] || {}, f = x.f10 || {};
  if (!f.days) return `<b>上線後追蹤：</b>自 ${esc(mmdd(live.since))} 起已記錄 ${live.days} 天，還沒有滿 10 個交易日的紀錄。`;
  return `<b>上線後追蹤（真正沒看過的資料，自 ${esc(mmdd(live.since))} 起）：</b>10 日後相對全市場 ${fmtS(f.excess, 2)}%${isNum(f.se) ? `（±${fmt(f.se, 2)}）` : ""}，勝出 ${fmt(f.hit, 0)}% 的日子，共 ${f.days} 天${f.days < 20 ? "；天數還少，僅供參考" : ""}。`;
}
let scanMethod = null;
async function viewScan() {
  $("#view").innerHTML = `<div class="panel empty">掃描全市場中…（每個資料日第一次需要幾秒）</div>`;
  const s = await api("/scan");
  const names = Object.keys(s.methods);
  if (!scanMethod || !names.includes(scanMethod)) scanMethod = s.defaultMethod;
  const draw = () => {
    const m = s.methods[scanMethod], v = (s.validation || {})[scanMethod];
    const vx = seg => v && v[seg] && isNum(v[seg].excess) ? `${fmtS(v[seg].excess, 2)}%（勝出 ${fmt(v[seg].hit, 0)}% 的日子）` : "—";
    const weak = v && isNum(v["樣本外"].excess) && v["樣本外"].excess <= 0;
    const reg = v && v[s.regime] && isNum(v[s.regime].excess) ? v[s.regime].excess : null;
    $("#view").innerHTML = `<section class="panel" style="display:flex;flex-direction:column;gap:10px">
      <div class="row between"><div><h2>全市場強勢注意清單</h2><div class="small muted">資料日 ${esc(s.date)}・大盤${esc(s.regime)}・流動性合格 ${s.liquid} 檔 → 趨勢合格 ${s.trend} 檔 → 取前 ${s.top} 名</div></div>
        <div class="row"><label class="small muted" for="scanM">排序</label><select id="scanM">${names.map(n => `<option${n === scanMethod ? " selected" : ""}>${esc(n)}</option>`).join("")}</select></div></div>
      <div class="banner ${weak || (reg !== null && reg < 0) ? "warn" : ""} small">${v ? `<b>回溯驗證（前 ${s.top} 名 10 日後，相對全市場平均）：</b>樣本內 ${vx("樣本內")}、樣本外 ${vx("樣本外")}；多頭 ${vx("多頭")}、盤整 ${vx("盤整")}、空頭 ${vx("空頭")}。
        ${weak ? "<br><b>這個排序在樣本外沒有勝過全市場，名單僅供對照。</b>" : ""}${!weak && reg !== null && reg < 0 ? `<br><b>這個排序在大盤${esc(s.regime)}時落後全市場。</b>` : ""}` : "還沒有回溯驗證結果（執行 deploy/backtest.sh 後會顯示）。"}
        <br>${liveLine(s.live, scanMethod)}
        <br>買賣一趟的費用與稅約 0.6%，超額報酬扣掉後所剩不多：這是研究名單，不是買進建議。</div>
      ${m.rows.length ? `<div class="tablewrap"><table class="sortable" data-sort="scan-${esc(scanMethod)}"><thead><tr><th>#</th><th class="l">代號</th><th class="l">名稱</th><th class="l">市場</th><th>收盤</th><th>法人 5 日買超<br>佔成交量</th><th>20 日<br>贏大盤</th><th>60 日<br>贏大盤</th><th>5 日線<br>乖離</th><th class="l">當日<br>轉弱</th><th>月線<br>乖離</th><th>日均額<br>（億）</th><th data-nosort></th></tr></thead><tbody>
        ${m.rows.map((r, i) => `<tr><td class="num">${i + 1}</td><td class="l num"><a href="#/stock/${esc(r.code)}" data-from="scan" title="看走勢與籌碼">${esc(r.code)}</a></td><td class="l"><a href="#/stock/${esc(r.code)}" data-from="scan" class="plain">${esc(r.name)}</a>${r.alert ? " " + alertBadge(r.alert) : ""}</td><td class="l">${mktName(r.market)}</td><td class="num">${fmt(r.close, pd(r.close))}</td>
          <td class="num ${cls(r.inst5Pct)}">${isNum(r.inst5Pct) ? fmtS(r.inst5Pct, 1) + "%" : "—"}</td><td class="num ${cls(r.rs20)}">${fmtS(r.rs20, 1)}%</td><td class="num ${cls(r.rs60)}">${fmtS(r.rs60, 1)}%</td>
          <td class="num" data-v="${isNum(r.bias5) ? r.bias5 : ""}">${isNum(r.bias5) ? fmtS(r.bias5, 1) + "%" : "—"}${r.hot ? ` <span class="badge b-wait" title="5 日乖離在這檔自己近一年的前 10%">偏熱</span>` : ""}</td>
          <td class="l" data-v="${r.weak ? 0 : 1}">${r.weak ? `<span class="badge b-stop" title="當天${esc(r.weak)}（${fmtP(r.chgPct)}、量比 ${fmt(r.volRatio, 1)}）：賣壓大於買盤">${esc(r.weak)}</span>` : ""}</td>
          <td class="num">${fmtS(r.maDist, 1)}%</td><td class="num">${fmt(r.value20, 1)}</td>
          <td>${r.inWatchlist ? `<span class="small muted">已在清單</span>` : `<button data-add="${esc(r.code)}">加入觀察</button>`}</td></tr>`).join("")}
        </tbody></table></div>` : `<div class="empty">今天沒有股票通過篩選${s.trend ? "（這個排序需要法人資料）" : "：大盤弱勢時，站穩月線與季線、接近 60 日高點的股票很少"}。</div>`}
      <div class="small muted">篩選：一般股票、近 20 日平均成交金額 ≥ 1 億元；收盤 > 月線 > 季線、月線走平或上揚、收盤在 60 日最高的 90% 以上。
        「法人」依外資＋投信近 5 日累計買超佔成交量排序；「相對強度」依近 20、60 日漲幅勝過加權指數的幅度；「不追高」排除標示偏熱的股票；「當日轉弱」＝爆量長黑（跌 ≥ 3%、量比 ≥ 1.5）或爆量長上影黑 K，「排除轉弱」的排序會拿掉這些股票。
        「注意」「處置中」為交易所公告（處置期間改為分盤撮合，大量委託須預收款券，流動性大減）。名單只依公開資料機械式篩選，未考慮基本面與個別風險，不構成投資建議。</div></section>`;
    $("#scanM").onchange = ev => { scanMethod = ev.target.value; draw(); applySorts(); };
    document.querySelectorAll("[data-from=scan]").forEach(x => x.onclick = () => { try { sessionStorage.setItem("twradar-from", "scan"); } catch (e) {} });
    document.querySelectorAll("[data-add]").forEach(b => b.onclick = () => { const r = m.rows.find(x => x.code === b.dataset.add);
      addStockDialog({code: r.code, name: r.name, market: r.market}, () => { r.inWatchlist = true; b.outerHTML = `<span class="small muted">已在清單</span>`; }); });
  };
  draw();
}

/* ---------- 調整持股（從持有提示開啟） ---------- */
async function positionDialog(code) {
  let wl, e;
  try { [wl, e] = await Promise.all([api("/watchlist"), api("/latest")]); } catch (err) { toast(err.message); return; }
  const w = wl.find(x => x.code === code);
  if (!w) { toast(`${code} 不在觀察清單`); return; }
  const s = (e.stocks || []).find(x => x.code === code), h = s && s.signal.hold, px = s && s.data ? s.data.close : null;
  const sh = w.holding ? w.shares || 0 : 0, cost = w.holding ? w.cost : null;
  const tip = !h ? "" : h.light === "exit" ? "規則提示「退場」：照規則是全部賣出。" : h.light === "reduce" ? "規則提示「減碼」：照規則是先賣出一部分（例如一半）。" : "規則提示「續抱」。";
  sheet(`<div class="row between"><h2><span class="num">${esc(code)}</span> ${esc(w.name)}・調整持股</h2>${h ? badge(h.light) : ""}</div>
    <div class="small">${esc(tip)}${h ? `<br><span class="muted">${esc(holdLine(h))}</span>` : ""}</div>
    <div class="grid2 small"><div>目前股數：<b class="num">${sh ? fmt(sh) : "未填"}</b></div><div>成本：<b class="num">${isNum(cost) ? fmt(cost, 2) : "未填"}</b></div>
      <div>最新收盤：<b class="num">${isNum(px) ? fmt(px, pd(px)) : "—"}</b></div><div>買進日期：<b>${esc(w.buyDate || "未填")}</b></div></div>
    <div class="tabs2" role="tablist"><button type="button" data-act="sell" class="on">賣出</button><button type="button" data-act="buy">買進／加碼</button></div>
    <form id="posForm" style="display:flex;flex-direction:column;gap:10px">
      <div id="sellBox" class="row">${sh ? `<button type="button" data-q="${sh}">全部（${fmt(sh)}）</button><button type="button" data-q="${Math.floor(sh / 2000) * 1000 || Math.floor(sh / 2)}">約一半（${fmt(Math.floor(sh / 2000) * 1000 || Math.floor(sh / 2))}）</button>` : ""}
        <span class="small muted">${sh ? "" : "沒有填股數：按「全部賣出」會改成未持有。"}</span></div>
      <div class="grid2"><div class="field"><label for="p-qty" id="p-qtyL">賣出股數</label><input id="p-qty" inputmode="numeric" placeholder="${sh ? "例如 1000" : "可留空"}"></div>
        <div class="field" id="p-pxF" hidden><label for="p-px">買進價格</label><input id="p-px" inputmode="decimal" value="${isNum(px) ? px : ""}"></div></div>
      <div id="p-preview" class="small muted"></div>
      <div class="row between"><button type="button" class="linkish small" id="p-full">完整編輯…</button>
        <div class="row"><button type="button" data-close>取消</button>${sh || !w.holding ? "" : `<button type="button" class="danger" id="p-clear">全部賣出</button>`}<button class="primary">確定</button></div></div></form>`,
    sheetEl => {
      const q = x => sheetEl.querySelector(x); let act = "sell";
      const calc = () => {
        const n = parseInt(q("#p-qty").value, 10) || 0, p = Number(q("#p-px").value);
        if (act === "sell") {
          if (!n) return null;
          if (sh && n > sh) return {err: "賣出股數超過持有"};
          const left = sh ? sh - n : 0;
          return left > 0 ? {holding: true, shares: left, cost, buyDate: w.buyDate, text: `賣出後剩 ${fmt(left)} 股，成本維持 ${isNum(cost) ? fmt(cost, 2) : "—"}`}
            : {holding: false, shares: null, cost: null, buyDate: null, text: "全部賣出：改為未持有，成本與買進日期清除"};
        }
        if (!n || !(p > 0)) return null;
        const tot = sh + n, avg = sh && isNum(cost) ? (cost * sh + p * n) / tot : p;
        return {holding: true, shares: tot, cost: Math.round(avg * 100) / 100, buyDate: (w.holding && w.buyDate) || new Date().toISOString().slice(0, 10),
          text: `買進後共 ${fmt(tot)} 股，平均成本 ${fmt(avg, 2)}${w.holding && w.buyDate ? "，買進日期維持第一次買進" : "，買進日期記為今天"}`};
      };
      const show = () => { const c = calc(); q("#p-preview").textContent = c ? (c.err || c.text) : ""; q("#p-preview").className = "small " + (c && c.err ? "down" : "muted"); };
      sheetEl.querySelectorAll("[data-act]").forEach(b => b.onclick = () => { act = b.dataset.act; sheetEl.querySelectorAll("[data-act]").forEach(x => x.classList.toggle("on", x === b));
        q("#sellBox").hidden = act !== "sell"; q("#p-pxF").hidden = act !== "buy"; q("#p-qtyL").textContent = act === "sell" ? "賣出股數" : "買進股數"; q("#p-qty").value = ""; show(); });
      sheetEl.querySelectorAll("[data-q]").forEach(b => b.onclick = () => { q("#p-qty").value = b.dataset.q; show(); });
      if (!w.holding) sheetEl.querySelector("[data-act=buy]").click();   // 未持有：直接開在「買進」
      q("#p-qty").oninput = show; q("#p-px").oninput = show;
      const save = async c => { try { await api("/watchlist", {method: "POST", body: JSON.stringify({code, holding: c.holding, shares: c.shares, cost: c.cost, buyDate: c.buyDate})});
        await api("/analyze", {method: "POST", body: "{}"}).catch(() => {}); $("#layer").innerHTML = ""; toast(`${code} 持股已更新`); route(); } catch (err) { toast(err.message); } };
      const clr = q("#p-clear"); if (clr) clr.onclick = () => save({holding: false, shares: null, cost: null, buyDate: null});
      q("#posForm").addEventListener("submit", ev => { ev.preventDefault(); const c = calc(); if (!c) { toast("請填股數" + (act === "buy" ? "與價格" : "")); return; } if (c.err) { toast(c.err); return; } save(c); });
      q("#p-full").onclick = async () => { const labels = await loadLabels(); editStock(w, labels, null, () => route()); };
    });
}
// 捕獲階段處理：卡片本身點了會開個股頁，這裡先攔下來
document.addEventListener("click", ev => { const b = ev.target.closest("[data-hold]"); if (b) { ev.preventDefault(); ev.stopPropagation(); positionDialog(b.dataset.hold); } }, true);

/* ---------- 路由 ---------- */
async function route() {
  const h = location.hash.replace(/^#/, "") || "/";
  const [, page, arg] = h.split("/");
  document.querySelectorAll("#tabs a").forEach(a => a.classList.toggle("on", a.dataset.tab === (["report", "settings", "scan", "flows"].includes(page) ? page : "home")));
  try {
    if (page !== "settings" && !Object.keys(LABELS).length) await loadLabels();
    if (page !== "stock") { try { sessionStorage.removeItem("twradar-from"); } catch (e) {} }
    if (page === "report") await viewReport(arg);
    else if (page === "settings") await viewSettings();
    else if (page === "scan") await viewScan();
    else if (page === "flows") await viewFlows();
    else if (page === "stock" && arg) await viewStock(arg);
    else await viewHome(page === "day" ? arg : null);
    applySorts();
  } catch (err) { $("#view").innerHTML = `<div class="panel empty">無法載入：${esc(err.message)}</div>`; }
}
window.addEventListener("hashchange", route);
route();
})();
