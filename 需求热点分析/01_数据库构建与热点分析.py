"""
上海市无人机配送需求热点分析 —— 数据库构建脚本
================================================
功能：
  1. 从各 GeoPackage 图层读取 POI 坐标
  2. 整合订单配送数据（模拟上海需求分布）
  3. 构建标准化 SQLite 空间数据库
  4. 核密度估计（KDE）热点分析
  5. 导出 GeoPackage 供 ArcGIS 加载

依赖：Python 3.10+，仅需 pandas、numpy（标准库）
作者：上海市无人机配送资源配置研究项目组
"""

import sqlite3
import struct
import math
import json
import os
import csv
import numpy as np
import pandas as pd
from datetime import datetime

# ============================================================
# 路径配置（相对于本脚本所在文件夹的上级，即 shanghai_drone/）
# ============================================================
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, "创新训练类上海市无人机")
THEME_DIR = os.path.join(DATA_DIR, "11_专题层")
EMERG_DIR = os.path.join(DATA_DIR, "07_灾害与应急数据")
UAV_DIR   = os.path.join(DATA_DIR, "03_无人机配送现状数据")
ORDER_CSV = os.path.join(BASE, "无人机分析数据集20260523", "配送与订单数据分析集.csv")
OUT_DIR   = os.path.dirname(os.path.abspath(__file__))
TMP_DIR   = "/tmp/drone_analysis"   # 临时工作目录（SQLite 需要本地文件锁）
os.makedirs(TMP_DIR, exist_ok=True)
DB_PATH   = os.path.join(TMP_DIR, "shanghai_drone_demand.db")

# ============================================================
# 工具函数：从 GeoPackage R-tree 提取点坐标
# ============================================================

def read_gpkg_points(gpkg_path, table_name, rtree_name=None):
    """
    利用 GeoPackage 的 R-tree 索引快速读取点要素坐标。
    对于 POINT 类型，R-tree 的 minx==maxx（经度），miny==maxy（纬度）。

    返回：[(fid, lon, lat, name), ...]
    """
    if not os.path.exists(gpkg_path):
        print(f"  ⚠  文件不存在: {gpkg_path}")
        return []

    conn = sqlite3.connect(gpkg_path)
    try:
        # 获取真实表名
        tables = [r[0] for r in conn.execute(
            "SELECT table_name FROM gpkg_contents WHERE data_type='features'"
        ).fetchall()]
        if not tables:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'gpkg_%' AND name NOT LIKE 'rtree_%' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()]

        real_table = table_name if table_name in tables else (tables[0] if tables else None)
        if not real_table:
            return []

        rt = rtree_name or f"rtree_{real_table}_geom"

        # 检查 rtree 是否存在
        has_rtree = conn.execute(
            f"SELECT count(*) FROM sqlite_master WHERE type='table' AND name='{rt}'"
        ).fetchone()[0]

        if has_rtree:
            # 从 R-tree 读坐标，JOIN 主表读名称
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info({real_table})").fetchall()]
            name_col = next((c for c in cols if 'name' in c.lower()), None)

            if name_col:
                rows = conn.execute(f"""
                    SELECT t.fid, r.minx, r.miny, t.{name_col}
                    FROM {real_table} t
                    JOIN {rt} r ON t.fid = r.id
                """).fetchall()
            else:
                rows = conn.execute(f"""
                    SELECT t.fid, r.minx, r.miny, '' as name
                    FROM {real_table} t
                    JOIN {rt} r ON t.fid = r.id
                """).fetchall()
        else:
            # 没有 rtree，尝试解析 WKB（简单 POINT）
            rows = []
            raw = conn.execute(f"SELECT fid, geom FROM {real_table}").fetchall()
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info({real_table})").fetchall()]
            name_col = next((c for c in cols if 'name' in c.lower()), None)
            names = {}
            if name_col:
                for r in conn.execute(f"SELECT fid, {name_col} FROM {real_table}").fetchall():
                    names[r[0]] = r[1]
            for fid, geom in raw:
                try:
                    # GPKG WKB: 8 bytes header + standard WKB
                    wkb = bytes(geom)[8:]
                    byte_order = wkb[0]
                    fmt = '<' if byte_order == 1 else '>'
                    lon, lat = struct.unpack(fmt + 'dd', wkb[5:21])
                    rows.append((fid, lon, lat, names.get(fid, '')))
                except Exception:
                    pass

        return [(int(r[0]), float(r[1]), float(r[2]), str(r[3] or '')) for r in rows
                if 120 < float(r[1]) < 122.5 and 30 < float(r[2]) < 32]  # 上海范围过滤
    finally:
        conn.close()


