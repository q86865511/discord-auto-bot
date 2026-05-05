"""
Web dashboard — 在 localhost (或 LAN) 顯示 bot 即時狀態 + 控制台。

純 Python stdlib（http.server + json）就夠，不引入 FastAPI 等新套件，
打包 .exe 也不會肥。

頁面結構：
- /            概覽（餘額 / 排程 / 累計淨收圖 / 最近下注）
- /analysis    Slot 分析（EV / 賠率分布 / 符號統計 / 線路統計）
- /control     控制台（暫停 / 重置分析 / 重啟 / 編輯設定）

API：
- GET  /api/state         概覽快照
- GET  /api/analysis      分析快照
- GET  /api/config        當前設定
- POST /api/action/<name> 動作: pause / resume / reset_analysis / restart
- POST /api/config        更新 config（merge + save_config）

使用：
- main.py 啟動 dashboard 在背景 thread
- 設定：config["dashboard"] = {"enabled": true, "host": "0.0.0.0", "port": 8765}
- 預設 host 是 0.0.0.0（同 LAN 手機可開；要鎖本機就改 "127.0.0.1"）
"""
from __future__ import annotations

import http.server
import json
import logging
import os
import socket
import socketserver
import threading
import time
from datetime import datetime
from typing import Any, Callable


