"""
上海市无人机配送需求热点 —— 交互式可视化地图
===============================================
从已建好的 SQLite 数据库读取分析结果，
生成可在浏览器直接打开的交互式 HTML 地图。

依赖：folium（已安装），pandas，json，sqlite3（标准库）
"""

import sqlite3
import json
import os
import sys
import pandas as pd

# ---- 路径配置 ----
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TMP_DB     = "/tmp/drone_analysis/shanghai_drone_demand.db"   # 优先读临时目录
COPY_DB    = os.path.join(SCRIPT_DIR, "shanghai_drone_demand.db")
OUT_HTML   = os.path.join(SCRIPT_DIR, "上海无人机需求热点地图.html")

import shutil

# 确保有可读的数据库（挂载文件系统不支持 SQLite 锁，从 /tmp/ 读）
if os.path.exists(TMP_DB):
    DB_PATH = TMP_DB
elif os.path.exists(COPY_DB):
    # 把已复制的文件再拷回 /tmp/
    os.makedirs("/tmp/drone_analysis", exist_ok=True)
    shutil.copy2(COPY_DB, TMP_DB)
    DB_PATH = TMP_DB
else:
    print(f"❌ 找不到数据库，请先运行 01_数据库构建与热点分析.py")
    sys.exit(1)

print(f"  数据库: {DB_PATH}")

print("=" * 55)
print("上海市无人机配送需求热点 — 可视化地图生成")
print("=" * 55)

# ---- 从数据库读数据 ----
print("\n[1] 从数据库加载数据 ...")
conn = sqlite3.connect(DB_PATH)

poi_df = pd.read_sql("SELECT lon, lat, category, name, weight FROM poi_demand", conn)
order_df = pd.read_sql("SELECT lon, lat, area_type, category, delivery_time FROM order_demand", conn)
hotspot_df = pd.read_sql(
    "SELECT lon, lat, kde_score, hotspot_level, level_label FROM hotspot_grid WHERE hotspot_level >= 2",
    conn
)
high_df = pd.read_sql(
    "SELECT lon, lat, kde_score, hotspot_level, level_label FROM hotspot_grid WHERE hotspot_level >= 3",
    conn
)
stats_df = pd.read_sql("SELECT stat_key, stat_value FROM order_stats", conn)
conn.close()

stats = dict(zip(stats_df['stat_key'], stats_df['stat_value']))
print(f"  POI点: {len(poi_df)},  模拟订单: {len(order_df)},  热点格: {len(hotspot_df)}")

# ---- 颜色配置 ----
LEVEL_COLOR = {1: '#3388ff', 2: '#ffd700', 3: '#ff8c00', 4: '#cc0000'}
LEVEL_NAME  = {1: '低需求', 2: '中需求', 3: '高需求', 4: '极高需求'}
CAT_COLOR = {
    'hospital':     '#e63946',
    'community':    '#2a9d8f',
    'shelter':      '#e9c46a',
    'police':       '#457b9d',
    'fire_station': '#f4a261',
    'uav_takeoff':  '#6a0dad',
}
CAT_ICON = {
    'hospital':     '🏥',
    'community':    '🏘',
    'shelter':      '⛺',
    'police':       '🚔',
    'fire_station': '🚒',
    'uav_takeoff':  '🚁',
}
CAT_NAME = {
    'hospital':     '医院',
    'community':    '社区中心',
    'shelter':      '应急避难点',
    'police':       '警务点',
    'fire_station': '消防站',
    'uav_takeoff':  '无人机起降点',
}

# ============================================================
# 纯 HTML + Leaflet.js 版本（不依赖 folium，避免导入问题）
# ============================================================
print("\n[2] 生成交互式 HTML 地图 ...")

# 构建热点网格 GeoJSON
hotspot_features = []
GRID_STEP_LON = (122.05 - 120.85) / 80
GRID_STEP_LAT = (31.55 - 30.70)  / 80
HALF_LON = GRID_STEP_LON / 2 * 0.85
HALF_LAT = GRID_STEP_LAT / 2 * 0.85