# ============================================================
# 1. 读取所有 POI 图层
# ============================================================
print("=" * 55)
print("上海市无人机配送需求热点分析 — 数据库构建")
print("=" * 55)
print(f"\n[1] 读取 POI 图层 ...")

layers = {
    "hospital":        (os.path.join(THEME_DIR, "上海_医院.gpkg"),        "hospitals"),
    "fire_station":    (os.path.join(THEME_DIR, "上海_消防站.gpkg"),       "fire_stations"),
    "community":       (os.path.join(THEME_DIR, "上海_社区中心.gpkg"),     "community_centres"),
    "shelter":         (os.path.join(EMERG_DIR, "上海_应急避难点_OSM.gpkg"), "emergency_shelters"),
    "police":          (os.path.join(EMERG_DIR, "上海_警务点_OSM.gpkg"),    "police"),
    "uav_takeoff":     (os.path.join(UAV_DIR,   "上海_公开可得起降相关点_OSM.gpkg"), None),
}

poi_records = []  # [dict(lon, lat, category, name, weight)]

WEIGHTS = {
    "hospital":     5.0,   # 医院需求最高（药品/急救物资）
    "community":    3.0,   # 社区中心（日常配送密集区）
    "shelter":      4.0,   # 应急避难点（应急物资需求高）
    "police":       2.0,   # 警务点
    "fire_station": 2.5,   # 消防站
    "uav_takeoff":  1.0,   # 已有起降点（供给侧参考）
}

for cat, (path, tbl) in layers.items():
    pts = read_gpkg_points(path, tbl or "")
    print(f"  {cat:15s}: {len(pts):4d} 个点")
    for fid, lon, lat, name in pts:
        poi_records.append({
            "lon": lon, "lat": lat,
            "category": cat,
            "name": name,
            "weight": WEIGHTS.get(cat, 1.0)
        })

poi_df = pd.DataFrame(poi_records)
print(f"  → 合计 {len(poi_df)} 个需求/供给点")

# ============================================================
# 2. 处理订单数据（提取规律，映射到上海坐标）
# ============================================================
print("\n[2] 处理订单数据 ...")

order_df = pd.read_csv(ORDER_CSV, encoding='utf-8')
print(f"  原始订单: {len(order_df)} 条")

# 清洗
order_df.columns = order_df.columns.str.strip()
order_df['Weather']  = order_df['Weather'].str.strip()
order_df['Traffic']  = order_df['Traffic'].str.strip()
order_df['Vehicle']  = order_df['Vehicle'].str.strip()
order_df['Area']     = order_df['Area'].str.strip()
order_df['Category'] = order_df['Category'].str.strip()
order_df = order_df.dropna(subset=['Store_Latitude', 'Drop_Latitude', 'Delivery_Time'])
order_df['Delivery_Time'] = pd.to_numeric(order_df['Delivery_Time'], errors='coerce')
order_df = order_df[order_df['Delivery_Time'] > 0]
print(f"  清洗后: {len(order_df)} 条")

# 统计分析
area_dist  = order_df['Area'].value_counts(normalize=True).to_dict()
cat_dist   = order_df['Category'].value_counts(normalize=True).to_dict()
weather_dt = order_df['Weather'].value_counts().to_dict()
traffic_dt = order_df['Traffic'].value_counts().to_dict()
avg_delivery_by_area = order_df.groupby('Area')['Delivery_Time'].mean().to_dict()
avg_delivery_by_weather = order_df.groupby('Weather')['Delivery_Time'].mean().to_dict()

print(f"  区域分布: {area_dist}")
print(f"  品类分布（前5）: {dict(list(cat_dist.items())[:5])}")
print(f"  平均配送时间(分钟): {order_df['Delivery_Time'].mean():.1f}")

# 上海市范围内生成模拟需求点（基于人口密度分区）
# 上海中心城区（内环以内）：121.38-121.52, 31.15-31.28
# 中间圈（内外环间）：121.25-121.65, 31.05-31.40
# 外围区：121.10-121.80, 30.85-31.55
np.random.seed(42)

