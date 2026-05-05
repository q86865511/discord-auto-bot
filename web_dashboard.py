"""
Web dashboard — 在 localhost 顯示 bot 即時狀態，可手機/筆電瀏覽。
不引入 FastAPI 等新套件，純 Python stdlib（http.server + json）就夠。

使用：
- main.py 在 main() 裡啟動 dashboard_loop()，跑在背景 thread
- 預設綁 127.0.0.1:8765；要從手機看可改 0.0.0.0（同 LAN 即可）
- 設定：config["dashboard"] = {"enabled": true, "host": "127.0.0.1", "port": 8765}
"""
from __future__ import annotations

import http.server
import json
import logging
import socketserver
import threading
import time
from datetime import datetime
from typing import Any


# ── HTML 頁面（單檔，AJAX 每 2 秒拉 /api/state）──────────────────────────────
_INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Discord Auto Bot Dashboard</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 16px;
    font-family: -apple-system, "Segoe UI", "Microsoft JhengHei", sans-serif;
    background: #0d1117; color: #e6edf3;
  }
  h1 { font-size: 18px; margin: 0 0 12px 0; color: #58a6ff; }
  .grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 12px;
  }
  .card {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 12px;
  }
  .card h2 {
    font-size: 14px; margin: 0 0 8px 0; color: #8b949e;
    text-transform: uppercase; letter-spacing: 0.5px;
  }
  .row { display: flex; justify-content: space-between; padding: 4px 0; }
  .row .label { color: #8b949e; }
  .row .value { font-weight: 600; font-variant-numeric: tabular-nums; }
  .green  { color: #3fb950; }
  .red    { color: #f85149; }
  .yellow { color: #d29922; }
  .dim    { color: #6e7681; }
  #status-bar {
    display: flex; align-items: center; gap: 8px;
    margin-bottom: 12px; padding: 8px 12px;
    background: #161b22; border-radius: 6px; font-size: 13px;
  }
  #status-dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: #3fb950; animation: pulse 2s infinite;
  }
  #status-dot.paused { background: #d29922; }
  #status-dot.dead   { background: #f85149; }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
  }
  .footer {
    margin-top: 16px; padding-top: 8px;
    border-top: 1px solid #30363d;
    color: #6e7681; font-size: 12px; text-align: center;
  }
  .history-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .history-table th, .history-table td {
    text-align: left; padding: 4px 6px;
    border-bottom: 1px solid #30363d;
  }
  .history-table th { color: #8b949e; font-weight: normal; }
  canvas { width: 100%; max-width: 100%; height: 200px; }
</style>
</head>
<body>
  <div id="status-bar">
    <div id="status-dot"></div>
    <strong>Discord Auto Bot</strong>
    <span id="status-text" class="dim">─</span>
    <span class="dim" style="margin-left: auto;">最後更新 <span id="updated">─</span></span>
  </div>

  <div class="grid">
    <div class="card">
      <h2>💰 餘額 / 損益</h2>
      <div class="row"><span class="label">目前餘額</span><span class="value" id="balance">─</span></div>
      <div class="row"><span class="label">起始餘額</span><span class="value" id="start_balance">─</span></div>
      <div class="row"><span class="label">本次盈虧</span><span class="value" id="diff">─</span></div>
      <div class="row"><span class="label">賭博淨收</span><span class="value" id="net_change">─</span></div>
      <div class="row"><span class="label">當前下注</span><span class="value" id="current_bet">─</span></div>
    </div>

    <div class="card">
      <h2>🎯 目標 / 停損</h2>
      <div class="row"><span class="label">目標進度</span><span class="value" id="goal">─</span></div>
      <div class="row"><span class="label">停損狀態</span><span class="value" id="loss_floor">─</span></div>
    </div>

    <div class="card">
      <h2>🎲 賭博統計</h2>
      <div class="row"><span class="label">總下注</span><span class="value" id="total_bets">─</span></div>
      <div class="row"><span class="label">勝 / 敗</span><span class="value" id="wins_losses">─</span></div>
      <div class="row"><span class="label">勝率</span><span class="value" id="win_rate">─</span></div>
      <div class="row"><span class="label">EV (期望值)</span><span class="value" id="ev">─</span></div>
      <div class="row"><span class="label">Kelly f*</span><span class="value" id="kelly">─</span></div>
    </div>

    <div class="card">
      <h2>⏰ 排程 / 事件</h2>
      <div class="row"><span class="label">/hourly 倒數</span><span class="value" id="hourly_next">─</span></div>
      <div class="row"><span class="label">/daily 倒數</span><span class="value" id="daily_next">─</span></div>
      <div class="row"><span class="label">貓娘狀態</span><span class="value" id="neko">─</span></div>
      <div class="row"><span class="label">/hourly 領取</span><span class="value" id="ev_hourly">0</span></div>
      <div class="row"><span class="label">/daily 領取</span><span class="value" id="ev_daily">0</span></div>
      <div class="row"><span class="label">轉帳 / 中大獎</span><span class="value" id="ev_transfer_bigwin">0 / 0</span></div>
    </div>

    <div class="card" style="grid-column: 1/-1;">
      <h2>📈 累計淨收 (最近 100 筆)</h2>
      <canvas id="chart" width="600" height="200"></canvas>
    </div>

    <div class="card" style="grid-column: 1/-1;">
      <h2>📋 最近 15 筆下注</h2>
      <table class="history-table">
        <thead>
          <tr><th>時間</th><th>下注</th><th>變動</th><th>餘額後</th><th>結果</th></tr>
        </thead>
        <tbody id="history-body"></tbody>
      </table>
    </div>
  </div>

  <div class="footer">
    自動刷新間隔: 2 秒 · 按 F5 強制刷新
  </div>

<script>
function fmt(n) {
  if (n === null || n === undefined) return "─";
  return n.toLocaleString();
}
function fmtSign(n) {
  if (n === null || n === undefined) return "─";
  const s = (n >= 0 ? "+" : "") + n.toLocaleString();
  return n >= 0 ? `<span class="green">${s}</span>` : `<span class="red">${s}</span>`;
}
function fmtRemaining(epoch) {
  if (!epoch) return "─";
  const r = Math.floor(epoch - Date.now()/1000);
  if (r <= 0) return '<span class="green">即將執行</span>';
  const h = Math.floor(r / 3600);
  const m = Math.floor((r % 3600) / 60);
  const s = r % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2,'0')}m`;
  if (m > 0) return `${m}m ${String(s).padStart(2,'0')}s`;
  return `${s}s`;
}

let chartHistory = [];

function drawChart() {
  const c = document.getElementById('chart');
  if (!c) return;
  const ctx = c.getContext('2d');
  const w = c.clientWidth, h = 200;
  c.width = w; c.height = h;

  if (chartHistory.length < 2) {
    ctx.fillStyle = '#6e7681';
    ctx.font = '12px sans-serif';
    ctx.fillText('資料不足（需要至少 2 筆下注）', 12, 24);
    return;
  }
  const nets = [];
  let cum = 0;
  chartHistory.forEach(r => { cum += r.change; nets.push(cum); });
  const min = Math.min(0, ...nets), max = Math.max(0, ...nets);
  const range = max - min || 1;
  const xStep = (w - 20) / (nets.length - 1);

  // baseline (y=0)
  const zeroY = 10 + (max / range) * (h - 30);
  ctx.strokeStyle = '#30363d';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(10, zeroY); ctx.lineTo(w - 10, zeroY); ctx.stroke();

  // line
  ctx.strokeStyle = nets[nets.length-1] >= 0 ? '#3fb950' : '#f85149';
  ctx.lineWidth = 2;
  ctx.beginPath();
  nets.forEach((v, i) => {
    const x = 10 + i * xStep;
    const y = 10 + ((max - v) / range) * (h - 30);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

async function refresh() {
  try {
    const r = await fetch('/api/state');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();

    // status dot
    const dot = document.getElementById('status-dot');
    dot.className = '';
    if (d.paused) { dot.classList.add('paused'); }
    if (!d.balance && d.dead_notified) { dot.classList.add('dead'); }
    document.getElementById('status-text').textContent =
      d.paused ? '已暫停' : (d.status || '運行中');

    document.getElementById('balance').textContent = fmt(d.balance);
    document.getElementById('start_balance').textContent = fmt(d.start_balance);
    document.getElementById('diff').innerHTML =
      (d.balance != null && d.start_balance != null)
      ? fmtSign(d.balance - d.start_balance) : '─';
    document.getElementById('net_change').innerHTML = fmtSign(d.net_change);
    document.getElementById('current_bet').textContent =
      d.current_bet ? fmt(d.current_bet) : '─';

    document.getElementById('goal').textContent = d.goal_str || '─';
    document.getElementById('loss_floor').textContent = d.loss_str || '─';

    document.getElementById('total_bets').textContent = fmt(d.total_bets);
    document.getElementById('wins_losses').innerHTML =
      `<span class="green">${d.wins}</span> / <span class="red">${d.losses}</span>`;
    document.getElementById('win_rate').textContent =
      (d.total_bets > 0 ? (d.wins / d.total_bets * 100).toFixed(1) : '0.0') + '%';
    document.getElementById('ev').innerHTML = d.ev_str || '─';
    document.getElementById('kelly').textContent = d.kelly_str || '─';

    document.getElementById('hourly_next').textContent = fmtRemaining(d.hourly_next);
    document.getElementById('daily_next').textContent = fmtRemaining(d.daily_next);
    document.getElementById('neko').textContent = d.neko_str || '─';

    const ev = d.events || {};
    document.getElementById('ev_hourly').textContent = ev.hourly_claims || 0;
    document.getElementById('ev_daily').textContent = ev.daily_claims || 0;
    document.getElementById('ev_transfer_bigwin').textContent =
      `${ev.transfers || 0} / ${ev.bigwins || 0}`;

    chartHistory = d.history_recent || [];
    drawChart();

    const tbody = document.getElementById('history-body');
    tbody.innerHTML = '';
    (d.history_last_15 || []).slice().reverse().forEach(r => {
      const tr = document.createElement('tr');
      const change = r.change || 0;
      const cls = change > 0 ? 'green' : (change < 0 ? 'red' : 'dim');
      tr.innerHTML = `
        <td class="dim">${r.ts || ''}</td>
        <td>${fmt(r.bet)}</td>
        <td class="${cls}">${(change >= 0 ? '+' : '') + change.toLocaleString()}</td>
        <td>${fmt(r.after)}</td>
        <td class="${cls}">${r.result || ''}</td>`;
      tbody.appendChild(tr);
    });

    document.getElementById('updated').textContent = new Date().toLocaleTimeString();
  } catch (err) {
    document.getElementById('status-text').innerHTML =
      '<span class="red">無法連線到 bot（' + err.message + '）</span>';
  }
}

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


def _build_state_snapshot(state: dict, config: dict) -> dict:
    """從 main.py 的 state + config 抽出 dashboard 需要的欄位（避免直接暴露整個 state）。"""
    gcfg = config.get("gambling", {}) or {}
    bal = state.get("balance")
    start = state.get("start_balance")
    goal = int(gcfg.get("goal", 0) or 0)

    # 目標字串
    if goal > 0 and isinstance(bal, int):
        pct = min(100.0, bal / goal * 100)
        goal_str = f"{bal:,} / {goal:,} ({pct:.1f}%)"
    elif goal > 0:
        goal_str = f"─ / {goal:,}"
    else:
        goal_str = "未設定"

    # 停損字串
    floor = int(gcfg.get("loss_floor", 0) or 0)
    if floor > 0 and isinstance(bal, int):
        if bal <= floor:
            loss_str = f"⚠ {bal:,} ≤ {floor:,}"
        else:
            loss_str = f"{bal:,} > {floor:,} (緩衝 +{bal - floor:,})"
    elif floor > 0:
        loss_str = f"─ > {floor:,}"
    else:
        loss_str = "未設定"

    # EV / Kelly（從 slot_analysis 算）
    ev_str = "─"
    kelly_str = "─"
    sa = state.get("slot_analysis") or {}
    n = sa.get("total_spins", 0)
    if n > 0:
        try:
            from slot_analysis import compute_slot_stats, MIN_KELLY_SAMPLES
            stats = compute_slot_stats(sa)
            edge_pct = stats["edge"] * 100
            ev_str = (f"{stats['ev']:.3f}x ({'+' if edge_pct >= 0 else ''}"
                      f"{edge_pct:.2f}%) n={n}")
            if stats.get("sufficient_data") and stats.get("kelly_fraction", 0) > 0:
                kf = stats["kelly_fraction"]
                kelly_str = f"{kf:.4f} (½={kf/2:.4f})"
            else:
                kelly_str = f"資料不足 (需 {MIN_KELLY_SAMPLES})"
        except Exception:
            pass

    # 貓娘
    neko_st = state.get("neko_status", "unknown")
    neko_dl = state.get("neko_deadline_ts")
    if neko_st == "dispatching":
        if neko_dl:
            r = max(0, int(neko_dl - time.time()))
            h, rem = divmod(r, 3600)
            m = rem // 60
            neko_str = f"派遣中 {h}h{m:02d}m" if h else f"派遣中 {m}m"
        else:
            neko_str = "派遣中"
    elif neko_st == "not_dispatching":
        neko_str = "閒置/待領取"
    else:
        neko_str = "─"

    # history slice
    history = state.get("history") or []
    history_last_15 = history[-15:]
    # 給折線圖用：最近 100 筆，只挑必要欄位
    history_recent = [
        {"change": r.get("change", 0)}
        for r in history[-100:]
    ]

    return {
        "ts":            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status":        state.get("status", "─"),
        "paused":        state.get("paused", False),
        "balance":       bal,
        "start_balance": start,
        "net_change":    state.get("net_change", 0),
        "current_bet":   state.get("current_bet", 0),
        "total_bets":    state.get("total_bets", 0),
        "wins":          state.get("wins", 0),
        "losses":        state.get("losses", 0),
        "goal_str":      goal_str,
        "loss_str":      loss_str,
        "ev_str":        ev_str,
        "kelly_str":     kelly_str,
        "hourly_next":   state.get("hourly_next"),
        "daily_next":    state.get("daily_next"),
        "neko_str":      neko_str,
        "events":        dict(state.get("events", {})),
        "dead_notified": state.get("dead_notified", False),
        "history_last_15": history_last_15,
        "history_recent":  history_recent,
    }


def _make_handler(state: dict, config_holder: list):
    """工廠函式 — 動態產出 BaseHTTPRequestHandler subclass，閉包進 state/config_holder。"""

    class DashboardHandler(http.server.BaseHTTPRequestHandler):
        # 抑制標準 logging（避免污染 stdout）；改用我們的 logger
        def log_message(self, format, *args):
            logging.getLogger("dashboard").debug(format, *args)

        def do_GET(self):  # noqa: N802 — 名稱由父類別決定
            if self.path == "/" or self.path == "/index.html":
                self._respond(200, "text/html; charset=utf-8", _INDEX_HTML.encode("utf-8"))
            elif self.path == "/api/state":
                try:
                    snap = _build_state_snapshot(state, config_holder[0])
                    body = json.dumps(snap, ensure_ascii=False, default=str).encode("utf-8")
                    self._respond(200, "application/json; charset=utf-8", body)
                except Exception as e:
                    err = json.dumps({"error": str(e)}).encode("utf-8")
                    self._respond(500, "application/json", err)
            else:
                self._respond(404, "text/plain", b"Not Found")

        def _respond(self, code: int, ctype: str, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

    return DashboardHandler


class _ReusableTCPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """允許 reload 時 port 立刻釋放、多 client 同時連。"""
    allow_reuse_address = True
    daemon_threads = True


def start_dashboard_thread(state: dict, config_holder: list,
                            host: str = "127.0.0.1", port: int = 8765
                            ) -> threading.Thread | None:
    """
    啟動 dashboard HTTP server 在背景 thread。
    回傳 Thread 物件（已 start），若綁 port 失敗則回傳 None。
    """
    log = logging.getLogger("dashboard")
    handler_cls = _make_handler(state, config_holder)
    try:
        server = _ReusableTCPServer((host, port), handler_cls)
    except OSError as e:
        log.warning("dashboard 啟動失敗（port %d 被占用？）: %s", port, e)
        return None

    def _serve():
        log.info("dashboard 啟動在 http://%s:%d/", host, port)
        try:
            server.serve_forever(poll_interval=0.5)
        except Exception as e:
            log.error("dashboard server 例外: %s", e)
        finally:
            server.server_close()
            log.info("dashboard 已關閉")

    t = threading.Thread(target=_serve, daemon=True, name="dashboard")
    t.start()
    # 暫存 server 實例到 thread，給呼叫端能 shutdown
    t._dashboard_server = server  # type: ignore[attr-defined]
    return t


def stop_dashboard_thread(t: threading.Thread | None):
    """關閉 dashboard server（讓 thread 跳出 serve_forever）。"""
    if t is None:
        return
    server = getattr(t, "_dashboard_server", None)
    if server is None:
        return
    try:
        server.shutdown()
    except Exception:
        pass
