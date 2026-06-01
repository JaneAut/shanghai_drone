"""
基于强化学习的应急物资预调整模型
===================================

问题描述
--------
在灾害发生前的预警窗口（24小时）内，智能体（RL Agent）通过观察
各无人机站点的物资储量、需求热点分布和天气风险，决策向哪些站点
补充应急物资，以在灾害发生时最大化救援覆盖率、最小化响应时间。

算法：DQN（Deep Q-Network）—— 纯 numpy 实现
  • 两层 MLP：状态维度 → 64 → 32 → 动作数
  • Experience Replay 缓冲区
  • ε-greedy 探索策略（ε从0.9线性衰减至0.05）
  • 目标网络（每50步同步一次）
  • Adam 优化器（手动实现）

状态空间（36维）
  s = [supply_ratio × 17, demand_pressure × 17, time_ratio × 1, weather_risk × 1]

动作空间（18个离散动作）
  a=0        : 待命，不调配
  a=1..17    : 向第 i 号站点补充1单位物资（从中央仓库拨出）

奖励函数
  每步：-0.02 × 仓库出库量（轻微调度成本）
  灾害触发时：各应急需求点覆盖得分之和
    覆盖：+2.0（站点有库存且在服务范围内）
    缺口：-1.0（未覆盖或库存不足）
    快速响应奖励：若平均飞行时间 < 10分钟额外 +5

依赖：numpy, pandas, json, sqlite3, os（均为标准库或已安装）
"""

import os, sys, json, sqlite3, shutil, struct, time
import numpy as np
import pandas as pd
from datetime import datetime
from collections import deque

# ================================================================
# 路径配置
# ================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT    = os.path.dirname(SCRIPT_DIR)
TMP        = "/tmp/drone_analysis"
os.makedirs(TMP, exist_ok=True)

# 加载优化方案数据库（读模块二产物）
DB_SRC = os.path.join(PROJECT, "多目标优化", "优化方案.db")
DB_TMP = os.path.join(TMP, "优化方案.db")
if not os.path.exists(DB_TMP) and os.path.exists(DB_SRC):
    shutil.copy2(DB_SRC, DB_TMP)

# 加载需求热点数据库
DEMAND_SRC = os.path.join(PROJECT, "需求热点分析", "shanghai_drone_demand.db")
DEMAND_TMP = os.path.join(TMP, "shanghai_drone_demand.db")
if not os.path.exists(DEMAND_TMP) and os.path.exists(DEMAND_SRC):
    shutil.copy2(DEMAND_SRC, DEMAND_TMP)

np.random.seed(42)

print("=" * 60)
print("基于 DQN 的应急物资预调整模型训练")
print("=" * 60)

# ================================================================
# 1. 加载站点与需求数据
# ================================================================
print("\n[1] 加载站点与需求数据 ...")

# 读取高覆盖方案（scheme_id=2）站点
conn = sqlite3.connect(DB_TMP)
station_rows = conn.execute(
    "SELECT lon, lat, station_type, station_name, n_drones, service_km "
    "FROM representative_stations WHERE scheme_id=2"
).fetchall()
conn.close()

stations = []
for r in station_rows:
    lon, lat, stype, sname, ndrones, svc_km = r
    max_supply = {2: 10, 3: 20}[stype]   # 综合站10单位，枢纽站20单位
    stations.append({
        "lon": lon, "lat": lat, "type": stype, "name": sname,
        "ndrones": ndrones, "service_km": svc_km,
        "max_supply": max_supply,
        "emg_km": {2: 15.0, 3: 28.0}[stype],
    })
K = len(stations)  # 站点数

# 读取应急敏感需求点（医院+避难点）
conn2 = sqlite3.connect(DEMAND_TMP)
poi_df = pd.read_sql(
    "SELECT lon, lat, category, weight FROM poi_demand WHERE category IN ('hospital','shelter')",
    conn2
)
conn2.close()