SHANGHAI_ZONES = [
    # (lon_center, lat_center, lon_std, lat_std, n_points, area_label, pop_weight)
    (121.46, 31.22, 0.04, 0.04, 200, "Urban_Core",     1.0),   # 黄浦/静安/徐汇核心
    (121.50, 31.28, 0.03, 0.03, 150, "Urban_Core",     1.0),   # 虹口/杨浦
    (121.41, 31.19, 0.04, 0.03, 130, "Urban_Core",     1.0),   # 长宁/闵行北
    (121.55, 31.22, 0.04, 0.04, 160, "Urban_Core",     1.0),   # 浦东新区核心
    (121.45, 31.15, 0.05, 0.04, 100, "Suburban",       0.7),   # 闵行
    (121.60, 31.18, 0.05, 0.05, 100, "Suburban",       0.7),   # 浦东南
    (121.40, 31.32, 0.06, 0.04,  80, "Suburban",       0.7),   # 普陀/嘉定南
    (121.30, 31.25, 0.07, 0.06,  60, "Suburban",       0.5),   # 嘉定
    (121.65, 31.15, 0.06, 0.05,  60, "Suburban",       0.5),   # 浦东外圈
    (121.20, 31.10, 0.08, 0.06,  40, "Peripheral",     0.3),   # 金山/松江
    (121.75, 31.25, 0.08, 0.07,  40, "Peripheral",     0.3),   # 浦东远郊
    (121.38, 31.45, 0.09, 0.07,  35, "Peripheral",     0.3),   # 宝山/崇明
]

sim_demand = []
for lon_c, lat_c, lon_s, lat_s, n, area, pw in SHANGHAI_ZONES:
    lons = np.random.normal(lon_c, lon_s, n)
    lats = np.random.normal(lat_c, lat_s, n)
    # 从订单数据随机采样品类和配送时间
    sampled = order_df.sample(n=n, replace=True, random_state=42)
    for i in range(n):
        sim_demand.append({
            "lon":           round(float(lons[i]), 6),
            "lat":           round(float(lats[i]), 6),
            "area_type":     area,
            "category":      sampled.iloc[i]['Category'],
            "weather":       sampled.iloc[i]['Weather'],
            "traffic":       sampled.iloc[i]['Traffic'],
            "delivery_time": float(sampled.iloc[i]['Delivery_Time']),
            "demand_weight": pw,
        })

demand_df = pd.DataFrame(sim_demand)
# 过滤上海范围
demand_df = demand_df[(demand_df['lon'].between(120.8, 122.2)) &
                      (demand_df['lat'].between(30.7, 31.9))]
print(f"\n  生成模拟需求点: {len(demand_df)} 条")
print(f"  区域分布:\n{demand_df['area_type'].value_counts().to_string()}")

# ============================================================
# 3. 构建 SQLite 数据库
# ============================================================
print("\n[3] 构建 SQLite 数据库 ...")

if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

conn = sqlite3.connect(DB_PATH)
cur  = conn.cursor()

# --- 表1：POI需求/供给点 ---
cur.execute("""
CREATE TABLE poi_demand (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    lon       REAL NOT NULL,
    lat       REAL NOT NULL,
    category  TEXT,          -- hospital / community / shelter 等
    name      TEXT,
    weight    REAL,          -- 需求权重
    created_at TEXT
)
""")

cur.execute("""
CREATE TABLE order_demand (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lon             REAL NOT NULL,
    lat             REAL NOT NULL,
    area_type       TEXT,    -- Urban_Core / Suburban / Peripheral
    category        TEXT,    -- 配送品类
    weather         TEXT,
    traffic         TEXT,
    delivery_time   REAL,    -- 分钟
    demand_weight   REAL,
    created_at      TEXT
)
""")

cur.execute("""
CREATE TABLE order_stats (
    stat_key   TEXT PRIMARY KEY,
    stat_value TEXT
)
""")

cur.execute("""
CREATE TABLE hotspot_grid (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lon         REAL,
    lat         REAL,
    kde_score   REAL,       -- 核密度值（归一化 0-1）
    hotspot_level INTEGER,  -- 1=低 2=中 3=高 4=极高
    level_label TEXT,
    created_at  TEXT
)
""")

now = datetime.now().isoformat()

# 插入 POI 数据
poi_rows = [(r['lon'], r['lat'], r['category'], r['name'], r['weight'], now)
            for _, r in poi_df.iterrows()]
cur.executemany(
    "INSERT INTO poi_demand (lon,lat,category,name,weight,created_at) VALUES (?,?,?,?,?,?)",
    poi_rows
)

