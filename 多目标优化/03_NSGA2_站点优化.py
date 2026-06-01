"""
上海市无人机配送站点资源配给 —— 多目标优化算法（NSGA-II）
============================================================

三个优化目标：
  f1 最小化总成本（站点建设 + 无人机采购 + 运营）
  f2 最大化服务覆盖率（需求点在服务半径内的比例）
  f3 最小化应急响应时间（高风险需求点到最近站点的加权飞行时间）

站点类型（参考立项书分层设计）：
  1 = 微型站（核心区）  —— 2架无人机，3km服务半径
  2 = 综合站（郊区）    —— 5架无人机，10km服务半径
  3 = 枢纽站（远郊）    —— 10架无人机，20km服务半径

无人机型号：DJI FlyCart 30
  最大载重 30kg，巡航速度 54 km/h（15 m/s），最大续航里程 ~28km（满载）

算法：NSGA-II（Non-dominated Sorting Genetic Algorithm II）
  染色体：长度 = 候选站点数，每基因 ∈ {0, 1, 2, 3}（站点类型）
  种群：80个体，迭代150代
  交叉：均匀交叉（p=0.9），变异：随机重置（p=0.08/基因）

依赖：numpy, pandas（均已安装）；无需 scipy/geopandas
"""

import os, sys, struct, json, sqlite3, shutil, time
import numpy as np
import pandas as pd
from datetime import datetime

# ================================================================
# 路径配置
# ================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
DB_TMP  = "/tmp/drone_analysis/shanghai_drone_demand.db"
DB_COPY = os.path.join(PROJECT_DIR, "需求热点分析", "shanghai_drone_demand.db")
TMP_OUT = "/tmp/drone_analysis"
os.makedirs(TMP_OUT, exist_ok=True)

# 确保能读到数据库（挂载文件系统不支持SQLite锁，用/tmp/）
if not os.path.exists(DB_TMP):
    if os.path.exists(DB_COPY):
        shutil.copy2(DB_COPY, DB_TMP)
    else:
        print("❌ 请先运行 01_数据库构建与热点分析.py"); sys.exit(1)

# ================================================================
# DJI FlyCart 30 技术参数（官方手册）
# ================================================================
UAV_PARAMS = {
    "model":            "DJI FlyCart 30",
    "max_payload_kg":   30,          # 最大载重 30kg
    "cruise_speed_kmh": 54,          # 巡航速度 54 km/h
    "max_range_km":     28,          # 满载最大续航里程
    "battery_swap_min": 3,           # 换电池时间（分钟）
    "takeoff_time_min": 1,           # 起降时间合计（分钟）
}

# ================================================================
# 站点类型参数（结合立项书三级布局方案）
# ================================================================
STATION_TYPES = {
    0: None,    # 不建站
    1: {        # 微型站（核心区）
        "name":         "微型站",
        "layer":        "核心区",
        "n_drones":     2,
        "service_km":   3.0,         # 服务半径（km）
        "emg_km":       5.0,         # 应急服务半径
        "build_cost":   50,          # 建设成本（万元）
        "drone_cost":   40,          # 无人机采购（万元/架 × n）
        "annual_ops":   20,          # 年运营费（万元）
    },
    2: {        # 综合站（郊区）
        "name":         "综合站",
        "layer":        "郊区",
        "n_drones":     5,
        "service_km":   10.0,
        "emg_km":       15.0,
        "build_cost":   150,
        "drone_cost":   40,
        "annual_ops":   60,
    },
    3: {        # 枢纽站（远郊）
        "name":         "枢纽站",
        "layer":        "远郊",
        "n_drones":     10,
        "service_km":   20.0,
        "emg_km":       28.0,
        "build_cost":   300,
        "drone_cost":   40,
        "annual_ops":   120,
    },
}

def station_total_cost(stype):
    """计算单站点总成本（万元）= 建设 + 无人机采购 + 5年运营"""
    p = STATION_TYPES[stype]
    return p["build_cost"] + p["n_drones"] * p["drone_cost"] + p["annual_ops"] * 5

