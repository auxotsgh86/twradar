"""回溯分析報表：單一 HTML 檔，可直接用瀏覽器開啟或列印。"""
from __future__ import annotations

import html

TAG_ORDER = ["全部", "大型", "中型", "上櫃", "觀察清單"]
REG_ORDER = ["全部", "多頭", "盤整", "空頭"]
ENTRY_KINDS = ["上車", "加碼"]


def _e(x) -> str:
    return html.escape(str(x))


def _d(d: str) -> str:
    return f"{d[:4]}/{d[4:6]}/{d[6:]}"


def _num(v, nd=2, sign=False, pct=False) -> str:
    if v is None:
        return "—"
    s = f"{v:+,.{nd}f}" if sign else f"{v:,.{nd}f}"
    return s + ("%" if pct else "")


def _cls(v) -> str:
    return "" if v is None else ("up" if v > 0 else "down" if v < 0 else "")


def _cell(st: dict, h: int, invert=False) -> str:
    s = st.get(f"f{h}") or {}
    if not s.get("n"):
        return "<td>—</td><td>—</td>"
    ex = s.get("excess")
    good = None if ex is None else (ex < 0 if invert else ex > 0)
    return (f'<td class="num {_cls(s["mean"])}">{_num(s["mean"], sign=True, pct=True)}<br><span class="sm">勝率 {_num(s["win"], 0)}%</span></td>'
            f'<td class="num {"good" if good else "bad" if good is False else ""}">{_num(ex, sign=True)}</td>')


def _event_table(events: list[dict], kinds: list[str], invert=False, tags=None, note="") -> str:
    tags = tags or TAG_ORDER
    idx = {(e["kind"], e["tag"], e["regime"], e["sample"]): e for e in events}
    rows = []
    for k in kinds:
        for tag in tags:
            for reg in REG_ORDER:
                cells = []
                n_total = 0
                for sample in ("樣本內", "樣本外"):
                    e = idx.get((k, tag, reg, sample))
                    n = (e or {}).get("f20", {}).get("n", 0) if e else 0
                    n_total += n
                    cells.append(f'<td class="num">{n}</td>' + (_cell(e, 20, invert) if e else "<td>—</td><td>—</td>"))
                if n_total == 0:
                    continue
                e_all = idx.get((k, tag, reg, "全期")) or {}
                cells.append(_cell(e_all, 5, invert) + _cell(e_all, 10, invert))
                rows.append(f"<tr><td>{_e(k)}</td><td>{_e(tag)}</td><td>{_e(reg)}</td>{''.join(cells)}</tr>")
    if not rows:
        return '<p class="muted">（期間內沒有這類訊號）</p>'
    head = ('<tr><th rowspan=2>訊號</th><th rowspan=2>股票組</th><th rowspan=2>大盤</th>'
            '<th colspan=3>樣本內（20 日）</th><th colspan=3>樣本外（20 日）</th><th colspan=2>全期 5 日</th><th colspan=2>全期 10 日</th></tr>'
            '<tr><th>筆數</th><th>平均報酬</th><th>超額</th><th>筆數</th><th>平均報酬</th><th>超額</th>'
            '<th>平均</th><th>超額</th><th>平均</th><th>超額</th></tr>')
    return f'{note}<div class="tw"><table>{head}{"".join(rows)}</table></div>'


def _trade_row(name: str, t: dict) -> str:
    if not t.get("n"):
        return f"<tr><td>{_e(name)}</td><td colspan=9 class='muted'>沒有交易</td></tr>"
    return (f"<tr><td>{_e(name)}</td><td class='num'>{t['n']}</td><td class='num'>{_num(t['win'], 0)}%</td>"
            f"<td class='num {_cls(t['meanRet'])}'>{_num(t['meanRet'], sign=True, pct=True)}</td>"
            f"<td class='num {_cls(t['medianRet'])}'>{_num(t['medianRet'], sign=True, pct=True)}</td>"
            f"<td class='num'>{_num(t['profitFactor'])}</td><td class='num {_cls(t['pnl'])}'>{_num(t['pnl'], sign=True)}</td>"
            f"<td class='num'>{_num(t['maxDD'])}</td><td class='num'>{_num(t['avgDays'], 0)}</td>"
            f"<td class='num'>{_num(t.get('worstMae'), 1, pct=True)}</td></tr>")