# 插入模拟需求
dem_rows = [(r['lon'], r['lat'], r['area_type'], r['category'],
             r['weather'], r['traffic'], r['delivery_time'], r['demand_weight'], now)
            for _, r in demand_df.iterrows()]
cur.executemany(
    "INSERT INTO order_demand (lon,lat,area_type,category,weather,traffic,delivery_time,demand_weight,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
    dem_rows
)

# 插入统计摘要
stats = {
    "total_poi":           str(len(poi_df)),
    "total_sim_demand":    str(len(demand_df)),
    "area_distribution":   json.dumps(area_dist, ensure_ascii=False),
    "category_distribution": json.dumps(cat_dist, ensure_ascii=False),
    "avg_delivery_time":   f"{order_df['Delivery_Time'].mean():.2f}",
    "weather_distribution": json.dumps(weather_dt, ensure_ascii=False),
    "data_build_time":     now,
    "coord_system":        "WGS84 (EPSG:4326)",
}
cur.executemany("INSERT INTO order_stats VALUES (?,?)", stats.items())

conn.commit()
print(f"  ✓ 数据库已创建: {DB_PATH}")
print(f"  ✓ poi_demand: {len(poi_rows)} 条")
print(f"  ✓ order_demand: {len(dem_rows)} 条")

# ============================================================
# 4. 核密度估计（KDE）热点分析
# ============================================================
print("\n[4] 核密度估计（KDE）热点分析 ...")

# 合并所有需求点（POI加权 + 模拟订单）
all_pts = []
for _, r in poi_df.iterrows():
    w = r['weight']
    all_pts.extend([(r['lon'], r['lat'], w)] * max(1, int(w)))

for _, r in demand_df.iterrows():
    all_pts.append((r['lon'], r['lat'], r['demand_weight']))

pts_arr = np.array(all_pts)  # (N, 3): lon, lat, weight
lons_all = pts_arr[:, 0]
lats_all = pts_arr[:, 1]
wts_all  = pts_arr[:, 2]

# 构建评估网格（上海范围，约100x100 = 10000格）
LON_MIN, LON_MAX = 120.85, 122.05
LAT_MIN, LAT_MAX = 30.70,  31.55
GRID_N = 80  # 每边格子数（80x80 = 6400个格子）

grid_lons = np.linspace(LON_MIN, LON_MAX, GRID_N)
grid_lats = np.linspace(LAT_MIN, LAT_MAX, GRID_N)
glon, glat = np.meshgrid(grid_lons, grid_lats)
grid_pts = np.column_stack([glon.ravel(), glat.ravel()])  # (6400, 2)

# Gaussian KDE（手动实现，不需要 scipy）
# bandwidth（Silverman 规则）
n = len(lons_all)
std_lon = np.std(lons_all)
std_lat = np.std(lats_all)
h_lon = 1.06 * std_lon * n**(-0.2)
h_lat = 1.06 * std_lat * n**(-0.2)
print(f"  KDE bandwidth: lon={h_lon:.4f}°, lat={h_lat:.4f}°")

# 分批计算（避免内存溢出）
BATCH = 500
kde_vals = np.zeros(len(grid_pts))

for i in range(0, len(grid_pts), BATCH):
    g_batch = grid_pts[i:i+BATCH]       # (B, 2)
    g_lon = g_batch[:, 0:1]             # (B, 1)
    g_lat = g_batch[:, 1:2]             # (B, 1)
    d_lon = (lons_all[np.newaxis, :] - g_lon) / h_lon   # (B, N)
    d_lat = (lats_all[np.newaxis, :] - g_lat) / h_lat   # (B, N)
    kernel = np.exp(-0.5 * (d_lon**2 + d_lat**2))        # (B, N)
    kde_vals[i:i+BATCH] = (kernel * wts_all[np.newaxis, :]).sum(axis=1)

# 归一化到 0-1
kde_min, kde_max = kde_vals.min(), kde_vals.max()
kde_norm = (kde_vals - kde_min) / (kde_max - kde_min + 1e-10)

# 分级（使用分位数）
q25 = np.percentile(kde_norm, 25)
q50 = np.percentile(kde_norm, 50)
q75 = np.percentile(kde_norm, 75)
q90 = np.percentile(kde_norm, 90)