# ================================================================
# 地理工具：Haversine 球面距离（km）
# ================================================================
def haversine_matrix(lons1, lats1, lons2, lats2):
    """
    计算两组点之间的球面距离矩阵（km）
    lons1, lats1: shape (M,)
    lons2, lats2: shape (N,)
    返回: (M, N) 距离矩阵
    """
    R = 6371.0
    lon1 = np.radians(lons1)[:, None]   # (M, 1)
    lat1 = np.radians(lats1)[:, None]
    lon2 = np.radians(lons2)[None, :]   # (1, N)
    lat2 = np.radians(lats2)[None, :]
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = np.sin(dlat/2)**2 + np.cos(lat1)*np.cos(lat2)*np.sin(dlon/2)**2
    return 2 * R * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def haversine(lon1, lat1, lon2, lat2):
    """单点对距离"""
    R = 6371.0
    dlat = np.radians(lat2 - lat1)
    dlon = np.radians(lon2 - lon1)
    a = np.sin(dlat/2)**2 + np.cos(np.radians(lat1))*np.cos(np.radians(lat2))*np.sin(dlon/2)**2
    return 2 * R * np.arcsin(np.sqrt(a))

# ================================================================
# 1. 加载数据
# ================================================================
print("=" * 60)
print("上海市无人机配送站点多目标优化 —— NSGA-II")
print("=" * 60)
print(f"\n[1] 加载需求数据 ...")

conn = sqlite3.connect(DB_TMP)

# 需求点：POI（加权）+ 模拟订单点
poi_raw = pd.read_sql(
    "SELECT lon, lat, category, weight FROM poi_demand", conn)
ord_raw = pd.read_sql(
    "SELECT lon, lat, area_type, demand_weight FROM order_demand", conn)

# 热点网格（用于生成候选站点）
grid_raw = pd.read_sql(
    "SELECT lon, lat, kde_score, hotspot_level FROM hotspot_grid", conn)
conn.close()

# ================================================================
# 2. 构建候选站点集
# ================================================================
print("\n[2] 构建候选站点集 ...")

# 策略：在高/极高需求区均匀采样，加入少量中需求区和郊区覆盖点
# 上海市各区中心点（补充地理覆盖）
DISTRICT_CENTERS = [
    (121.4737, 31.2304, "黄浦"),  (121.4538, 31.1999, "徐汇"),
    (121.4279, 31.2208, "长宁"),  (121.4484, 31.2489, "静安"),
    (121.3961, 31.2498, "普陀"),  (121.5047, 31.2783, "虹口"),
    (121.5272, 31.2793, "杨浦"),  (121.3767, 31.1139, "闵行"),
    (121.3731, 31.3637, "宝山"),  (121.2274, 31.3748, "嘉定"),
    (121.6105, 31.1994, "浦东"),  (121.0504, 30.7417, "金山"),
    (121.2264, 30.9975, "松江"),  (121.1088, 31.0768, "青浦"),
    (121.4580, 30.8419, "奉贤"),  (121.9716, 31.5156, "崇明"),
]

# 从热点网格采样候选点
high_grid  = grid_raw[grid_raw['hotspot_level'] >= 4].sample(
    n=min(20, len(grid_raw[grid_raw['hotspot_level'] >= 4])), random_state=42)
med_grid   = grid_raw[grid_raw['hotspot_level'] == 3].sample(
    n=min(15, len(grid_raw[grid_raw['hotspot_level'] == 3])), random_state=42)
low_grid   = grid_raw[grid_raw['hotspot_level'] == 2].sample(
    n=min(5,  len(grid_raw[grid_raw['hotspot_level'] == 2])), random_state=42)

candidates = []

# 热点区候选
for _, r in pd.concat([high_grid, med_grid, low_grid]).iterrows():
    candidates.append({
        "lon": float(r['lon']), "lat": float(r['lat']),
        "kde": float(r['kde_score']), "source": "hotspot",
        "prefer_type": 1 if r['hotspot_level'] >= 4 else 2,
    })

# 各区中心候选（保证空间覆盖）
for lon, lat, name in DISTRICT_CENTERS:
    candidates.append({
        "lon": lon, "lat": lat, "kde": 0.3,
        "source": "district", "prefer_type": 2,
    })

cand_df = pd.DataFrame(candidates).drop_duplicates(subset=['lon', 'lat'])
cand_df = cand_df.reset_index(drop=True)
N_CAND = len(cand_df)
print(f"  候选站点: {N_CAND} 个（热点采样 {len(high_grid)+len(med_grid)+len(low_grid)} + 行政区中心 {len(DISTRICT_CENTERS)}）")

