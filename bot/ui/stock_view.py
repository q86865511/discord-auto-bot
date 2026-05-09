"""T 鍵 — 終端內顯示股票分析頁面。

呼叫端要先 live.stop() 再進這裡(會用 input() 等使用者按 Enter)。
資料來源:state.stock_last_snapshot(由 stock_loop 維護)。
顯示內容:
- 帳戶概況 + 訊號摘要(類似 Dashboard 的「總覽」分頁)
- 持股明細(若有)
- 買進建議(score ≥ 60)
- 全部股票清單
"""
from __future__ import annotations

import os

from rich.console import Console
from rich.table import Table

from bot.core.state import BotState

console = Console()


def show_stock_analysis(state: BotState) -> None:
    """同步顯示股票頁面。呼叫端應 live.stop() 後再呼叫。"""
    os.system("cls")
    snap = state.stock_last_snapshot or {}

    console.print()
    console.rule("[bold cyan]📈  股票監控[/]")

    if not snap:
        console.print()
        console.print("  [dim]尚無股票資料[/dim]")
        console.print()
        console.print("  原因可能是:")
        console.print("    - 股票功能未啟用(C → [8] 股票監視 → [1] 啟用)")
        console.print("    - 第一次 poll 還沒跑完(loop 啟動 60 秒後第一次)")
        console.print("    - poll 失敗(看 logs/bot.log 排查)")
        cmd = input("\n  按 Enter 返回 / R + Enter 立即重 poll: ").strip().lower()
        if cmd == "r":
            state.stock_force_poll = True
            print("  ✓ 已請求立即重 poll(30 秒內生效)")
            input("  按 Enter 繼續...")
        return

    ts       = snap.get("ts", "─")
    prices   = snap.get("prices", {})
    holdings = snap.get("holdings", {})
    signals  = snap.get("signals", [])

    # ── 1. 帳戶概況 + 訊號摘要 ─────────────────────────────────────
    total_value = 0.0
    total_pnl = 0.0
    for sym, h in holdings.items():
        cur = prices.get(sym, h.get("current_price", 0))
        total_value += h["shares"] * cur
        total_pnl   += h["shares"] * (cur - h.get("avg_cost", 0))

    # Bug-prone:`sell_eval` 對未持有的股是 None,不是 dict;
    # `s.get("sell_eval", {})` 會回 None 而不是 {}(default 只在 key 缺時用)
    sells = [s for s in signals
             if (s.get("sell_eval") or {}).get("signal") == "sell"]
    strong_buys = [s for s in signals
                   if (s.get("buy_eval") or {}).get("signal") == "buy"
                   and (s.get("buy_eval") or {}).get("score", 0) >= 80]
    mid_buys = [s for s in signals
                if 60 <= (s.get("buy_eval") or {}).get("score", 0) < 80]

    summary = Table(box=None, show_header=False, padding=(0, 2), expand=True)
    summary.add_column(style="dim", width=18)
    summary.add_column()
    summary.add_column(style="dim", width=18)
    summary.add_column()
    summary.add_row(
        "📊 已抓股票數", f"[bold]{len(prices)}[/]",
        "💼 持股種類", f"[bold]{len(holdings)}[/]",
    )
    pnl_color = "green" if total_pnl > 0 else ("red" if total_pnl < 0 else "dim")
    summary.add_row(
        "💰 持股總市值", f"[bold]{int(total_value):,}[/]",
        "📈 未實現損益", f"[{pnl_color}]{int(total_pnl):+,}[/{pnl_color}]",
    )
    summary.add_row(
        "🔴 建議賣出", f"[red]{len(sells)}[/red]",
        "🟢 強買進(≥80)", f"[green]{len(strong_buys)}[/green]",
    )
    summary.add_row(
        "🟡 中買進(60~79)", f"[yellow]{len(mid_buys)}[/yellow]",
        "⏰ 最近 poll", f"[dim]{ts}[/dim]",
    )
    console.print(summary)

    # ── 2. Top 3 即時推薦 ─────────────────────────────────────────
    top_rows: list[tuple[str, str, dict, str]] = []
    for s in signals:
        sym = s["symbol"]
        sev = s.get("sell_eval")
        bev = s.get("buy_eval")
        if sev and sev.get("signal") == "sell":
            top_rows.append(("賣", sym, sev, "red"))
        if bev and bev.get("signal") == "buy" and bev.get("score", 0) >= 70:
            top_rows.append(("買", sym, bev, "green"))
    top_rows.sort(key=lambda r: -r[2].get("score", 0))
    if top_rows:
        console.print()
        console.rule("[bold]🔥 Top 3 即時推薦[/]")
        t = Table(show_header=True, header_style="bold cyan")
        t.add_column("類型", justify="center")
        t.add_column("Symbol")
        t.add_column("現價", justify="right")
        t.add_column("分數", justify="right")
        t.add_column("說明")
        for typ, sym, ev, cls in top_rows[:3]:
            t.add_row(
                f"[{cls}]{typ}[/{cls}]",
                f"[bold]{sym}[/bold]",
                f"{ev.get('current', 0):.2f}",
                f"[{cls}]{ev.get('score', 0)}[/{cls}]",
                ev.get("reason", "")[:60],
            )
        console.print(t)

    # ── 3. 持股明細 ───────────────────────────────────────────────
    console.print()
    console.rule("[bold]💼 持股明細[/]")
    if not holdings:
        console.print("  [dim]目前沒有持股[/dim]")
    else:
        ht = Table(show_header=True, header_style="bold cyan")
        ht.add_column("Symbol")
        ht.add_column("股數",     justify="right")
        ht.add_column("均買價",   justify="right")
        ht.add_column("現價",     justify="right")
        ht.add_column("損益 %",  justify="right")
        ht.add_column("盈虧",    justify="right")
        ht.add_column("建議",     justify="right")
        ht.add_column("說明")
        for sym, h in sorted(holdings.items()):
            sig = next((s for s in signals if s["symbol"] == sym), None)
            sev = sig.get("sell_eval") if sig else None
            cur = prices.get(sym, h.get("current_price", 0))
            avg = h.get("avg_cost", 0)
            shares = h.get("shares", 0)
            if avg > 0:
                pct = (cur - avg) / avg * 100
                pct_color = "green" if pct > 0 else ("red" if pct < 0 else "dim")
                pct_str = f"[{pct_color}]{pct:+.2f}%[/{pct_color}]"
                # 優先用 portfolio embed 的精確盈虧;parser 沒抓到才自算
                # (自算會因 avg/cur 已 round 而偏差幾塊到數十塊)
                pnl = h.get("pnl")
                if pnl is None:
                    pnl = shares * (cur - avg)
                pnl_color = "green" if pnl > 0 else ("red" if pnl < 0 else "dim")
                pnl_str = f"[{pnl_color}]{pnl:+,.2f}[/{pnl_color}]"
            else:
                pct_str = "─"
                pnl_str = "─"
            if sev:
                sig_name = sev["signal"].upper()
                sig_color = "red" if sev["signal"] == "sell" else (
                    "green" if sev["signal"] == "buy_more" else "dim"
                )
                sig_str = f"[{sig_color}]{sig_name} ({sev['score']})[/{sig_color}]"
                reason = sev.get("reason", "")[:50]
            else:
                sig_str = "─"
                reason = "─"
            ht.add_row(
                f"[bold]{sym}[/bold]",
                f"{int(shares):,}",
                f"{avg:.2f}" if avg else "─",
                f"{cur:.2f}",
                pct_str, pnl_str, sig_str, reason,
            )
        console.print(ht)

    # ── 4. 賣出建議(score ≥ 60 的持股) ──────────────────────────
    sell_candidates = [
        s for s in signals
        if (s.get("sell_eval") or {}).get("score", 0) >= 60
        and s["symbol"] in holdings
    ]
    sell_candidates.sort(key=lambda s: -s["sell_eval"]["score"])
    console.print()
    console.rule("[bold]🔴 賣出建議(score ≥ 60)[/]")
    if sell_candidates:
        st = Table(show_header=True, header_style="bold cyan")
        st.add_column("Symbol")
        st.add_column("持有",   justify="right")
        st.add_column("均買價", justify="right")
        st.add_column("現價",   justify="right")
        st.add_column("損益 %", justify="right")
        st.add_column("Score", justify="right")
        st.add_column("說明")
        for s in sell_candidates:
            ev = s["sell_eval"]
            h  = holdings[s["symbol"]]
            sc = ev["score"]
            cls = "red" if sc >= 80 else "yellow"
            pp = ev.get("profit_pct")
            if pp is None:
                pp_str = "─"
            else:
                pp_color = "green" if pp > 0 else ("red" if pp < 0 else "dim")
                pp_str = f"[{pp_color}]{pp:+.2f}%[/{pp_color}]"
            st.add_row(
                f"[bold]{s['symbol']}[/bold]",
                f"{int(h['shares']):,}",
                f"{h.get('avg_cost', 0):.2f}",
                f"{ev.get('current', 0):.2f}",
                pp_str,
                f"[{cls}]{sc}[/{cls}]",
                ev.get("reason", "")[:60],
            )
        console.print(st)
    elif not holdings:
        console.print("  [dim]目前沒有持股(賣出訊號需要持股才有意義)[/dim]")
    else:
        console.print("  [dim]目前沒有 score ≥ 60 的賣出訊號 — 可在「持股明細」看完整評估[/dim]")

    # ── 5. 買進建議 ──────────────────────────────────────────────
    buy_candidates = [
        s for s in signals
        if s.get("buy_eval") and s["buy_eval"].get("score", 0) >= 60
    ]
    buy_candidates.sort(key=lambda s: -s["buy_eval"]["score"])
    if buy_candidates:
        console.print()
        console.rule("[bold]🟢 買進建議(score ≥ 60)[/]")
        bt = Table(show_header=True, header_style="bold cyan")
        bt.add_column("Symbol")
        bt.add_column("現價",   justify="right")
        bt.add_column("短均",   justify="right")
        bt.add_column("長均",   justify="right")
        bt.add_column("Score", justify="right")
        bt.add_column("說明")
        for s in buy_candidates:
            ev = s["buy_eval"]
            sc = ev["score"]
            cls = "green" if sc >= 80 else "yellow"
            ms = ev.get("ma_short")
            ml = ev.get("ma_long")
            bt.add_row(
                f"[bold]{s['symbol']}[/bold]",
                f"{ev.get('current', 0):.2f}",
                f"{ms:.2f}" if ms else "─",
                f"{ml:.2f}" if ml else "─",
                f"[{cls}]{sc}[/{cls}]",
                ev.get("reason", "")[:60],
            )
        console.print(bt)
    elif signals:
        console.print()
        console.rule("[bold]🟢 買進建議[/]")
        console.print("  [dim]目前沒有 score ≥ 60 的買進機會(或樣本不足)[/dim]")

    # ── 6. 全部股票報價 ──────────────────────────────────────────
    console.print()
    console.rule(f"[bold]📋 全部股票({len(prices)} 支)[/]")
    if not prices:
        console.print("  [dim]沒有抓到任何股票[/dim]")
    else:
        at = Table(show_header=True, header_style="bold cyan")
        at.add_column("Symbol")
        at.add_column("現價",     justify="right")
        at.add_column("持股",     justify="right")
        at.add_column("樣本數",   justify="right")
        at.add_column("買 score", justify="right")
        at.add_column("賣 score", justify="right")
        for sym in sorted(prices.keys()):
            cur = prices[sym]
            sig = next((s for s in signals if s["symbol"] == sym), None)
            held = holdings.get(sym)
            if held:
                held_str = f"[green]{int(held['shares'])}[/green]"
            else:
                held_str = "[dim]─[/dim]"
            n = sig["n_samples"] if sig else 0
            buy_sc = sig["buy_eval"]["score"] if sig and sig.get("buy_eval") else None
            sell_sc = sig["sell_eval"]["score"] if sig and sig.get("sell_eval") else None
            if buy_sc is None:
                buy_str = "[dim]─[/dim]"
            elif buy_sc >= 70:
                buy_str = f"[green]{buy_sc}[/green]"
            elif buy_sc >= 60:
                buy_str = f"[yellow]{buy_sc}[/yellow]"
            else:
                buy_str = f"[dim]{buy_sc}[/dim]"
            if sell_sc is None:
                sell_str = "[dim]─[/dim]"
            elif sell_sc >= 65:
                sell_str = f"[red]{sell_sc}[/red]"
            else:
                sell_str = f"[dim]{sell_sc}[/dim]"
            at.add_row(
                f"[bold]{sym}[/bold]",
                f"{cur:.2f}",
                held_str,
                str(n),
                buy_str,
                sell_str,
            )
        console.print(at)

    # R = 立即重 poll(賣股後想馬上看到反應、或想 force refresh 都用這個)
    cmd = input("\n  按 Enter 返回 / R + Enter 立即重 poll: ").strip().lower()
    if cmd == "r":
        state.stock_force_poll = True
        print("  ✓ 已請求立即重 poll(30 秒內生效)")
        input("  按 Enter 繼續...")