for _, r in hotspot_df.iterrows():
    lon, lat = float(r['lon']), float(r['lat'])
    feat = {
        "type": "Feature",
        "properties": {
            "kde": round(float(r['kde_score']), 4),
            "level": int(r['hotspot_level']),
            "label": r['level_label'],
            "color": LEVEL_COLOR[int(r['hotspot_level'])],
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [lon - HALF_LON, lat - HALF_LAT],
                [lon + HALF_LON, lat - HALF_LAT],
                [lon + HALF_LON, lat + HALF_LAT],
                [lon - HALF_LON, lat + HALF_LAT],
                [lon - HALF_LON, lat - HALF_LAT],
            ]]
        }
    }
    hotspot_features.append(feat)

hotspot_geojson = json.dumps({"type": "FeatureCollection", "features": hotspot_features}, ensure_ascii=False)

# 构建 POI GeoJSON
poi_features = []
for _, r in poi_df.iterrows():
    cat = r['category']
    poi_features.append({
        "type": "Feature",
        "properties": {
            "category": cat,
            "name": r['name'] or CAT_NAME.get(cat, cat),
            "weight": float(r['weight']),
            "color": CAT_COLOR.get(cat, '#888'),
            "icon": CAT_ICON.get(cat, '📍'),
            "cat_name": CAT_NAME.get(cat, cat),
        },
        "geometry": {"type": "Point", "coordinates": [float(r['lon']), float(r['lat'])]}
    })
poi_geojson = json.dumps({"type": "FeatureCollection", "features": poi_features}, ensure_ascii=False)

# 构建订单热力图数据
heatmap_pts = []
for _, r in order_df.iterrows():
    heatmap_pts.append([float(r['lat']), float(r['lon']), float(r['delivery_time']) / 200])
heatmap_json = json.dumps(heatmap_pts)

# 统计摘要
cat_counts = poi_df.groupby('category').size().to_dict()
level_counts = hotspot_df.groupby('level_label').size().to_dict()