demand_lons = poi_df['lon'].values
demand_lats = poi_df['lat'].values
demand_wts  = poi_df['weight'].values / poi_df['weight'].sum()
N_DEM = len(poi_df)

print(f"  站点: {K} 个（综合×{sum(1 for s in stations if s['type']==2)}，枢纽×{sum(1 for s in stations if s['type']==3)}）")
print(f"  应急需求点: {N_DEM} 个（医院+避难点）")

# 预计算站点 → 需求点距离矩阵（km）
def haversine_m(lon1, lat1, lon2, lat2):
    R = 6371.0
    dlat = np.radians(lat2 - lat1)
    dlon = np.radians(lon2 - lon1)
    a = np.sin(dlat/2)**2 + np.cos(np.radians(lat1))*np.cos(np.radians(lat2))*np.sin(dlon/2)**2
    return 2 * R * np.arcsin(np.sqrt(np.clip(a, 0, 1)))

s_lons = np.array([s["lon"] for s in stations])
s_lats = np.array([s["lat"] for s in stations])
# (K, N_DEM)
DIST = np.array([[haversine_m(s["lon"], s["lat"], demand_lons[j], demand_lats[j])
                  for j in range(N_DEM)] for i, s in enumerate(stations)])
SVC_RAD  = np.array([s["service_km"] for s in stations])    # (K,)
EMG_RAD  = np.array([s["emg_km"]     for s in stations])
MAX_SUP  = np.array([s["max_supply"] for s in stations])     # (K,)
TOTAL_CAP = int(MAX_SUP.sum() * 0.5)  # 中央仓库初始库存 = 总容量的50%

# 预计算需求点最近站点（静态）
nearest_station = np.argmin(DIST, axis=0)     # (N_DEM,)
min_dist_to_sta = DIST[nearest_station, np.arange(N_DEM)]  # (N_DEM,)

# 需求压力（每站点服务圈内的需求点加权总量）
demand_pressure = np.zeros(K)
for j in range(N_DEM):
    for i in range(K):
        if DIST[i, j] <= SVC_RAD[i]:
            demand_pressure[i] += demand_wts[j]
dp_max = demand_pressure.max() + 1e-9

print(f"  中央仓库初始库存: {TOTAL_CAP} 单位（总容量: {MAX_SUP.sum()} 单位）")
print(f"  需求压力最高站点: 站点{np.argmax(demand_pressure)} = {demand_pressure.max():.3f}")

# ================================================================
# 2. RL 环境定义
# ================================================================
print("\n[2] 初始化 RL 环境 ...")

STATE_DIM  = K + K + 1 + 1   # supply_ratio(K) + demand_pressure(K) + time + weather
N_ACTIONS  = K + 1            # 0=待命, 1..K=向站点i补货

# 天气风险场景（对应上海常见天气）
WEATHER_PROFILES = {
    "晴好": 0.1,    "多云": 0.3,    "阴雨": 0.5,
    "强风": 0.65,   "暴雨": 0.85,   "台风": 1.0,
}
WEATHER_NAMES = list(WEATHER_PROFILES.keys())
WEATHER_RISKS = list(WEATHER_PROFILES.values())

# 灾害场景（触发时随机选择）
DISASTER_TYPES = {
    "洪涝": {"zone_radius_km": 5,  "duration_h": 6,  "demand_multiplier": 3.0},
    "地震": {"zone_radius_km": 3,  "duration_h": 12, "demand_multiplier": 5.0},
    "大火": {"zone_radius_km": 2,  "duration_h": 3,  "demand_multiplier": 2.0},
    "停电": {"zone_radius_km": 8,  "duration_h": 4,  "demand_multiplier": 1.5},
}


