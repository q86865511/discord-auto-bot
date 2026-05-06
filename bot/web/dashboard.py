"""Web dashboard — 在 localhost (或 LAN) 顯示 bot 即時狀態 + 控制台。

安全強化(v2):
- HTTP Basic Auth 用 hmac.compare_digest 比對(防 timing attack)
- 0.0.0.0 + 無密碼 = 啟動失敗(由 main 層在 wizard 已修正,這裡再做最後保護)
- /api/config POST 走 schema 驗證(BotConfig.validate),拒絕不合理值
- /api/logs 過濾 password / token / secret 等敏感字
- HTML 主體經 escape;後端不再回傳含 HTML markup 的 string 欄位
- 加 CSRF 保護:所有 POST 必須有 Origin/Referer 與 Host 同源

純 Python stdlib(http.server + json),零外部依賴。
"""
from __future__ import annotations

import hmac
import http.server
import json
import logging
import os
import re
import socket
import socketserver
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from bot.core.constants import (
    HIGH_MULT_THRESHOLD,
    LOG_FILE_PATH,
    MIN_KELLY_SAMPLES,
    PAYOUT_BUCKETS,
)
from bot.core.log_filter import redact_text

if TYPE_CHECKING:
    from bot.core.config import BotConfig
    from bot.core.db import Database
    from bot.core.state import BotState

log = logging.getLogger("dashboard")


# ── HTML chrome ──────────────────────────────────────────────────────
_CSS = """
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


_NAV_ITEMS = [
    ("overview", "/",         "📊 概覽"),
    ("analysis", "/analysis", "🎯 拉霸分析"),
    ("logs",     "/logs",     "📋 即時日誌"),
    ("control",  "/control",  "🛠️ 系統設定"),
]


def _esc(s: Any) -> str:
    """HTML escape;非字串轉成字串。None 變空字串。"""
    if s is None:
        return ""
    text = str(s)
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
                .replace("'", "&#39;"))


def _html_shell(title: str, body_html: str, active: str = "overview") -> str:
    nav_html = "".join(
        f'<a href="{href}" class="nav-link{" active" if key == active else ""}">{label}</a>'
        for key, href, label in _NAV_ITEMS
    )
    # 注意:body_html 內容由 caller 確保安全(各頁 body 是寫死的 template,不含使用者資料)
    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(title)} — Discord Auto Bot</title>
<style>{_CSS}</style>
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


def _html_close() -> str:
    return "</body></html>"


# ── 頁面 body templates(純 markup,不含使用者資料) ────────────────
_OVERVIEW_BODY = r"""
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


_ANALYSIS_BODY = r"""
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


_CONTROL_BODY = r"""
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