html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>上海市无人机配送需求热点分析</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Microsoft YaHei', 'PingFang SC', sans-serif; background: #1a1a2e; color: #eee; }}
  #app {{ display: flex; flex-direction: column; height: 100vh; }}

  /* 顶栏 */
  #header {{
    background: linear-gradient(135deg, #16213e 0%, #0f3460 100%);
    padding: 10px 20px;
    display: flex; align-items: center; justify-content: space-between;
    border-bottom: 1px solid #334;
    flex-shrink: 0;
  }}
  #header h1 {{ font-size: 18px; color: #7ec8e3; letter-spacing: 1px; }}
  #header .subtitle {{ font-size: 12px; color: #aaa; margin-top: 2px; }}
  .badge {{ background: #0f3460; border: 1px solid #7ec8e3; color: #7ec8e3;
            padding: 3px 10px; border-radius: 12px; font-size: 12px; }}

  /* 主体 */
  #main {{ display: flex; flex: 1; overflow: hidden; }}

  /* 侧边栏 */
  #sidebar {{
    width: 300px; background: #16213e; overflow-y: auto;
    border-right: 1px solid #334; padding: 12px; flex-shrink: 0;
  }}
  .section {{ margin-bottom: 14px; }}
  .section h3 {{ font-size: 13px; color: #7ec8e3; border-bottom: 1px solid #334;
                padding-bottom: 5px; margin-bottom: 8px; }}
  .layer-toggle {{
    display: flex; align-items: center; gap: 8px; padding: 5px 4px;
    border-radius: 4px; cursor: pointer; font-size: 12px;
    transition: background 0.2s;
  }}
  .layer-toggle:hover {{ background: #0f3460; }}
  .layer-toggle input {{ cursor: pointer; }}
  .dot {{ width: 12px; height: 12px; border-radius: 2px; flex-shrink: 0; }}

  .stat-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }}
  .stat-card {{
    background: #0f3460; border-radius: 6px; padding: 8px; text-align: center;
  }}
  .stat-card .num {{ font-size: 20px; font-weight: bold; color: #7ec8e3; }}
  .stat-card .lbl {{ font-size: 10px; color: #aaa; margin-top: 2px; }}

  .legend-row {{ display: flex; align-items: center; gap: 6px; font-size: 11px; margin: 3px 0; }}
  .legend-sq {{ width: 14px; height: 14px; border-radius: 2px; flex-shrink: 0; }}

  #info-box {{
    background: #0f3460; border-radius: 6px; padding: 8px;
    font-size: 11px; color: #ccc; min-height: 50px;
    border: 1px solid #334;
  }}
  #info-box b {{ color: #7ec8e3; }}

  /* 地图 */
  #map {{ flex: 1; }}

  /* 图例浮层 */
  .map-legend {{
    position: absolute; bottom: 30px; right: 10px; z-index: 1000;
    background: rgba(22,33,62,0.92); border: 1px solid #334;
    padding: 10px 14px; border-radius: 8px; font-size: 11px;
    color: #ddd; min-width: 130px;
  }}
  .map-legend h4 {{ color: #7ec8e3; margin-bottom: 6px; font-size: 12px; }}
</style>
</head>
<body>
<div id="app">

<!-- 顶栏 -->
<div id="header">
  <div>
    <h1>🚁 上海市无人机配送需求热点分析</h1>
    <div class="subtitle">华东师范大学公共管理学院 · 创新训练项目 · 数据更新: {stats.get('data_build_time','')[:10]}</div>
  </div>
  <div style="display:flex;gap:8px;">
    <span class="badge">坐标系: WGS84</span>
    <span class="badge">POI: {len(poi_df)} 个</span>
    <span class="badge">热点格: {len(hotspot_df)} 个</span>
  </div>
</div>

<div id="main">

<!-- 侧边栏 -->
<div id="sidebar">

  <!-- 统计摘要 -->
  <div class="section">
    <h3>📊 数据概览</h3>
    <div class="stat-grid">
      <div class="stat-card"><div class="num">{len(poi_df)}</div><div class="lbl">需求/供给 POI</div></div>
      <div class="stat-card"><div class="num">{len(order_df)}</div><div class="lbl">模拟订单点</div></div>
      <div class="stat-card"><div class="num">{len(high_df)}</div><div class="lbl">高/极高需求格</div></div>
      <div class="stat-card"><div class="num">{stats.get('avg_delivery_time','—')}</div><div class="lbl">平均配送(分钟)</div></div>
    </div>
  </div>

  <!-- 图层控制 -->
  <div class="section">
    <h3>🗂 图层控制</h3>
    <label class="layer-toggle">
      <input type="checkbox" id="toggle-hotspot" checked onchange="toggleLayer('hotspot')">
      <div class="dot" style="background:#cc0000;"></div> 需求热点网格
    </label>
    <label class="layer-toggle">
      <input type="checkbox" id="toggle-heatmap" onchange="toggleLayer('heatmap')">
      <div class="dot" style="background:#ff6b6b;"></div> 订单密度热力图
    </label>
    <label class="layer-toggle">
      <input type="checkbox" id="toggle-hospital" checked onchange="togglePOI('hospital')">
      <div class="dot" style="background:#e63946;"></div> 医院 ({cat_counts.get('hospital', 0)})
    </label>
    <label class="layer-toggle">
      <input type="checkbox" id="toggle-community" checked onchange="togglePOI('community')">
      <div class="dot" style="background:#2a9d8f;"></div> 社区中心 ({cat_counts.get('community', 0)})
    </label>
    <label class="layer-toggle">
      <input type="checkbox" id="toggle-shelter" checked onchange="togglePOI('shelter')">
      <div class="dot" style="background:#e9c46a;"></div> 应急避难点 ({cat_counts.get('shelter', 0)})
    </label>
    <label class="layer-toggle">
      <input type="checkbox" id="toggle-police" onchange="togglePOI('police')">
      <div class="dot" style="background:#457b9d;"></div> 警务点 ({cat_counts.get('police', 0)})
    </label>
    <label class="layer-toggle">
      <input type="checkbox" id="toggle-fire_station" onchange="togglePOI('fire_station')">
      <div class="dot" style="background:#f4a261;"></div> 消防站 ({cat_counts.get('fire_station', 0)})
    </label>
    <label class="layer-toggle">
      <input type="checkbox" id="toggle-uav_takeoff" checked onchange="togglePOI('uav_takeoff')">
      <div class="dot" style="background:#6a0dad;"></div> 无人机起降点 ({cat_counts.get('uav_takeoff', 0)})
    </label>
  </div>

  <!-- 热点分级图例（带说明） -->
  <div class="section">
    <h3>🌡 需求热点分级说明</h3>
    <div class="legend-row" style="align-items:flex-start;">
      <div class="legend-sq" style="background:#cc0000;margin-top:3px;flex-shrink:0;"></div>
      <div><b style="color:#cc0000;">极高需求区</b>（核密度 Top 10%）<br>
      <span style="color:#aaa;font-size:10px;">人口密度最高，医院/避难所聚集，优先建站区域</span></div>
    </div>
    <div class="legend-row" style="align-items:flex-start;margin-top:6px;">
      <div class="legend-sq" style="background:#ff8c00;margin-top:3px;flex-shrink:0;"></div>
      <div><b style="color:#ff8c00;">高需求区</b>（75–90% 分位）<br>
      <span style="color:#aaa;font-size:10px;">次级核心区，适合设综合站扩大覆盖</span></div>
    </div>
    <div class="legend-row" style="align-items:flex-start;margin-top:6px;">
      <div class="legend-sq" style="background:#ffd700;margin-top:3px;flex-shrink:0;"></div>
      <div><b style="color:#ffd700;">中需求区</b>（50–75% 分位）<br>
      <span style="color:#aaa;font-size:10px;">郊区居住/工业混合区，可作备用站候选</span></div>
    </div>
    <div class="legend-row" style="align-items:flex-start;margin-top:6px;color:#888;">
      <div class="legend-sq" style="background:#3388ff;opacity:0.4;margin-top:3px;flex-shrink:0;"></div>
      <div><b>低需求区</b>（已隐藏）<br>
      <span style="font-size:10px;">远郊/水域，当前暂不建站</span></div>
    </div>
  </div>

  <!-- 分析方法说明 -->
  <div class="section">
    <h3>🔍 分析方法</h3>
    <div style="font-size:11px;color:#ccc;line-height:1.7;">
      <b style="color:#7ec8e3;">核密度估计（KDE）</b><br>
      以 Silverman 带宽规则（经度 0.030°，纬度 0.022°）对 80×80 评估网格计算高斯核密度，综合考虑医院（权重×5）、避难点（×4）、社区中心（×3）等 389 个需求/供给点，叠加 1,155 个模拟订单点，最终归一化至 0–1 后按分位数分级。
    </div>
  </div>

  <!-- 核心结论 -->
  <div class="section">
    <h3>📌 核心结论</h3>
    <div style="font-size:11px;color:#ccc;line-height:1.8;">
      • <b style="color:#cc0000;">浦东新区核心–黄浦–徐汇</b> 为极高需求热点，应优先配置微型站<br>
      • <b style="color:#ff8c00;">闵行–嘉定–宝山</b> 为高需求区，适合布设综合站<br>
      • 现有 <b style="color:#6a0dad;">5 个起降点</b> 覆盖不足，与极高需求区存在空白<br>
      • 建议结合多目标优化结果，按"核心区微型–郊区综合–远郊枢纽"三级布局
    </div>
  </div>

  <!-- 点击信息 -->
  <div class="section">
    <h3>🖱 点击要素信息</h3>
    <div id="info-box">点击地图上的要素查看详情...</div>
  </div>

  <!-- 底图切换 -->
  <div class="section">
    <h3>🗺 底图</h3>
    <label class="layer-toggle">
      <input type="radio" name="basemap" value="osm" onchange="switchBasemap(this.value)" checked>
      OpenStreetMap（默认）
    </label>
    <label class="layer-toggle">
      <input type="radio" name="basemap" value="dark" onchange="switchBasemap(this.value)">
      暗色底图
    </label>
    <label class="layer-toggle">
      <input type="radio" name="basemap" value="satellite" onchange="switchBasemap(this.value)">
      卫星影像（Esri）
    </label>
  </div>

</div><!-- /sidebar -->

<!-- 地图 -->
<div id="map"></div>

</div><!-- /main -->
</div><!-- /app -->

<script>
// ============================================================
// 数据注入
// ============================================================
const HOTSPOT_GEOJSON = {hotspot_geojson};
const POI_GEOJSON     = {poi_geojson};
const HEATMAP_PTS     = {heatmap_json};

// ============================================================
// 初始化地图
// ============================================================
const map = L.map('map', {{
  center: [31.23, 121.47],
  zoom: 11,
  zoomControl: true,
}});

// 底图
const basemaps = {{
  osm: L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    attribution: '© OpenStreetMap contributors', maxZoom: 19
  }}),
  dark: L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
    attribution: '© Carto', maxZoom: 19
  }}),
  satellite: L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
    attribution: '© Esri', maxZoom: 19
  }}),
}};
basemaps.osm.addTo(map);
let currentBasemap = 'osm';

