"""回溯分析報表：單一 HTML 檔，可直接用瀏覽器開啟或列印。"""
from __future__ import annotations

import html

TAG_ORDER = ["全部", "大型", "中型", "上櫃", "觀察清單"]
REG_ORDER = ["全部", "多頭", "盤整", "空頭"]
ENTRY_KINDS = ["上車", "加碼", "試單"]


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


def _cond_section(cs: dict | None) -> str:
    if not cs:
        return ""
    from .study import verdict
    rows = []
    for cid, row in sorted(cs.items(), key=lambda kv: (kv[1]["side"], kv[0])):
        tds = []
        for smp in ("樣本內", "樣本外"):
            cell = row["趨勢股"][smp]
            p, f, df = cell["pass"]["f10"], cell["fail"]["f10"], cell["diff"].get("f10")
            v = verdict(cell)
            tds.append(f"<td class='num'>{p.get('n', 0):,}</td><td class='num {_cls(p.get('excess'))}'>{_num(p.get('excess'), sign=True, pct=True)}</td>"
                       f"<td class='num {_cls(f.get('excess'))}'>{_num(f.get('excess'), sign=True, pct=True)}</td>"
                       f"<td class='num {_cls(df)}'>{_num(df, sign=True)}</td><td class='sm'>{_e(v)}</td>")
        raw = "・原始報酬" if row.get("raw") else ""
        rows.append(f"<tr><td>{_e(row['label'])}<div class='sm'>{_e(cid)}・{_e(row['side'])}{raw}</div></td>{''.join(tds)}</tr>")
    head = "".join(f"<th>{s} 成立筆數</th><th>成立</th><th>不成立</th><th>差距</th><th>判讀</th>" for s in ("樣本內", "樣本外"))
    return f"""<h2>條件研究：每個條件的區分力</h2><section>
<p class="sm">母體：趨勢股（收盤在月線上、月線走平或上揚）。數字為 10 日超額報酬（同日同組平均為基準）；
大盤層級的條件（大盤、廣度、外資態度，同一天每檔都一樣）改用 10 日原始報酬，標「原始報酬」。
否決類條件「差距」應為負、買進類應為正；兩段樣本方向一致才可信。「看不出差別」＝差距小於兩倍標準誤。</p>
<div class="tw"><table><tr><th>條件</th>{head}</tr>{''.join(rows)}</table></div></section>"""


def _scan_section(sc: dict | None) -> str:
    if not sc:
        return ""
    rows = []
    for m, v in sc["methods"].items():
        for b in ("全市場", "趨勢股"):
            tds = "".join(
                f"<td class='num {_cls(v[b][sg]['f10'].get('excess'))}'>{_num(v[b][sg]['f10'].get('excess'), sign=True, pct=True)}"
                f"<div class='sm'>±{_num(v[b][sg]['f10'].get('se'))}・勝 {_num(v[b][sg]['f10'].get('hit'), 0)}%</div></td>"
                for sg in ("樣本內", "樣本外", "多頭", "盤整", "空頭"))
            rows.append(f"<tr><td>{_e(m)}</td><td>{_e(b)}</td>{tds}</tr>")
    c = sc["config"]
    return f"""<h2>全市場掃描驗證</h2><section>
<p class="sm">每天從全市場（日均成交額 ≥ {c['minValue'] / 1e8:.0f} 億、平均 {sc['avgLiquid']} 檔）篩出趨勢股（平均 {sc['avgTrend']} 檔），依各方法取前 {c['top']} 名，
隔日開盤買、10 日後收盤。數字＝每天（前 {c['top']} 名平均 − 基準平均）的平均；±為標準誤、勝＝勝過基準的天數比例。
對「趨勢股」的超額代表排序本身有沒有加分。</p>
<div class="tw"><table><tr><th>方法</th><th>基準</th><th>樣本內</th><th>樣本外</th><th>多頭</th><th>盤整</th><th>空頭</th></tr>{''.join(rows)}</table></div></section>"""


PALETTE = ("#b71c1c", "#1f4e79", "#2e7d32", "#b7791f", "#7a5cb8", "#00838f", "#6d4c41", "#c2185b", "#455a64", "#9e9d24")