class EmergencyEnv:
    """
    应急物资预调整强化学习环境（Gym风格）

    回合流程：
    ┌─────────────────────────────────────────┐
    │ reset()  →  初始化物资分配 + 随机天气    │
    │ step(a)  ×T →  调配物资，时间推进        │
    │ 随机在 [T//2, T] 触发灾害事件            │
    │ 灾害发生时计算最终覆盖奖励并结束回合      │
    └─────────────────────────────────────────┘
    """

    def __init__(self, max_steps=24):
        self.max_steps   = max_steps
        self.state_dim   = STATE_DIM
        self.n_actions   = N_ACTIONS

    # ──────────────────────────────────────────
    def reset(self):
        """重置环境，返回初始状态向量"""
        # 平均分配初始物资（稍加随机扰动）
        base = TOTAL_CAP // K
        self.supply = np.array([
            min(MAX_SUP[i], max(0, base + np.random.randint(-2, 3)))
            for i in range(K)
        ], dtype=float)
        self.depot   = float(TOTAL_CAP - self.supply.sum())
        self.t       = 0

        # 随机天气
        widx = np.random.randint(0, len(WEATHER_NAMES))
        self.weather_name = WEATHER_NAMES[widx]
        self.weather_risk = WEATHER_RISKS[widx]

        # 随机灾害触发时刻（回合后半段）
        self.disaster_t    = np.random.randint(self.max_steps // 2, self.max_steps)
        self.disaster_type = np.random.choice(list(DISASTER_TYPES.keys()))
        # 灾害中心 = 随机需求点附近
        center_idx = np.random.choice(N_DEM, p=demand_wts)
        self.disaster_center = (demand_lons[center_idx], demand_lats[center_idx])

        self.done = False
        return self._get_state()

    # ──────────────────────────────────────────
    def _get_state(self):
        supply_ratio  = self.supply / (MAX_SUP + 1e-9)          # (K,) ∈[0,1]
        dp_norm       = demand_pressure / dp_max                 # (K,) ∈[0,1]
        time_ratio    = self.t / self.max_steps                  # scalar ∈[0,1]
        weather       = self.weather_risk                        # scalar ∈[0,1]
        return np.concatenate([supply_ratio, dp_norm,
                                [time_ratio], [weather]]).astype(np.float32)

    # ──────────────────────────────────────────
    def step(self, action):
        """
        执行动作，返回 (next_state, reward, done, info)

        动作编码：
          0       → 待命
          1..K    → 向站点 (action-1) 补充1单位物资
        """
        assert not self.done, "回合已结束，请先调用 reset()"

        reward = 0.0
        info   = {"action_type": "待命", "depot_remaining": self.depot}

        # 执行动作
        if action > 0:
            target = action - 1    # 站点索引
            if self.depot >= 1.0 and self.supply[target] < MAX_SUP[target]:
                self.supply[target] += 1.0
                self.depot -= 1.0
                reward -= 0.02      # 轻微调度成本
                info["action_type"] = f"补货→站点{target}({stations[target]['name']})"
            else:
                reward -= 0.05      # 无效动作惩罚（仓库空或站点满）

        self.t += 1

        # 判断是否触发灾害
        if self.t >= self.disaster_t:
            reward += self._compute_disaster_reward()
            self.done = True
        elif self.t >= self.max_steps:
            # 未触发也结束（用较低奖励）
            reward += self._compute_disaster_reward() * 0.5
            self.done = True

        info["depot_remaining"] = self.depot
        return self._get_state(), reward, self.done, info

    # ──────────────────────────────────────────
    def _compute_disaster_reward(self):
        """
        灾害发生时评估物资覆盖质量
        对每个应急需求点：
          - 找最近且有库存的站点
          - 若在服务半径内且库存>0：+2.0
          - 否则：-1.0
        额外奖励：平均飞行时间 < 10分钟 → +5.0
        """
        d_info    = DISASTER_TYPES[self.disaster_type]
        d_lon, d_lat = self.disaster_center
        d_radius  = d_info["zone_radius_km"]
        d_mult    = d_info["demand_multiplier"]

        # 受灾影响的需求点（在灾害半径内的加权需求点）
        affected = []
        for j in range(N_DEM):
            dist_to_center = haversine_m(d_lon, d_lat, demand_lons[j], demand_lats[j])
            if dist_to_center <= d_radius:
                affected.append(j)

        # 若灾区没有需求点，扩大范围
        if not affected:
            affected = list(range(N_DEM))

        score = 0.0
        fly_times = []
        supply_copy = self.supply.copy()

        for j in affected:
            w = demand_wts[j] * d_mult
            # 找最近有库存的站点
            covered = False
            for i in np.argsort(DIST[:, j]):
                if supply_copy[i] > 0 and DIST[i, j] <= EMG_RAD[i]:
                    supply_copy[i] -= 1.0   # 消耗一单位（简化）
                    fly_time = DIST[i, j] / 54.0 * 60 + 1.0  # 54km/h + 起降1min
                    fly_times.append(fly_time)
                    score += 2.0 * w
                    covered = True
                    break
            if not covered:
                score -= 1.0 * w

        # 快速响应奖励
        if fly_times and np.mean(fly_times) < 10.0:
            score += 5.0

        # 天气折扣（天气越恶劣，实际可响应能力越弱）
        score *= (1.0 - self.weather_risk * 0.3)

        return float(score)


# 快速功能测试
env = EmergencyEnv()
s = env.reset()
print(f"  状态维度: {env.state_dim}，动作数: {env.n_actions}")
print(f"  状态向量前5维（供给比例）: {s[:5].round(3)}")
_, r, done, info = env.step(3)
print(f"  执行动作3（补货→站点2）：奖励={r:.3f}，{info['action_type']}")

# ================================================================
# 3. DQN 神经网络（纯 numpy）
# ================================================================
print("\n[3] 构建 DQN 网络 ...")

class NumpyMLP:
    """
    两隐层全连接网络：in → h1 → h2 → out
    激活函数：ReLU（隐层）
    优化器：Adam
    """

    def __init__(self, in_dim, h1, h2, out_dim, lr=1e-3):
        self.lr = lr
        # Xavier 初始化
        def xavier(fan_in, fan_out):
            std = np.sqrt(2.0 / (fan_in + fan_out))
            return np.random.randn(fan_in, fan_out) * std

        self.W1 = xavier(in_dim, h1);  self.b1 = np.zeros(h1)
        self.W2 = xavier(h1, h2);      self.b2 = np.zeros(h2)
        self.W3 = xavier(h2, out_dim); self.b3 = np.zeros(out_dim)

        # Adam 状态
        self._adam_init()

    def _adam_init(self):
        self.t_adam = 0
        beta1, beta2 = 0.9, 0.999
        self.beta1, self.beta2, self.eps = beta1, beta2, 1e-8
        params = [self.W1, self.b1, self.W2, self.b2, self.W3, self.b3]
        self.m  = [np.zeros_like(p) for p in params]
        self.v  = [np.zeros_like(p) for p in params]

    def relu(self, x):
        return np.maximum(0, x)

    def relu_grad(self, x):
        return (x > 0).astype(float)

    def forward(self, x):
        """前向传播，x: (batch, in_dim) 或 (in_dim,)"""
        batch = x.ndim == 2
        if not batch:
            x = x[None, :]
        self.x0 = x
        self.z1 = x @ self.W1 + self.b1
        self.a1 = self.relu(self.z1)
        self.z2 = self.a1 @ self.W2 + self.b2
        self.a2 = self.relu(self.z2)
        self.z3 = self.a2 @ self.W3 + self.b3
        out = self.z3
        return out if batch else out[0]

    def backward(self, target_q):
        """
        MSE 损失反向传播，更新权重
        target_q: (batch, out_dim)
        返回损失值
        """
        batch_size = self.x0.shape[0]
        diff   = self.z3 - target_q              # (B, out)
        loss   = (diff**2).mean()
        dz3    = 2 * diff / batch_size            # (B, out)

        dW3 = self.a2.T @ dz3                    # (h2, out)
        db3 = dz3.sum(axis=0)
        da2 = dz3 @ self.W3.T                    # (B, h2)
        dz2 = da2 * self.relu_grad(self.z2)
        dW2 = self.a1.T @ dz2
        db2 = dz2.sum(axis=0)
        da1 = dz2 @ self.W2.T
        dz1 = da1 * self.relu_grad(self.z1)
        dW1 = self.x0.T @ dz1
        db1 = dz1.sum(axis=0)

        grads = [dW1, db1, dW2, db2, dW3, db3]
        params = [self.W1, self.b1, self.W2, self.b2, self.W3, self.b3]
        self._adam_step(params, grads)
        return loss

    def _adam_step(self, params, grads):
        self.t_adam += 1
        lr_t = self.lr * np.sqrt(1 - self.beta2**self.t_adam) / (1 - self.beta1**self.t_adam)
        for i, (p, g) in enumerate(zip(params, grads)):
            self.m[i] = self.beta1 * self.m[i] + (1 - self.beta1) * g
            self.v[i] = self.beta2 * self.v[i] + (1 - self.beta2) * g**2
            p -= lr_t * self.m[i] / (np.sqrt(self.v[i]) + self.eps)

    def copy_weights_from(self, other):
        """从另一个网络复制权重（目标网络同步）"""
        self.W1 = other.W1.copy(); self.b1 = other.b1.copy()
        self.W2 = other.W2.copy(); self.b2 = other.b2.copy()
        self.W3 = other.W3.copy(); self.b3 = other.b3.copy()

    def save(self, path):
        np.savez(path, W1=self.W1, b1=self.b1, W2=self.W2,
                 b2=self.b2, W3=self.W3, b3=self.b3)
        print(f"  ✓ 权重已保存: {os.path.basename(path)}.npz")

    def load(self, path):
        d = np.load(path + ".npz")
        self.W1, self.b1 = d['W1'], d['b1']
        self.W2, self.b2 = d['W2'], d['b2']
        self.W3, self.b3 = d['W3'], d['b3']


# DQN 超参数
H1, H2      = 64, 32
LR          = 5e-4
GAMMA       = 0.95        # 折扣因子
EPSILON_START = 0.9
EPSILON_END   = 0.05
EPSILON_DECAY = 500       # 回合数
BATCH_SIZE  = 64
BUFFER_SIZE = 5000
TARGET_UPDATE = 50        # 每多少步同步目标网络
N_EPISODES  = 600
MAX_STEPS   = 24

# 建网络
q_net     = NumpyMLP(STATE_DIM, H1, H2, N_ACTIONS, lr=LR)
target_net = NumpyMLP(STATE_DIM, H1, H2, N_ACTIONS, lr=LR)
target_net.copy_weights_from(q_net)

print(f"  网络结构: {STATE_DIM} → {H1} → {H2} → {N_ACTIONS}")
print(f"  参数量: {STATE_DIM*H1+H1 + H1*H2+H2 + H2*N_ACTIONS+N_ACTIONS} 个")

# Replay Buffer
ReplayBuffer = deque(maxlen=BUFFER_SIZE)

# ================================================================
# 4. 训练
# ================================================================
print(f"\n[4] 开始训练（{N_EPISODES} 回合）...")
print(f"  ε: {EPSILON_START}→{EPSILON_END}，γ={GAMMA}，batch={BATCH_SIZE}")

train_log = []    # [(episode, total_reward, epsilon, loss, disaster_type)]
best_reward = -1e9
best_weights_path = os.path.join(TMP, "best_dqn_weights")
total_steps = 0

t_train_start = time.time()

for ep in range(1, N_EPISODES + 1):

    # ε 线性衰减
    eps = max(EPSILON_END,
              EPSILON_START - (EPSILON_START - EPSILON_END) * ep / EPSILON_DECAY)

    state = env.reset()
    ep_reward = 0.0
    ep_loss   = []
    disaster  = env.disaster_type

    for _ in range(MAX_STEPS):
        # ε-greedy 选动作
        if np.random.random() < eps:
            action = np.random.randint(0, N_ACTIONS)
        else:
            q_vals = q_net.forward(state)
            action = int(np.argmax(q_vals))

        next_state, reward, done, _ = env.step(action)
        ep_reward += reward

        # 存入 Replay Buffer
        ReplayBuffer.append((state, action, reward, next_state, done))
        state = next_state
        total_steps += 1

        # 经验回放训练
        if len(ReplayBuffer) >= BATCH_SIZE:
            idx = np.random.choice(len(ReplayBuffer), BATCH_SIZE, replace=False)
            batch = [ReplayBuffer[i] for i in idx]

            s_b   = np.array([b[0] for b in batch], dtype=np.float32)
            a_b   = np.array([b[1] for b in batch])
            r_b   = np.array([b[2] for b in batch], dtype=np.float32)
            ns_b  = np.array([b[3] for b in batch], dtype=np.float32)
            d_b   = np.array([b[4] for b in batch], dtype=np.float32)

            # 当前 Q 值
            q_curr = q_net.forward(s_b)       # (B, N_ACTIONS)

            # 目标 Q 值（Bellman 方程）
            q_next = target_net.forward(ns_b)  # (B, N_ACTIONS)
            q_target = q_curr.copy()
            for bi in range(BATCH_SIZE):
                td_target = r_b[bi] + GAMMA * q_next[bi].max() * (1 - d_b[bi])
                q_target[bi, a_b[bi]] = td_target

            loss = q_net.backward(q_target)
            ep_loss.append(float(loss))

        # 同步目标网络
        if total_steps % TARGET_UPDATE == 0:
            target_net.copy_weights_from(q_net)

        if done:
            break

    avg_loss = float(np.mean(ep_loss)) if ep_loss else 0.0
    train_log.append({
        "episode": ep, "reward": round(ep_reward, 3),
        "epsilon": round(eps, 4), "loss": round(avg_loss, 6),
        "disaster": disaster,
    })

    # 保存最优权重
    if ep_reward > best_reward:
        best_reward = ep_reward
        q_net.save(best_weights_path)

    # 打印进度
    if ep % 50 == 0:
        recent = train_log[-50:]
        avg_r  = np.mean([r['reward'] for r in recent])
        avg_l  = np.mean([r['loss']   for r in recent if r['loss'] > 0])
        elapsed = time.time() - t_train_start
        print(f"  Ep {ep:4d}/{N_EPISODES} | ε={eps:.3f} | "
              f"近50回合平均奖励={avg_r:+.3f} | 损失={avg_l:.5f} | "
              f"耗时={elapsed:.0f}s")

print(f"\n  ✓ 训练完成，总步数={total_steps}，最优奖励={best_reward:.3f}")

# ================================================================
# 5. 策略评估（加载最优权重，运行10次测试）
# ================================================================
print("\n[5] 策略评估（测试10回合）...")

q_net.load(best_weights_path)
test_rewards = []
test_details = []

for t_ep in range(10):
    state = env.reset()
    total_r = 0.0
    actions_taken = []
    for _ in range(MAX_STEPS):
        q_vals = q_net.forward(state)
        action = int(np.argmax(q_vals))
        actions_taken.append(action)
        state, r, done, info = env.step(action)
        total_r += r
        if done: break
    test_rewards.append(total_r)
    # 记录非待命动作
    supply_actions = [a for a in actions_taken if a > 0]
    test_details.append({
        "episode": t_ep+1,
        "reward": round(total_r, 3),
        "weather": env.weather_name,
        "disaster": env.disaster_type,
        "n_supply_actions": len(supply_actions),
        "supply_distribution": {
            f"站点{a-1}({stations[a-1]['name']})": supply_actions.count(a)
            for a in set(supply_actions)
        }
    })

print(f"  测试奖励: {[round(r,2) for r in test_rewards]}")
print(f"  平均奖励: {np.mean(test_rewards):.3f} ± {np.std(test_rewards):.3f}")
print(f"  最优测试回合: 奖励={max(test_rewards):.3f}")
best_test = test_details[np.argmax(test_rewards)]
print(f"  最优回合调配方案（天气:{best_test['weather']} | 灾害:{best_test['disaster']}）:")
for k, v in best_test['supply_distribution'].items():
    if v > 0:
        print(f"    → {k}: +{v} 单位")

# ================================================================
# 6. 保存训练日志和测试结果
# ================================================================
print("\n[6] 保存训练日志 ...")

log_tmp = os.path.join(TMP, "RL训练日志.json")
log_dst = os.path.join(SCRIPT_DIR, "RL训练日志.json")
result  = {
    "train_log":    train_log,
    "test_results": test_details,
    "best_reward":  best_reward,
    "hyperparams": {
        "n_episodes": N_EPISODES, "max_steps": MAX_STEPS,
        "lr": LR, "gamma": GAMMA, "h1": H1, "h2": H2,
        "epsilon_start": EPSILON_START, "epsilon_end": EPSILON_END,
        "buffer_size": BUFFER_SIZE, "batch_size": BATCH_SIZE,
        "target_update": TARGET_UPDATE,
    },
    "env_config": {
        "n_stations": K, "n_demand_pts": N_DEM,
        "total_supply_capacity": int(MAX_SUP.sum()),
        "initial_depot": TOTAL_CAP,
        "state_dim": STATE_DIM, "n_actions": N_ACTIONS,
    },
    "station_info": stations,
}
with open(log_tmp, 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
shutil.copy2(log_tmp, log_dst)

# 最优预调整方案（用最优权重运行一次并记录每步动作）
# 演示场景：以"晴好+洪涝"为典型场景
# 初始物资从半满开始（贴近实际预警状态），灾害中心选需求最密集的区域中心
state = env.reset()
env.weather_name = "晴好"; env.weather_risk = 0.1
env.disaster_t = 20; env.disaster_type = "洪涝"
# 灾害中心：上海中心城区（121.47, 31.23），医院和避难所最密集的区域
env.disaster_center = (121.47, 31.23)
# 初始物资固定为各站点容量的60%（标准预警状态），而非随机
env.supply = np.array([min(MAX_SUP[i], int(MAX_SUP[i] * 0.6)) for i in range(K)], dtype=float)
env.depot  = float(TOTAL_CAP - env.supply.sum())
state = env._get_state()

optimal_plan = []
total_r = 0.0
for step in range(MAX_STEPS):
    q_vals = q_net.forward(state)
    action = int(np.argmax(q_vals))
    state, r, done, info = env.step(action)
    total_r += r
    optimal_plan.append({
        "step": step+1, "action": action,
        "action_desc": info["action_type"],
        "reward": round(r, 4),
        "supply_snapshot": env.supply.tolist(),
        "depot_remaining": round(env.depot, 1),
    })
    if done: break

plan_tmp = os.path.join(TMP, "最优预调整方案.json")
plan_dst = os.path.join(SCRIPT_DIR, "最优预调整方案.json")
with open(plan_tmp, 'w', encoding='utf-8') as f:
    json.dump({"scenario": "晴好+洪涝（典型应急预调整演示）",
               "scenario_note": "初始物资60%满仓，灾害中心为上海中心城区，共20步预警窗口",
               "total_reward": round(total_r, 3),
               "steps": optimal_plan, "stations": stations}, f, ensure_ascii=False, indent=2)
shutil.copy2(plan_tmp, plan_dst)

# 复制最优权重
for ext in [".npz"]:
    src = best_weights_path + ext
    dst = os.path.join(SCRIPT_DIR, "最优DQN权重") + ext
    if os.path.exists(src):
        with open(src,'rb') as f: data=f.read()
        with open(dst,'wb') as f: f.write(data)
        print(f"  ✓ 权重文件: 最优DQN权重.npz ({len(data)//1024} KB)")

print(f"  ✓ 训练日志: RL训练日志.json")
print(f"  ✓ 最优方案: 最优预调整方案.json")

print(f"\n{'='*60}")
print(f"✅ RL 训练完成！接下来运行 06_RL结果可视化.py 生成可视化报告")
print(f"{'='*60}")