function switchBasemap(name) {{
  map.removeLayer(basemaps[currentBasemap]);
  basemaps[name].addTo(map);
  map.getPane('tilePane').style.zIndex = 200;
  currentBasemap = name;
}}

// ============================================================
// 热点网格图层
// ============================================================
const LEVEL_OPACITY = {{2: 0.35, 3: 0.55, 4: 0.75}};
const hotspotLayer = L.geoJSON(HOTSPOT_GEOJSON, {{
  style: function(f) {{
    const lv = f.properties.level;
    return {{
      fillColor: f.properties.color,
      fillOpacity: LEVEL_OPACITY[lv] || 0.2,
      color: 'none',
      weight: 0,
    }};
  }},
  onEachFeature: function(f, layer) {{
    layer.on('click', function() {{
      const p = f.properties;
      document.getElementById('info-box').innerHTML =
        '<b>需求热点格</b><br>' +
        '需求等级: <b>' + p.label + '</b><br>' +
        'KDE得分: ' + (p.kde * 100).toFixed(1) + '%<br>' +
        '位置: ' + f.geometry.coordinates[0][0][1].toFixed(4) + ', ' +
                   f.geometry.coordinates[0][0][0].toFixed(4);
    }});
  }}
}}).addTo(map);

// ============================================================
// POI 图层（按类别分组）
// ============================================================
const poiLayers = {{}};
const CAT_ICON_MAP = {{
  hospital: '🏥', community: '🏘', shelter: '⛺',
  police: '🚔', fire_station: '🚒', uav_takeoff: '🚁'
}};
const CAT_NAME_MAP = {{
  hospital: '医院', community: '社区中心', shelter: '应急避难点',
  police: '警务点', fire_station: '消防站', uav_takeoff: '无人机起降点'
}};
const SHOW_BY_DEFAULT = new Set(['hospital','community','shelter','uav_takeoff']);

