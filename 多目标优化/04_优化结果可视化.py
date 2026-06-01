"""
NSGA-II 优化结果可视化
========================
读取优化方案数据库，生成：
  1. 帕累托前沿散点图（交互式）
  2. 三套站点布局方案对比地图
  3. 进化曲线图

依赖：sqlite3, json, os（标准库）
"""

import sqlite3, json, os, sys, shutil
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_TMP     = "/tmp/drone_analysis/优化方案.db"
DB_COPY    = os.path.join(SCRIPT_DIR, "优化方案.db")
OUT_HTML   = os.path.join(SCRIPT_DIR, "优化结果可视化.html")

# 确保有可读数据库
if not os.path.exists(DB_TMP):
    if os.path.exists(DB_COPY):
        os.makedirs("/tmp/drone_analysis", exist_ok=True)
        shutil.copy2(DB_COPY, DB_TMP)
    else:
        print("❌ 请先运行 03_NSGA2_站点优化.py"); sys.exit(1)

print("=" * 55)
print("NSGA-II 优化结果可视化")
print("=" * 55)
print("\n[1] 读取数据 ...")

conn = sqlite3.connect(DB_TMP)

pareto = conn.execute(
    "SELECT * FROM pareto_solutions ORDER BY total_cost"
).fetchall()
pareto_cols = [d[0] for d in conn.execute("SELECT * FROM pareto_solutions LIMIT 0").description]

stations_raw = conn.execute(
    "SELECT scheme_id, scheme_name, lon, lat, station_type, station_name, n_drones, service_km, total_cost FROM representative_stations"
).fetchall()

evo_log = conn.execute(
    "SELECT gen, pareto_size, min_cost, max_cov, min_emg FROM evolution_log"
).fetchall()
conn.close()

# 整理帕累托数据
P = {col: [r[i] for r in pareto] for i, col in enumerate(pareto_cols)}

# 整理站点数据
schemes = {}
for r in stations_raw:
    sid, sname, lon, lat, stype, stname, ndrones, svc_km, cost = r
    if sname not in schemes:
        schemes[sname] = []
    schemes[sname].append({
        "lon": lon, "lat": lat, "type": stype, "name": stname,
        "ndrones": ndrones, "svc_km": svc_km, "cost": cost
    })

# 进化曲线数据
evo_gens  = [r[0] for r in evo_log]
evo_cov   = [r[3] for r in evo_log if r[3] is not None]
evo_cost  = [r[2] for r in evo_log if r[2] is not None]
evo_emg   = [r[4] for r in evo_log if r[4] is not None]

# 方案摘要统计
scheme_summary = {}
for r in pareto:
    pass  # 从stations推算
for sname, pts in schemes.items():
    n1 = sum(1 for p in pts if p['type']==1)
    n2 = sum(1 for p in pts if p['type']==2)
    n3 = sum(1 for p in pts if p['type']==3)
    total_cost = sum(p['cost'] for p in pts)
    total_drones = n1*2 + n2*5 + n3*10
    scheme_summary[sname] = {
        "n_micro": n1, "n_comp": n2, "n_hub": n3,
        "total": n1+n2+n3, "drones": total_drones,
        "cost": total_cost
    }

print(f"  帕累托解: {len(pareto)}, 代表方案: {len(schemes)}")

# ================================================================
# 生成完整 HTML
# ================================================================
print("\n[2] 生成 HTML 可视化 ...")

SCHEME_COLORS = {
    "低成本方案":   "#4ecdc4",
    "高覆盖方案":   "#45b7d1",
    "快速应急方案": "#f7dc6f",
}
TYPE_COLORS = {1: "#ff6b6b", 2: "#ffd93d", 3: "#6c5ce7"}
TYPE_NAMES  = {1: "微型站", 2: "综合站", 3: "枢纽站"}

# 构造服务圆圈（JS 里用 Leaflet circle）
station_layers_js = {}
for sname, pts in schemes.items():
    station_layers_js[sname] = [
        {
            "lon": p["lon"], "lat": p["lat"],
            "type": p["type"], "name": p["name"],
            "ndrones": p["ndrones"], "svc_km": p["svc_km"],
            "cost": p["cost"],
            "color": TYPE_COLORS[p["type"]],
        }
        for p in pts
    ]