TRADE_HEAD = ("<tr><th></th><th>筆數</th><th>勝率</th><th>平均報酬</th><th>中位數</th><th>獲利因子</th>"
              "<th>累計損益（單位）</th><th>最大回落（單位）</th><th>平均持有（天）</th><th>最差浮虧</th></tr>")


def _findings(res: dict, cfg: dict) -> list[str]:
    """自動摘要：只陳述數字，不下投資結論。"""
    out = []
    r0 = res["results"][0]
    idx = {(e["kind"], e["tag"], e["regime"], e["sample"]): e for e in r0.get("events", [])}
    for k in ENTRY_KINDS:
        a, b = idx.get((k, "全部", "全部", "樣本內")), idx.get((k, "全部", "全部", "樣本外"))
        if not a and not b:
            out.append(f"「{k}」訊號在期間內沒有出現。")
            continue
        def ex(e):
            return (e or {}).get("f20", {}).get("excess")
        def n(e):
            return (e or {}).get("f20", {}).get("n", 0)
        line = f"「{k}」訊號 20 日超額報酬：樣本內 {_num(ex(a), sign=True)}（{n(a)} 筆）、樣本外 {_num(ex(b), sign=True)}（{n(b)} 筆）"
        if ex(a) is not None and ex(b) is not None:
            line += "，方向一致。" if (ex(a) > 0) == (ex(b) > 0) else "，**樣本內外方向相反，不穩定**。"
        if n(b) < 30:
            line += "樣本外筆數少於 30，參考價值有限。"
        out.append(line)
        regs = {reg: idx.get((k, "全部", reg, "全期")) for reg in ("多頭", "盤整", "空頭")}
        parts = [f"{reg} {_num(ex(e), sign=True)}（{n(e)}）" for reg, e in regs.items() if e and n(e)]
        if parts:
            out.append(f"「{k}」依大盤環境的 20 日超額報酬：" + "、".join(parts) + "。")
    t = r0["trades"]
    if t["全期"].get("n"):
        out.append(f"交易模擬共 {t['全期']['n']} 筆：勝率 {_num(t['全期']['win'], 0)}%、平均報酬 {_num(t['全期']['meanRet'], sign=True, pct=True)}；"
                   f"樣本外 {t['樣本外'].get('n', 0)} 筆，平均 {_num(t['樣本外'].get('meanRet'), sign=True, pct=True)}。")
    if len(res["results"]) > 1:
        rows = [(r["name"], r["trades"]["樣本內"].get("meanRet"), r["trades"]["樣本外"].get("meanRet")) for r in res["results"]]
        best_in = max((x for x in rows if x[1] is not None), key=lambda x: x[1], default=None)
        if best_in:
            out.append(f"參數組合中樣本內平均報酬最高的是「{best_in[0]}」（樣本內 {_num(best_in[1], sign=True, pct=True)}、"
                       f"樣本外 {_num(best_in[2], sign=True, pct=True)}）。選參數時請看樣本外，以及相鄰數值是否同樣穩定。")
    cov = res["coverage"]
    if cov["holdersWeeks"] < 4:
        out.append(f"集保股權分散表只有 {cov['holdersWeeks']} 週，「大戶減碼」「股東人數增加」在回溯期間多為資料不足。")
    rd = cov["regimeDays"]
    if rd.get("空頭", 0) < 20:
        out.append(f"期間內大盤空頭只有 {rd.get('空頭', 0)} 天，規則在空頭的表現無法充分驗證。")
    return out


