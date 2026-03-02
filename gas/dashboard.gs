// =============================================
// Super Scaner Monitoring Dashboard (GAS)
// 部署方式：Apps Script > 部署 > 新しいデプロイ > ウェブアプリ
// 實行者：自分、アクセス権：全員
// =============================================

const SPREADSHEET_ID = PropertiesService.getScriptProperties().getProperty('SPREADSHEET_ID');

function doGet() {
  return HtmlService.createHtmlOutput(getDashboardHtml())
    .setTitle('Super Scaner Monitor')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

function getMetrics() {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);

  // heartbeat - 最新行
  const hbSheet = ss.getSheetByName('heartbeat');
  const hbData = hbSheet.getLastRow() > 1
    ? hbSheet.getRange(hbSheet.getLastRow(), 1, 1, 6).getValues()[0]
    : ['', 'unknown', 0, 0, 0, 0];

  // processing_stats - 今日
  const today = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyy-MM-dd');
  const statsSheet = ss.getSheetByName('processing_stats');
  const statsData = statsSheet.getDataRange().getValues();
  let todayStats = [today, 0, 0, 0];
  for (let i = 1; i < statsData.length; i++) {
    if (statsData[i][0] === today) { todayStats = statsData[i]; break; }
  }

  // 最近7日の処理統計
  const last7Days = statsData.slice(-7).filter(r => r[0]);

  // logs - 最新50行
  const logsSheet = ss.getSheetByName('logs');
  const logsData = logsSheet.getLastRow() > 1
    ? logsSheet.getRange(Math.max(2, logsSheet.getLastRow() - 49), 1, 50, 3).getValues()
    : [];

  // heartbeat - 最近60分の CPU トレンド
  const recentHb = hbSheet.getLastRow() > 1
    ? hbSheet.getRange(Math.max(2, hbSheet.getLastRow() - 59), 1, 60, 6).getValues()
    : [];

  // 最後のハートビートからの経過時間
  let minutesAgo = 999;
  if (hbData[0]) {
    const lastTs = new Date(hbData[0]);
    minutesAgo = Math.floor((new Date() - lastTs) / 60000);
  }

  return JSON.stringify({
    timestamp: hbData[0] || '-',
    container_status: hbData[1] || 'unknown',
    restart_count: hbData[2] || 0,
    cpu_pct: hbData[3] || 0,
    ram_pct: hbData[4] || 0,
    disk_pct: hbData[5] || 0,
    minutes_ago: minutesAgo,
    today_success: todayStats[1] || 0,
    today_fail: todayStats[2] || 0,
    today_amount: todayStats[3] || 0,
    last7days: last7Days,
    logs: logsData.reverse(),
    cpu_trend: recentHb.map(r => ({ts: r[0], cpu: r[3]}))
  });
}