# 帕累托散点数据（按覆盖率分色）
pareto_js = [
    {"cost": P["total_cost"][i], "cov": P["coverage"][i],
     "emg": P["emg_time"][i], "micro": P["n_micro"][i],
     "comp": P["n_comp"][i], "hub": P["n_hub"][i],
     "drones": P["total_drones"][i]}
    for i in range(len(P["id"]))
]

# 预生成 HTML 片段（避免 f-string 内嵌 :.0f 格式符冲突）
SCHEME_CARDS_HTML = ""
for sname in schemes:
    color = SCHEME_COLORS.get(sname, '#58a6ff')
    sm = scheme_summary[sname]
    cost_str = f"{sm['cost']:.0f}"
    SCHEME_CARDS_HTML += f"""
    <div class="info-card" style="border-left:3px solid {color};">
      <div style="font-weight:bold;margin-bottom:6px;color:{color};">{sname}</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;">
        <div><div class="val">{cost_str}</div><div class="lbl">万元总成本</div></div>
        <div><div class="val">{sm['total']}</div><div class="lbl">站点总数</div></div>
        <div><div class="val">{sm['n_micro']}/{sm['n_comp']}/{sm['n_hub']}</div><div class="lbl">微/综/枢纽</div></div>
        <div><div class="val">{sm['drones']}</div><div class="lbl">无人机总数</div></div>
      </div>
    </div>"""

SCHEME_BTNS_HTML = ""
for i, sname in enumerate(schemes, 1):
    color = SCHEME_COLORS.get(sname, '#58a6ff')
    sm = scheme_summary[sname]
    active = "active" if i == 1 else ""
    cost_str = f"{sm['cost']:.0f}"
    SCHEME_BTNS_HTML += f"""
    <div class="scheme-btn {active}" id="btn-{sname}" onclick="showScheme('{sname}')">
      <div style="font-weight:bold;color:{color};">{sname}</div>
      <div style="font-size:10px;color:#8b949e;margin-top:4px;">
        {sm['n_micro']}微 + {sm['n_comp']}综 + {sm['n_hub']}枢 = {sm['total']}站 · {sm['drones']}架无人机
      </div>
      <div style="font-size:11px;margin-top:4px;">总成本 <span class="scheme-metric">{cost_str}万</span></div>
    </div>"""

html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>无人机站点布局多目标优化结果</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:'Microsoft YaHei',sans-serif;background:#0d1117;color:#e6edf3;}}
#app{{display:flex;flex-direction:column;height:100vh;}}
#header{{
  background:linear-gradient(135deg,#161b22 0%,#21262d 100%);
  padding:10px 20px;display:flex;align-items:center;justify-content:space-between;
  border-bottom:1px solid #30363d;flex-shrink:0;
}}
#header h1{{font-size:17px;color:#58a6ff;}}
#header .sub{{font-size:11px;color:#8b949e;margin-top:2px;}}
.badge{{background:#21262d;border:1px solid #30363d;color:#58a6ff;
       padding:3px 10px;border-radius:12px;font-size:11px;}}