def classify(v):
    if v >= q90: return 4, "极高需求区"
    if v >= q75: return 3, "高需求区"
    if v >= q50: return 2, "中需求区"
    return 1, "低需求区"

print(f"  分位数阈值: Q25={q25:.3f}, Q50={q50:.3f}, Q75={q75:.3f}, Q90={q90:.3f}")

# 写入数据库
grid_rows = []
for idx, (gp, kv) in enumerate(zip(grid_pts, kde_norm)):
    level, label = classify(kv)
    grid_rows.append((float(gp[0]), float(gp[1]), float(kv), level, label, now))

cur.executemany(
    "INSERT INTO hotspot_grid (lon,lat,kde_score,hotspot_level,level_label,created_at) VALUES (?,?,?,?,?,?)",
    grid_rows
)
conn.commit()

level_counts = {}
for _, _, _, lv, lb, _ in grid_rows:
    level_counts[lb] = level_counts.get(lb, 0) + 1
print(f"  热点网格分级统计:")
for lb, cnt in sorted(level_counts.items()):
    print(f"    {lb}: {cnt} 格")

print(f"  ✓ hotspot_grid: {len(grid_rows)} 个网格")

# ============================================================
# 5. 导出 GeoPackage（ArcGIS 可直接加载）
# ============================================================
print("\n[5] 导出 GeoPackage ...")

def create_gpkg(path):
    path = path.replace(OUT_DIR, TMP_DIR)  # 强制写到 /tmp/
    """初始化空 GeoPackage"""
    conn = sqlite3.connect(path)
    c = conn.cursor()
    c.executescript("""
        PRAGMA application_id = 1196444487;
        PRAGMA user_version = 10300;

        CREATE TABLE IF NOT EXISTS gpkg_spatial_ref_sys (
            srs_name TEXT NOT NULL, srs_id INTEGER NOT NULL PRIMARY KEY,
            organization TEXT NOT NULL, organization_coordsys_id INTEGER NOT NULL,
            definition TEXT NOT NULL, description TEXT
        );
        CREATE TABLE IF NOT EXISTS gpkg_contents (
            table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL,
            identifier TEXT, description TEXT, last_change DATETIME NOT NULL,
            min_x REAL, min_y REAL, max_x REAL, max_y REAL, srs_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS gpkg_geometry_columns (
            table_name TEXT NOT NULL, column_name TEXT NOT NULL,
            geometry_type_name TEXT NOT NULL, srs_id INTEGER NOT NULL,
            z TINYINT NOT NULL, m TINYINT NOT NULL,
            PRIMARY KEY (table_name, column_name)
        );
    """)
    # WGS84
    c.execute("""
        INSERT OR IGNORE INTO gpkg_spatial_ref_sys VALUES
        ('WGS 84 geodetic', 4326, 'EPSG', 4326,
         'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]',
         'longitude/latitude coordinates in decimal degrees on the WGS 84 spheroid')
    """)
    conn.commit()
    return conn


def wkb_point(lon, lat):
    """生成 GPKG 封装的 WKB Point（含8字节 GPKG 头）"""
    # GPKG 头: magic(2) + version(1) + flags(1) + srs_id(4)
    gpkg_header = b'GP' + b'\x00' + b'\x01' + struct.pack('<i', 4326)
    # WKB: byte_order(1) + wkb_type(4) + x(8) + y(8)
    wkb = b'\x01' + struct.pack('<I', 1) + struct.pack('<dd', lon, lat)
    return gpkg_header + wkb