# ── 共用 HTML 頭部 / 導覽列 ─────────────────────────────────────────────────
def _html_shell(title: str, body_html: str, active: str = "overview") -> str:
    nav_items = [
        ("overview", "/",         "📊 概覽"),
        ("analysis", "/analysis", "🎯 Slot 分析"),
        ("control",  "/control",  "🛠️ 控制台"),
        ("logs",     "/logs",     "📋 Logs"),
        ("qr",       "/qr",       "📱 QR"),
    ]
    nav_html = "".join(
        f'<a href="{href}" class="nav-link{" active" if key == active else ""}">{label}</a>'
        for key, href, label in nav_items
    )
    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — Discord Auto Bot</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 0;
    font-family: -apple-system, "Segoe UI", "Microsoft JhengHei", sans-serif;
    background: #0d1117; color: #e6edf3;
    min-height: 100vh;
  }}
  header {{
    background: #161b22; border-bottom: 1px solid #30363d;
    padding: 12px 16px; display: flex; align-items: center; gap: 12px;
    position: sticky; top: 0; z-index: 10;
  }}
  header h1 {{ font-size: 18px; margin: 0; color: #58a6ff; }}
  nav {{
    display: flex; gap: 4px; flex-wrap: wrap;
    margin-left: auto;
  }}
  .nav-link {{
    padding: 6px 12px; border-radius: 6px; text-decoration: none;
    color: #8b949e; font-size: 14px; transition: background 0.15s;
  }}
  .nav-link:hover {{ background: #21262d; color: #e6edf3; }}
  .nav-link.active {{ background: #1f6feb; color: white; }}
  main {{ padding: 16px; }}
  h2.section {{ font-size: 14px; color: #8b949e; text-transform: uppercase;
    letter-spacing: 0.5px; margin: 20px 0 8px 0; }}
  .grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 12px;
  }}
  .card {{
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 12px;
  }}
  .card h3 {{
    font-size: 14px; margin: 0 0 8px 0; color: #8b949e;
    text-transform: uppercase; letter-spacing: 0.5px;
  }}
  .row {{ display: flex; justify-content: space-between; padding: 4px 0;
    border-bottom: 1px solid #21262d; }}
  .row:last-child {{ border-bottom: none; }}
  .row .label {{ color: #8b949e; font-size: 13px; }}
  .row .value {{ font-weight: 600; font-variant-numeric: tabular-nums; }}
  .green  {{ color: #3fb950; }}
  .red    {{ color: #f85149; }}
  .yellow {{ color: #d29922; }}
  .blue   {{ color: #58a6ff; }}
  .dim    {{ color: #6e7681; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  table th, table td {{
    text-align: left; padding: 6px 8px;
    border-bottom: 1px solid #30363d;
    font-variant-numeric: tabular-nums;
  }}
  table th {{ color: #8b949e; font-weight: normal; font-size: 12px;
    text-transform: uppercase; letter-spacing: 0.5px; }}
  table.right-align td:not(:first-child),
  table.right-align th:not(:first-child) {{ text-align: right; }}
  #status-bar {{
    display: flex; align-items: center; gap: 8px; font-size: 13px;
  }}
  #status-dot {{
    width: 10px; height: 10px; border-radius: 50%;
    background: #3fb950; animation: pulse 2s infinite;
  }}
  #status-dot.paused {{ background: #d29922; animation: none; }}
  #status-dot.dead   {{ background: #f85149; animation: none; }}
  @keyframes pulse {{
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.5; }}
  }}
  .footer {{ margin-top: 24px; padding-top: 12px;
    border-top: 1px solid #30363d;
    color: #6e7681; font-size: 12px; text-align: center; }}
  canvas {{ width: 100%; max-width: 100%; height: 250px; display: block; }}
  /* control page */
  .btn {{
    background: #21262d; border: 1px solid #30363d; color: #e6edf3;
    padding: 10px 16px; border-radius: 6px; cursor: pointer;
    font-size: 14px; transition: all 0.15s;
  }}
  .btn:hover {{ background: #30363d; }}
  .btn.primary {{ background: #1f6feb; border-color: #1f6feb; }}
  .btn.primary:hover {{ background: #388bfd; }}
  .btn.danger  {{ background: #da3633; border-color: #da3633; }}
  .btn.danger:hover {{ background: #f85149; }}
  .btn.warning {{ background: #9e6a03; border-color: #9e6a03; }}
  .btn-row {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }}
  .field {{ margin: 8px 0; }}
  .field label {{ display: block; color: #8b949e; font-size: 12px;
    margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.5px; }}
  .field input, .field select {{
    width: 100%; padding: 6px 10px; background: #0d1117;
    border: 1px solid #30363d; border-radius: 4px;
    color: #e6edf3; font-size: 14px;
  }}
  .field input:focus, .field select:focus {{
    outline: none; border-color: #1f6feb;
  }}
  .toast {{
    position: fixed; bottom: 20px; right: 20px;
    padding: 10px 16px; border-radius: 6px;
    background: #1f6feb; color: white;
    transform: translateY(100px); opacity: 0;
    transition: all 0.3s;
  }}
  .toast.show {{ transform: translateY(0); opacity: 1; }}
  .toast.error {{ background: #da3633; }}
</style>
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
    document.getElementById('status-text').textContent =
      d.paused ? '已暫停' : (d.status || '運行中');
  }} catch(e) {{
    document.getElementById('status-text').innerHTML =
      '<span class="red">無法連線到 bot</span>';
  }}
}}
refreshStatus();
setInterval(refreshStatus, 5000);
</script>
"""


def _html_close() -> str:
    return "</body></html>"


# ── 頁面：Overview ─────────────────────────────────────────────────────────
_OVERVIEW_BODY = """
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
<div class="footer">
  自動刷新 2 秒 · <a href="/control" style="color:#58a6ff;">控制台</a> ·
  <a href="/analysis" style="color:#58a6ff;">完整分析</a>
</div>
<script>
function fmt(n) { if (n == null) return '─'; return n.toLocaleString(); }
function fmtSign(n) {
  if (n == null) return '─';
  const s = (n >= 0 ? '+' : '') + n.toLocaleString();
  return n >= 0 ? `<span class="green">${s}</span>` : `<span class="red">${s}</span>`;
}
function fmtRemaining(epoch) {
  if (!epoch) return '─';
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
  const w = c.clientWidth || 600, h = 250;
  c.width = w; c.height = h;
  const padL = 60, padR = 12, padT = 16, padB = 24;
  const innerW = w - padL - padR;
  const innerH = h - padT - padB;

  if (chartHistory.length < 2) {
    ctx.fillStyle = '#6e7681';
    ctx.font = '13px sans-serif';
    ctx.fillText('資料不足（需要至少 2 筆下注）', padL + 10, padT + 24);
    return;
  }

  const nets = [];
  let cum = 0;
  chartHistory.forEach(r => { cum += r.change; nets.push(cum); });
  const minN = Math.min(0, ...nets), maxN = Math.max(0, ...nets);
  let range = maxN - minN || 1;

  // 找 5 個 nice 刻度
  const nice = niceTicks(minN, maxN, 5);
  const yMin = nice.min, yMax = nice.max;
  range = yMax - yMin || 1;

  const xScale = (i) => padL + (i / Math.max(1, nets.length - 1)) * innerW;
  const yScale = (v) => padT + ((yMax - v) / range) * innerH;

  // 格線 + Y 軸標籤
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

  // 零基準線（特別亮）
  if (yMin < 0 && yMax > 0) {
    const zy = yScale(0);
    ctx.strokeStyle = '#484f58';
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.moveTo(padL, zy); ctx.lineTo(w - padR, zy); ctx.stroke();
  }

  // X 軸：第一筆與最後一筆
  ctx.fillStyle = '#6e7681';
  ctx.textAlign = 'left';
  ctx.fillText('1', padL, h - padB / 2);
  ctx.textAlign = 'right';
  ctx.fillText(`${nets.length}`, w - padR, h - padB / 2);

  // 折線
  ctx.strokeStyle = nets[nets.length - 1] >= 0 ? '#3fb950' : '#f85149';
  ctx.lineWidth = 2;
  ctx.beginPath();
  nets.forEach((v, i) => {
    const x = xScale(i);
    const y = yScale(v);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // 最後一點
  ctx.fillStyle = ctx.strokeStyle;
  const lastX = xScale(nets.length - 1);
  const lastY = yScale(nets[nets.length - 1]);
  ctx.beginPath();
  ctx.arc(lastX, lastY, 3.5, 0, Math.PI * 2);
  ctx.fill();
  // 最後一點的數值標籤
  ctx.fillStyle = '#e6edf3';
  ctx.textAlign = 'right';
  ctx.font = 'bold 12px sans-serif';
  const lastLabel = (nets[nets.length-1] >= 0 ? '+' : '') +
    nets[nets.length-1].toLocaleString();
  ctx.fillText(lastLabel, lastX - 8, lastY - 8);
}

// 找漂亮的整數刻度（5 段左右）
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
    document.getElementById('diff').innerHTML =
      (d.balance != null && d.start_balance != null)
      ? fmtSign(d.balance - d.start_balance) : '─';
    document.getElementById('net_change').innerHTML = fmtSign(d.net_change);
    document.getElementById('current_bet').textContent = d.current_bet ? fmt(d.current_bet) : '─';
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
  } catch (err) { /* status-bar handler shows error */ }
}
refresh();
setInterval(refresh, 2000);
window.addEventListener('resize', drawChart);
</script>
"""


# ── 頁面：Slot 分析 ────────────────────────────────────────────────────────
_ANALYSIS_BODY = """
<div class="grid">
  <div class="card">
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
    <h3>📈 賠率分布</h3>
    <table class="right-align" id="dist-table">
      <thead><tr><th>區間</th><th>次數</th><th>比例</th><th>分布</th><th>實際賠率</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <div class="card" style="grid-column: 1/-1;">
    <h3>🎯 符號統計（回收率 = 累計賠付 / 累計下注）</h3>
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
function fmtPct(p) { return (p * 100).toFixed(1) + '%'; }
function fmtMul(p) { return p.toFixed(4) + 'x'; }
async function refresh() {
  try {
    const r = await fetch('/api/analysis');
    const d = await r.json();
    if (!d.has_data) {
      document.getElementById('total_spins').textContent = '0';
      document.querySelector('main').insertAdjacentHTML('afterbegin',
        '<div class="card" style="margin-bottom:12px;"><span class="yellow">尚無分析資料 — 開始賭博後會自動累積。</span></div>');
      return;
    }
    document.getElementById('total_spins').textContent = d.total_spins.toLocaleString();
    document.getElementById('win_rate').textContent = fmtPct(d.win_rate);
    document.getElementById('ev').textContent = fmtMul(d.ev);
    const edge = d.edge;
    const edgeColor = edge >= 0 ? 'green' : 'red';
    document.getElementById('edge').innerHTML =
      `<span class="${edgeColor}">${(edge >= 0 ? '+' : '') + (edge*100).toFixed(2)}%</span>`;
    document.getElementById('std_dev').textContent = d.std_dev.toFixed(4);
    document.getElementById('variance').textContent = d.variance.toFixed(4);
    document.getElementById('kelly').textContent = d.kelly_str;

    // 賠率分布
    const distBody = document.querySelector('#dist-table tbody');
    distBody.innerHTML = '';
    d.payout_distribution.forEach(row => {
      const pct = row.pct;
      const barLen = Math.min(40, Math.floor(pct / 2));
      const bar = '█'.repeat(barLen);
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${row.bucket}</td><td>${row.count.toLocaleString()}</td>
        <td>${pct.toFixed(1)}%</td><td>${bar}</td><td>${row.actual || ''}</td>`;
      distBody.appendChild(tr);
    });

    // 符號統計
    const symBody = document.querySelector('#sym-table tbody');
    symBody.innerHTML = '';
    let hidden = 0;
    d.symbols.forEach(row => {
      if (row.hidden) { hidden++; return; }
      const tr = document.createElement('tr');
      const w = row.wins > 0 ? row.wins.toLocaleString() : '─';
      const m = row.wins > 0 ? row.avg_mult.toFixed(2) + 'x' : '─';
      const p = row.wins > 0 ? row.total_payout.toLocaleString() : '─';
      const rec = row.wins > 0 ? (row.recover_rate * 100).toFixed(1) + '%' : '─';
      const gp = row.grid_prob != null ? (row.grid_prob * 100).toFixed(1) + '%' : '─';
      tr.innerHTML = `<td>${row.display}</td><td>${w}</td><td>${m}</td>
        <td>${p}</td><td>${rec}</td><td>${gp}</td>`;
      symBody.appendChild(tr);
    });
    document.getElementById('noise-msg').textContent =
      hidden > 0 ? `（已隱藏 ${hidden} 個雜訊符號：未中獎且格子機率 < 0.1%）` : '';

    // 線路統計
    const lineBody = document.querySelector('#line-table tbody');
    lineBody.innerHTML = '';
    d.lines.forEach(row => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${row.line_name}</td><td>${row.hits.toLocaleString()}</td>
        <td>${(row.hit_rate*100).toFixed(1)}%</td>
        <td>${row.total_payout.toLocaleString()}</td>`;
      lineBody.appendChild(tr);
    });
  } catch (err) { console.error(err); }
}
refresh();
setInterval(refresh, 5000);
</script>
"""


# ── 頁面：控制台 ───────────────────────────────────────────────────────────
_CONTROL_BODY = """
<div class="grid">
  <div class="card">
    <h3>⚡ 快速動作</h3>
    <div class="btn-row">
      <button class="btn primary" onclick="doAction('toggle_pause')" id="btn-pause">⏸️ 暫停 / 恢復</button>
    </div>
    <div class="btn-row">
      <button class="btn warning" onclick="confirmAction('reset_analysis', '確定要重置 slot 分析資料嗎？此動作不可逆。')">🔄 重置 Slot 分析</button>
    </div>
    <div class="btn-row">
      <button class="btn danger" onclick="confirmAction('restart', '確定要重啟程式嗎？所有 loop 會中斷後重新啟動。')">🔁 重啟程式</button>
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
        <option value="auto">auto（按比例）</option>
        <option value="fixed">fixed（固定 min_bet）</option>
        <option value="kelly">kelly（依 EV 動態）</option>
      </select>
    </div>
    <div class="field">
      <label>保底門檻（餘額低於此就停止下注）</label>
      <input type="number" id="cfg-gambling-threshold" oninput="markDirty()">
    </div>
    <div class="field">
      <label>最小下注</label>
      <input type="number" id="cfg-gambling-min_bet" oninput="markDirty()">
    </div>
    <div class="field">
      <label>最大下注（0 = 自動）</label>
      <input type="number" id="cfg-gambling-max_bet" oninput="markDirty()">
    </div>
    <div class="field">
      <label>押注比例（auto 策略用）</label>
      <input type="number" step="0.01" id="cfg-gambling-bet_fraction" oninput="markDirty()">
    </div>
  </div>

  <div class="card">
    <h3>🏁 目標 / 停損</h3>
    <div class="field">
      <label>目標餘額（0 = 不設）</label>
      <input type="number" id="cfg-gambling-goal" oninput="markDirty()">
    </div>
    <div class="field">
      <label>達標行為</label>
      <select id="cfg-gambling-goal_action" onchange="markDirty()">
        <option value="pause">pause</option>
        <option value="raise">raise</option>
      </select>
    </div>
    <div class="field">
      <label>停損點（0 = 不設）</label>
      <input type="number" id="cfg-gambling-loss_floor" oninput="markDirty()">
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
      <label>對象（顯示名稱片段或 user ID）</label>
      <input type="text" id="cfg-transfer-target" oninput="markDirty()">
    </div>
    <div class="field">
      <label>金額</label>
      <input type="number" id="cfg-transfer-amount" oninput="markDirty()">
    </div>
    <div class="field">
      <label>間距（分鐘）</label>
      <input type="number" id="cfg-transfer-interval_min" oninput="markDirty()">
    </div>
  </div>
</div>

<div style="margin-top: 16px; text-align: center;">
  <button class="btn primary" onclick="saveConfig()" id="save-btn">💾 儲存設定</button>
  <button class="btn" onclick="loadConfig()">↺ 重新載入</button>
  <span id="dirty-indicator" class="dim" style="margin-left: 12px;"></span>
</div>

<div class="footer">
  <a href="/" style="color:#58a6ff;">回到概覽</a>
</div>

<script>
let dirty = false;
function markDirty() {
  dirty = true;
  document.getElementById('dirty-indicator').textContent = '● 未儲存';
  document.getElementById('dirty-indicator').className = 'yellow';
}
function clearDirty() {
  dirty = false;
  document.getElementById('dirty-indicator').textContent = '';
}

async function doAction(name) {
  try {
    const r = await fetch('/api/action/' + name, { method: 'POST' });
    const d = await r.json();
    showToast(d.message || '完成', !d.ok);
    if (name === 'toggle_pause') setTimeout(refreshStatus, 100);
  } catch(e) { showToast('動作失敗: ' + e.message, true); }
}
function confirmAction(name, msg) {
  if (confirm(msg)) doAction(name);
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
  if (type === 'bool') return v === 'true';
  if (type === 'int')  return parseInt(v, 10);
  if (type === 'float') return parseFloat(v);
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
  try {
    const r = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (d.ok) {
      showToast('設定已儲存');
      clearDirty();
    } else {
      showToast(d.message || '儲存失敗', true);
    }
  } catch(e) { showToast('儲存失敗: ' + e.message, true); }
}

loadConfig();
</script>
"""


# ── 頁面：QR Code（給手機掃 LAN URL）────────────────────────────────────
_QR_BODY = """
<div class="card" style="text-align: center;">
  <h3>📱 手機掃描登入</h3>
  <p class="dim" style="margin: 0 0 16px 0;">用手機相機掃描 → 在同 LAN 直接打開 dashboard</p>
  <div id="qr-box" style="background: white; padding: 20px; border-radius: 8px;
       display: inline-block; max-width: 90%;">
    <object data="/qr.svg" type="image/svg+xml" style="width: 320px; height: 320px;"></object>
  </div>
  <div style="margin-top: 16px;">
    <code id="url-display" class="blue" style="font-size: 14px;
         background: #0d1117; padding: 6px 12px; border-radius: 4px;
         display: inline-block;">─</code>
  </div>
  <div class="btn-row" style="justify-content: center; margin-top: 16px;">
    <button class="btn primary" onclick="copyUrl()">📋 複製網址</button>
    <button class="btn" onclick="window.open(document.getElementById('url-display').textContent)">🌐 在新分頁開</button>
  </div>
  <p class="dim" style="margin-top: 16px; font-size: 12px;">
    若手機掃描後無法連線：確認電腦防火牆有放行 8765 port、手機跟電腦在同一個 WiFi。
  </p>
</div>
<script>
async function loadUrl() {
  try {
    const r = await fetch('/api/qr-url');
    const d = await r.json();
    document.getElementById('url-display').textContent = d.url || '無法偵測 LAN IP';
  } catch(e) {
    document.getElementById('url-display').textContent = '錯誤: ' + e.message;
  }
}
async function copyUrl() {
  const url = document.getElementById('url-display').textContent;
  try {
    await navigator.clipboard.writeText(url);
    showToast('已複製: ' + url);
  } catch(e) {
    // 後備：用 selection
    const r = document.createRange();
    r.selectNode(document.getElementById('url-display'));
    window.getSelection().removeAllRanges();
    window.getSelection().addRange(r);
    document.execCommand('copy');
    showToast('已複製');
  }
}
loadUrl();
</script>
"""


# ── 頁面：Log viewer ───────────────────────────────────────────────────────
_LOGS_BODY = """
<div class="card">
  <div style="display: flex; justify-content: space-between; align-items: center;">
    <h3 style="margin: 0;">📋 Bot Log（最近 200 行）</h3>
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
async function refreshLogs() {
  try {
    const r = await fetch('/api/logs');
    const d = await r.json();
    const pre = document.getElementById('log-content');
    if (d.error) {
      pre.textContent = '錯誤: ' + d.error;
      pre.classList.add('red');
      return;
    }
    pre.classList.remove('red');
    // 上色：[ERROR] 紅、[WARNING] 黃、[INFO] 預設、[DEBUG] dim
    const lines = (d.lines || []).map(line => {
      let cls = '';
      if (line.includes('[ERROR]')) cls = 'color:#f85149;';
      else if (line.includes('[WARNING]')) cls = 'color:#d29922;';
      else if (line.includes('[DEBUG]')) cls = 'color:#6e7681;';
      // 明顯事件 highlight
      if (line.includes('🎰') || line.includes('中大獎')) cls = 'color:#3fb950;font-weight:600;';
      else if (line.includes('⛔') || line.includes('停損')) cls = 'color:#f85149;font-weight:600;';
      else if (line.includes('🐱') || line.includes('貓娘')) cls = 'color:#a371f7;';
      return `<span style="${cls}">${escapeHtml(line)}</span>`;
    });
    pre.innerHTML = lines.join('\\n');
    if (document.getElementById('autoscroll').checked) {
      pre.scrollTop = pre.scrollHeight;
    }
  } catch(e) {
    document.getElementById('log-content').textContent = '無法載入 log: ' + e.message;
  }
}
function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
refreshLogs();
setInterval(refreshLogs, 3000);
</script>
"""


def _build_qr_svg(text: str) -> bytes:
    """產生 QR code SVG bytes；qrcode 套件沒裝就回 None。"""
    try:
        import qrcode
        import qrcode.image.svg
        factory = qrcode.image.svg.SvgPathImage
        img = qrcode.make(text, image_factory=factory, box_size=10, border=2)
        import io
        buf = io.BytesIO()
        img.save(buf)
        return buf.getvalue()
    except ImportError:
        return None
    except Exception:
        return None


def _read_log_tail(path: str = "bot.log", max_lines: int = 200,
                   max_bytes: int = 256 * 1024) -> list[str]:
    """從 path 讀最後 max_lines 行；不存在或讀失敗回空 list。"""
    try:
        if not os.path.exists(path):
            return []
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()   # 跳掉可能切到一半的行
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
        return lines[-max_lines:]
    except OSError:
        return []


# ── 共用 helpers ───────────────────────────────────────────────────────────
def _build_state_snapshot(state: dict, config: dict) -> dict:
    """組出 /api/state 的快照（給 overview 頁用）。"""
    gcfg = config.get("gambling", {}) or {}
    bal = state.get("balance")
    start = state.get("start_balance")
    goal = int(gcfg.get("goal", 0) or 0)

    if goal > 0 and isinstance(bal, int):
        pct = min(100.0, bal / goal * 100)
        goal_str = f"{bal:,} / {goal:,} ({pct:.1f}%)"
    elif goal > 0:
        goal_str = f"─ / {goal:,}"
    else:
        goal_str = "未設定"

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

    ev_str = "─"
    kelly_str = "─"
    sa = state.get("slot_analysis") or {}
    n = sa.get("total_spins", 0)
    if n > 0:
        try:
            from bot.slot.analysis import compute_slot_stats, MIN_KELLY_SAMPLES
            stats = compute_slot_stats(sa)
            edge_pct = stats["edge"] * 100
            ev_str = (f'<span class="{"green" if edge_pct >= 0 else "red"}">'
                      f"{stats['ev']:.3f}x ({'+' if edge_pct >= 0 else ''}"
                      f"{edge_pct:.2f}%)</span> n={n}")
            if stats.get("sufficient_data") and stats.get("kelly_fraction", 0) > 0:
                kf = stats["kelly_fraction"]
                kelly_str = f"{kf:.4f} (½={kf/2:.4f})"
            else:
                kelly_str = f"資料不足 (需 {MIN_KELLY_SAMPLES})"
        except Exception:
            pass

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

    history = state.get("history") or []
    history_last_15 = history[-15:]
    history_recent = [{"change": r.get("change", 0)} for r in history[-100:]]

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


def _build_analysis_snapshot(state: dict) -> dict:
    """組出 /api/analysis 的快照（給 analysis 頁用）。"""
    sa = state.get("slot_analysis") or {}
    n = sa.get("total_spins", 0)
    if n == 0:
        return {"has_data": False, "total_spins": 0}

    from bot.slot.analysis import (
        compute_slot_stats, _format_symbol_display, _is_noise_symbol,
        PAYOUT_BUCKETS, HIGH_MULT_THRESHOLD, MIN_KELLY_SAMPLES,
    )

    stats = compute_slot_stats(sa)
    total_wagered = sa.get("total_wagered", 0) or 1

    # 賠率分布（含「以上」桶的實際 multipliers）
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

    # 符號統計
    si = stats.get("symbol_info", {}) or {}
    gp = stats.get("grid_symbol_prob", {}) or {}
    all_syms = set(si.keys()) | set(gp.keys())
    sym_rows = []
    for sym in sorted(all_syms,
                      key=lambda s: -(si.get(s, {}).get("total_payout", 0))):
        info = si.get(sym, {})
        wins = info.get("win_appearances", 0)
        prob = gp.get(sym)
        is_noise = _is_noise_symbol(sym, wins, prob or 0)
        sym_rows.append({
            "symbol":        sym,
            "display":       _format_symbol_display(sym),
            "wins":          wins,
            "avg_mult":      info.get("avg_mult", 0.0),
            "total_payout":  info.get("total_payout", 0),
            "recover_rate":  info.get("total_payout", 0) / total_wagered,
            "grid_prob":     prob,
            "hidden":        is_noise,
        })

    # 線路統計
    li = stats.get("line_info", {}) or {}
    line_rows = sorted(
        [{"line_name": ln, **info} for ln, info in li.items()],
        key=lambda r: -r["hits"],
    )

    # Kelly string
    if stats.get("sufficient_data") and stats.get("kelly_fraction", 0) > 0:
        kf = stats["kelly_fraction"]
        kelly_str = f"{kf:.4f} (½={kf/2:.4f})"
    else:
        kelly_str = f"資料不足 (需 {MIN_KELLY_SAMPLES}，目前 {n})"

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
    }


# 共用 deepmerge — 給 POST /api/config 用
def _deep_merge(target: dict, src: dict) -> dict:
    """src 蓋到 target；遞迴合併 dict，其餘直接覆蓋。回傳 target（原地修改）。"""
    for k, v in src.items():
        if v is None:
            continue
        if isinstance(v, dict) and isinstance(target.get(k), dict):
            _deep_merge(target[k], v)
        else:
            target[k] = v
    return target


# ── HTTP handler 工廠 ──────────────────────────────────────────────────────
def _make_handler(state: dict, config_holder: list,
                  on_action: Callable[[str], dict]):
    """工廠函式 — 動態產出 BaseHTTPRequestHandler subclass。"""

    class DashboardHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002, N802
            logging.getLogger("dashboard").debug(format, *args)

        # ── HTTP Basic Auth（dashboard.password 設了才啟用）───────────────
        def _check_auth(self) -> bool:
            """設定密碼則檢查 Authorization header；沒設密碼直接放行。"""
            dcfg = config_holder[0].get("dashboard", {})
            pwd = (dcfg.get("password") or "").strip()
            if not pwd:
                return True   # 沒設密碼 = 不啟用
            user = (dcfg.get("username") or "admin").strip() or "admin"

            auth_hdr = self.headers.get("Authorization", "")
            if not auth_hdr.startswith("Basic "):
                self._send_401()
                return False
            import base64
            try:
                decoded = base64.b64decode(auth_hdr[6:]).decode("utf-8", "replace")
                u, _, p = decoded.partition(":")
            except Exception:
                self._send_401()
                return False
            if u != user or p != pwd:
                self._send_401()
                return False
            return True

        def _send_401(self):
            body = b"Unauthorized"
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="DiscordBot"')
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ── 路由 ─────────────────────────────────────────────────────────
        def do_GET(self):  # noqa: N802
            if not self._check_auth():
                return
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._html(_html_shell("概覽", _OVERVIEW_BODY, "overview")
                           + _html_close())
            elif path == "/analysis":
                self._html(_html_shell("Slot 分析", _ANALYSIS_BODY, "analysis")
                           + _html_close())
            elif path == "/control":
                self._html(_html_shell("控制台", _CONTROL_BODY, "control")
                           + _html_close())
            elif path == "/qr":
                self._html(_html_shell("QR Code", _QR_BODY, "qr")
                           + _html_close())
            elif path == "/logs":
                self._html(_html_shell("Logs", _LOGS_BODY, "logs")
                           + _html_close())
            elif path == "/qr.svg":
                # 用 LAN URL 產生 QR；qrcode 套件沒裝就回 placeholder
                from urllib.parse import urlparse
                # 偵測 LAN IP（直接重用 _detect_lan_ip）
                dcfg = config_holder[0].get("dashboard", {})
                port = int(dcfg.get("port", 8765))
                host_cfg = dcfg.get("host", "0.0.0.0")
                ip = _detect_lan_ip() if host_cfg == "0.0.0.0" else host_cfg
                url = f"http://{ip}:{port}/"
                svg = _build_qr_svg(url)
                if svg:
                    self._respond(200, "image/svg+xml", svg)
                else:
                    placeholder = (
                        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 320">'
                        f'<rect width="320" height="320" fill="#fff"/>'
                        f'<text x="160" y="150" text-anchor="middle" '
                        f'font-family="sans-serif" font-size="14" fill="#000">'
                        f'qrcode 套件未安裝</text>'
                        f'<text x="160" y="180" text-anchor="middle" '
                        f'font-family="sans-serif" font-size="12" fill="#666">'
                        f'pip install qrcode</text>'
                        f'</svg>'
                    ).encode("utf-8")
                    self._respond(200, "image/svg+xml", placeholder)
            elif path == "/api/qr-url":
                dcfg = config_holder[0].get("dashboard", {})
                port = int(dcfg.get("port", 8765))
                host_cfg = dcfg.get("host", "0.0.0.0")
                ip = _detect_lan_ip() if host_cfg == "0.0.0.0" else host_cfg
                self._json({"url": f"http://{ip}:{port}/"})
            elif path == "/api/logs":
                lines = _read_log_tail("bot.log", max_lines=200)
                self._json({"lines": lines, "count": len(lines)})
            elif path == "/api/state":
                self._json(_build_state_snapshot(state, config_holder[0]))
            elif path == "/api/analysis":
                self._json(_build_analysis_snapshot(state))
            elif path == "/api/config":
                # 回傳整份 config（敏感欄位遮罩）
                cfg = json.loads(json.dumps(config_holder[0]))   # deep copy
                if "email" in cfg and "password" in cfg["email"]:
                    cfg["email"]["password"] = "***" if cfg["email"].get("password") else ""
                if "dashboard" in cfg and "password" in cfg["dashboard"]:
                    cfg["dashboard"]["password"] = "***" if cfg["dashboard"].get("password") else ""
                self._json(cfg)
            else:
                self._respond(404, "text/plain", b"Not Found")

        def do_POST(self):  # noqa: N802
            if not self._check_auth():
                return
            path = self.path.split("?", 1)[0]

            if path.startswith("/api/action/"):
                action = path[len("/api/action/"):]
                try:
                    result = on_action(action)
                    self._json(result)
                except Exception as e:
                    self._json({"ok": False, "message": f"錯誤: {e}"}, code=500)
                return

            if path == "/api/config":
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError as e:
                    self._json({"ok": False, "message": f"JSON 錯誤: {e}"}, code=400)
                    return
                # 過濾掉 None / 空字串以外的「保留現值」訊號
                # （前端輸入空欄位時，type=int 會送 NaN，過濾掉它）
                _strip_invalid_numbers(payload)
                # 不允許 dashboard 改自己的 host / port（避免鎖死自己）
                payload.pop("dashboard", None)
                # 也不允許從 dashboard 改 password（前端只看到 ***）
                if "email" in payload:
                    payload["email"].pop("password", None)
                try:
                    _deep_merge(config_holder[0], payload)
                    # 用 main 注入的 save_config callback
                    from main import save_config as _save_config
                    _save_config(config_holder[0])
                    config_holder[0] = json.loads(json.dumps(config_holder[0]))
                    self._json({"ok": True, "message": "已儲存"})
                except Exception as e:
                    self._json({"ok": False, "message": f"儲存失敗: {e}"}, code=500)
                return

            self._respond(404, "text/plain", b"Not Found")

        # ── 回應 helpers ─────────────────────────────────────────────────
        def _html(self, body: str):
            self._respond(200, "text/html; charset=utf-8", body.encode("utf-8"))

        def _json(self, obj: Any, code: int = 200):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self._respond(code, "application/json; charset=utf-8", body)

        def _respond(self, code: int, ctype: str, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

    return DashboardHandler


def _strip_invalid_numbers(d):
    """遞迴把 NaN / 不是有效數字的 number 改成 None（讓 _deep_merge 略過）。"""
    if isinstance(d, dict):
        for k in list(d.keys()):
            v = d[k]
            if isinstance(v, float) and v != v:   # NaN
                d[k] = None
            elif isinstance(v, dict):
                _strip_invalid_numbers(v)


class _ReusableTCPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _detect_lan_ip() -> str:
    """嘗試找出本機在 LAN 上的 IPv4（給 README hint 用，連不上就回 '?'）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "?"


def start_dashboard_thread(state: dict, config_holder: list,
                            on_action: Callable[[str], dict],
                            host: str = "0.0.0.0", port: int = 8765
                            ) -> threading.Thread | None:
    """啟動 dashboard HTTP server 在背景 thread。"""
    log = logging.getLogger("dashboard")
    handler_cls = _make_handler(state, config_holder, on_action)
    try:
        server = _ReusableTCPServer((host, port), handler_cls)
    except OSError as e:
        log.warning("dashboard 啟動失敗（port %d 被占用？）: %s", port, e)
        return None

    lan_ip = _detect_lan_ip()
    if host == "0.0.0.0":
        log.info("dashboard 啟動 — 本機: http://127.0.0.1:%d/  / LAN: http://%s:%d/",
                 port, lan_ip, port)
    else:
        log.info("dashboard 啟動 — http://%s:%d/", host, port)

    def _serve():
        try:
            server.serve_forever(poll_interval=0.5)
        except Exception as e:
            log.error("dashboard server 例外: %s", e)
        finally:
            server.server_close()
            log.info("dashboard 已關閉")

    t = threading.Thread(target=_serve, daemon=True, name="dashboard")
    t.start()
    t._dashboard_server = server  # type: ignore[attr-defined]
    return t


def stop_dashboard_thread(t: threading.Thread | None):
    """關閉 dashboard server。"""
    if t is None:
        return
    server = getattr(t, "_dashboard_server", None)
    if server is None:
        return
    try:
        server.shutdown()
    except Exception:
        pass