def render(res: dict) -> str:
    cfg, cov = res["config"], res["coverage"]
    r0 = res["results"][0]
    ev = r0.get("events", [])
    hold_kinds = sorted({e["kind"] for e in ev if e["kind"].startswith(("退場", "減碼"))})
    other = [k for k in ("否決（未持有）", "大盤逆風（被擋）") if any(e["kind"] == k for e in ev)]
    t = r0["trades"]
    variants = "".join(
        f"<tr><td>{_e(r['name'])}</td>" + "".join(
            f"<td class='num'>{r['trades'][s].get('n', 0)}</td><td class='num {_cls(r['trades'][s].get('meanRet'))}'>"
            f"{_num(r['trades'][s].get('meanRet'), sign=True, pct=True)}</td><td class='num'>{_num(r['trades'][s].get('win'), 0)}%</td>"
            f"<td class='num'>{_num(r['trades'][s].get('profitFactor'))}</td><td class='num'>{_num(r['trades'][s].get('maxDD'))}</td>"
            for s in ("樣本內", "樣本外")) + "</tr>" for r in res["results"])
    bh = "".join(f"<tr><td>{_e(k)}</td><td class='num'>{v.get('n', 0)}</td><td class='num {_cls(v.get('mean'))}'>"
                 f"{_num(v.get('mean'), sign=True, pct=True)}</td><td class='num'>{_num(v.get('median'), sign=True, pct=True)}</td>"
                 f"<td class='num'>{_num(v.get('win'), 0)}%</td></tr>" for k, v in res["buyHold"].items())
    worst = sorted(r0.get("tradeList", []), key=lambda x: x["ret"])[:10]
    best = sorted(r0.get("tradeList", []), key=lambda x: -x["ret"])[:10]

    def tl(ts):
        return "".join(f"<tr><td>{_e(x['code'])}</td><td>{_d(x['entry'])}</td><td>{_d(x['exit'])}</td><td>{_e(x['rule'])}</td>"
                       f"<td>{_e(x['reason'])}</td><td class='num'>{x['adds']}</td><td class='num {_cls(x['ret'])}'>"
                       f"{_num(x['ret'], sign=True, pct=True)}</td><td class='num'>{_num(x['mae'], 1, pct=True)}</td></tr>" for x in ts)
    tl_head = "<tr><th>代號</th><th>買進</th><th>出場</th><th>進場</th><th>出場原因</th><th>加碼</th><th>報酬</th><th>最差浮虧</th></tr>"
    findings = "".join(f"<li>{x.replace('**', '')}</li>" for x in _findings(res, cfg))
    return f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>右側加碼雷達：回溯分析 {_d(cfg['start'])}～{_d(cfg['end'])}</title>