# ================================================================
# 3. 构建需求点集
# ================================================================
print("\n[3] 构建需求点集 ...")

# 合并 POI（高权重）和订单需求点
demand_pts = []
for _, r in poi_raw.iterrows():
    demand_pts.append({
        "lon": float(r['lon']), "lat": float(r['lat']),
        "weight": float(r['weight']),
        "is_emergency": 1 if r['category'] in ('hospital', 'shelter') else 0,
    })
for _, r in ord_raw.iterrows():
    demand_pts.append({
        "lon": float(r['lon']), "lat": float(r['lat']),
        "weight": float(r['demand_weight']),
        "is_emergency": 0,
    })

dem_df = pd.DataFrame(demand_pts)
N_DEM = len(dem_df)
dem_lons = dem_df['lon'].values
dem_lats = dem_df['lat'].values
dem_wts  = dem_df['weight'].values / dem_df['weight'].sum()   # 归一化权重
dem_emg  = dem_df['is_emergency'].values.astype(float)
print(f"  需求点: {N_DEM} 个（POI {len(poi_raw)} + 订单模拟 {len(ord_raw)}）")
print(f"  其中应急敏感点（医院+避难点）: {int(dem_emg.sum())} 个")

# 预计算距离矩阵（候选站 × 需求点），单位 km —— 核心加速
cand_lons = cand_df['lon'].values
cand_lats = cand_df['lat'].values
print(f"  预计算距离矩阵 ({N_CAND} × {N_DEM}) ...")
t0 = time.time()
DIST_MAT = haversine_matrix(cand_lons, cand_lats, dem_lons, dem_lats)  # (N_CAND, N_DEM)
print(f"  完成，用时 {time.time()-t0:.1f}s，矩阵形状 {DIST_MAT.shape}")

# ================================================================
# 4. 目标函数
# ================================================================

def evaluate(chromosome):
    """
    输入：chromosome — shape (N_CAND,)，每元素 ∈ {0,1,2,3}
    输出：(f1, f2, f3)
      f1 = 总成本（万元）    ↓ 越小越好
      f2 = 覆盖率（0-1）     ↑ 越大越好（取负值方便最小化）
      f3 = 应急响应时间（分钟）↓ 越小越好
    """
    station_mask = chromosome > 0   # 建站的位置
    station_idx  = np.where(station_mask)[0]

    # ---------- f1：总成本 ----------
    f1 = sum(station_total_cost(int(chromosome[i])) for i in station_idx)
    if len(station_idx) == 0:
        return 1e9, 0.0, 1e9

    # ---------- 服务覆盖矩阵 ----------
    # 对每个建站位置，获取其服务半径
    service_radii = np.array([STATION_TYPES[int(chromosome[i])]["service_km"]
                               for i in station_idx])   # (K,)
    emg_radii     = np.array([STATION_TYPES[int(chromosome[i])]["emg_km"]
                               for i in station_idx])

    # 距离子矩阵：(K, N_DEM)
    sub_dist = DIST_MAT[station_idx, :]   # (K, N_DEM)

    # 每个需求点到最近站点的距离
    min_dist = sub_dist.min(axis=0)       # (N_DEM,)

    # 每个需求点最近站点的服务半径（取最优覆盖站点）
    nearest_idx = sub_dist.argmin(axis=0) # (N_DEM,) — 对应station_idx中的索引
    nearest_radius = service_radii[nearest_idx]   # (N_DEM,)
    nearest_emg    = emg_radii[nearest_idx]

    # ---------- f2：加权覆盖率（日常） ----------
    covered = (min_dist <= nearest_radius).astype(float)
    f2_neg  = -np.dot(covered, dem_wts)   # 越负越好（最小化）

    # ---------- f3：应急响应时间（分钟） ----------
    # 只考虑应急敏感点，飞行时间 = 距离/速度 + 起降时间
    speed_kmh = UAV_PARAMS["cruise_speed_kmh"]
    emg_mask  = dem_emg > 0
    if emg_mask.sum() == 0:
        f3 = 0.0
    else:
        emg_dist   = min_dist[emg_mask]   # 应急点到最近站距离
        emg_w      = dem_wts[emg_mask] / dem_wts[emg_mask].sum()
        fly_time   = emg_dist / speed_kmh * 60 + UAV_PARAMS["takeoff_time_min"]
        # 超出应急半径的点惩罚（×5）
        emg_radius_for_emg = nearest_emg[emg_mask]
        penalty = np.where(emg_dist > emg_radius_for_emg, 5.0, 1.0)
        f3 = np.dot(fly_time * penalty, emg_w)

    return f1, f2_neg, f3