_LOGS_BODY = r"""
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


# ── Snapshot helpers ──────────────────────────────────────────────────
def _read_log_tail(path: str = LOG_FILE_PATH, max_lines: int = 200,
                   max_bytes: int = 256 * 1024) -> list[str]:
    """讀最後 N 行,redact 敏感字。"""
    try:
        if not os.path.exists(path):
            return []
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()[-max_lines:]
        return [redact_text(line) for line in lines]
    except OSError:
        return []


def _build_state_snapshot(state: "BotState", config: "BotConfig") -> dict:
    """組 /api/state 快照(plain-text 欄位,前端組裝 HTML)。"""
    gcfg = config.gambling
    bal = state.balance
    start = state.start_balance
    goal = int(gcfg.goal or 0)

    if goal > 0 and isinstance(bal, int):
        pct = min(100.0, bal / goal * 100)
        goal_str = f"{bal:,} / {goal:,} ({pct:.1f}%)"
    elif goal > 0:
        goal_str = f"─ / {goal:,}"
    else:
        goal_str = "未設定"

    floor = int(gcfg.loss_floor or 0)
    if floor > 0 and isinstance(bal, int):
        if bal <= floor:
            loss_str = f"⚠ {bal:,} ≤ {floor:,}"
        else:
            loss_str = f"{bal:,} > {floor:,} (緩衝 +{bal - floor:,})"
    elif floor > 0:
        loss_str = f"─ > {floor:,}"
    else:
        loss_str = "未設定"

    ev_str = "─"
    kelly_str = "─"
    sa = state.slot_analysis or {}
    n = sa.get("total_spins", 0)
    if n > 0:
        try:
            from bot.slot.analysis import compute_slot_stats
            stats = compute_slot_stats(sa)
            edge_pct = stats["edge"] * 100
            ev_str = (f"{stats['ev']:.3f}x ({'+' if edge_pct >= 0 else ''}"
                      f"{edge_pct:.2f}%) n={n}")
            if stats.get("sufficient_data") and stats.get("kelly_fraction", 0) > 0:
                kf = stats["kelly_fraction"]
                kelly_str = f"{kf:.4f} (½={kf/2:.4f})"
            else:
                kelly_str = f"資料不足 (需 {MIN_KELLY_SAMPLES})"
        except Exception:    # noqa: BLE001
            log.exception("compute_slot_stats 失敗")

    neko_st = state.neko_status
    neko_dl = state.neko_deadline_ts
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

    history = state.history or []
    history_last_15 = history[-15:]
    history_recent = [{"change": r.get("change", 0)} for r in history[-100:]]

    cs = state.current_streak
    max_w = state.max_win_streak
    max_l = state.max_loss_streak
    if cs > 0:
        streak_str = f"🔥 {cs} 連勝 (最高 {max_w}勝/{max_l}敗)"
    elif cs < 0:
        streak_str = f"💀 {abs(cs)} 連敗 (最高 {max_w}勝/{max_l}敗)"
    else:
        streak_str = f"─ (最高 {max_w}勝/{max_l}敗)"

    sess_start = state.session_start_ts
    pph_str = "─"
    if sess_start:
        hrs = max(1/60, (time.time() - sess_start) / 3600)
        pph = state.net_change / hrs
        pph_str = f"{'+' if pph >= 0 else ''}{int(pph):,} / 小時 ({hrs:.1f}h)"

    return {
        "ts":            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status":        state.status,
        "paused":        state.paused,
        "balance":       bal,
        "start_balance": start,
        "net_change":    state.net_change,
        "current_bet":   state.current_bet,
        "total_bets":    state.total_bets,
        "wins":          state.wins,
        "losses":        state.losses,
        "goal_str":      goal_str,
        "loss_str":      loss_str,
        "ev_str":        ev_str,
        "kelly_str":     kelly_str,
        "streak_str":    streak_str,
        "pph_str":       pph_str,
        "hourly_next":   state.hourly_next,
        "daily_next":    state.daily_next,
        "neko_str":      neko_str,
        "events":        dict(state.events.__dict__),
        "dead_notified": state.dead_notified,
        "history_last_15": history_last_15,
        "history_recent":  history_recent,
    }


def _build_analysis_snapshot(state: "BotState") -> dict:
    sa = state.slot_analysis or {}
    n = sa.get("total_spins", 0)
    if n == 0:
        return {"has_data": False, "total_spins": 0}

    from bot.slot.analysis import (
        compute_drawdown,
        compute_slot_stats,
        format_symbol_display,
        is_noise_symbol,
    )

    stats = compute_slot_stats(sa)
    total_wagered = sa.get("total_wagered", 0) or 1

    high_mults = stats.get("high_mults", [])
    dist_rows = []
    for bucket in PAYOUT_BUCKETS:
        count = int(stats["payout_distribution"].get(bucket, 0))
        pct = count / n * 100 if n else 0
        actual = ""
        if bucket == "以上" and count > 0 and high_mults:
            recent = sorted(high_mults, reverse=True)[:5]
            actual = ", ".join(f"{m:.1f}x" for m in recent)
            if len(high_mults) > len(recent):
                actual += f" …+{len(high_mults) - len(recent)}"
        dist_rows.append({
            "bucket": bucket, "count": count, "pct": pct, "actual": actual,
        })

    si = stats.get("symbol_info", {}) or {}
    gp = stats.get("grid_symbol_prob", {}) or {}
    all_syms = set(si.keys()) | set(gp.keys())
    sym_rows = []
    for sym in sorted(all_syms,
                      key=lambda s: -(si.get(s, {}).get("total_payout", 0))):
        info = si.get(sym, {})
        wins = info.get("win_appearances", 0)
        prob = gp.get(sym)
        is_noise = is_noise_symbol(sym, wins, prob or 0)
        sym_rows.append({
            "symbol":        sym,
            "display":       format_symbol_display(sym),
            "wins":          wins,
            "avg_mult":      info.get("avg_mult", 0.0),
            "total_payout":  info.get("total_payout", 0),
            "recover_rate":  info.get("total_payout", 0) / total_wagered,
            "grid_prob":     prob,
            "hidden":        is_noise,
        })

    li = stats.get("line_info", {}) or {}
    line_rows = sorted(
        [{"line_name": ln, **info} for ln, info in li.items()],
        key=lambda r: -r["hits"],
    )

    if stats.get("sufficient_data") and stats.get("kelly_fraction", 0) > 0:
        kf = stats["kelly_fraction"]
        kelly_str = f"{kf:.4f} (½={kf/2:.4f})"
    else:
        valid_n = stats.get("valid_rr_count", n)
        kelly_str = f"資料不足 (需 {MIN_KELLY_SAMPLES},目前 {valid_n})"

    drawdown = compute_drawdown(state.history or [])

    return {
        "has_data":       True,
        "total_spins":    stats["total_spins"],
        "win_rate":       stats["win_rate"],
        "ev":             stats["ev"],
        "edge":           stats["edge"],
        "std_dev":        stats["std_dev"],
        "variance":       stats["variance"],
        "kelly_str":      kelly_str,
        "payout_distribution": dist_rows,
        "symbols":        sym_rows,
        "lines":          line_rows,
        "drawdown":       drawdown,
    }


# ── HTTP handler ──────────────────────────────────────────────────────
def _make_handler(
    state: "BotState",
    config_provider: Callable[[], "BotConfig"],
    on_action: Callable[[str], dict],
    on_config_save_sync: Callable[[dict], dict],
):
    """工廠 — 動態產出 BaseHTTPRequestHandler subclass。

    on_config_save_sync 接收 partial config dict,**同步**回傳
    {"ok": bool, "errors": [...], "warnings": [...]}.
    Dashboard 在 thread 中跑,不能直接 await async function;改用 sync 包裝。
    """

    class DashboardHandler(http.server.BaseHTTPRequestHandler):
        # 縮短 server header(避免洩漏 Python 版本)
        server_version = "DashboardSrv/1.0"
        sys_version = ""

        def log_message(self, fmt, *args):  # noqa: A002, N802
            log.debug(fmt, *args)

        # ── Auth ─────────────────────────────────────────────────────
        def _check_auth(self) -> bool:
            cfg = config_provider().dashboard
            pwd = (cfg.password or "").strip()
            if not pwd:
                # 沒設密碼 — 但若 host=0.0.0.0 我們在 server start 階段就會拒
                return True
            user = (cfg.username or "admin").strip() or "admin"

            auth_hdr = self.headers.get("Authorization", "")
            if not auth_hdr.startswith("Basic "):
                self._send_401()
                return False
            import base64
            try:
                decoded = base64.b64decode(auth_hdr[6:]).decode("utf-8", "replace")
                u, _sep, p = decoded.partition(":")
            except Exception:    # noqa: BLE001
                self._send_401()
                return False
            # hmac.compare_digest:恆定時間比對,防 timing attack
            if not (hmac.compare_digest(u, user) and hmac.compare_digest(p, pwd)):
                self._send_401()
                return False
            return True

        def _send_401(self):
            body = b"Unauthorized"
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="DiscordBot"')
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ── CSRF / Origin 檢查(POST 才需要) ────────────────────────
        def _check_csrf(self) -> bool:
            """檢查 Origin / Referer 是否與 Host 同源。

            原則:Same-Origin Policy + 額外要求 X-Requested-With Header
            (XHR / fetch 自動帶,form-submit 不會帶 → 防 CSRF form attack)。
            """
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin", "")
            referer = self.headers.get("Referer", "")
            xrw = self.headers.get("X-Requested-With", "")

            # 必須是 fetch/XHR(瀏覽器跨站 form 不會自動帶 X-Requested-With)
            if not xrw:
                return False

            # Origin / Referer 必須是 http://<host>...
            for h in (origin, referer):
                if not h:
                    continue
                # 只取 scheme://host:port 部分比對
                m = re.match(r"^(https?)://([^/]+)", h)
                if not m:
                    return False
                if m.group(2).lower() != host.lower():
                    return False
            # 至少要有一個來源 header
            return bool(origin or referer)

        # ── GET routing ──────────────────────────────────────────────
        def do_GET(self):  # noqa: N802
            if not self._check_auth():
                return
            path = self.path.split("?", 1)[0]

            routes_html = {
                "/":          ("概覽",     _OVERVIEW_BODY, "overview"),
                "/index.html":("概覽",     _OVERVIEW_BODY, "overview"),
                "/analysis":  ("Slot 分析", _ANALYSIS_BODY, "analysis"),
                "/control":   ("系統設定", _CONTROL_BODY, "control"),
                "/logs":      ("即時日誌", _LOGS_BODY, "logs"),
            }
            if path in routes_html:
                title, body, active = routes_html[path]
                self._html(_html_shell(title, body, active) + _html_close())
                return

            if path == "/api/logs":
                lines = _read_log_tail()
                self._json({"lines": lines, "count": len(lines)})
                return
            if path == "/api/state":
                self._json(_build_state_snapshot(state, config_provider()))
                return
            if path == "/api/analysis":
                self._json(_build_analysis_snapshot(state))
                return
            if path == "/api/config":
                cfg = config_provider().to_redacted_dict()
                self._json(cfg)
                return
            self._respond(404, "text/plain", b"Not Found")

        def do_POST(self):  # noqa: N802
            if not self._check_auth():
                return
            if not self._check_csrf():
                self._json({"ok": False, "message": "請求被拒(CSRF/Origin 檢查失敗)"}, code=403)
                return

            path = self.path.split("?", 1)[0]
            if path.startswith("/api/action/"):
                action = path[len("/api/action/"):]
                # 白名單
                if action not in ("toggle_pause", "reset_analysis", "restart"):
                    self._json({"ok": False, "message": f"未知動作: {action}"}, code=400)
                    return
                try:
                    result = on_action(action)
                    self._json(result)
                except Exception as e:    # noqa: BLE001
                    log.exception("action %s 失敗", action)
                    self._json({"ok": False, "message": f"錯誤: {e}"}, code=500)
                return

            if path == "/api/config":
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length > 64 * 1024:    # 64KB 上限
                    self._json({"ok": False, "message": "payload 過大"}, code=413)
                    return
                raw = self.rfile.read(length) if length else b""
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError as e:
                    self._json({"ok": False, "message": f"JSON 錯誤: {e}"}, code=400)
                    return
                if not isinstance(payload, dict):
                    self._json({"ok": False, "message": "payload 必須是 dict"}, code=400)
                    return
                _strip_invalid_numbers(payload)
                # 不允許 dashboard / email 修改密碼或 host(防鎖死自己)
                if "dashboard" in payload:
                    payload["dashboard"].pop("password", None)
                    payload["dashboard"].pop("host", None)
                    payload["dashboard"].pop("port", None)
                if "email" in payload:
                    payload["email"].pop("password", None)
                # 也不允許改 guild_id / channel_id
                payload.pop("guild_id", None)
                payload.pop("channel_id", None)

                try:
                    result = on_config_save_sync(payload)
                    if result.get("ok"):
                        self._json(result)
                    else:
                        self._json(result, code=400)
                except Exception as e:    # noqa: BLE001
                    log.exception("儲存設定失敗")
                    self._json({"ok": False, "message": f"儲存失敗: {e}"}, code=500)
                return

            self._respond(404, "text/plain", b"Not Found")

        # ── Response helpers ─────────────────────────────────────────
        def _html(self, body: str) -> None:
            self._respond(200, "text/html; charset=utf-8", body.encode("utf-8"))

        def _json(self, obj: Any, code: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self._respond(code, "application/json; charset=utf-8", body)

        def _respond(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
            self.end_headers()
            self.wfile.write(body)

    return DashboardHandler


def _strip_invalid_numbers(d: Any) -> None:
    if isinstance(d, dict):
        for k in list(d.keys()):
            v = d[k]
            if isinstance(v, float) and v != v:    # NaN
                d[k] = None
            elif isinstance(v, dict):
                _strip_invalid_numbers(v)


class _ReusableTCPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


# ── Server lifecycle ──────────────────────────────────────────────────
def start_dashboard_thread(
    state: "BotState",
    config_provider: Callable[[], "BotConfig"],
    on_action: Callable[[str], dict],
    on_config_save_sync: Callable[[dict], dict],
    host: str | None = None,
    port: int | None = None,
) -> threading.Thread | None:
    cfg = config_provider().dashboard
    host = host or cfg.host
    port = port or cfg.port

    # 安全保險:0.0.0.0 + 無密碼 → 拒絕啟動
    if host == "0.0.0.0" and not (cfg.password or "").strip():
        log.error("Dashboard 配置不安全(0.0.0.0 + 無密碼),拒絕啟動。"
                  "請設定密碼或改 host=127.0.0.1。")
        return None

    handler_cls = _make_handler(state, config_provider, on_action, on_config_save_sync)
    try:
        server = _ReusableTCPServer((host, port), handler_cls)
    except OSError as e:
        log.warning("dashboard 啟動失敗(port %d 被占用?): %s", port, e)
        return None

    if host == "0.0.0.0":
        from bot.web.url_helpers import detect_lan_ip
        log.info("dashboard 啟動 — 本機: http://127.0.0.1:%d/  / LAN: http://%s:%d/",
                 port, detect_lan_ip(), port)
    else:
        log.info("dashboard 啟動 — http://%s:%d/", host, port)

    def _serve() -> None:
        try:
            server.serve_forever(poll_interval=0.5)
        except Exception as e:    # noqa: BLE001
            log.error("dashboard server 例外: %s", e)
        finally:
            server.server_close()
            log.info("dashboard 已關閉")

    t = threading.Thread(target=_serve, daemon=True, name="dashboard")
    t.start()
    t._dashboard_server = server  # type: ignore[attr-defined]
    return t


def stop_dashboard_thread(t: threading.Thread | None) -> None:
    if t is None:
        return
    server = getattr(t, "_dashboard_server", None)
    if server is None:
        return
    try:
        server.shutdown()
    except Exception:    # noqa: BLE001
        log.warning("關閉 dashboard 時發生錯誤", exc_info=True)
