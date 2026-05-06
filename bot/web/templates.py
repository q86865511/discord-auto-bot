"""Web dashboard 的 HTML / CSS / JS templates。

純 markup,沒有任何使用者資料(資料由前端 JS fetch /api/* 抓 + escape)。
從 dashboard.py 拆出來,讓那邊只剩 server lifecycle。

公開符號:
    OVERVIEW_BODY / ANALYSIS_BODY / CONTROL_BODY / LOGS_BODY  body templates
    NAV_ITEMS                                                 nav links
    html_shell(title, body, active)                           完整 HTML wrapper
    html_close()                                              </body></html>
    esc(s)                                                    HTML escape util
"""
from __future__ import annotations

from typing import Any

# ── HTML chrome ──────────────────────────────────────────────────────
CSS = """
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0;
  font-family: -apple-system, "Segoe UI", "Microsoft JhengHei", sans-serif;
  background: #0d1117; color: #e6edf3;
  min-height: 100vh;
}
header {
  background: #161b22; border-bottom: 1px solid #30363d;
  padding: 12px 16px; display: flex; align-items: center; gap: 12px;
  position: sticky; top: 0; z-index: 10;
}
header h1 { font-size: 18px; margin: 0; color: #58a6ff; }
nav {
  display: flex; gap: 4px; flex-wrap: wrap;
  margin-left: auto;
}
.nav-link {
  padding: 6px 12px; border-radius: 6px; text-decoration: none;
  color: #8b949e; font-size: 14px; transition: background 0.15s;
}
.nav-link:hover { background: #21262d; color: #e6edf3; }
.nav-link.active { background: #1f6feb; color: white; }
main { padding: 16px; }
h2.section { font-size: 14px; color: #8b949e; text-transform: uppercase;
  letter-spacing: 0.5px; margin: 20px 0 8px 0; }
.grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  /* 預設 align-items: stretch — 同一橫列的卡片自動切齊到該列最高一張的高度,
     列與列之間維持各自自然高度(不會把 chart card 也拉高) */
}
@media (max-width: 1100px) { .grid { grid-template-columns: repeat(3, 1fr); } }
@media (max-width: 900px)  { .grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 600px)  { .grid { grid-template-columns: 1fr; } }
.card {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 12px;
}
.card h3 {
  font-size: 14px; margin: 0 0 8px 0; color: #8b949e;
  text-transform: uppercase; letter-spacing: 0.5px;
}
.row { display: flex; justify-content: space-between; padding: 4px 0;
  border-bottom: 1px solid #21262d; }
.row:last-child { border-bottom: none; }
.row .label { color: #8b949e; font-size: 13px; }
.row .value { font-weight: 600; font-variant-numeric: tabular-nums; }
.green  { color: #3fb950; }
.red    { color: #f85149; }
.yellow { color: #d29922; }
.blue   { color: #58a6ff; }
.dim    { color: #6e7681; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
table th, table td {
  text-align: left; padding: 6px 8px;
  border-bottom: 1px solid #30363d;
  font-variant-numeric: tabular-nums;
}
table th { color: #8b949e; font-weight: normal; font-size: 12px;
  text-transform: uppercase; letter-spacing: 0.5px; }
table.right-align td:not(:first-child),
table.right-align th:not(:first-child) { text-align: right; }
@media (max-width: 600px) {
  table { font-size: 12px; }
  table th, table td { padding: 4px; }
}
#status-bar { display: flex; align-items: center; gap: 8px; font-size: 13px; }
#status-dot {
  width: 10px; height: 10px; border-radius: 50%;
  background: #3fb950; animation: pulse 2s infinite;
}
#status-dot.paused { background: #d29922; animation: none; }
#status-dot.dead   { background: #f85149; animation: none; }
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}
.footer { margin-top: 24px; padding-top: 12px;
  border-top: 1px solid #30363d;
  color: #6e7681; font-size: 12px; text-align: center; }
canvas { width: 100%; max-width: 100%; height: 250px; display: block; }
@media (max-width: 600px) { canvas { height: 180px; } }
.btn {
  background: #21262d; border: 1px solid #30363d; color: #e6edf3;
  padding: 10px 16px; border-radius: 6px; cursor: pointer;
  font-size: 14px; transition: all 0.15s;
}
.btn:hover { background: #30363d; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn.primary { background: #1f6feb; border-color: #1f6feb; }
.btn.primary:hover { background: #388bfd; }
.btn.danger  { background: #da3633; border-color: #da3633; }
.btn.danger:hover { background: #f85149; }
.btn.warning { background: #9e6a03; border-color: #9e6a03; }
.btn-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
.field { margin: 8px 0; }
.field label { display: block; color: #8b949e; font-size: 12px;
  margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.5px; }
.field input, .field select {
  width: 100%; padding: 6px 10px; background: #0d1117;
  border: 1px solid #30363d; border-radius: 4px;
  color: #e6edf3; font-size: 14px;
}
.field input:focus, .field select:focus {
  outline: none; border-color: #1f6feb;
}
.field .err { color: #f85149; font-size: 12px; margin-top: 4px; }
.toast {
  position: fixed; bottom: 20px; right: 20px;
  padding: 10px 16px; border-radius: 6px;
  background: #1f6feb; color: white;
  transform: translateY(100px); opacity: 0;
  transition: all 0.3s;
  max-width: 80%;
}
.toast.show { transform: translateY(0); opacity: 1; }
.toast.error { background: #da3633; }
""".strip()