# ================================================================
# 5. NSGA-II 算法
# ================================================================
print("\n[4] 初始化 NSGA-II ...")

# ----- 超参数 -----
POP_SIZE   = 80     # 种群大小
N_GEN      = 150    # 迭代代数
P_CROSS    = 0.9    # 交叉概率
P_MUT      = 0.08   # 每基因变异概率
N_GENES    = N_CAND # 基因长度

# 站点数量约束（软约束，超出则惩罚成本）
MAX_MICRO  = 20
MAX_COMP   = 12
MAX_HUB    = 5

np.random.seed(2024)

def random_individual():
    """生成随机个体，偏向候选点的建议类型"""
    chrom = np.zeros(N_GENES, dtype=int)
    # 随机激活 5-15 个站点
    n_active = np.random.randint(5, 16)
    active_idx = np.random.choice(N_GENES, n_active, replace=False)
    prefer = cand_df['prefer_type'].values
    for i in active_idx:
        # 70% 概率按建议类型，30% 随机
        if np.random.random() < 0.7:
            chrom[i] = prefer[i]
        else:
            chrom[i] = np.random.randint(1, 4)
    return chrom

def apply_constraint(chrom):
    """强制约束：限制各类站点数量上限"""
    chrom = chrom.copy()
    for stype, maxn in [(1, MAX_MICRO), (2, MAX_COMP), (3, MAX_HUB)]:
        idx = np.where(chrom == stype)[0]
        if len(idx) > maxn:
            remove = np.random.choice(idx, len(idx) - maxn, replace=False)
            chrom[remove] = 0
    return chrom

def crossover(p1, p2):
    """均匀交叉"""
    if np.random.random() > P_CROSS:
        return p1.copy(), p2.copy()
    mask = np.random.randint(0, 2, N_GENES).astype(bool)
    c1 = np.where(mask, p1, p2)
    c2 = np.where(mask, p2, p1)
    return c1, c2

def mutate(chrom):
    """随机重置变异"""
    chrom = chrom.copy()
    for i in range(N_GENES):
        if np.random.random() < P_MUT:
            chrom[i] = np.random.randint(0, 4)
    return apply_constraint(chrom)

# ----- 非支配排序 -----
def fast_nondominated_sort(obj_matrix):
    """
    obj_matrix: (N, 3) 所有目标值（均为最小化方向）
    返回: list of fronts, 每个front是个体索引列表
    """
    N = len(obj_matrix)
    n_dominated = np.zeros(N, dtype=int)  # 被支配计数
    dominated_set = [[] for _ in range(N)]  # 我支配的个体集合
    fronts = [[]]

    for i in range(N):
        for j in range(N):
            if i == j:
                continue
            # i 支配 j：i在所有目标≤j，至少一个<j
            if (np.all(obj_matrix[i] <= obj_matrix[j]) and
                    np.any(obj_matrix[i] < obj_matrix[j])):
                dominated_set[i].append(j)
            elif (np.all(obj_matrix[j] <= obj_matrix[i]) and
                    np.any(obj_matrix[j] < obj_matrix[i])):
                n_dominated[i] += 1
        if n_dominated[i] == 0:
            fronts[0].append(i)

    k = 0
    while fronts[k]:
        next_front = []
        for i in fronts[k]:
            for j in dominated_set[i]:
                n_dominated[j] -= 1
                if n_dominated[j] == 0:
                    next_front.append(j)
        k += 1
        fronts.append(next_front)

    return fronts[:-1]  # 去掉最后空前沿