function getDashboardHtml() {
  return `<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Super Scaner Monitor</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; }
  .header { background: #1e293b; padding: 16px 24px; border-bottom: 1px solid #334155; display: flex; align-items: center; gap: 12px; }
  .header h1 { font-size: 18px; font-weight: 700; color: #f1f5f9; }
  .badge { font-size: 11px; padding: 2px 8px; border-radius: 9999px; background: #334155; color: #94a3b8; }
  .tabs { display: flex; gap: 2px; padding: 16px 24px 0; background: #0f172a; }
  .tab { padding: 8px 20px; border-radius: 8px 8px 0 0; cursor: pointer; font-size: 13px; color: #94a3b8; background: #1e293b; border: 1px solid #334155; border-bottom: none; }
  .tab.active { background: #1e40af; color: #fff; border-color: #1e40af; }
  .panel { display: none; padding: 24px; }
  .panel.active { display: block; }
  .card { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 20px; margin-bottom: 16px; }
  .status-big { font-size: 48px; font-weight: 900; }
  .status-online { color: #22c55e; }
  .status-offline { color: #ef4444; }
  .status-unknown { color: #f59e0b; }
  .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
  .stat-card { background: #0f172a; border-radius: 8px; padding: 16px; text-align: center; }
  .stat-value { font-size: 28px; font-weight: 800; color: #38bdf8; }
  .stat-label { font-size: 11px; color: #64748b; margin-top: 4px; }
  .progress-bar { background: #334155; border-radius: 9999px; height: 8px; margin-top: 8px; overflow: hidden; }
  .progress-fill { height: 100%; border-radius: 9999px; transition: width 0.3s; }
  .fill-ok { background: #22c55e; }
  .fill-warn { background: #f59e0b; }
  .fill-danger { background: #ef4444; }
  .log-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .log-table th { text-align: left; padding: 6px 8px; color: #64748b; border-bottom: 1px solid #334155; }
  .log-table td { padding: 5px 8px; border-bottom: 1px solid #1e293b; font-family: monospace; }
  .log-error { color: #ef4444; }
  .log-warn { color: #f59e0b; }
  .log-info { color: #94a3b8; }
  .filter-input { background: #0f172a; border: 1px solid #334155; color: #e2e8f0; padding: 6px 12px; border-radius: 6px; font-size: 13px; width: 280px; margin-bottom: 12px; }
  .refresh-info { font-size: 11px; color: #475569; text-align: right; padding: 8px 0; }
  canvas { max-width: 100%; }
</style>
</head>
<body>
<div class="header">
  <h1>🤖 Super Scaner Monitor</h1>
  <span class="badge" id="lastUpdate">読込中...</span>
</div>
<div class="tabs">
  <div class="tab active" onclick="switchTab('status')">📊 ステータス</div>
  <div class="tab" onclick="switchTab('stats')">📈 統計</div>
  <div class="tab" onclick="switchTab('logs')">📋 ログ</div>
  <div class="tab" onclick="switchTab('system')">💻 システム</div>
</div>

<div id="panel-status" class="panel active">
  <div class="card">
    <div class="status-big" id="statusIcon">⏳</div>
    <div style="margin-top:8px;font-size:14px;color:#94a3b8" id="statusLabel">読込中...</div>
    <div style="margin-top:4px;font-size:12px;color:#64748b" id="lastHeartbeat"></div>
    <div style="margin-top:4px;font-size:12px;color:#f59e0b" id="restartWarning"></div>
  </div>
</div>

<div id="panel-stats" class="panel">
  <div class="stat-grid" id="statsGrid">
    <div class="stat-card"><div class="stat-value" id="successCount">-</div><div class="stat-label">本日 成功</div></div>
    <div class="stat-card"><div class="stat-value" id="failCount" style="color:#ef4444">-</div><div class="stat-label">本日 失敗</div></div>
    <div class="stat-card"><div class="stat-value" id="totalAmount" style="color:#a78bfa">-</div><div class="stat-label">本日 処理金額（円）</div></div>
  </div>
  <div class="card">
    <div style="font-size:13px;color:#64748b;margin-bottom:12px">過去7日間 処理件数</div>
    <canvas id="chart7days" height="80"></canvas>
  </div>
</div>

<div id="panel-logs" class="panel">
  <input class="filter-input" id="logFilter" placeholder="🔍 キーワードフィルター..." oninput="renderLogs()">
  <div class="card" style="padding:0;overflow:hidden">
    <table class="log-table">
      <thead><tr><th>時刻</th><th>レベル</th><th>メッセージ</th></tr></thead>
      <tbody id="logBody"></tbody>
    </table>
  </div>
</div>

<div id="panel-system" class="panel">
  <div class="card">
    <div style="font-size:13px;color:#64748b;margin-bottom:12px">CPU 使用率</div>
    <div id="cpuPct" style="font-size:24px;font-weight:700">-</div>
    <div class="progress-bar"><div class="progress-fill" id="cpuBar" style="width:0%"></div></div>
    <div style="margin-top:16px;font-size:13px;color:#64748b">直近60分 CPU トレンド</div>
    <canvas id="cpuTrend" height="60" style="margin-top:8px"></canvas>
  </div>
  <div class="card">
    <div style="font-size:13px;color:#64748b;margin-bottom:8px">RAM 使用率</div>
    <div id="ramPct" style="font-size:24px;font-weight:700">-</div>
    <div class="progress-bar"><div class="progress-fill" id="ramBar" style="width:0%"></div></div>
  </div>
  <div class="card">
    <div style="font-size:13px;color:#64748b;margin-bottom:8px">ディスク 使用率</div>
    <div id="diskPct" style="font-size:24px;font-weight:700">-</div>
    <div class="progress-bar"><div class="progress-fill" id="diskBar" style="width:0%"></div></div>
  </div>
</div>

<div class="refresh-info" style="padding:8px 24px">自動更新: 60秒ごと | <span id="nextRefresh"></span></div>

<script>
let metricsData = null;
let countdown = 60;

function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', ['status','stats','logs','system'][i] === name));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.getElementById('panel-' + name).classList.add('active');
}

function fillBar(id, pct) {
  const el = document.getElementById(id);
  el.style.width = pct + '%';
  el.className = 'progress-fill ' + (pct >= 90 ? 'fill-danger' : pct >= 70 ? 'fill-warn' : 'fill-ok');
}

function renderMetrics(data) {
  metricsData = data;
  // Status tab
  const isOnline = data.container_status === 'running' && data.minutes_ago < 5;
  const el = document.getElementById('statusIcon');
  el.textContent = isOnline ? '● 稼働中' : (data.container_status === 'running' ? '⚠️ 応答遅延' : '✕ 停止');
  el.className = 'status-big ' + (isOnline ? 'status-online' : data.container_status === 'running' ? 'status-unknown' : 'status-offline');
  document.getElementById('statusLabel').textContent = 'コンテナ: ' + data.container_status;
  document.getElementById('lastHeartbeat').textContent = '最終ハートビート: ' + (data.minutes_ago < 999 ? data.minutes_ago + '分前' : '不明') + ' (' + data.timestamp + ')';
  document.getElementById('restartWarning').textContent = data.restart_count > 0 ? '⚠️ 再起動回数: ' + data.restart_count : '';

  // Stats tab
  document.getElementById('successCount').textContent = data.today_success;
  document.getElementById('failCount').textContent = data.today_fail;
  document.getElementById('totalAmount').textContent = '¥' + (data.today_amount || 0).toLocaleString();
  drawBarChart('chart7days', data.last7days);

  // System tab
  document.getElementById('cpuPct').textContent = data.cpu_pct + '%';
  document.getElementById('ramPct').textContent = data.ram_pct + '%';
  document.getElementById('diskPct').textContent = data.disk_pct + '%';
  fillBar('cpuBar', data.cpu_pct);
  fillBar('ramBar', data.ram_pct);
  fillBar('diskBar', data.disk_pct);
  drawLineChart('cpuTrend', data.cpu_trend.map(d => d.cpu));

  // Header
  document.getElementById('lastUpdate').textContent = '更新: ' + new Date().toLocaleTimeString('ja-JP');
  renderLogs();
}

function renderLogs() {
  if (!metricsData) return;
  const filter = document.getElementById('logFilter').value.toLowerCase();
  const rows = metricsData.logs
    .filter(r => !filter || (r[2] || '').toLowerCase().includes(filter))
    .slice(0, 50)
    .map(r => {
      const cls = r[1] === 'ERROR' ? 'log-error' : r[1] === 'WARNING' ? 'log-warn' : 'log-info';
      return '<tr class="' + cls + '"><td>' + (r[0]||'') + '</td><td>' + (r[1]||'') + '</td><td>' + escHtml(r[2]||'') + '</td></tr>';
    }).join('');
  document.getElementById('logBody').innerHTML = rows || '<tr><td colspan="3" style="text-align:center;color:#475569;padding:20px">ログなし</td></tr>';
}

function escHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function drawBarChart(id, data) {
  const canvas = document.getElementById(id);
  const ctx = canvas.getContext('2d');
  canvas.width = canvas.parentElement.offsetWidth - 40;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!data || data.length === 0) return;
  const max = Math.max(...data.map(d => (d[1]||0) + (d[2]||0)), 1);
  const w = canvas.width / data.length;
  data.forEach((d, i) => {
    const success = (d[1]||0) / max * canvas.height * 0.8;
    const fail = (d[2]||0) / max * canvas.height * 0.8;
    ctx.fillStyle = '#22c55e';
    ctx.fillRect(i*w+2, canvas.height - success, w-4, success);
    ctx.fillStyle = '#ef4444';
    ctx.fillRect(i*w+2, canvas.height - success - fail, w-4, fail);
  });
}

function drawLineChart(id, values) {
  const canvas = document.getElementById(id);
  const ctx = canvas.getContext('2d');
  canvas.width = canvas.parentElement.offsetWidth - 40;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!values || values.length < 2) return;
  const max = Math.max(...values, 1);
  ctx.strokeStyle = '#38bdf8';
  ctx.lineWidth = 2;
  ctx.beginPath();
  values.forEach((v, i) => {
    const x = i / (values.length - 1) * canvas.width;
    const y = canvas.height - (v / max * canvas.height * 0.9);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function fetchData() {
  google.script.run.withSuccessHandler(json => {
    try { renderMetrics(JSON.parse(json)); } catch(e) { console.error(e); }
  }).getMetrics();
}

// 自動更新
setInterval(() => {
  countdown--;
  document.getElementById('nextRefresh').textContent = '次回更新まで ' + countdown + '秒';
  if (countdown <= 0) { countdown = 60; fetchData(); }
}, 1000);

fetchData();
</script>
</body>
</html>`;
}