NAV_ITEMS = [
    ("overview", "/",         "📊 概覽"),
    ("analysis", "/analysis", "🎯 拉霸分析"),
    ("logs",     "/logs",     "📋 即時日誌"),
    ("control",  "/control",  "🛠️ 系統設定"),
]


def esc(s: Any) -> str:
    """HTML escape;非字串轉成字串。None 變空字串。"""
    if s is None:
        return ""
    text = str(s)
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
                .replace("'", "&#39;"))


def html_shell(title: str, body_html: str, active: str = "overview") -> str:
    """完整 HTML 頁面 wrapper(含 nav + status bar + toast 容器)。"""
    nav_html = "".join(
        f'<a href="{href}" class="nav-link{" active" if key == active else ""}">{label}</a>'
        for key, href, label in NAV_ITEMS
    )
    # 注意:body_html 內容由 caller 確保安全(各頁 body 是寫死的 template,不含使用者資料)
    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)} — Discord Auto Bot</title>
<style>{CSS}</style>
</head>
<body>
  <header>
    <h1>🤖 Discord Auto Bot</h1>
    <div id="status-bar">
      <div id="status-dot"></div>
      <span id="status-text" class="dim">─</span>
    </div>
    <nav>{nav_html}</nav>
  </header>
  <main>{body_html}</main>
  <div id="toast" class="toast"></div>
<script>
function escapeHtml(s) {{
  if (s == null) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}}
function showToast(msg, isError) {{
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show' + (isError ? ' error' : '');
  setTimeout(() => t.className = 'toast', 2500);
}}
async function refreshStatus() {{
  try {{
    const r = await fetch('/api/state');
    const d = await r.json();
    const dot = document.getElementById('status-dot');
    dot.className = '';
    if (d.paused) dot.classList.add('paused');
    if (d.dead_notified) dot.classList.add('dead');
    const t = document.getElementById('status-text');
    t.textContent = d.paused ? '已暫停' : (d.status || '運行中');
  }} catch(e) {{
    document.getElementById('status-text').textContent = '無法連線到 bot';
    document.getElementById('status-text').className = 'red';
  }}
}}
refreshStatus();
setInterval(refreshStatus, 5000);
</script>
"""


def html_close() -> str:
    return "</body></html>"


# ── 頁面 body templates(純 markup,不含使用者資料) ────────────────
OVERVIEW_BODY = r"""
<div class="grid">
  <div class="card">
    <h3>💰 餘額 / 損益</h3>
    <div class="row"><span class="label">目前餘額</span><span class="value" id="balance">─</span></div>
    <div class="row"><span class="label">起始餘額</span><span class="value" id="start_balance">─</span></div>
    <div class="row"><span class="label">本次盈虧</span><span class="value" id="diff">─</span></div>
    <div class="row"><span class="label">賭博淨收</span><span class="value" id="net_change">─</span></div>
    <div class="row"><span class="label">當前下注</span><span class="value" id="current_bet">─</span></div>
  </div>
  <div class="card">
    <h3>🎯 目標 / 停損</h3>
    <div class="row"><span class="label">目標進度</span><span class="value" id="goal">─</span></div>
    <div class="row"><span class="label">停損狀態</span><span class="value" id="loss_floor">─</span></div>
  </div>
  <div class="card">
    <h3>🎲 賭博統計</h3>
    <div class="row"><span class="label">總下注</span><span class="value" id="total_bets">─</span></div>
    <div class="row"><span class="label">勝 / 敗</span><span class="value" id="wins_losses">─</span></div>
    <div class="row"><span class="label">勝率</span><span class="value" id="win_rate">─</span></div>
    <div class="row"><span class="label">連勝紀錄</span><span class="value" id="streak">─</span></div>
    <div class="row"><span class="label">平均時薪</span><span class="value" id="profit_per_hour">─</span></div>
    <div class="row"><span class="label">EV (期望值)</span><span class="value" id="ev">─</span></div>
    <div class="row"><span class="label">Kelly f*</span><span class="value" id="kelly">─</span></div>
  </div>
  <div class="card">
    <h3>⏰ 排程 / 事件</h3>
    <div class="row"><span class="label">/hourly 倒數</span><span class="value" id="hourly_next">─</span></div>
    <div class="row"><span class="label">/daily 倒數</span><span class="value" id="daily_next">─</span></div>
    <div class="row"><span class="label">貓娘狀態</span><span class="value" id="neko">─</span></div>
    <div class="row"><span class="label">/hourly 領取</span><span class="value" id="ev_hourly">0</span></div>
    <div class="row"><span class="label">/daily 領取</span><span class="value" id="ev_daily">0</span></div>
    <div class="row"><span class="label">轉帳 / 中大獎</span><span class="value" id="ev_transfer_bigwin">0 / 0</span></div>
  </div>
  <div class="card" style="grid-column: 1/-1;">
    <h3>📈 累計淨收 (最近 100 筆)</h3>
    <canvas id="chart" width="600" height="250"></canvas>
  </div>
  <div class="card" style="grid-column: 1/-1;">
    <h3>📋 最近 15 筆下注</h3>
    <table class="right-align">
      <thead><tr><th>時間</th><th>下注</th><th>變動</th><th>餘額後</th><th>結果</th></tr></thead>
      <tbody id="history-body"></tbody>
    </table>
  </div>