def crowding_distance(obj_matrix, front):
    """计算给定front中各个体的拥挤距离"""
    if len(front) <= 2:
        return {i: float('inf') for i in front}

    distances = {i: 0.0 for i in front}
    n_obj = obj_matrix.shape[1]
    front_arr = np.array(front)

    for m in range(n_obj):
        vals = obj_matrix[front_arr, m]
        order = np.argsort(vals)
        sorted_front = front_arr[order]
        sorted_vals  = vals[order]

        rng = sorted_vals[-1] - sorted_vals[0]
        if rng < 1e-10:
            continue

        distances[sorted_front[0]]  = float('inf')
        distances[sorted_front[-1]] = float('inf')
        for k in range(1, len(sorted_front) - 1):
            distances[sorted_front[k]] += (sorted_vals[k+1] - sorted_vals[k-1]) / rng

    return distances

def tournament_select(pop, obj_matrix, fronts, crowd_dist, k=2):
    """锦标赛选择：先比较前沿等级，相同则比较拥挤距离（越大越好）"""
    front_rank = np.zeros(len(pop), dtype=int)
    for rank, front in enumerate(fronts):
        for i in front:
            front_rank[i] = rank

    candidates = np.random.choice(len(pop), k, replace=False)
    best = candidates[0]
    for c in candidates[1:]:
        if (front_rank[c] < front_rank[best] or
                (front_rank[c] == front_rank[best] and
                 crowd_dist.get(c, 0) > crowd_dist.get(best, 0))):
            best = c
    return pop[best].copy()

# ----- 初始种群 -----
print(f"  种群大小={POP_SIZE}，迭代={N_GEN}代，候选站点={N_CAND}")
population = np.array([apply_constraint(random_individual()) for _ in range(POP_SIZE)])

# 评估初始种群
print(f"  评估初始种群 ...")
objectives = np.array([evaluate(ind) for ind in population])   # (POP, 3)

best_log = []  # 记录每代帕累托前沿

# ================================================================
# 6. 主循环
# ================================================================
print(f"\n[5] 开始进化 ...")
t_start = time.time()

for gen in range(N_GEN):

    # --- 非支配排序 + 拥挤距离 ---
    fronts = fast_nondominated_sort(objectives)
    all_crowd = {}
    for front in fronts:
        cd = crowding_distance(objectives, front)
        all_crowd.update(cd)

    # --- 生成子代 ---
    offspring = []
    while len(offspring) < POP_SIZE:
        p1 = tournament_select(population, objectives, fronts, all_crowd)
        p2 = tournament_select(population, objectives, fronts, all_crowd)
        c1, c2 = crossover(p1, p2)
        offspring.append(mutate(c1))
        offspring.append(mutate(c2))

    offspring = np.array(offspring[:POP_SIZE])
    off_obj   = np.array([evaluate(ind) for ind in offspring])

    # --- 合并父代+子代，选取最优 POP_SIZE 个 ---
    combined_pop = np.vstack([population, offspring])
    combined_obj = np.vstack([objectives, off_obj])

    all_fronts = fast_nondominated_sort(combined_obj)
    all_crowd2 = {}
    for front in all_fronts:
        cd = crowding_distance(combined_obj, front)
        all_crowd2.update(cd)

    selected = []
    for front in all_fronts:
        if len(selected) + len(front) <= POP_SIZE:
            selected.extend(front)
        else:
            # 按拥挤距离排序，取前面的
            remaining = POP_SIZE - len(selected)
            front_sorted = sorted(front, key=lambda x: -all_crowd2.get(x, 0))
            selected.extend(front_sorted[:remaining])
            break

    population = combined_pop[selected]
    objectives = combined_obj[selected]

    # --- 日志 ---
    pareto_front_0 = [i for i, ind in enumerate(selected)
                      if combined_obj[ind][0] < 1e8][:10]  # 取前10个非无穷解
    if (gen + 1) % 10 == 0:
        valid = objectives[objectives[:, 0] < 1e8]
        if len(valid):
            print(f"  Gen {gen+1:3d}/{N_GEN} | Pareto前沿: {len(all_fronts[0])} 个解 | "
                  f"成本范围: {valid[:,0].min():.0f}-{valid[:,0].max():.0f}万元 | "
                  f"覆盖率: {-valid[:,1].max()*100:.1f}% | "
                  f"应急时间: {valid[:,2].min():.1f}分钟 | "
                  f"用时: {time.time()-t_start:.0f}s")

    best_log.append({
        "gen": gen + 1,
        "pareto_size": len(all_fronts[0]),
        "min_cost": float(objectives[objectives[:,0]<1e8][:,0].min()) if (objectives[:,0]<1e8).any() else None,
        "max_cov":  float(-objectives[objectives[:,0]<1e8][:,1].max()) if (objectives[:,0]<1e8).any() else None,
        "min_emg":  float(objectives[objectives[:,0]<1e8][:,2].min()) if (objectives[:,0]<1e8).any() else None,
    })

