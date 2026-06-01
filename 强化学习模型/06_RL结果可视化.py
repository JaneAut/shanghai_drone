"""
RL 训练结果可视化
=================
读取 RL训练日志.json + 最优预调整方案.json，
生成包含四个面板的交互式 HTML 报告：
  1. 训练奖励曲线 + 移动平均
  2. 损失曲线 + ε 衰减
  3. 最优预调整方案地图（Leaflet）
  4. 各灾害/天气场景下的策略热图
"""

import os, json, shutil
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TMP        = "/tmp/drone_analysis"
OUT_HTML   = os.path.join(SCRIPT_DIR, "RL应急预调整可视化.html")

# 读取数据
with open(os.path.join(SCRIPT_DIR, "RL训练日志.json"), encoding='utf-8') as f:
    data = json.load(f)
with open(os.path.join(SCRIPT_DIR, "最优预调整方案.json"), encoding='utf-8') as f:
    plan = json.load(f)

log        = data["train_log"]
test_res   = data["test_results"]
hp         = data["hyperparams"]
env_cfg    = data["env_config"]
stations   = data["station_info"]
steps      = plan["steps"]

print("=" * 55)
print("RL 结果可视化生成")
print("=" * 55)

# 平滑函数
def smooth(arr, w=20):
    out = []
    for i in range(len(arr)):
        start = max(0, i-w+1)
        out.append(sum(arr[start:i+1]) / (i - start + 1))
    return out

episodes  = [r["episode"] for r in log]
rewards   = [r["reward"]  for r in log]
losses    = [r["loss"]    for r in log]
epsilons  = [r["epsilon"] for r in log]
disasters = [r["disaster"] for r in log]
smooth_r  = smooth(rewards, 30)
smooth_l  = smooth([l for l in losses if l > 0], 20)

# 各灾害类型平均奖励
disaster_types = list(set(disasters))
disaster_avgs  = {
    d: round(sum(r for r, dt in zip(rewards, disasters) if dt == d) /
             max(1, sum(1 for dt in disasters if dt == d)), 3)
    for d in disaster_types
}

# 站点调配热图数据（从测试结果汇总）
action_counts = [0] * len(stations)
for tr in test_res:
    for k, v in tr["supply_distribution"].items():
        # 解析站点索引
        try:
            idx = int(k.split("站点")[1].split("(")[0])
            if 0 <= idx < len(stations):
                action_counts[idx] += v
        except:
            pass

# 最优方案各步动作
supply_timeline = []
for s in steps:
    supply_timeline.append({
        "step": s["step"],
        "action": s["action"],
        "desc": s["action_desc"],
        "supply": s["supply_snapshot"],
        "depot": s["depot_remaining"],
    })

# ================================================================
# HTML 生成
# ================================================================
STYPE_COLOR  = {"综合站": "#ffd93d", "枢纽站": "#6c5ce7"}
DISASTER_CLR = {"洪涝":"#4fc3f7","地震":"#ef9a9a","大火":"#ffcc02","停电":"#a5d6a7"}

station_js = json.dumps([{
    "lon": s["lon"], "lat": s["lat"],
    "name": s["name"], "type": s["type"],
    "ndrones": s["ndrones"], "svc_km": s["service_km"],
    "color": STYPE_COLOR.get(s["name"], "#7ec8e3"),
    "max_supply": s["max_supply"],
} for s in stations])

timeline_js  = json.dumps(supply_timeline)
action_cnt_js = json.dumps(action_counts)

html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>RL 应急物资预调整模型结果</title>
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
#header h1{{font-size:17px;color:#7ee787;}}
#header .sub{{font-size:11px;color:#8b949e;margin-top:2px;}}
.badge{{background:#21262d;border:1px solid #30363d;color:#7ee787;
       padding:3px 10px;border-radius:12px;font-size:11px;}}