<style>
body{{font-family:"Noto Sans TC","PingFang TC","Microsoft JhengHei",sans-serif;margin:0;padding:16px;color:#16202b;background:#f5f7fa;line-height:1.55}}
main{{max-width:1100px;margin:auto}} h1{{font-size:22px;margin:4px 0}} h2{{font-size:17px;margin:26px 0 8px;border-left:4px solid #b71c1c;padding-left:8px}}
section{{background:#fff;border:1px solid #dde3ea;border-radius:10px;padding:14px 16px;margin:12px 0}}
.tw{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;font-size:13px}} th,td{{border-bottom:1px solid #e6eaef;padding:5px 7px;text-align:left;white-space:nowrap}}
th{{background:#f0f3f7;font-weight:600}} td.num{{text-align:right;font-variant-numeric:tabular-nums}} .sm{{font-size:11px;color:#6b7785}}
.up{{color:#c62828}} .down{{color:#2e7d32}} .good{{background:#fdecea;font-weight:600}} .bad{{background:#e8f5e9}} .muted{{color:#6b7785}}
.kv{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px}} .kv div{{background:#f7f9fb;border-radius:8px;padding:8px}}
.kv b{{display:block;font-size:18px}} li{{margin:4px 0}} @media print{{body{{background:#fff}} section{{break-inside:avoid}}}}
</style></head><body><main>
<h1>右側加碼雷達：回溯分析</h1>
<div class="muted">期間 {_d(cfg['start'])}～{_d(cfg['end'])}；{_d(cfg['split'])} 起為樣本外。股票池：{_e('每季依前 60 日成交金額排名（上市前 50＝大型、51～150＝中型、上櫃前 50＝上櫃）＋觀察清單' if cfg['universe'] == 'auto' else cfg['universe'])}。</div>

<section><div class="kv">
<div>交易日<b>{cov['tradingDays']}</b></div><div>股票數<b>{cov['stocks']}</b></div>
<div>多頭／盤整／空頭（天）<b>{cov['regimeDays'].get('多頭', 0)}／{cov['regimeDays'].get('盤整', 0)}／{cov['regimeDays'].get('空頭', 0)}</b></div>
<div>集保資料（週）<b>{cov['holdersWeeks']}</b></div><div>訊號事件<b>{r0.get('eventCount', 0):,}</b></div></div></section>

<h2>摘要</h2><section><ul>{findings}</ul>
<p class="sm">「超額」＝訊號後平均報酬 − 同期、同組、同大盤環境下所有股票每天的平均報酬（基準）。進場訊號超額為正、出場訊號超額為負，代表訊號有用。
報酬以「訊號隔日開盤買進」計算，使用還原股價，未扣費用。</p></section>

<h2>進場訊號：上車與加碼</h2><section>{_event_table(ev, ENTRY_KINDS)}</section>

<h2>持有面訊號：減碼與退場</h2><section>{_event_table(ev, hold_kinds, invert=True, tags=["全部", "大型", "中型", "上櫃"],
    note='<p class="sm">出場後股價的走勢：超額為負（綠底）代表出場後確實相對轉弱，訊號有幫你避開下跌。</p>')}</section>

<h2>否決與大盤濾網</h2><section>{_event_table(ev, other, invert=True, tags=["全部", "大型", "中型", "上櫃"],
    note='<p class="sm">被擋下的訊號之後走勢：超額為負代表擋得對。</p>')}</section>

<h2>交易模擬（目前設定）</h2><section>
<p class="sm">上車訊號隔日開盤買進 1 單位；持有中出現加碼訊號再買 1 單位（最多 {cfg['maxAdds']} 次）；減碼賣一半（每筆一次）；退場全賣。扣手續費與證交稅。</p>
<div class="tw"><table>{TRADE_HEAD}{_trade_row('全期', t['全期'])}{_trade_row('樣本內', t['樣本內'])}{_trade_row('樣本外', t['樣本外'])}</table></div>
<h3>依股票組（全期）</h3><div class="tw"><table>{TRADE_HEAD}{''.join(_trade_row(k, v['全期']) for k, v in r0.get('byTag', {}).items())}</table></div>
<h3>依出場原因</h3><div class="tw"><table>{TRADE_HEAD}{''.join(_trade_row(k, v) for k, v in r0.get('byReason', {}).items())}</table></div>
<h3>每半年（依買進日）</h3><div class="tw"><table>{TRADE_HEAD}{''.join(_trade_row(k, v) for k, v in r0.get('byHalfYear', {}).items())}</table></div>
</section>

<h2>參數組合比較</h2><section>
<div class="tw"><table><tr><th rowspan=2>組合</th><th colspan=5>樣本內</th><th colspan=5>樣本外</th></tr>
<tr><th>筆數</th><th>平均報酬</th><th>勝率</th><th>獲利因子</th><th>最大回落</th><th>筆數</th><th>平均報酬</th><th>勝率</th><th>獲利因子</th><th>最大回落</th></tr>
{variants}</table></div><p class="sm">選參數時以樣本外為準，並優先選「相鄰數值表現也接近」的參數，避免只是配合過去走勢。</p></section>

<h2>對照：一直抱著</h2><section><p class="sm">股票在股票池期間第一天買、最後一天賣的報酬（還原股價，未扣費用）。</p>
<div class="tw"><table><tr><th>股票組</th><th>檔數</th><th>平均</th><th>中位數</th><th>上漲比例</th></tr>{bh}</table></div></section>

<h2>報酬最差與最好的 10 筆交易</h2><section><div class="tw"><table>{tl_head}{tl(worst)}</table></div>
<div class="tw" style="margin-top:10px"><table>{tl_head}{tl(best)}</table></div></section>

<h2>注意事項</h2><section><ul>
<li>股票池每季依「當時」的成交金額排名，避免用今天的成份股回測過去（倖存者偏差）；觀察清單是事後挑的，該組結果會偏樂觀。</li>
<li>集保週資料只有接入後才有；回溯期間的「大戶減碼」「股東人數增加」多半無法判斷。</li>
<li>沒有模擬「連續 N 日確認」，也沒有考慮漲跌停買不到、賣不掉的情況。</li>
<li>本報表為歷史統計，不代表未來表現，也不構成投資建議。</li></ul></section>
</main></body></html>"""