</div>
<div class="footer">自動刷新 2 秒</div>
<script>
function fmt(n) { if (n == null) return '─'; return Number(n).toLocaleString(); }
function fmtSign(n) {
  if (n == null) return '─';
  const s = (n >= 0 ? '+' : '') + Number(n).toLocaleString();
  const cls = n >= 0 ? 'green' : 'red';
  const span = document.createElement('span');
  span.className = cls;
  span.textContent = s;
  return span;
}
function fmtRemaining(epoch) {
  if (!epoch) return '─';
  const r = Math.floor(epoch - Date.now()/1000);
  if (r <= 0) return '即將執行';
  const h = Math.floor(r / 3600);
  const m = Math.floor((r % 3600) / 60);
  const s = r % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2,'0')}m`;
  if (m > 0) return `${m}m ${String(s).padStart(2,'0')}s`;
  return `${s}s`;
}
function setSign(id, n) {
  const el = document.getElementById(id);
  el.textContent = '';
  if (n == null) { el.textContent = '─'; return; }
  el.appendChild(fmtSign(n));
}

let chartHistory = [];
function drawChart() {
  const c = document.getElementById('chart');
  if (!c) return;
  const ctx = c.getContext('2d');
  const w = c.clientWidth || 600, h = c.clientHeight || 250;
  c.width = w; c.height = h;
  const padL = 60, padR = 12, padT = 16, padB = 24;
  const innerW = w - padL - padR;
  const innerH = h - padT - padB;
  if (chartHistory.length < 2) {
    ctx.fillStyle = '#6e7681';
    ctx.font = '13px sans-serif';
    ctx.fillText('資料不足(需要至少 2 筆下注)', padL + 10, padT + 24);
    return;
  }
  const nets = [];
  let cum = 0;
  chartHistory.forEach(r => { cum += r.change; nets.push(cum); });
  const minN = Math.min(0, ...nets), maxN = Math.max(0, ...nets);
  const nice = niceTicks(minN, maxN, 5);
  const yMin = nice.min, yMax = nice.max;
  const range = yMax - yMin || 1;
  const xScale = (i) => padL + (i / Math.max(1, nets.length - 1)) * innerW;
  const yScale = (v) => padT + ((yMax - v) / range) * innerH;
  ctx.strokeStyle = '#21262d';
  ctx.lineWidth = 1;
  ctx.fillStyle = '#6e7681';
  ctx.font = '11px sans-serif';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  nice.ticks.forEach(v => {
    const y = yScale(v);
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(w - padR, y);
    ctx.stroke();
    const label = (v >= 0 ? '+' : '') + Math.round(v).toLocaleString();
    ctx.fillText(label, padL - 6, y);
  });
  if (yMin < 0 && yMax > 0) {
    const zy = yScale(0);
    ctx.strokeStyle = '#484f58';
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.moveTo(padL, zy); ctx.lineTo(w - padR, zy); ctx.stroke();
  }
  ctx.fillStyle = '#6e7681';
  ctx.textAlign = 'left';
  ctx.fillText('1', padL, h - padB / 2);
  ctx.textAlign = 'right';
  ctx.fillText(`${nets.length}`, w - padR, h - padB / 2);
  ctx.strokeStyle = nets[nets.length - 1] >= 0 ? '#3fb950' : '#f85149';
  ctx.lineWidth = 2;
  ctx.beginPath();
  nets.forEach((v, i) => {
    const x = xScale(i);
    const y = yScale(v);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.fillStyle = ctx.strokeStyle;
  const lastX = xScale(nets.length - 1);
  const lastY = yScale(nets[nets.length - 1]);
  ctx.beginPath();
  ctx.arc(lastX, lastY, 3.5, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = '#e6edf3';
  ctx.textAlign = 'right';
  ctx.font = 'bold 12px sans-serif';
  const lastLabel = (nets[nets.length-1] >= 0 ? '+' : '') +
    nets[nets.length-1].toLocaleString();
  ctx.fillText(lastLabel, lastX - 8, lastY - 8);
}
function niceTicks(min, max, count) {
  const range = max - min || 1;
  const rough = range / count;
  const mag = Math.pow(10, Math.floor(Math.log10(rough)));
  let step;
  const norm = rough / mag;
  if      (norm < 1.5) step = 1 * mag;
  else if (norm < 3)   step = 2 * mag;
  else if (norm < 7)   step = 5 * mag;
  else                 step = 10 * mag;
  const niceMin = Math.floor(min / step) * step;
  const niceMax = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = niceMin; v <= niceMax + 1e-9; v += step) ticks.push(v);
  return { min: niceMin, max: niceMax, step, ticks };
}
async function refresh() {
  try {
    const r = await fetch('/api/state');
    const d = await r.json();
    document.getElementById('balance').textContent = fmt(d.balance);
    document.getElementById('start_balance').textContent = fmt(d.start_balance);
    if (d.balance != null && d.start_balance != null) {
      setSign('diff', d.balance - d.start_balance);
    } else {
      document.getElementById('diff').textContent = '─';
    }
    setSign('net_change', d.net_change);
    document.getElementById('current_bet').textContent = d.current_bet ? fmt(d.current_bet) : '─';
    document.getElementById('goal').textContent = d.goal_str || '─';
    document.getElementById('loss_floor').textContent = d.loss_str || '─';
    document.getElementById('total_bets').textContent = fmt(d.total_bets);
    const wl = document.getElementById('wins_losses');
    wl.textContent = '';
    const sw = document.createElement('span'); sw.className = 'green'; sw.textContent = String(d.wins);
    const sl = document.createElement('span'); sl.className = 'red';   sl.textContent = String(d.losses);
    wl.appendChild(sw); wl.append(' / '); wl.appendChild(sl);
    document.getElementById('win_rate').textContent =
      (d.total_bets > 0 ? (d.wins / d.total_bets * 100).toFixed(1) : '0.0') + '%';
    // streak / pph / ev / kelly:後端只回 plain-text,前端組裝 + escape
    document.getElementById('streak').textContent = d.streak_str || '─';
    document.getElementById('profit_per_hour').textContent = d.pph_str || '─';
    document.getElementById('ev').textContent = d.ev_str || '─';
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
      const tdTs   = document.createElement('td'); tdTs.className   = 'dim'; tdTs.textContent = r.ts || '';
      const tdBet  = document.createElement('td'); tdBet.textContent  = fmt(r.bet);
      const tdCh   = document.createElement('td'); tdCh.className    = cls;
      tdCh.textContent = (change >= 0 ? '+' : '') + change.toLocaleString();
      const tdAft  = document.createElement('td'); tdAft.textContent  = fmt(r.after);
      const tdRes  = document.createElement('td'); tdRes.className   = cls; tdRes.textContent = r.result || '';
      tr.appendChild(tdTs); tr.appendChild(tdBet); tr.appendChild(tdCh);
      tr.appendChild(tdAft); tr.appendChild(tdRes);
      tbody.appendChild(tr);
    });
  } catch (err) { /* status bar */ }
}
refresh();
setInterval(refresh, 2000);
window.addEventListener('resize', drawChart);
</script>
"""


ANALYSIS_BODY = r"""
<div class="grid">
  <div class="card" style="grid-column: 1/-1;">
    <h3>📊 基本統計</h3>
    <div class="row"><span class="label">總旋轉次數</span><span class="value" id="total_spins">─</span></div>
    <div class="row"><span class="label">勝率</span><span class="value" id="win_rate">─</span></div>
    <div class="row"><span class="label">期望值 (EV)</span><span class="value" id="ev">─</span></div>
    <div class="row"><span class="label">邊際</span><span class="value" id="edge">─</span></div>
    <div class="row"><span class="label">標準差</span><span class="value" id="std_dev">─</span></div>
    <div class="row"><span class="label">變異數</span><span class="value" id="variance">─</span></div>
    <div class="row"><span class="label">Kelly f*</span><span class="value" id="kelly">─</span></div>
  </div>
  <div class="card" style="grid-column: 1/-1;">
    <h3>📉 Drawdown</h3>
    <div class="row"><span class="label">歷史峰值</span><span class="value" id="dd_peak">─</span></div>
    <div class="row"><span class="label">當前累計淨收</span><span class="value" id="dd_cur">─</span></div>
    <div class="row"><span class="label">最大跌幅</span><span class="value" id="dd_max">─</span></div>
    <div class="row"><span class="label">當前距峰值</span><span class="value" id="dd_now">─</span></div>
  </div>
  <div class="card" style="grid-column: 1/-1;">
    <h3>📈 賠率分布</h3>
    <table class="right-align" id="dist-table">
      <thead><tr><th>區間</th><th>次數</th><th>比例</th><th>分布</th><th>實際賠率</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>
  <div class="card" style="grid-column: 1/-1;">
    <h3>🎯 符號統計</h3>
    <table class="right-align" id="sym-table">
      <thead><tr><th>符號</th><th>中獎次數</th><th>平均倍率</th><th>累計賠付</th><th>回收率</th><th>格子機率</th></tr></thead>
      <tbody></tbody>
    </table>
    <div id="noise-msg" class="dim" style="margin-top: 8px; font-size: 12px;"></div>
  </div>
  <div class="card" style="grid-column: 1/-1;">
    <h3>📐 線路統計</h3>
    <table class="right-align" id="line-table">
      <thead><tr><th>線路</th><th>命中次數</th><th>命中率</th><th>總賠付</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>
</div>
<script>
function escapeHtml(s){if(s==null)return'';return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
function fmtPct(p) { return (p * 100).toFixed(1) + '%'; }
function fmtMul(p) { return p.toFixed(4) + 'x'; }
function appendCell(tr, text, cls) {
  const td = document.createElement('td');
  if (cls) td.className = cls;
  td.textContent = text;
  tr.appendChild(td);
}
async function refresh() {
  try {
    const r = await fetch('/api/analysis');
    const d = await r.json();
    if (!d.has_data) {
      document.getElementById('total_spins').textContent = '0';
      return;
    }
    document.getElementById('total_spins').textContent = d.total_spins.toLocaleString();
    document.getElementById('win_rate').textContent = fmtPct(d.win_rate);
    document.getElementById('ev').textContent = fmtMul(d.ev);
    const edge = d.edge;
    const edgeEl = document.getElementById('edge');
    edgeEl.className = 'value ' + (edge >= 0 ? 'green' : 'red');
    edgeEl.textContent = (edge >= 0 ? '+' : '') + (edge*100).toFixed(2) + '%';
    document.getElementById('std_dev').textContent = d.std_dev.toFixed(4);
    document.getElementById('variance').textContent = d.variance.toFixed(4);
    document.getElementById('kelly').textContent = d.kelly_str;

    const dd = d.drawdown || {};
    document.getElementById('dd_peak').textContent = (dd.peak >= 0 ? '+' : '') + (dd.peak||0).toLocaleString();
    document.getElementById('dd_cur').textContent  = (dd.current_net >= 0 ? '+' : '') + (dd.current_net||0).toLocaleString();
    document.getElementById('dd_max').textContent  = (dd.max_drawdown||0).toLocaleString();
    document.getElementById('dd_now').textContent  = (dd.current_drawdown||0).toLocaleString();

    const distBody = document.querySelector('#dist-table tbody');
    distBody.innerHTML = '';
    d.payout_distribution.forEach(row => {
      const pct = row.pct;
      const barLen = Math.min(40, Math.floor(pct / 2));
      const bar = '█'.repeat(barLen);
      const tr = document.createElement('tr');
      appendCell(tr, row.bucket);
      appendCell(tr, row.count.toLocaleString());
      appendCell(tr, pct.toFixed(1) + '%');
      appendCell(tr, bar);
      appendCell(tr, row.actual || '');
      distBody.appendChild(tr);
    });

    const symBody = document.querySelector('#sym-table tbody');
    symBody.innerHTML = '';
    let hidden = 0;
    d.symbols.forEach(row => {
      if (row.hidden) { hidden++; return; }
      const tr = document.createElement('tr');
      appendCell(tr, row.display);
      appendCell(tr, row.wins > 0 ? row.wins.toLocaleString() : '─');
      appendCell(tr, row.wins > 0 ? row.avg_mult.toFixed(2) + 'x' : '─');
      appendCell(tr, row.wins > 0 ? row.total_payout.toLocaleString() : '─');
      appendCell(tr, row.wins > 0 ? (row.recover_rate * 100).toFixed(1) + '%' : '─');
      appendCell(tr, row.grid_prob != null ? (row.grid_prob * 100).toFixed(1) + '%' : '─');
      symBody.appendChild(tr);
    });
    document.getElementById('noise-msg').textContent =
      hidden > 0 ? `(已隱藏 ${hidden} 個雜訊符號:未中獎且格子機率 < 0.1%)` : '';

    const lineBody = document.querySelector('#line-table tbody');
    lineBody.innerHTML = '';
    d.lines.forEach(row => {
      const tr = document.createElement('tr');
      appendCell(tr, row.line_name);
      appendCell(tr, row.hits.toLocaleString());
      appendCell(tr, (row.hit_rate*100).toFixed(1) + '%');
      appendCell(tr, row.total_payout.toLocaleString());
      lineBody.appendChild(tr);
    });
  } catch (err) { console.error(err); }
}
refresh();
setInterval(refresh, 5000);
</script>
"""


CONTROL_BODY = r"""
<div id="errors" class="dim" style="margin-bottom: 8px;"></div>
<div class="grid">
  <div class="card">
    <h3>⚡ 快速動作</h3>
    <div class="btn-row">
      <button class="btn primary" onclick="doAction('toggle_pause', this)">⏸️ 暫停 / 恢復</button>
    </div>
    <div class="btn-row">
      <button class="btn warning" onclick="confirmAction('reset_analysis', '確定要重置 slot 分析資料?', this)">🔄 重置 Slot 分析</button>
    </div>
    <div class="btn-row">
      <button class="btn danger" onclick="confirmAction('restart', '確定要重啟程式?', this)">🔁 重啟程式</button>
    </div>
  </div>

  <div class="card">
    <h3>🎰 賭博設定</h3>
    <div class="field">
      <label>啟用 / 停用</label>
      <select id="cfg-gambling-enabled" onchange="markDirty()">
        <option value="true">啟用</option>
        <option value="false">停用</option>
      </select>
    </div>
    <div class="field">
      <label>策略</label>
      <select id="cfg-gambling-strategy" onchange="markDirty()">
        <option value="auto">auto(按比例)</option>
        <option value="fixed">fixed(固定 min_bet)</option>
        <option value="kelly">kelly(依 EV 動態)</option>
      </select>
    </div>
    <div class="field">
      <label>保底門檻(餘額低於此就停止下注)</label>
      <input type="number" min="0" id="cfg-gambling-threshold" oninput="markDirty()">
    </div>
    <div class="field">
      <label>最小下注</label>
      <input type="number" min="1" id="cfg-gambling-min_bet" oninput="markDirty()">
    </div>
    <div class="field">
      <label>最大下注(0 = 自動)</label>
      <input type="number" min="0" id="cfg-gambling-max_bet" oninput="markDirty()">
    </div>
    <div class="field">
      <label>押注比例(0~1)</label>
      <input type="number" step="0.01" min="0" max="1" id="cfg-gambling-bet_fraction" oninput="markDirty()">
    </div>
  </div>

  <div class="card">
    <h3>🏁 目標 / 停損</h3>
    <div class="field">
      <label>目標餘額(0 = 不設)</label>
      <input type="number" min="0" id="cfg-gambling-goal" oninput="markDirty()">
    </div>
    <div class="field">
      <label>達標行為</label>
      <select id="cfg-gambling-goal_action" onchange="markDirty()">
        <option value="pause">pause</option>
        <option value="raise">raise</option>
      </select>
    </div>
    <div class="field">
      <label>停損點(0 = 不設)</label>
      <input type="number" min="0" id="cfg-gambling-loss_floor" oninput="markDirty()">
    </div>
    <div class="field">
      <label>停損行為</label>
      <select id="cfg-gambling-loss_action" onchange="markDirty()">
        <option value="pause">pause</option>
        <option value="lower_threshold">lower_threshold</option>
      </select>
    </div>
  </div>

  <div class="card">
    <h3>💸 自動轉帳</h3>
    <div class="field">
      <label>啟用</label>
      <select id="cfg-transfer-enabled" onchange="markDirty()">
        <option value="true">啟用</option>
        <option value="false">停用</option>
      </select>
    </div>
    <div class="field">
      <label>對象(顯示名稱片段或 user ID)</label>
      <input type="text" maxlength="200" id="cfg-transfer-target" oninput="markDirty()">
    </div>
    <div class="field">
      <label>金額</label>
      <input type="number" min="0" id="cfg-transfer-amount" oninput="markDirty()">
    </div>
    <div class="field">
      <label>間距(分鐘)</label>
      <input type="number" min="1" id="cfg-transfer-interval_min" oninput="markDirty()">
    </div>
  </div>
</div>

<div style="margin-top: 16px; text-align: center;">
  <button class="btn primary" onclick="saveConfig()" id="save-btn">💾 儲存設定</button>
  <button class="btn" onclick="loadConfig()">↺ 重新載入</button>
  <span id="dirty-indicator" class="dim" style="margin-left: 12px;"></span>
</div>

<div class="footer">
  ⚠ 出於安全考量,Dashboard 不開放修改密碼 / Discord ID / 監聽位址等敏感欄位。請於主程式 UI(C 鍵)修改。
</div>

<script>
let dirty = false;
function markDirty() {
  dirty = true;
  const i = document.getElementById('dirty-indicator');
  i.textContent = '● 未儲存'; i.className = 'yellow';
}
function clearDirty() {
  dirty = false;
  document.getElementById('dirty-indicator').textContent = '';
}
async function doAction(name, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/action/' + encodeURIComponent(name), {
      method: 'POST',
      headers: {'X-Requested-With': 'fetch'},
    });
    const d = await r.json();
    showToast(d.message || '完成', !d.ok);
    if (name === 'toggle_pause') setTimeout(refreshStatus, 100);
  } catch(e) { showToast('動作失敗: ' + e.message, true); }
  finally { if (btn) setTimeout(()=>{ btn.disabled = false; }, 800); }
}
function confirmAction(name, msg, btn) {
  if (confirm(msg)) doAction(name, btn);
}
function setVal(id, v) {
  const el = document.getElementById(id);
  if (!el) return;
  if (typeof v === 'boolean') el.value = v ? 'true' : 'false';
  else if (v != null) el.value = v;
}
function getVal(id, type) {
  const el = document.getElementById(id);
  if (!el) return undefined;
  const v = el.value;
  if (v === '' || v == null) return null;
  if (type === 'bool') return v === 'true';
  if (type === 'int')  { const n = parseInt(v, 10); return isNaN(n) ? null : n; }
  if (type === 'float') { const n = parseFloat(v); return isNaN(n) ? null : n; }
  return v;
}
async function loadConfig() {
  try {
    const r = await fetch('/api/config');
    const c = await r.json();
    const g = c.gambling || {};
    setVal('cfg-gambling-enabled',      !!g.enabled);
    setVal('cfg-gambling-strategy',     g.strategy || 'auto');
    setVal('cfg-gambling-threshold',    g.threshold);
    setVal('cfg-gambling-min_bet',      g.min_bet);
    setVal('cfg-gambling-max_bet',      g.max_bet);
    setVal('cfg-gambling-bet_fraction', g.bet_fraction);
    setVal('cfg-gambling-goal',         g.goal);
    setVal('cfg-gambling-goal_action',  g.goal_action || 'pause');
    setVal('cfg-gambling-loss_floor',   g.loss_floor);
    setVal('cfg-gambling-loss_action',  g.loss_action || 'pause');
    const t = c.transfer || {};
    setVal('cfg-transfer-enabled',      !!t.enabled);
    setVal('cfg-transfer-target',       t.target || '');
    setVal('cfg-transfer-amount',       t.amount);
    setVal('cfg-transfer-interval_min', t.interval_min);
    clearDirty();
  } catch(e) { showToast('載入失敗: ' + e.message, true); }
}
async function saveConfig() {
  document.getElementById('errors').innerHTML = '';
  const payload = {
    gambling: {
      enabled:      getVal('cfg-gambling-enabled', 'bool'),
      strategy:     getVal('cfg-gambling-strategy'),
      threshold:    getVal('cfg-gambling-threshold', 'int'),
      min_bet:      getVal('cfg-gambling-min_bet', 'int'),
      max_bet:      getVal('cfg-gambling-max_bet', 'int'),
      bet_fraction: getVal('cfg-gambling-bet_fraction', 'float'),
      goal:         getVal('cfg-gambling-goal', 'int'),
      goal_action:  getVal('cfg-gambling-goal_action'),
      loss_floor:   getVal('cfg-gambling-loss_floor', 'int'),
      loss_action:  getVal('cfg-gambling-loss_action'),
    },
    transfer: {
      enabled:      getVal('cfg-transfer-enabled', 'bool'),
      target:       getVal('cfg-transfer-target'),
      amount:       getVal('cfg-transfer-amount', 'int'),
      interval_min: getVal('cfg-transfer-interval_min', 'int'),
    },
  };
  const btn = document.getElementById('save-btn');
  btn.disabled = true;
  try {
    const r = await fetch('/api/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Requested-With': 'fetch'},
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (d.ok) {
      showToast('設定已儲存');
      clearDirty();
      if (d.warnings && d.warnings.length) {
        document.getElementById('errors').innerHTML =
          '⚠ 驗證警告(已儲存,但可能影響行為):<br>' +
          d.warnings.map(escapeHtml).join('<br>');
      }
    } else {
      showToast(d.message || '儲存失敗', true);
      if (d.errors && d.errors.length) {
        document.getElementById('errors').innerHTML =
          '❌ 驗證錯誤(未儲存):<br>' + d.errors.map(escapeHtml).join('<br>');
      }
    }
  } catch(e) { showToast('儲存失敗: ' + e.message, true); }
  finally { btn.disabled = false; }
}
loadConfig();
</script>
"""


LOGS_BODY = r"""
<div class="card">
  <div style="display: flex; justify-content: space-between; align-items: center;">
    <h3 style="margin: 0;">📋 Bot Log(最近 200 行,密碼/token 自動遮罩)</h3>
    <div>
      <label class="dim" style="font-size: 12px;">
        <input type="checkbox" id="autoscroll" checked> 自動捲到底
      </label>
      <button class="btn" onclick="refreshLogs()" style="margin-left: 8px;">↻ 手動刷新</button>
    </div>
  </div>
  <pre id="log-content" style="background: #010409; padding: 12px;
       border-radius: 6px; margin: 12px 0 0 0; max-height: 70vh;
       overflow-y: auto; font-size: 12px; line-height: 1.4;
       white-space: pre-wrap; word-break: break-all;
       color: #c9d1d9;"></pre>
</div>
<script>
function escapeHtml(s){if(s==null)return'';return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
async function refreshLogs() {
  try {
    const r = await fetch('/api/logs');
    const d = await r.json();
    const pre = document.getElementById('log-content');
    if (d.error) { pre.textContent = '錯誤: ' + d.error; return; }
    pre.innerHTML = '';
    (d.lines || []).forEach(line => {
      const span = document.createElement('span');
      span.textContent = line;
      if (line.includes('[ERROR]')) span.style.color = '#f85149';
      else if (line.includes('[WARNING]')) span.style.color = '#d29922';
      else if (line.includes('[DEBUG]')) span.style.color = '#6e7681';
      if (line.includes('🎰') || line.includes('中大獎')) {
        span.style.color = '#3fb950'; span.style.fontWeight = '600';
      } else if (line.includes('⛔') || line.includes('停損')) {
        span.style.color = '#f85149'; span.style.fontWeight = '600';
      } else if (line.includes('🐱') || line.includes('貓娘')) {
        span.style.color = '#a371f7';
      }
      pre.appendChild(span);
      pre.appendChild(document.createTextNode('\n'));
    });
    if (document.getElementById('autoscroll').checked) {
      pre.scrollTop = pre.scrollHeight;
    }
  } catch(e) {
    document.getElementById('log-content').textContent = '無法載入 log: ' + e.message;
  }
}
refreshLogs();
setInterval(refreshLogs, 3000);
</script>
"""