// 按类别分组
const poiByCat = {{}};
POI_GEOJSON.features.forEach(f => {{
  const cat = f.properties.category;
  if (!poiByCat[cat]) poiByCat[cat] = [];
  poiByCat[cat].push(f);
}});

Object.entries(poiByCat).forEach(([cat, features]) => {{
  const color = features[0].properties.color;
  const icon  = CAT_ICON_MAP[cat] || '📍';
  const layer = L.layerGroup(
    features.map(f => {{
      const [lon, lat] = f.geometry.coordinates;
      const w = f.properties.weight;
      const r = 6 + w * 2;
      const marker = L.circleMarker([lat, lon], {{
        radius: r, fillColor: color, color: '#fff',
        weight: 1.5, fillOpacity: 0.85,
      }});
      marker.on('click', function() {{
        const p = f.properties;
        document.getElementById('info-box').innerHTML =
          p.icon + ' <b>' + (p.name || p.cat_name) + '</b><br>' +
          '类型: ' + p.cat_name + '<br>' +
          '需求权重: ' + p.weight + '<br>' +
          '坐标: ' + lat.toFixed(4) + ', ' + lon.toFixed(4);
      }});
      return marker;
    }})
  );
  poiLayers[cat] = layer;
  if (SHOW_BY_DEFAULT.has(cat)) layer.addTo(map);
}});