def add_point_layer(gpkg_conn, table_name, identifier, columns_def, rows, col_names):
    """向 GeoPackage 添加点图层"""
    c = gpkg_conn.cursor()
    cols_sql = ", ".join(f"{n} {t}" for n, t in columns_def)
    c.execute(f"""
        CREATE TABLE {table_name} (
            fid INTEGER PRIMARY KEY AUTOINCREMENT,
            geom BLOB NOT NULL,
            {cols_sql}
        )
    """)
    placeholders = ",".join(["?"] * (len(col_names) + 1))  # +1 for geom (fid is AUTOINCREMENT)
    for row in rows:
        lon, lat = row[0], row[1]
        geom = wkb_point(lon, lat)
        vals = (geom,) + tuple(row[2:])
        c.execute(f"INSERT INTO {table_name} (geom, {','.join(col_names)}) VALUES ({placeholders})", vals)

    # 计算 bbox
    lons_ = [r[0] for r in rows]
    lats_ = [r[1] for r in rows]
    now_ = datetime.now().isoformat()
    c.execute("""
        INSERT INTO gpkg_contents VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (table_name, 'features', identifier, '', now_,
          min(lons_), min(lats_), max(lons_), max(lats_), 4326))
    c.execute("""
        INSERT INTO gpkg_geometry_columns VALUES (?,?,?,?,?,?)
    """, (table_name, 'geom', 'POINT', 4326, 0, 0))
    gpkg_conn.commit()
    print(f"  ✓ 图层 {table_name}: {len(rows)} 要素")


# --- 输出1：需求热点网格（降采样，只输出高/极高区）---
gpkg1_path = os.path.join(TMP_DIR, "需求热点_KDE分析结果.gpkg")
g1 = create_gpkg(gpkg1_path)

hotspot_rows = [(gp[0], gp[1], float(kv), level, label)
                for gp, kv, (level, label) in zip(grid_pts, kde_norm, [classify(v) for v in kde_norm])]

# 全量网格
add_point_layer(g1, "hotspot_all_grid", "全部热点网格",
    [("kde_score", "REAL"), ("hotspot_level", "INTEGER"), ("level_label", "TEXT")],
    hotspot_rows,
    ["kde_score", "hotspot_level", "level_label"])

# 仅高/极高区
high_rows = [r for r in hotspot_rows if r[3] >= 3]
if high_rows:
    add_point_layer(g1, "hotspot_high", "高/极高需求区",
        [("kde_score", "REAL"), ("hotspot_level", "INTEGER"), ("level_label", "TEXT")],
        high_rows,
        ["kde_score", "hotspot_level", "level_label"])
g1.close()

# --- 输出2：POI需求点 ---
gpkg2_path = os.path.join(TMP_DIR, "需求POI点层.gpkg")
g2 = create_gpkg(gpkg2_path)
poi_export = [(r['lon'], r['lat'], r['category'], r['name'], r['weight'])
              for _, r in poi_df.iterrows()]
add_point_layer(g2, "poi_demand", "需求POI点",
    [("category", "TEXT"), ("name", "TEXT"), ("weight", "REAL")],
    poi_export,
    ["category", "name", "weight"])
g2.close()

# --- 输出3：模拟订单需求点 ---
gpkg3_path = os.path.join(TMP_DIR, "模拟订单需求点.gpkg")
g3 = create_gpkg(gpkg3_path)
order_export = [(r['lon'], r['lat'], r['area_type'], r['category'],
                 r['delivery_time'], r['demand_weight'])
                for _, r in demand_df.iterrows()]
add_point_layer(g3, "sim_order_demand", "模拟订单需求点",
    [("area_type", "TEXT"), ("category", "TEXT"), ("delivery_time", "REAL"), ("demand_weight", "REAL")],
    order_export,
    ["area_type", "category", "delivery_time", "demand_weight"])
g3.close()

conn.close()

# ============================================================
# 6. 将文件从 /tmp/ 复制到项目文件夹
# ============================================================
print("\n[6] 复制文件到项目文件夹 ...")
import shutil

files_to_copy = [
    "shanghai_drone_demand.db",
    "需求热点_KDE分析结果.gpkg",
    "需求POI点层.gpkg",
    "模拟订单需求点.gpkg",
]
for fname in files_to_copy:
    src = os.path.join(TMP_DIR, fname)
    dst = os.path.join(OUT_DIR, fname)
    if os.path.exists(src):
        shutil.copy2(src, dst)
        size_kb = os.path.getsize(dst) / 1024
        print(f"  ✓ {fname}  ({size_kb:.0f} KB)")

print(f"\n{'='*55}")
print("✅ 数据库构建 + KDE 热点分析完成！")
print(f"{'='*55}")
print(f"输出文件（均在 需求热点分析/ 文件夹）：")
print(f"  📦 {os.path.basename(DB_PATH)}    — SQLite 数据库（含完整记录）")
print(f"  🗺  需求热点_KDE分析结果.gpkg      — ArcGIS 热点网格图层")
print(f"  🗺  需求POI点层.gpkg               — ArcGIS POI需求点图层")
print(f"  🗺  模拟订单需求点.gpkg             — ArcGIS 订单分布点图层")
print(f"\n提示: 在 ArcGIS Pro 中直接拖入 .gpkg 文件即可加载图层。")
print(f"      热点图层字段 hotspot_level: 1=低 2=中 3=高 4=极高")