print(f"\n  ✓ 进化完成，总用时 {time.time()-t_start:.1f}s")

# ================================================================
# 7. 提取帕累托最优解
# ================================================================
print("\n[6] 提取帕累托最优解 ...")

final_fronts = fast_nondominated_sort(objectives)
pareto_idx   = final_fronts[0]
pareto_pop   = population[pareto_idx]
pareto_obj   = objectives[pareto_idx]

# 过滤无效解（成本过高）
valid_mask   = pareto_obj[:, 0] < 1e8
pareto_pop   = pareto_pop[valid_mask]
pareto_obj   = pareto_obj[valid_mask]

print(f"  帕累托前沿有效解: {len(pareto_pop)} 个")
print(f"  成本范围:    {pareto_obj[:,0].min():.0f} ~ {pareto_obj[:,0].max():.0f} 万元")
print(f"  覆盖率范围:  {-pareto_obj[:,1].max()*100:.1f}% ~ {-pareto_obj[:,1].min()*100:.1f}%")
print(f"  应急时间:    {pareto_obj[:,2].min():.1f} ~ {pareto_obj[:,2].max():.1f} 分钟")

# ----------------------------------------------------------------
# 从帕累托集挑选3个典型方案
# ----------------------------------------------------------------
def pick_representative(obj_matrix, n=3):
    """按极值挑代表解：偏低成本、偏高覆盖、偏短应急，再找综合折中"""
    indices = []
    # 低成本方案
    indices.append(int(np.argmin(obj_matrix[:, 0])))
    # 高覆盖方案（f2最负 = 覆盖率最高）
    indices.append(int(np.argmin(obj_matrix[:, 1])))
    # 快速应急方案
    indices.append(int(np.argmin(obj_matrix[:, 2])))
    # 归一化后取综合折中（距离原点最近）
    norm = (obj_matrix - obj_matrix.min(axis=0)) / (
           (obj_matrix.max(axis=0) - obj_matrix.min(axis=0)) + 1e-10)
    indices.append(int(np.argmin(np.linalg.norm(norm, axis=1))))
    return list(dict.fromkeys(indices))[:n]  # 去重取前n个

rep_idx = pick_representative(pareto_obj)
scheme_names = ["低成本方案", "高覆盖方案", "快速应急方案"]

print(f"\n  典型代表方案:")
for k, (si, sname) in enumerate(zip(rep_idx, scheme_names)):
    chrom = pareto_pop[si]
    obj   = pareto_obj[si]
    n1 = (chrom == 1).sum(); n2 = (chrom == 2).sum(); n3 = (chrom == 3).sum()
    total_drones = n1*2 + n2*5 + n3*10
    print(f"\n  [{sname}]")
    print(f"    总成本:     {obj[0]:.0f} 万元")
    print(f"    服务覆盖率: {-obj[1]*100:.1f}%")
    print(f"    应急响应:   {obj[2]:.1f} 分钟（加权平均）")
    print(f"    站点配置:   微型站×{n1} + 综合站×{n2} + 枢纽站×{n3} = 共{n1+n2+n3}站")
    print(f"    无人机总数: {total_drones} 架")

# ================================================================
# 8. 导出结果
# ================================================================
print("\n[7] 导出结果 ...")

# ---- 工具：GeoPackage 写入 ----
def wkb_point(lon, lat):
    return b'GP\x00\x01' + struct.pack('<i', 4326) + \
           b'\x01' + struct.pack('<I', 1) + struct.pack('<dd', lon, lat)