// ============================================================
// 订单热力图
// ============================================================
const heatLayer = L.heatLayer(HEATMAP_PTS, {{
  radius: 18, blur: 20, maxZoom: 14,
  gradient: {{0.2:'#00f', 0.5:'#0f0', 0.7:'#ff0', 1.0:'#f00'}}
}});

// ============================================================
// 图层控制函数
// ============================================================
function toggleLayer(name) {{
  const checked = document.getElementById('toggle-' + name).checked;
  if (name === 'hotspot') {{
    checked ? hotspotLayer.addTo(map) : map.removeLayer(hotspotLayer);
  }} else if (name === 'heatmap') {{
    checked ? heatLayer.addTo(map) : map.removeLayer(heatLayer);
  }}
}}

function togglePOI(cat) {{
  const checked = document.getElementById('toggle-' + cat).checked;
  if (!poiLayers[cat]) return;
  checked ? poiLayers[cat].addTo(map) : map.removeLayer(poiLayers[cat]);
}}

// ============================================================
// 上海范围框
// ============================================================
L.rectangle([[30.70, 120.85], [31.55, 122.05]], {{
  color: '#7ec8e3', weight: 1.5, fill: false, dashArray: '5 5', opacity: 0.5
}}).addTo(map);

// ============================================================
// 图例（浮层）
// ============================================================
const legend = L.control({{position: 'bottomright'}});
legend.onAdd = function() {{
  const div = L.DomUtil.create('div', 'map-legend');
  div.innerHTML = `
    <h4>📍 POI 图例</h4>
    <div class="legend-row">🏥 医院</div>
    <div class="legend-row">🏘 社区中心</div>
    <div class="legend-row">⛺ 应急避难点</div>
    <div class="legend-row">🚁 无人机起降点</div>
    <div class="legend-row">🚔 警务点</div>
    <div class="legend-row">🚒 消防站</div>
  `;
  return div;
}};
legend.addTo(map);

// ============================================================
// 坐标显示
// ============================================================
const coordDisplay = L.control({{position: 'bottomleft'}});
coordDisplay.onAdd = function() {{
  const div = L.DomUtil.create('div');
  div.style.cssText = 'background:rgba(22,33,62,0.85);color:#7ec8e3;padding:4px 8px;border-radius:4px;font-size:11px;font-family:monospace;';
  div.id = 'coord-display';
  div.innerHTML = '移动鼠标查看坐标';
  return div;
}};
coordDisplay.addTo(map);
map.on('mousemove', function(e) {{
  document.getElementById('coord-display').innerHTML =
    'Lat: ' + e.latlng.lat.toFixed(5) + '  Lon: ' + e.latlng.lng.toFixed(5);
}});

console.log('地图加载完成 | 热点格:', HOTSPOT_GEOJSON.features.length, '| POI:', POI_GEOJSON.features.length);
</script>
</body>
</html>"""

# ---- 写文件（先写到 /tmp/，再复制到项目目录）----
TMP_HTML = "/tmp/drone_analysis/上海无人机需求热点地图.html"
with open(TMP_HTML, 'w', encoding='utf-8') as f:
    f.write(html_content)
shutil.copy2(TMP_HTML, OUT_HTML)

size_kb = os.path.getsize(OUT_HTML) / 1024
print(f"  ✓ HTML 地图: {os.path.basename(OUT_HTML)}  ({size_kb:.0f} KB)")
print(f"\n{'='*55}")
print("✅ 可视化地图生成完成！")
print(f"{'='*55}")
print(f"  🌐 {OUT_HTML}")
print(f"\n  在浏览器中直接打开此文件即可查看交互式热点地图。")
print(f"  功能：图层开关 | 底图切换 | 点击查看要素属性 | 鼠标坐标显示")