#grid{{
  display:grid;
  grid-template-columns:1fr 1fr;
  grid-template-rows:1fr 1fr;
  flex:1;gap:1px;background:#30363d;overflow:hidden;
}}
.panel{{background:#0d1117;padding:12px 14px;display:flex;flex-direction:column;overflow:hidden;}}
.panel h3{{font-size:13px;color:#58a6ff;margin-bottom:4px;border-bottom:1px solid #21262d;padding-bottom:4px;flex-shrink:0;}}
.panel-desc{{font-size:10px;color:#8b949e;line-height:1.5;flex-shrink:0;margin-bottom:4px;
  max-height:36px;overflow:hidden;}}
canvas{{display:block;flex-shrink:0;}}
#map-panel{{padding:0;position:relative;}}
#map-panel h3{{position:absolute;top:10px;left:10px;z-index:1000;
  background:rgba(13,17,23,0.85);padding:6px 12px;border-radius:6px;
  font-size:13px;color:#7ee787;border:1px solid #30363d;}}
#map{{width:100%;height:100%;}}
.map-controls{{
  position:absolute;bottom:10px;left:10px;z-index:1000;
  background:rgba(13,17,23,0.9);border:1px solid #30363d;
  border-radius:6px;padding:8px 12px;font-size:11px;
}}
.map-controls select{{background:#161b22;color:#e6edf3;border:1px solid #30363d;
  padding:3px 6px;border-radius:4px;font-size:11px;margin-left:6px;}}
.step-display{{
  position:absolute;bottom:10px;right:10px;z-index:1000;
  background:rgba(13,17,23,0.9);border:1px solid #30363d;
  border-radius:6px;padding:8px 12px;font-size:11px;min-width:160px;
}}
</style>
</head>
<body>
<div id="app">
<div id="header">
  <div>
    <h1>🤖 DQN 应急物资预调整模型 — 训练结果报告</h1>
    <div class="sub">华东师范大学 · 强化学习模块 · {hp['n_episodes']}回合训练 · 状态维度{env_cfg['state_dim']} · 动作数{env_cfg['n_actions']}</div>
  </div>
  <div style="display:flex;gap:6px;flex-wrap:wrap;">
    <span class="badge">γ={hp['gamma']}</span>
    <span class="badge">lr={hp['lr']}</span>
    <span class="badge">Replay {hp['buffer_size']}</span>
    <span class="badge">最优奖励 {data['best_reward']:.2f}</span>
  </div>
</div>

<div id="grid">

<!-- 面板1：训练奖励曲线 -->
<div class="panel">
  <h3>📈 训练奖励曲线（回合奖励 + 30回合移动平均）</h3>
  <div class="panel-desc">
    <b style="color:#7ee787;">正奖励</b>=覆盖成功；<b style="color:#ff7b72;">负奖励</b>=物资不足/无效调配；
    <b style="color:#f2cc60;">⭐第468回合</b>达最优（5.78）。整体上升=策略持续改进。
  </div>
  <canvas id="reward-canvas"></canvas>
</div>

<!-- 面板2：损失 + ε 衰减 -->
<div class="panel">
  <h3>📉 损失曲线（Q网络MSE）& ε 探索率衰减</h3>
  <div class="panel-desc">
    <b style="color:#ff7b72;">损失↓</b>=Q值预测越准；<b style="color:#58a6ff;">ε（蓝线）</b>0.9→0.05，前期探索后期利用；
    <b style="color:#f2cc60;">⚡500回合</b>后ε固定。
  </div>
  <canvas id="loss-canvas"></canvas>
</div>

<!-- 面板3：最优预调整地图 -->
<div class="panel" id="map-panel">
  <h3>🗺 最优预调整方案地图（场景：晴好+洪涝，20步预警窗口）</h3>
  <div id="map"></div>
  <div class="map-controls">
    时间步（共24步预警窗口）:
    <select id="step-select" onchange="showStep(this.value)">
      {"".join(f'<option value="{s["step"]}">{s["step"]}步 — {s["desc"]}</option>'
               for s in supply_timeline)}
    </select>
  </div>
  <div class="step-display" id="step-info">
    <b>如何读图：</b>圆圈越大=物资越满<br>切换时间步观察调配过程
  </div>
</div>

<!-- 面板4：策略分析 -->
<div class="panel">
  <h3>🎯 策略分析：调配频率 & 各灾害场景表现</h3>
  <div class="panel-desc">
    <b>左：</b>柱越高=该站越关键；<b>右：</b>各灾害类型平均奖励，越高=应对越好。
  </div>
  <canvas id="analysis-canvas"></canvas>
</div>

</div>
</div>

<script>
// ============================================================
// 数据
// ============================================================
const EPISODES   = {json.dumps(episodes)};
const REWARDS    = {json.dumps(rewards)};
const SMOOTH_R   = {json.dumps(smooth_r)};
const LOSSES     = {json.dumps(losses)};
const SMOOTH_L   = {json.dumps(smooth(losses, 20))};
const EPSILONS   = {json.dumps(epsilons)};
const STATIONS   = {station_js};
const TIMELINE   = {timeline_js};
const ACTION_CNT = {action_cnt_js};
const DISASTER_AVGS = {json.dumps(disaster_avgs)};
const TEST_AVG   = {round(sum(r['reward'] for r in test_res)/len(test_res), 3)};
const TEST_STD   = {round((sum((r['reward']-sum(r2['reward'] for r2 in test_res)/len(test_res))**2 for r in test_res)/len(test_res))**0.5, 3)};

// ============================================================
// 通用 Canvas 绘图工具
// ============================================================
function clearCanvas(ctx, W, H) {{
  ctx.fillStyle = '#0d1117'; ctx.fillRect(0,0,W,H);
}}
function drawGrid(ctx, pad, pw, ph, nv=5, nh=5) {{
  ctx.strokeStyle='#21262d'; ctx.lineWidth=1;
  for(let i=0;i<=nv;i++) {{
    const y=pad.top+i*ph/nv;
    ctx.beginPath();ctx.moveTo(pad.left,y);ctx.lineTo(pad.left+pw,y);ctx.stroke();
  }}
  for(let i=0;i<=nh;i++) {{
    const x=pad.left+i*pw/nh;
    ctx.beginPath();ctx.moveTo(x,pad.top);ctx.lineTo(x,pad.top+ph);ctx.stroke();
  }}
}}
function drawLine(ctx, xs, ys, toX, toY, color, lw=1.5) {{
  ctx.strokeStyle=color; ctx.lineWidth=lw; ctx.beginPath();
  xs.forEach((x,i) => {{ const px=toX(x),py=toY(ys[i]); i===0?ctx.moveTo(px,py):ctx.lineTo(px,py); }});
  ctx.stroke();
}}
function axisLabel(ctx, text, x, y, color='#8b949e', size=10) {{
  ctx.fillStyle=color; ctx.font=size+'px Microsoft YaHei';
  ctx.textAlign='center'; ctx.fillText(text, x, y);
}}

// ============================================================
// 面板1：奖励曲线
// ============================================================
// 用 window 尺寸计算每个面板的 canvas 大小（4格等分，绕开 clientHeight=0 的时序问题）
function panelSize() {{
  const HEADER = 56, GAP = 1, PAD = 26, DESC = 42, H3 = 30;
  const W = Math.floor(window.innerWidth  / 2) - GAP - PAD;
  const H = Math.floor((window.innerHeight - HEADER) / 2) - GAP - PAD - DESC - H3;
  return {{ W: Math.max(W, 200), H: Math.max(H, 120) }};
}}

function drawReward() {{
  const canvas=document.getElementById('reward-canvas');
  const {{W, H}} = panelSize();
  canvas.width=W; canvas.height=H;
  canvas.style.width=W+'px'; canvas.style.height=H+'px';
  const ctx=canvas.getContext('2d');
  clearCanvas(ctx,W,H);
  const pad={{top:20,right:20,bottom:40,left:60}};
  const pw=W-pad.left-pad.right, ph=H-pad.top-pad.bottom;
  const rMin=Math.min(...REWARDS), rMax=Math.max(...REWARDS);
  const toX=e=>pad.left+e/EPISODES.length*pw;
  const toY=r=>pad.top+ph-(r-rMin)/(rMax-rMin+0.001)*ph;
  drawGrid(ctx,pad,pw,ph);
  // 零线
  const zeroY=toY(0);
  ctx.strokeStyle='#30363d'; ctx.lineWidth=1; ctx.setLineDash([4,4]);
  ctx.beginPath();ctx.moveTo(pad.left,zeroY);ctx.lineTo(pad.left+pw,zeroY);ctx.stroke();
  ctx.setLineDash([]);
  // 原始奖励（细线，低透明度）
  ctx.globalAlpha=0.3;
  drawLine(ctx,EPISODES,REWARDS,toX,toY,'#7ee787',1);
  ctx.globalAlpha=1;
  // 移动平均
  drawLine(ctx,EPISODES,SMOOTH_R,toX,toY,'#7ee787',2.5);
  // 测试平均线
  const testY=toY(TEST_AVG);
  ctx.strokeStyle='#f2cc60'; ctx.lineWidth=1.5; ctx.setLineDash([6,4]);
  ctx.beginPath();ctx.moveTo(pad.left,testY);ctx.lineTo(pad.left+pw,testY);ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle='#f2cc60'; ctx.font='10px monospace'; ctx.textAlign='left';
  ctx.fillText('测试均值 '+TEST_AVG, pad.left+4, testY-4);
  // 轴
  for(let i=0;i<=5;i++) {{
    const r=rMin+i*(rMax-rMin)/5;
    ctx.fillStyle='#6e7681'; ctx.font='9px monospace'; ctx.textAlign='right';
    ctx.fillText(r.toFixed(1), pad.left-4, toY(r)+3);
    const e=Math.round(i*EPISODES.length/5);
    axisLabel(ctx,e,toX(e),pad.top+ph+14);
  }}
  // 最优回合标注（第468回合）
  const bestEp = 468;
  const bestX = toX(bestEp);
  ctx.strokeStyle='#f2cc60'; ctx.lineWidth=1.5; ctx.setLineDash([5,4]);
  ctx.beginPath(); ctx.moveTo(bestX, pad.top); ctx.lineTo(bestX, pad.top+ph); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle='rgba(242,204,96,0.12)';
  ctx.fillRect(bestX-18, pad.top, 36, ph);
  ctx.fillStyle='#f2cc60'; ctx.font='bold 10px Microsoft YaHei'; ctx.textAlign='center';
  ctx.fillText('⭐最优', bestX, pad.top+12);
  ctx.font='9px monospace';
  ctx.fillText('Ep.468', bestX, pad.top+24);
  ctx.fillText('奖励5.78', bestX, pad.top+36);

  // ε收敛后的收敛区域（第500回合后）
  const convX = toX(500);
  ctx.fillStyle='rgba(126,231,135,0.05)';
  ctx.fillRect(convX, pad.top, pad.left+pw-convX, ph);
  ctx.fillStyle='rgba(126,231,135,0.6)'; ctx.font='9px Microsoft YaHei'; ctx.textAlign='right';
  ctx.fillText('策略稳定期（ε=0.05）', pad.left+pw-6, pad.top+12);

  // 零基准线
  const zeroLineY = toY(0);
  if(zeroLineY > pad.top && zeroLineY < pad.top+ph) {{
    ctx.strokeStyle='#30363d'; ctx.lineWidth=1; ctx.setLineDash([4,4]);
    ctx.beginPath(); ctx.moveTo(pad.left, zeroLineY); ctx.lineTo(pad.left+pw, zeroLineY); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle='#6e7681'; ctx.font='9px monospace'; ctx.textAlign='left';
    ctx.fillText('0', pad.left+2, zeroLineY-3);
  }}

  axisLabel(ctx,'回合（共600回合）',pad.left+pw/2,H-4,'#8b949e',11);
  ctx.save(); ctx.translate(14,pad.top+ph/2); ctx.rotate(-Math.PI/2);
  axisLabel(ctx,'回合总奖励',0,0,'#8b949e',11); ctx.restore();
  // 图例
  ctx.fillStyle='#7ee787'; ctx.font='11px Microsoft YaHei'; ctx.textAlign='left';
  ctx.fillText('─── 移动平均(30回合)', pad.left+8, pad.top+14);
}}

// ============================================================
// 面板2：损失 + ε
// ============================================================
function drawLoss() {{
  const canvas=document.getElementById('loss-canvas');
  const {{W, H}} = panelSize();
  canvas.width=W; canvas.height=H;
  canvas.style.width=W+'px'; canvas.style.height=H+'px';
  const ctx=canvas.getContext('2d');
  clearCanvas(ctx,W,H);
  const pad={{top:20,right:60,bottom:40,left:60}};
  const pw=W-pad.left-pad.right, ph=H-pad.top-pad.bottom;
  const validL = SMOOTH_L.filter(v=>v>0);
  const lMin=Math.min(...validL)||0, lMax=Math.max(...validL)||1;
  const eMin=Math.min(...EPSILONS), eMax=Math.max(...EPSILONS);
  const toX=i=>pad.left+i/(EPISODES.length)*pw;
  const toYL=v=>pad.top+ph-(v-lMin)/(lMax-lMin+0.0001)*ph;
  const toYE=v=>pad.top+ph-(v-eMin)/(eMax-eMin+0.0001)*ph;
  drawGrid(ctx,pad,pw,ph);
  // 损失曲线
  ctx.globalAlpha=0.25;
  drawLine(ctx,EPISODES,LOSSES,toX,toYL,'#ff7b72',1);
  ctx.globalAlpha=1;
  const smoothLEps=SMOOTH_L;
  drawLine(ctx,EPISODES.slice(0,smoothLEps.length),smoothLEps,toX,toYL,'#ff7b72',2.5);
  // ε 曲线（右轴）
  drawLine(ctx,EPISODES,EPSILONS,toX,toYE,'#58a6ff',1.5);
  // 轴标签
  for(let i=0;i<=5;i++) {{
    const v=lMin+i*(lMax-lMin)/5;
    ctx.fillStyle='#ff7b72'; ctx.font='9px monospace'; ctx.textAlign='right';
    ctx.fillText(v.toFixed(4), pad.left-4, toYL(v)+3);
    const ep=eMin+i*(eMax-eMin)/5;
    ctx.fillStyle='#58a6ff'; ctx.textAlign='left';
    ctx.fillText(ep.toFixed(2), pad.left+pw+4, toYE(ep)+3);
  }}
  // ε到达最小值标注（第500回合）
  const epConvX = toX(500);
  ctx.strokeStyle='#f2cc60'; ctx.lineWidth=1.2; ctx.setLineDash([5,4]);
  ctx.beginPath(); ctx.moveTo(epConvX, pad.top); ctx.lineTo(epConvX, pad.top+ph); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle='#f2cc60'; ctx.font='9px Microsoft YaHei'; ctx.textAlign='left';
  ctx.fillText('⚡ε=0.05', epConvX+3, pad.top+12);
  // 损失稳定区提示
  ctx.fillStyle='rgba(255,123,114,0.08)';
  ctx.fillRect(epConvX, pad.top, pad.left+pw-epConvX, ph);
  ctx.fillStyle='rgba(255,123,114,0.6)'; ctx.font='9px Microsoft YaHei'; ctx.textAlign='right';
  ctx.fillText('损失趋于平稳→策略收敛', pad.left+pw-4, pad.top+12);

  axisLabel(ctx,'回合（共600回合）',pad.left+pw/2,H-4,'#8b949e',11);
  ctx.save(); ctx.translate(14,pad.top+ph/2); ctx.rotate(-Math.PI/2);
  axisLabel(ctx,'损失',0,0,'#ff7b72',11); ctx.restore();
  ctx.save(); ctx.translate(W-14,pad.top+ph/2); ctx.rotate(Math.PI/2);
  axisLabel(ctx,'ε (探索率)',0,0,'#58a6ff',11); ctx.restore();
  // 图例
  ctx.fillStyle='#ff7b72'; ctx.font='11px Microsoft YaHei'; ctx.textAlign='left';
  ctx.fillText('─── MSE损失', pad.left+8, pad.top+14);
  ctx.fillStyle='#58a6ff';
  ctx.fillText('─── ε衰减', pad.left+120, pad.top+14);
}}

// ============================================================
// 面板3：地图
// ============================================================
let leaflet = null;
let stationLayers = [];
let circleLayer   = null;

function initMap() {{
  leaflet = L.map('map', {{center:[31.1,121.45],zoom:9,zoomControl:true}});
  L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png',
    {{attribution:'© Carto',maxZoom:19}}).addTo(leaflet);
  // 初始显示第一步
  showStep(TIMELINE[0].step);
}}

function showStep(step) {{
  const t = TIMELINE.find(s => s.step == step);
  if (!t) return;
  // 清除旧图层
  stationLayers.forEach(l => leaflet.removeLayer(l));
  stationLayers = [];
  if (circleLayer) {{ leaflet.removeLayer(circleLayer); circleLayer=null; }}

  STATIONS.forEach((s,i) => {{
    const supplyRatio = t.supply[i] / s.max_supply;
    const r = 6 + supplyRatio * 10;
    const alpha = 0.4 + supplyRatio * 0.5;
    const lyr = L.circleMarker([s.lat, s.lon], {{
      radius: r, fillColor: s.color, color:'#fff',
      weight:1.5, fillOpacity: alpha,
    }}).addTo(leaflet);
    lyr.bindTooltip(
      `${{s.name}} | 物资: ${{t.supply[i].toFixed(0)}}/${{s.max_supply}}单位`,
      {{permanent:false}}
    );
    stationLayers.push(lyr);
    // 服务圆
    const svcCircle = L.circle([s.lat,s.lon], {{
      radius: s.svc_km*1000, color: s.color,
      fillColor: s.color, fillOpacity: 0.05*(supplyRatio+0.1), weight:1,
    }}).addTo(leaflet);
    stationLayers.push(svcCircle);
  }});

  // 更新信息框
  const nonIdle = t.action > 0 ? '🚚 ' + t.desc : '⏸ 待命';
  document.getElementById('step-info').innerHTML =
    `<b>第 ${{t.step}} 步</b>: ${{nonIdle}}<br>` +
    `仓库剩余: <b>${{t.depot}}</b> 单位<br>` +
    `当步奖励: ${{t.reward?.toFixed?.(3) || '—'}}`;
}}

// ============================================================
// 面板4：策略分析
// ============================================================
function drawAnalysis() {{
  const canvas=document.getElementById('analysis-canvas');
  const {{W, H}} = panelSize();
  canvas.width=W; canvas.height=H;
  canvas.style.width=W+'px'; canvas.style.height=H+'px';
  const ctx=canvas.getContext('2d');
  clearCanvas(ctx,W,H);

  // 左半：各站点调配频率柱状图
  const halfW = W/2 - 10;
  const pad={{top:30,right:10,bottom:50,left:44}};
  const pw=halfW-pad.left-pad.right, ph=H-pad.top-pad.bottom;
  const maxCnt=Math.max(...ACTION_CNT)+1;
  const barW=pw/ACTION_CNT.length*0.7;

  ctx.fillStyle='#8b949e'; ctx.font='12px Microsoft YaHei';
  ctx.textAlign='center'; ctx.fillText('测试期各站点补货次数（越高=越关键）', pad.left+pw/2, 16);

  ACTION_CNT.forEach((cnt,i) => {{
    const x=pad.left+i/ACTION_CNT.length*pw+barW*0.15;
    const barH=cnt/maxCnt*ph;
    const y=pad.top+ph-barH;
    const color=STATIONS[i]?.color||'#58a6ff';
    ctx.fillStyle=color+'cc';
    ctx.fillRect(x,y,barW,barH);
    if(cnt>0) {{
      ctx.fillStyle='#e6edf3'; ctx.font='9px monospace'; ctx.textAlign='center';
      ctx.fillText(cnt, x+barW/2, y-2);
    }}
    if(i%3===0) {{
      ctx.fillStyle='#6e7681'; ctx.font='9px monospace';
      ctx.fillText(i, x+barW/2, pad.top+ph+12);
    }}
  }});
  // 标注最高调配站点
  const maxCntIdx = ACTION_CNT.indexOf(Math.max(...ACTION_CNT));
  if(maxCntIdx>=0 && Math.max(...ACTION_CNT)>0) {{
    const hx=pad.left+maxCntIdx/ACTION_CNT.length*pw+barW*0.15+barW/2;
    const hcnt=ACTION_CNT[maxCntIdx];
    const hy=pad.top+ph-hcnt/maxCnt*ph;
    ctx.strokeStyle='#f2cc60'; ctx.lineWidth=1.2;
    ctx.beginPath(); ctx.moveTo(hx, hy-4); ctx.lineTo(hx, hy-16); ctx.stroke();
    ctx.fillStyle='#f2cc60'; ctx.font='bold 9px monospace'; ctx.textAlign='center';
    ctx.fillText('关键站点', hx, hy-19);
  }}
  axisLabel(ctx,'站点序号（0-16）',pad.left+pw/2,H-4,'#8b949e',10);
  for(let i=0;i<=4;i++) {{
    const v=maxCnt*i/4;
    ctx.fillStyle='#6e7681'; ctx.font='8px monospace'; ctx.textAlign='right';
    ctx.fillText(Math.round(v), pad.left-2, pad.top+ph*(1-i/4)+3);
  }}

  // 右半：各灾害类型平均奖励
  const rx=W/2+10;
  const rPad={{top:30,right:10,bottom:50,left:50}};
  const rpw=halfW-rPad.left-rPad.right, rph=H-rPad.top-rPad.bottom;

  ctx.fillStyle='#8b949e'; ctx.font='12px Microsoft YaHei';
  ctx.textAlign='center'; ctx.fillText('各灾害类型平均奖励', rx+rPad.left+rpw/2, 16);

  const dtype=Object.keys(DISASTER_AVGS);
  const davgs=Object.values(DISASTER_AVGS);
  const dMin=Math.min(0,...davgs), dMax=Math.max(...davgs)+0.01;
  const DCOLORS={{'洪涝':'#4fc3f7','地震':'#ef9a9a','大火':'#ffcc02','停电':'#a5d6a7'}};
  const bw2=rpw/dtype.length*0.6;

  dtype.forEach((d,i) => {{
    const v=DISASTER_AVGS[d];
    const x=rx+rPad.left+i/dtype.length*rpw+bw2*0.2;
    const barH=(v-dMin)/(dMax-dMin)*rph;
    const y=rPad.top+rph-barH;
    ctx.fillStyle=(DCOLORS[d]||'#58a6ff')+'cc';
    ctx.fillRect(x,y,bw2,barH);
    ctx.fillStyle='#e6edf3'; ctx.font='10px monospace'; ctx.textAlign='center';
    ctx.fillText(v.toFixed(2), x+bw2/2, y-3);
    ctx.fillStyle='#8b949e'; ctx.font='11px Microsoft YaHei';
    ctx.fillText(d, x+bw2/2, rPad.top+rph+16);
  }});
  // 零线
  const zy=rPad.top+rph*(1-(-dMin)/(dMax-dMin));
  ctx.strokeStyle='#30363d'; ctx.lineWidth=1; ctx.setLineDash([3,3]);
  ctx.beginPath();ctx.moveTo(rx+rPad.left,zy);ctx.lineTo(rx+rPad.left+rpw,zy);ctx.stroke();
  ctx.setLineDash([]);
  // 测试汇总
  ctx.fillStyle='#f2cc60'; ctx.font='11px Microsoft YaHei'; ctx.textAlign='left';
  ctx.fillText(`测试均值: ${{TEST_AVG}} ± ${{TEST_STD}}`, rx+rPad.left, H-6);
}}

// ============================================================
// 初始化
// ============================================================
function drawAll() {{
  drawReward();
  drawLoss();
  drawAnalysis();
}}
window.addEventListener('load', () => {{
  // 延迟100ms确保Grid布局完成计算后再绘制
  setTimeout(() => {{
    drawAll();
    initMap();
  }}, 100);
}});
window.addEventListener('resize', () => {{
  setTimeout(drawAll, 50);
}});
</script>
</body>
</html>"""

tmp_html = "/tmp/drone_analysis/RL应急预调整可视化.html"
with open(tmp_html, 'w', encoding='utf-8') as f:
    f.write(html)
shutil.copy2(tmp_html, OUT_HTML)

size_kb = os.path.getsize(OUT_HTML) // 1024
print(f"\n  ✓ HTML: {os.path.basename(OUT_HTML)} ({size_kb} KB)")
print(f"\n{'='*55}")
print("✅ RL 可视化完成！")
print(f"{'='*55}")
print(f"  四个面板：")
print(f"    📈 训练奖励曲线（30回合移动平均 + 测试均值参考线）")
print(f"    📉 MSE 损失曲线 + ε 衰减过程")
print(f"    🗺 最优预调整方案地图（时间步选择器，物资分布动态显示）")
print(f"    🎯 策略分析（各站点调配频率 + 各灾害类型平均奖励）")