def init_gpkg(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        PRAGMA application_id = 1196444487; PRAGMA user_version = 10300;
        CREATE TABLE gpkg_spatial_ref_sys (
            srs_name TEXT NOT NULL, srs_id INTEGER NOT NULL PRIMARY KEY,
            organization TEXT NOT NULL, organization_coordsys_id INTEGER NOT NULL,
            definition TEXT NOT NULL, description TEXT);
        CREATE TABLE gpkg_contents (
            table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL,
            identifier TEXT, description TEXT, last_change DATETIME NOT NULL,
            min_x REAL, min_y REAL, max_x REAL, max_y REAL, srs_id INTEGER);
        CREATE TABLE gpkg_geometry_columns (
            table_name TEXT NOT NULL, column_name TEXT NOT NULL,
            geometry_type_name TEXT NOT NULL, srs_id INTEGER NOT NULL,
            z TINYINT NOT NULL, m TINYINT NOT NULL,
            PRIMARY KEY (table_name, column_name));
    """)
    conn.execute("""INSERT OR IGNORE INTO gpkg_spatial_ref_sys VALUES
        ('WGS 84 geodetic',4326,'EPSG',4326,
         'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]',
         'longitude/latitude coordinates')""")
    conn.commit()
    return conn

def add_station_layer(gpkg_conn, table_name, identifier, rows):
    """rows: list of (lon, lat, scheme, stype, sname, n_drones, svc_km, build_cost, total_cost, kde_score)"""
    c = gpkg_conn.cursor()
    c.execute(f"""CREATE TABLE {table_name} (
        fid INTEGER PRIMARY KEY AUTOINCREMENT,
        geom BLOB NOT NULL,
        scheme TEXT, station_type INTEGER, station_name TEXT,
        n_drones INTEGER, service_km REAL, build_cost REAL,
        total_cost REAL, kde_score REAL
    )""")
    for r in rows:
        c.execute(
            f"INSERT INTO {table_name} (geom,scheme,station_type,station_name,n_drones,service_km,build_cost,total_cost,kde_score) VALUES (?,?,?,?,?,?,?,?,?)",
            (wkb_point(r[0], r[1]), r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9])
        )
    lons = [r[0] for r in rows]; lats = [r[1] for r in rows]
    now = datetime.now().isoformat()
    c.execute("INSERT INTO gpkg_contents VALUES (?,?,?,?,?,?,?,?,?,?)",
              (table_name,'features',identifier,'',now,min(lons),min(lats),max(lons),max(lats),4326))
    c.execute("INSERT INTO gpkg_geometry_columns VALUES (?,?,?,?,?,?)",
              (table_name,'geom','POINT',4326,0,0))
    gpkg_conn.commit()
    print(f"    ✓ 图层 {table_name}: {len(rows)} 个站点")

# ---- 写 GeoPackage ----
gpkg_path_tmp = os.path.join(TMP_OUT, "站点优化方案.gpkg")
g = init_gpkg(gpkg_path_tmp)

for k, (si, sname) in enumerate(zip(rep_idx, scheme_names)):
    chrom = pareto_pop[si]
    obj   = pareto_obj[si]
    rows  = []
    for i in range(N_CAND):
        t = int(chrom[i])
        if t == 0:
            continue
        p = STATION_TYPES[t]
        kde = float(cand_df.iloc[i]['kde'])
        rows.append((
            float(cand_df.iloc[i]['lon']),
            float(cand_df.iloc[i]['lat']),
            sname, t, p['name'],
            p['n_drones'], p['service_km'],
            p['build_cost'], station_total_cost(t), kde
        ))
    tbl = f"scheme_{k+1}_{sname.replace('方案','')}"
    add_station_layer(g, tbl, sname, rows)

g.close()

# ---- 复制到项目目录 ----
gpkg_dst = os.path.join(SCRIPT_DIR, "站点优化方案.gpkg")
with open(gpkg_path_tmp, 'rb') as f: data = f.read()
with open(gpkg_dst, 'wb') as f: f.write(data)
print(f"    ✓ GeoPackage 已写入: {os.path.basename(gpkg_dst)} ({len(data)//1024} KB)")

# ---- 导出帕累托前沿 CSV ----
pareto_records = []
for i, (chrom, obj) in enumerate(zip(pareto_pop, pareto_obj)):
    n1=(chrom==1).sum(); n2=(chrom==2).sum(); n3=(chrom==3).sum()
    pareto_records.append({
        "solution_id": i,
        "total_cost_万元": round(float(obj[0]), 1),
        "coverage_pct":   round(-float(obj[1])*100, 2),
        "emg_time_min":   round(float(obj[2]), 2),
        "n_micro":  int(n1), "n_comp": int(n2), "n_hub": int(n3),
        "total_stations": int(n1+n2+n3),
        "total_drones": int(n1*2+n2*5+n3*10),
    })
pareto_df = pd.DataFrame(pareto_records).sort_values("total_cost_万元")

csv_tmp = os.path.join(TMP_OUT, "帕累托前沿方案集.csv")
csv_dst = os.path.join(SCRIPT_DIR, "帕累托前沿方案集.csv")
pareto_df.to_csv(csv_tmp, index=False, encoding='utf-8-sig')
import shutil; shutil.copy2(csv_tmp, csv_dst)
print(f"    ✓ CSV 已写入: 帕累托前沿方案集.csv ({len(pareto_df)} 条方案)")

# ---- 进化过程 JSON（供可视化读取）----
evo_tmp = os.path.join(TMP_OUT, "进化过程.json")
evo_dst = os.path.join(SCRIPT_DIR, "进化过程.json")
with open(evo_tmp, 'w', encoding='utf-8') as f:
    json.dump(best_log, f, ensure_ascii=False, indent=2)
shutil.copy2(evo_tmp, evo_dst)
print(f"    ✓ JSON 已写入: 进化过程.json")

# ================================================================
# 9. 存入方案数据库（供可视化读取）
# ================================================================
scheme_db_tmp = os.path.join(TMP_OUT, "优化方案.db")
scheme_db_dst = os.path.join(SCRIPT_DIR, "优化方案.db")

conn2 = sqlite3.connect(scheme_db_tmp)
conn2.execute("""CREATE TABLE pareto_solutions (
    id INTEGER PRIMARY KEY, total_cost REAL, coverage REAL,
    emg_time REAL, n_micro INTEGER, n_comp INTEGER, n_hub INTEGER,
    total_stations INTEGER, total_drones INTEGER)""")
conn2.execute("""CREATE TABLE representative_stations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scheme_id INTEGER, scheme_name TEXT,
    lon REAL, lat REAL, station_type INTEGER, station_name TEXT,
    n_drones INTEGER, service_km REAL, total_cost REAL, kde_score REAL)""")
conn2.execute("""CREATE TABLE evolution_log (
    gen INTEGER, pareto_size INTEGER,
    min_cost REAL, max_cov REAL, min_emg REAL)""")

for r in pareto_records:
    conn2.execute(
        "INSERT INTO pareto_solutions VALUES (?,?,?,?,?,?,?,?,?)",
        (r['solution_id'], r['total_cost_万元'], r['coverage_pct'],
         r['emg_time_min'], r['n_micro'], r['n_comp'], r['n_hub'],
         r['total_stations'], r['total_drones']))

for k, (si, sname) in enumerate(zip(rep_idx, scheme_names)):
    chrom = pareto_pop[si]
    for i in range(N_CAND):
        t = int(chrom[i])
        if t == 0: continue
        p = STATION_TYPES[t]
        conn2.execute(
            "INSERT INTO representative_stations (scheme_id,scheme_name,lon,lat,station_type,station_name,n_drones,service_km,total_cost,kde_score) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (k+1, sname, float(cand_df.iloc[i]['lon']), float(cand_df.iloc[i]['lat']),
             t, p['name'], p['n_drones'], p['service_km'], station_total_cost(t),
             float(cand_df.iloc[i]['kde'])))

for log in best_log:
    conn2.execute("INSERT INTO evolution_log VALUES (?,?,?,?,?)",
        (log['gen'], log['pareto_size'], log['min_cost'], log['max_cov'], log['min_emg']))

conn2.commit(); conn2.close()
with open(scheme_db_tmp, 'rb') as f: data = f.read()
with open(scheme_db_dst, 'wb') as f: f.write(data)
print(f"    ✓ 方案数据库: 优化方案.db ({len(data)//1024} KB)")

print(f"\n{'='*60}")
print("✅ NSGA-II 多目标优化完成！")
print(f"{'='*60}")
print(f"输出（多目标优化/ 文件夹）：")
print(f"  🗺  站点优化方案.gpkg       — ArcGIS 三套站点布局方案")
print(f"  📊  帕累托前沿方案集.csv    — 所有Pareto最优解")
print(f"  📈  进化过程.json           — 供可视化脚本读取")
print(f"  📦  优化方案.db             — SQLite数据库（供可视化用）")
print(f"\n接下来运行 04_优化结果可视化.py 生成交互式对比地图")