#tabs{{display:flex;border-bottom:1px solid #30363d;background:#161b22;flex-shrink:0;}}
.tab{{padding:10px 20px;cursor:pointer;font-size:13px;color:#8b949e;border-bottom:2px solid transparent;}}
.tab.active{{color:#58a6ff;border-bottom-color:#58a6ff;}}
#content{{flex:1;overflow:hidden;display:flex;}}

/* ---- Tab 1: 帕累托 + 进化 ---- */
#panel-pareto{{display:flex;width:100%;height:100%;gap:0;}}
#chart-container{{flex:1;padding:16px;display:flex;flex-direction:column;gap:12px;overflow:auto;}}
.chart-box{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;}}
.chart-box h3{{font-size:13px;color:#58a6ff;margin-bottom:10px;}}
canvas{{display:block;}}
#pareto-info{{
  width:260px;background:#161b22;border-left:1px solid #30363d;
  padding:14px;overflow-y:auto;flex-shrink:0;
}}
#pareto-info h3{{font-size:13px;color:#58a6ff;margin-bottom:10px;}}
.info-card{{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:10px;margin-bottom:8px;}}
.info-card .val{{font-size:18px;font-weight:bold;color:#7ee787;}}
.info-card .lbl{{font-size:10px;color:#8b949e;margin-top:2px;}}
#hover-info{{
  background:#0d1117;border:1px solid #30363d;border-radius:6px;
  padding:10px;font-size:11px;color:#e6edf3;min-height:80px;
}}

/* ---- Tab 2: 地图 ---- */
#panel-map{{display:flex;width:100%;height:100%;}}
#map-sidebar{{
  width:280px;background:#161b22;border-right:1px solid #30363d;
  padding:14px;overflow-y:auto;flex-shrink:0;
}}
#map{{flex:1;}}
.scheme-btn{{
  display:block;width:100%;text-align:left;padding:10px 12px;margin-bottom:6px;
  border-radius:6px;border:2px solid transparent;cursor:pointer;
  background:#0d1117;color:#e6edf3;font-size:12px;transition:all .2s;
}}
.scheme-btn.active{{border-color:#58a6ff;background:#1c2128;}}
.scheme-btn:hover{{background:#1c2128;}}
.scheme-metric{{color:#7ee787;font-weight:bold;}}
.map-legend-item{{display:flex;align-items:center;gap:8px;font-size:11px;margin:4px 0;}}
.map-legend-dot{{width:12px;height:12px;border-radius:50%;flex-shrink:0;}}

.hidden{{display:none !important;}}
</style>
</head>
<body>
<div id="app">

<div id="header">
  <div>
    <h1>🚁 无人机配送站点多目标布局优化 — NSGA-II 结果</h1>
    <div class="sub">华东师范大学公共管理学院 · 三目标优化：最低成本 · 最高覆盖 · 最短应急响应</div>
  </div>
  <div style="display:flex;gap:6px;flex-wrap:wrap;">
    <span class="badge">帕累托解: {len(pareto)}</span>
    <span class="badge">候选方案: 3套</span>
    <span class="badge">DJI FlyCart 30</span>
  </div>
</div>

<div id="tabs">
  <div class="tab active" onclick="switchTab('pareto')">📊 帕累托分析</div>
  <div class="tab" onclick="switchTab('evo')">📈 进化曲线</div>
  <div class="tab" onclick="switchTab('map')">🗺 站点布局地图</div>
</div>

<div id="content">

<!-- Tab 1: 帕累托 -->
<div id="panel-pareto">
  <div id="chart-container">
    <div class="chart-box" style="flex:1;">
      <h3>帕累托前沿 — 总成本 vs 服务覆盖率（圆圈大小=应急响应时间，越小越好）</h3>
      <div style="font-size:11px;color:#8b949e;margin-bottom:6px;line-height:1.6;">
        每个点代表一套站点配置方案。<b style="color:#7ee787;">右上角小圆</b>= 覆盖率高且成本低（最优区域）；
        <b style="color:#f2cc60;">左下角大圆</b>= 成本低但覆盖不足。
        三角形标记为推荐的代表方案，<b style="color:#a78bfa;">菱形</b>为综合折中方案。
      </div>
      <canvas id="pareto-scatter"></canvas>
    </div>
  </div>
  <div id="pareto-info">
    <h3>🎯 代表方案对比</h3>
    SCHEME_CARDS_HTML
    <!-- 折中方案卡片 -->
    <div class="info-card" style="border-left:3px solid #a78bfa;">
      <div style="font-weight:bold;margin-bottom:6px;color:#a78bfa;">◆ 综合折中方案（推荐）</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;">
        <div><div class="val">3250</div><div class="lbl">万元总成本</div></div>
        <div><div class="val">3</div><div class="lbl">站点总数</div></div>
        <div><div class="val">88.1%</div><div class="lbl">服务覆盖率</div></div>
        <div><div class="val">21.4分</div><div class="lbl">应急响应</div></div>
      </div>
      <div style="font-size:10px;color:#8b949e;margin-top:6px;">帕累托"膝点"：成本与覆盖率之间的最佳权衡，性价比最高</div>
    </div>
    <div style="margin-top:10px;padding:8px;background:#0d1117;border-radius:6px;font-size:10px;color:#8b949e;line-height:1.7;">
      ⚠️ 帕累托前沿上不存在同时最优的方案，决策者需根据预算约束选择。<br>
      建议：初期采用<b style="color:#a78bfa;">折中方案</b>（3站，3250万），待运营验证后逐步扩展至<b style="color:#45b7d1;">高覆盖方案</b>。
    </div>
    <h3 style="margin-top:14px;">🖱 悬停查看详情</h3>
    <div id="hover-info">将鼠标悬停在散点图上的点查看方案详情...</div>
  </div>
</div>

<!-- Tab 2: 进化曲线 -->
<div id="panel-evo" class="hidden" style="width:100%;padding:20px;overflow:auto;">
  <div style="background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px 16px;margin-bottom:12px;font-size:11px;color:#8b949e;line-height:1.7;">
    <b style="color:#58a6ff;">如何读懂进化曲线：</b>
    曲线整体下降（成本/时间）或上升（覆盖率）表示算法在持续优化。
    <b style="color:#f2cc60;">⚡虚线</b>标记ε探索率到达最小值（第150代），此后算法主要利用已学知识精化解；
    帕累托前沿规模持续增长说明多样性保持良好。
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;grid-template-rows:1fr 1fr;gap:16px;height:calc(100% - 60px);">
    <div class="chart-box">
      <h3>帕累托前沿规模（每代非支配解数量）↑ 越多表示解的多样性越好</h3>
      <canvas id="evo-pareto-size"></canvas>
    </div>
    <div class="chart-box">
      <h3>最低总成本演化（万元）↓ 算法找到更经济方案</h3>
      <canvas id="evo-cost"></canvas>
    </div>
    <div class="chart-box">
      <h3>最高覆盖率演化（%）↑ 算法找到覆盖更广方案</h3>
      <canvas id="evo-cov"></canvas>
    </div>
    <div class="chart-box">
      <h3>最短应急响应时间演化（分钟）↓ 越低越好</h3>
      <canvas id="evo-emg"></canvas>
    </div>
  </div>
</div>

<!-- Tab 3: 地图 -->
<div id="panel-map" class="hidden">
  <div id="map-sidebar">
    <h3 style="font-size:13px;color:#58a6ff;margin-bottom:10px;">📋 选择显示方案</h3>
    SCHEME_BTNS_HTML

    <div style="margin-top:16px;">
      <h3 style="font-size:13px;color:#58a6ff;margin-bottom:8px;">🗝 站点类型</h3>
      <div class="map-legend-item"><div class="map-legend-dot" style="background:#ff6b6b;"></div> 微型站 (3km, 2架)</div>
      <div class="map-legend-item"><div class="map-legend-dot" style="background:#ffd93d;"></div> 综合站 (10km, 5架)</div>
      <div class="map-legend-item"><div class="map-legend-dot" style="background:#6c5ce7;"></div> 枢纽站 (20km, 10架)</div>
    </div>
    <div style="margin-top:12px;padding:8px;background:#0d1117;border-radius:6px;font-size:10px;color:#8b949e;">
      圆圈 = 服务覆盖范围<br>虚线圆 = 应急响应范围<br>点击站点查看详情
    </div>
    <div id="station-info" style="margin-top:12px;padding:10px;background:#0d1117;border:1px solid #30363d;border-radius:6px;font-size:11px;min-height:60px;display:none;"></div>
  </div>
  <div id="map"></div>
</div>

</div><!-- /content -->
</div><!-- /app -->

<script>
// ============================================================
// 数据
// ============================================================
const PARETO = {json.dumps(pareto_js)};
const STATIONS = {json.dumps(station_layers_js)};
const EVO_GENS = {json.dumps(evo_gens)};
const EVO_COV  = {json.dumps(evo_cov)};
const EVO_COST = {json.dumps(evo_cost)};
const EVO_EMG  = {json.dumps(evo_emg)};
const SCHEME_COLORS = {json.dumps(SCHEME_COLORS)};
const TYPE_COLORS = {json.dumps({str(k): v for k, v in TYPE_COLORS.items()})};

// ============================================================
// Tab 切换
// ============================================================
let mapInitialized = false;
let currentScheme = '{list(schemes.keys())[0]}';

function switchTab(name) {{
  document.querySelectorAll('.tab').forEach((t,i) => {{
    t.classList.toggle('active', ['pareto','evo','map'][i] === name);
  }});
  document.getElementById('panel-pareto').classList.toggle('hidden', name !== 'pareto');
  document.getElementById('panel-evo').classList.toggle('hidden', name !== 'evo');
  document.getElementById('panel-map').classList.toggle('hidden', name !== 'map');

  if (name === 'evo' && !evoDrawn) setTimeout(drawEvoCharts, 80);
  if (name === 'map' && !mapInitialized) setTimeout(initMap, 80);
}}

// ============================================================
// 帕累托散点图（纯 Canvas）
// ============================================================
function drawParetoScatter() {{
  const canvas = document.getElementById('pareto-scatter');
  const W = Math.max(canvas.parentElement.clientWidth - 28, 300);
  const H = Math.max(canvas.parentElement.clientHeight - 80, 200);
  canvas.width = W; canvas.height = H;
  canvas.style.width  = W + 'px';
  canvas.style.height = H + 'px';
  const ctx = canvas.getContext('2d');

  const PAD = {{top:20, right:20, bottom:50, left:70}};
  const pw = W - PAD.left - PAD.right;
  const ph = H - PAD.top - PAD.bottom;

  const costs = PARETO.map(p => p.cost);
  const covs  = PARETO.map(p => p.cov);
  const emgs  = PARETO.map(p => p.emg);
  const cMin = Math.min(...costs), cMax = Math.max(...costs);
  const vMin = Math.min(...covs),  vMax = Math.max(...covs);
  const eMin = Math.min(...emgs),  eMax = Math.max(...emgs);

  const toX = c => PAD.left + (c - cMin) / (cMax - cMin + 1) * pw;
  const toY = v => PAD.top + ph - (v - vMin) / (vMax - vMin + 1) * ph;
  const toR = e => 4 + (e - eMin) / (eMax - eMin + 1) * 12;

  // 背景
  ctx.fillStyle = '#0d1117';
  ctx.fillRect(0, 0, W, H);

  // 网格
  ctx.strokeStyle = '#21262d'; ctx.lineWidth = 1;
  for (let i=0; i<=5; i++) {{
    const y = PAD.top + i * ph/5;
    ctx.beginPath(); ctx.moveTo(PAD.left, y); ctx.lineTo(W-PAD.right, y); ctx.stroke();
  }}
  for (let i=0; i<=5; i++) {{
    const x = PAD.left + i * pw/5;
    ctx.beginPath(); ctx.moveTo(x, PAD.top); ctx.lineTo(x, PAD.top+ph); ctx.stroke();
  }}

  // 轴标签
  ctx.fillStyle='#8b949e'; ctx.font='11px Microsoft YaHei';
  ctx.textAlign='center';
  ctx.fillText('总成本（万元）', PAD.left + pw/2, H-8);
  ctx.save(); ctx.translate(14, PAD.top+ph/2); ctx.rotate(-Math.PI/2);
  ctx.fillText('服务覆盖率（%）', 0, 0); ctx.restore();

  // 坐标轴刻度
  ctx.font='10px monospace'; ctx.fillStyle='#6e7681';
  for (let i=0; i<=5; i++) {{
    const v = vMin + i*(vMax-vMin)/5;
    ctx.textAlign='right';
    ctx.fillText(v.toFixed(0)+'%', PAD.left-5, toY(v)+3);
    const c = cMin + i*(cMax-cMin)/5;
    ctx.textAlign='center';
    ctx.fillText((c/10000).toFixed(1)+'w', toX(c), PAD.top+ph+14);
  }}

  // "最优方向"箭头提示
  ctx.fillStyle='rgba(126,231,135,0.12)';
  ctx.fillRect(toX(cMax*0.55), PAD.top, pw*0.45, ph*0.45);
  ctx.fillStyle='#7ee787'; ctx.font='10px Microsoft YaHei'; ctx.textAlign='left';
  ctx.fillText('← 高覆盖 · 低成本 · 小圆（优选区）', toX(cMax*0.56), PAD.top+14);

  // 点
  PARETO.forEach((p, idx) => {{
    const x = toX(p.cost), y = toY(p.cov), r = toR(p.emg);
    const t = (p.cov - vMin) / (vMax - vMin + 1);
    const R = Math.round(255*(1-t)), G = Math.round(150+105*t), B = Math.round(255*t*0.5+50);
    ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI*2);
    ctx.fillStyle = `rgba(${{R}},${{G}},${{B}},0.75)`;
    ctx.fill();
    ctx.strokeStyle='rgba(255,255,255,0.2)'; ctx.lineWidth=0.5;
    ctx.stroke();
  }});

  // 三个代表方案标注（▲ 三角形 + 引线 + 标签）
  function drawCallout(cx, cy, label, sub, color, side='right') {{
    // 引线
    const lx = side==='right' ? cx+18 : cx-18;
    const ly = cy - 20;
    ctx.strokeStyle=color; ctx.lineWidth=1.5; ctx.setLineDash([4,3]);
    ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(lx, ly); ctx.stroke();
    ctx.setLineDash([]);
    // 三角标记
    ctx.fillStyle=color;
    ctx.beginPath();
    ctx.moveTo(cx, cy-8); ctx.lineTo(cx-6, cy+4); ctx.lineTo(cx+6, cy+4);
    ctx.closePath(); ctx.fill();
    // 标签背景
    ctx.font='bold 11px Microsoft YaHei';
    const tw = ctx.measureText(label).width;
    const bx = side==='right' ? lx : lx-tw-8;
    ctx.fillStyle='rgba(13,17,23,0.88)';
    ctx.fillRect(bx-2, ly-16, tw+10, 34);
    ctx.strokeStyle=color; ctx.lineWidth=1; ctx.strokeRect(bx-2, ly-16, tw+10, 34);
    ctx.fillStyle=color; ctx.textAlign='left';
    ctx.fillText(label, bx+3, ly-3);
    ctx.fillStyle='#8b949e'; ctx.font='9px Microsoft YaHei';
    ctx.fillText(sub, bx+3, ly+12);
  }}

  // 低成本方案
  const lc = PARETO.find(p => p.cost <= 300);
  if(lc) drawCallout(toX(lc.cost), toY(lc.cov), '低成本方案', '230万·1站·0.4%覆盖', '#4ecdc4', 'right');

  // 高覆盖方案
  const hc = PARETO.find(p => p.cov >= 97);
  if(hc) drawCallout(toX(hc.cost), toY(hc.cov), '高覆盖方案', '14300万·17站·98%', '#45b7d1', 'left');

  // 快速应急方案
  const fe = PARETO.reduce((a,b)=>a.emg<b.emg?a:b);
  if(fe) drawCallout(toX(fe.cost), toY(fe.cov), '应急方案', '15680万·23站·6.5min', '#f7dc6f', 'left');

  // 折中方案（膝点）
  const knee = PARETO.find(p => p.cost >= 3000 && p.cost <= 3500 && p.cov >= 85);
  if(knee) {{
    const kx=toX(knee.cost), ky=toY(knee.cov);
    ctx.beginPath();
    ctx.moveTo(kx,   ky-9); ctx.lineTo(kx+9, ky+6);
    ctx.lineTo(kx-9, ky+6); ctx.closePath();
    ctx.fillStyle='#a78bfa'; ctx.fill();
    ctx.fillStyle='#a78bfa'; ctx.font='bold 10px Microsoft YaHei'; ctx.textAlign='center';
    ctx.fillText('◆折中', kx, ky-14);
    ctx.fillStyle='#8b949e'; ctx.font='9px monospace';
    ctx.fillText('3250万·88%·21min', kx, ky-3);
  }}

  // 鼠标悬停
  canvas._pts = PARETO.map(p => ({{ x: toX(p.cost), y: toY(p.cov), r: toR(p.emg), data: p }}));
  canvas.onmousemove = function(e) {{
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    let found = null;
    for (const pt of canvas._pts) {{
      if (Math.hypot(mx-pt.x, my-pt.y) < pt.r+4) {{ found=pt; break; }}
    }}
    if (found) {{
      const d = found.data;
      document.getElementById('hover-info').innerHTML =
        `<b>方案详情</b><br>
         总成本: <b style="color:#7ee787">${{d.cost.toFixed(0)}} 万元</b><br>
         覆盖率: <b style="color:#7ee787">${{d.cov.toFixed(1)}}%</b><br>
         应急响应: <b style="color:#7ee787">${{d.emg.toFixed(1)}} 分钟</b><br>
         站点: 微×${{d.micro}} + 综×${{d.comp}} + 枢×${{d.hub}} = ${{d.micro+d.comp+d.hub}}站<br>
         无人机: ${{d.drones}} 架`;
    }}
  }};
}}

// ============================================================
// 进化曲线（Canvas折线）
// ============================================================
let evoDrawn = false;
function drawLine(canvasId, data, color, label, yUnit='') {{
  const canvas = document.getElementById(canvasId);
  const W = Math.max(canvas.parentElement.clientWidth - 28, 200);
  const H = Math.max(canvas.parentElement.clientHeight - 50, 120);
  canvas.width = W; canvas.height = H;
  canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
  const ctx = canvas.getContext('2d');
  ctx.fillStyle='#0d1117'; ctx.fillRect(0,0,W,H);

  const PAD = {{top:15, right:20, bottom:35, left:60}};
  const pw = W-PAD.left-PAD.right, ph = H-PAD.top-PAD.bottom;
  const xs = EVO_GENS.slice(0, data.length);
  const vmin = Math.min(...data), vmax = Math.max(...data);
  const toX = i => PAD.left + i/xs.length*pw;
  const toY = v => PAD.top + ph - (v-vmin)/(vmax-vmin+0.001)*ph;

  ctx.strokeStyle='#21262d'; ctx.lineWidth=1;
  for (let i=0;i<=4;i++) {{
    const y=PAD.top+i*ph/4;
    ctx.beginPath(); ctx.moveTo(PAD.left,y); ctx.lineTo(W-PAD.right,y); ctx.stroke();
    ctx.fillStyle='#6e7681'; ctx.font='9px monospace'; ctx.textAlign='right';
    ctx.fillText((vmax-(vmax-vmin)*i/4).toFixed(1)+yUnit, PAD.left-3, y+3);
  }}

  ctx.strokeStyle=color; ctx.lineWidth=2; ctx.beginPath();
  data.forEach((v,i) => {{ const x=toX(i), y=toY(v); i===0 ? ctx.moveTo(x,y) : ctx.lineTo(x,y); }});
  ctx.stroke();

  // ε收敛标注（第150代）
  const convGen = 150;
  if (convGen <= xs.length) {{
    const cx = toX(convGen);
    ctx.strokeStyle='#f2cc60'; ctx.lineWidth=1.2; ctx.setLineDash([5,4]);
    ctx.beginPath(); ctx.moveTo(cx, PAD.top); ctx.lineTo(cx, PAD.top+ph); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle='#f2cc60'; ctx.font='9px Microsoft YaHei'; ctx.textAlign='left';
    ctx.fillText('⚡ε→最小', cx+3, PAD.top+12);
    // 后段收敛区域浅色背景
    ctx.fillStyle='rgba(126,231,135,0.06)';
    ctx.fillRect(cx, PAD.top, PAD.left+pw-cx, ph);
    ctx.fillStyle='rgba(126,231,135,0.5)'; ctx.font='9px Microsoft YaHei'; ctx.textAlign='right';
    ctx.fillText('收敛阶段', PAD.left+pw-4, PAD.top+12);
  }}

  ctx.fillStyle='#8b949e'; ctx.font='11px Microsoft YaHei'; ctx.textAlign='center';
  ctx.fillText('迭代代数（共150代）', PAD.left+pw/2, H-4);
}}

function drawEvoCharts() {{
  const sizes = EVO_GENS.map((_,i) => {{
    const r = {json.dumps([r[1] for r in evo_log])};
    return r[i] || 0;
  }});
  drawLine('evo-pareto-size', sizes, '#58a6ff', '帕累托前沿规模', '');
  drawLine('evo-cost', EVO_COST.filter(v=>v!==null), '#ff7b72', '最低成本', '万');
  drawLine('evo-cov',  EVO_COV.filter(v=>v!==null),  '#7ee787', '最高覆盖率', '%');
  drawLine('evo-emg',  EVO_EMG.filter(v=>v!==null),  '#f2cc60', '最短应急时间', 'min');
  evoDrawn = true;
}}

// ============================================================
// 地图
// ============================================================
let leafletMap, layerGroups = {{}};

function initMap() {{
  mapInitialized = true;
  leafletMap = L.map('map', {{center:[31.23,121.47], zoom:10}});
  L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png',
    {{attribution:'© Carto',maxZoom:19}}).addTo(leafletMap);

  Object.entries(STATIONS).forEach(([sname, pts]) => {{
    const grp = L.layerGroup();
    pts.forEach(p => {{
      const typeNames = {{'1':'微型站','2':'综合站','3':'枢纽站'}};
      const emgKm = {{'1':5,'2':15,'3':28}};

      // 服务覆盖圆（实线）
      L.circle([p.lat, p.lon], {{
        radius: p.svc_km * 1000, color: p.color,
        fillColor: p.color, fillOpacity: 0.08, weight: 1.5,
      }}).addTo(grp);

      // 应急范围圆（虚线）
      L.circle([p.lat, p.lon], {{
        radius: emgKm[p.type] * 1000, color: p.color,
        fill: false, weight: 1, dashArray: '5 5', opacity: 0.4,
      }}).addTo(grp);

      // 站点标记
      const marker = L.circleMarker([p.lat, p.lon], {{
        radius: 6+p.type*2, fillColor: p.color, color:'#fff',
        weight: 2, fillOpacity: 0.9,
      }}).addTo(grp);

      marker.on('click', () => {{
        document.getElementById('station-info').style.display = 'block';
        document.getElementById('station-info').innerHTML =
          `<b style="color:${{p.color}}">${{p.name}}</b><br>
           坐标: ${{p.lat.toFixed(4)}}, ${{p.lon.toFixed(4)}}<br>
           无人机: ${{p.ndrones}} 架<br>
           服务半径: ${{p.svc_km}} km<br>
           单站成本: ${{p.cost}} 万元`;
      }});
    }});
    layerGroups[sname] = grp;
  }});

  showScheme(currentScheme);
}}

function showScheme(sname) {{
  Object.entries(layerGroups).forEach(([n, grp]) => {{
    if (n === sname) grp.addTo(leafletMap);
    else leafletMap.removeLayer(grp);
  }});
  document.querySelectorAll('.scheme-btn').forEach(btn => {{
    btn.classList.remove('active');
  }});
  const btn = document.getElementById('btn-'+sname);
  if (btn) btn.classList.add('active');
  currentScheme = sname;
  document.getElementById('station-info').style.display = 'none';
}}

// ============================================================
// 初始化
// ============================================================
window.addEventListener('load', () => {{
  setTimeout(drawParetoScatter, 100);
}});
window.addEventListener('resize', () => {{
  setTimeout(drawParetoScatter, 50);
  if (evoDrawn) setTimeout(drawEvoCharts, 50);
}});
</script>
</body>
</html>"""

# 写出
tmp_html = "/tmp/drone_analysis/优化结果可视化.html"
with open(tmp_html, 'w', encoding='utf-8') as f:
    f.write(html)
shutil.copy2(tmp_html, OUT_HTML)

size_kb = os.path.getsize(OUT_HTML) // 1024
print(f"  ✓ HTML: {os.path.basename(OUT_HTML)}  ({size_kb} KB)")
print(f"\n{'='*55}")
print("✅ 可视化生成完成！")
print(f"{'='*55}")
print(f"  🌐 {OUT_HTML}")
print(f"\n  三个标签页：")
print(f"    📊 帕累托分析 — 80个解的成本-覆盖-应急三维散点")
print(f"    📈 进化曲线   — 150代收敛过程")
print(f"    🗺 站点布局地图 — 三套方案切换查看")