def _equity_svg(results: list[dict], split: str, start: str, end: str) -> str:
    """各組合的累積損益曲線（依出場日；每次買進 1 單位）。第一條（目前設定）加粗。"""
    from .dates import to_date
    series = [(r["name"], r.get("equity") or []) for r in results]
    vals = [v for _, e in series for _, v in e]
    if not vals:
        return '<p class="sm">沒有交易。</p>'
    W, H, L, R, T, B = 1000, 340, 64, 14, 14, 26
    lo, hi = min(0.0, *vals), max(0.0, *vals)
    pad = (hi - lo) * .06 or 1
    lo, hi = lo - pad, hi + pad
    a, z = to_date(start), to_date(end)
    span = max(1, (z - a).days)
    X = lambda d: L + (to_date(d) - a).days / span * (W - L - R)  # noqa: E731
    Y = lambda v: T + (hi - v) / (hi - lo) * (H - T - B)          # noqa: E731
    g = []
    step = 10 ** int(f"{(hi - lo) / 4:e}".split("e")[1])
    for m in (1, 2, 5, 10):
        if (hi - lo) / (step * m) <= 6:
            step *= m
            break
    v = (lo // step + 1) * step
    while v < hi:
        g.append(f'<line x1="{L}" x2="{W - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="#e6eaef"/>'
                 f'<text x="{L - 6}" y="{Y(v) + 4:.1f}" text-anchor="end" class="ax">{v:g}</text>')
        v += step
    g.append(f'<line x1="{L}" x2="{W - R}" y1="{Y(0):.1f}" y2="{Y(0):.1f}" stroke="#90a4ae"/>')
    y0, m0 = a.year, a.month
    while (y0, m0) <= (z.year, z.month):
        d = f"{y0}{m0:02d}01"
        if start <= d <= end and m0 in (1, 4, 7, 10):
            g.append(f'<text x="{X(d):.1f}" y="{H - 8}" text-anchor="middle" class="ax">{y0 % 100:02d}/{m0:02d}</text>')
        m0 += 1
        if m0 > 12:
            y0, m0 = y0 + 1, 1
    if start < split <= end:
        g.append(f'<line x1="{X(split):.1f}" x2="{X(split):.1f}" y1="{T}" y2="{H - B}" stroke="#90a4ae" stroke-dasharray="4 3"/>'
                 f'<text x="{X(split) + 4:.1f}" y="{T + 10}" class="ax">樣本外 →</text>')
    legend = []
    for i, (name, e) in enumerate(series):
        if not e:
            continue
        c, w = PALETTE[i % len(PALETTE)], 2.6 if i == 0 else 1.4
        pts = f"{X(start):.1f},{Y(0):.1f} " + " ".join(f"{X(d):.1f},{Y(v):.1f}" for d, v in e)
        g.append(f'<polyline points="{pts}" fill="none" stroke="{c}" stroke-width="{w}" opacity="{1 if i == 0 else .75}"/>')
        legend.append(f'<span><i style="background:{c};height:{4 if i == 0 else 2}px"></i>{_e(name)}（{e[-1][1]:+.1f}）</span>')
    return (f'<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto">{"".join(g)}</svg>'
            f'<div class="lg">{"".join(legend)}</div>')


def _bench_section(res: dict) -> str:
    """最關鍵的問題：照訊號操作，有沒有贏過直接買 0050／大盤？"""
    r0 = res["results"][0]
    pf, bm = r0.get("portfolio") or {}, res.get("benchmarks") or {}
    if not pf:
        return ""
    cfg = res["config"]

    def row(name, x, extra=""):
        return (f"<tr><td>{_e(name)}</td><td class='num {_cls(x.get('total'))}'>{_num(x.get('total'), 1, sign=True, pct=True)}</td>"
                f"<td class='num {_cls(x.get('cagr'))}'>{_num(x.get('cagr'), 1, sign=True, pct=True)}</td>"
                f"<td class='num'>{_num(x.get('maxDD'), 1, pct=True)}</td>"
                f"<td class='num {_cls(x.get('in'))}'>{_num(x.get('in'), 1, sign=True, pct=True)}</td>"
                f"<td class='num {_cls(x.get('out'))}'>{_num(x.get('out'), 1, sign=True, pct=True)}</td><td class='num'>{extra}</td></tr>")
    def nm(n):
        return f"照訊號：資金分 {n.replace('排序', '')} 份" + ("，挑法人買超最強" if "排序" in n else "，依代號順序")
    sec = lambda t: f"<tr><th colspan=7 style='background:#fafbfc'>{t}</th></tr>"  # noqa: E731
    rows = sec("對照：什麼都不做")
    rows += "".join(row(k, v, "100%") for k, v in bm.items())
    rows += sec("照訊號買賣個股")
    rows += "".join(row(nm(n), x, f"{_num(x.get('exposure'), 0)}%（買 {x.get('taken', 0)}、錯過 {x.get('skipped', 0)}）")
                    for n, x in pf.items())
    if res.get("timing"):
        rows += sec("抱 0050，只用大盤條件決定進出（訊號隔日開盤執行）")
        rows += "".join(row(k, v, f"{_num(v.get('exposure'), 0)}%（進出 {v.get('switches', 0)} 次）") for k, v in res["timing"].items())
    if res.get("combo"):
        rows += sec("核心＋衛星（一開始各半，之後不再平衡）")
        rows += "".join(row(k, v, "") for k, v in res["combo"].items())
    # 曲線：10 份 vs 對照
    lines = [("照訊號（30 份，挑最強）", (pf.get("30排序") or pf.get("10") or {}).get("curve") or [], "#b71c1c", 2.6)]
    lines += [(k, v.get("curve") or [], c, 1.6) for (k, v), c in zip(bm.items(), ("#90a4ae", "#1f4e79"))]
    tm = res.get("timing") or {}
    if tm:
        best = max(tm.items(), key=lambda kv: kv[1].get("total") or -1e9)
        lines.append((best[0], best[1].get("curve") or [], "#2e7d32", 2.0))
    cb = res.get("combo") or {}
    if cb:
        k0 = next(iter(cb))
        lines.append((k0, cb[k0].get("curve") or [], "#b7791f", 1.6))
    vals = [v for _, cv, _, _ in lines for _, v in cv]
    svg = ""
    if vals:
        from .dates import to_date
        W, H, L, R, T, B = 1000, 300, 56, 14, 12, 24
        lo, hi = min(vals), max(vals)
        pad = (hi - lo) * .06 or .05
        lo, hi = lo - pad, hi + pad
        a, z = to_date(cfg["start"]), to_date(cfg["end"])
        span = max(1, (z - a).days)
        X = lambda d: L + (to_date(d) - a).days / span * (W - L - R)  # noqa: E731
        Y = lambda v: T + (hi - v) / (hi - lo) * (H - T - B)          # noqa: E731
        g = [f'<line x1="{L}" x2="{W - R}" y1="{Y(1):.1f}" y2="{Y(1):.1f}" stroke="#90a4ae" stroke-dasharray="3 3"/>'
             f'<text x="{L - 6}" y="{Y(1) + 4:.1f}" text-anchor="end" class="ax">起點</text>']
        for v in (lo + (hi - lo) * k / 4 for k in (0, 1, 3, 4)):
            g.append(f'<text x="{L - 6}" y="{Y(v) + 4:.1f}" text-anchor="end" class="ax">{(v - 1) * 100:+.0f}%</text>')
        if cfg["start"] < cfg["split"] <= cfg["end"]:
            g.append(f'<line x1="{X(cfg["split"]):.1f}" x2="{X(cfg["split"]):.1f}" y1="{T}" y2="{H - B}" stroke="#90a4ae" stroke-dasharray="4 3"/>'
                     f'<text x="{X(cfg["split"]) + 4:.1f}" y="{T + 10}" class="ax">樣本外 →</text>')
        leg = []
        for name, cv, c, w in lines:
            if cv:
                g.append(f'<polyline points="{" ".join(f"{X(d):.1f},{Y(v):.1f}" for d, v in cv)}" fill="none" stroke="{c}" stroke-width="{w}"/>')
                leg.append(f'<span><i style="background:{c};height:3px"></i>{_e(name)}</span>')
        svg = f'<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto">{"".join(g)}</svg><div class="lg">{"".join(leg)}</div>'
    return f"""<h2>最關鍵的比較：照訊號操作 vs 直接買 0050／大盤</h2><section>
<p class="sm">目標是最大獲利：一種做法要在總報酬上贏 0050 才算真的有用；報酬接近但最大回落小很多，代表風險較低。只有兩年資料，樣本不多。</p>
<p class="sm">把訊號放進一個真實的投資組合：資金分成 N 份，有訊號且有空位才買（同一天太多檔時依代號順序），每次用當時總資產的 1/N；
開盤成交、逐日以收盤計價，沒用到的錢放現金。簡化：不含加碼、減碼。0050 為還原股息的報酬；加權指數是價格指數，不含股息（會低估約每年 2～3%）。</p>
<div class="tw"><table><tr><th>方式</th><th>總報酬</th><th>年化</th><th>最大回落</th><th>樣本內</th><th>樣本外</th><th>平均持股比例</th></tr>{rows}</table></div>
{svg}</section>"""


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
            for s in ("樣本內", "樣本外"))
        + f"<td class='num {_cls(((r.get('portfolio') or {}).get('10') or {}).get('total'))}'>{_num(((r.get('portfolio') or {}).get('10') or {}).get('total'), 1, sign=True, pct=True)}</td>"
          f"<td class='num'>{_num(((r.get('portfolio') or {}).get('10') or {}).get('maxDD'), 1, pct=True)}</td></tr>" for r in res["results"])
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
.kv b{{display:block;font-size:18px}} li{{margin:4px 0}} .ax{{font-size:11px;fill:#6b7785}}
.lg{{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:12px;margin-top:6px}} .lg i{{display:inline-block;width:18px;vertical-align:middle;margin-right:5px}} @media print{{body{{background:#fff}} section{{break-inside:avoid}}}}
</style></head><body><main>
<h1>右側加碼雷達：回溯分析</h1>
<div class="muted">期間 {_d(cfg['start'])}～{_d(cfg['end'])}；{_d(cfg['split'])} 起為樣本外。股票池：{_e('每季依前 60 日成交金額排名（上市前 50＝大型、51～150＝中型、上櫃前 50＝上櫃）＋觀察清單' if cfg['universe'] == 'auto' else cfg['universe'])}。</div>

<section><div class="kv">
<div>交易日<b>{cov['tradingDays']}</b></div><div>股票數<b>{cov['stocks']}</b></div>
<div>多頭／盤整／空頭（天）<b>{cov['regimeDays'].get('多頭', 0)}／{cov['regimeDays'].get('盤整', 0)}／{cov['regimeDays'].get('空頭', 0)}</b></div>
<div>集保資料（週）<b>{cov['holdersWeeks']}</b></div><div>訊號事件<b>{r0.get('eventCount', 0):,}</b></div></div></section>

{_bench_section(res)}
<h2>摘要</h2><section><ul>{findings}</ul>
<p class="sm">「超額」＝訊號後平均報酬 − 同期、同組、同大盤環境下所有股票每天的平均報酬（基準）。進場訊號超額為正、出場訊號超額為負，代表訊號有用。
報酬以「訊號隔日開盤買進」計算，使用還原股價，未扣費用。</p></section>

<h2>進場訊號：上車與加碼</h2><section>{_event_table(ev, ENTRY_KINDS)}</section>
{_cond_section(r0.get("conditions"))}
{_scan_section(res.get("scan"))}

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

<h2>規則與參數對照</h2><section>
<h3>累積損益（資金曲線）</h3>
<p class="sm">依出場日累加每筆交易的損益；每次買進投入 1 單位，所以縱軸是「賺了幾個單位」。曲線下彎的段落就是虧損期，越平穩越好。粗線是目前設定，括號內是期末累積損益。</p>
{_equity_svg(res["results"], cfg["split"], cfg["start"], cfg["end"])}
<div class="tw" style="margin-top:12px"><table><tr><th rowspan=2>組合</th><th colspan=5>樣本內</th><th colspan=5>樣本外</th><th colspan=2>投資組合（10 份）</th></tr>
<tr><th>筆數</th><th>平均報酬</th><th>勝率</th><th>獲利因子</th><th>最大回落</th><th>筆數</th><th>平均報酬</th><th>勝率</th><th>獲利因子</th><th>最大回落</th><th>總報酬</th><th>最大回落</th></tr>
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
