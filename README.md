# SG-Lean：复用导向的轻量自博弈 Lean 4 证明器

> 输入一条 Lean 命题，输出一段通过 Lean 内核终检的证明。
> 做法上沿用 **SGS**（*Scaling Self-Play with Self-Guidance*）的三角色自博弈框架，
> 但把它从**训练期搬到推理期**：用**形式化、可计算的判据**替代 LLM 打分，
> 用**引理库 + 提示词注入**替代权重更新。全程**不做任何梯度训练**。

## 目标与评判指标

| 优先级 | 指标 | 定义 |
|---|---|---|
| **1** | **正确率** | 冻结测试集上的 `pass@1` / `pass@k`，**目标级**统计 |
| **2** | **成本** | `CostPerSolved`、`CostPerLemma`、**`CostPerReusable`**（建库 token 除以"被至少两个不同目标引用过"的引理数） |
| **3** | **轻量化** | **硬约束**：0 可训练参数、0 GPU、单实验 ≤12 小时、单实验 API ≤500 元 |

取舍规则：正确率有显著差异就选正确率高的；无差异选成本低的；都接近选实现简单的；
违反轻量化硬约束的一律淘汰。

## 核心思路

保留 SGS 的三角色结构与循环形状，**替换其中两样东西**：

| SGS（训练期） | SG-Lean（推理期） |
|---|---|
| 求解者：整篇生成证明 | **保留**，仍是语言模型（Lean 是裁判，不是求解者） |
| 出题者：条件化在未解目标上出合成题 | 保留结构，**输入扩展**：额外喂入"跨题重复出现的子目标需求" |
| Guide：LLM 给合成数据打分 | **替换为形式化复用判据** |
| 奖励 × 权重更新 | **替换为库更新 + 提示词注入** |
| 评测在训练题上 | **留出集协议**：建库集与报告集不同源 |

### 唯一的方法性改动：把 Guide 换成"复用判据"

SGS 的 Guide 衡量的是"这条合成题**对当前这道目标**有没有用"；而本领域反复承认的痛点是
**"抽出的引理大多问题特异、不可复用"**——**判据与痛点是错位的**。我们把判据改成面向跨题复用的量：

```
reuse(l) = l 被多少个【不同目标】的【通过验收的】证明实际引用
cost(l)  = l 进入提示词的 token 数
score(l) = reuse(l) / cost(l)
```

它同时承担**准入、排序与淘汰**。选择问题因此成为提示词预算约束下的**单调子模最大化**，
用密度贪心并取较优后具有 `1/2(1-1/e)` 的近似保证。

三个必须照做的设计决定：按"不同目标"而非引用次数计数；只统计通过验收的证明里的引用；
每次实验前做**环境预检**（确认处理臂真的能 `import` 到库）。

## 框架：两个时间尺度

```
┌─ 在线：一道题（秒级至分钟级）───────────────────────────────┐
│  命题 → 门检 → 廉价 tactic 兜底 → 分层检索 → 求解 k 篇      │
│                                    ↑              ↓          │
│                         库(精排) + Mathlib(兜底)  内核终检   │
│                                                  ├ 过 → 输出 │
│                                                  └ 败 → repair│
└─────────────────────────────────────────────────────────────┘
                        ▲ 库注入（受 token 预算约束）
┌─ 离线：一批课程集（小时级）─────────────────────────────────┐
│  采轨迹 → 挖需求 → 出题者出引理 → 硬门 → 求解验证           │
│  → 复用判据打分 → 选择入库 → 物化 → 下一轮                  │
└─────────────────────────────────────────────────────────────┘
```

两个尺度只通过**库**这一个接口相连。**在线只有求解者出场；出题者与评审者只存在于离线**。

## 当前状态

**已具备**（均有离线测试与真实输出）：

- **Lean 侧**：门检 `Gate`、内核终检 `Verify`、非平凡 `Trivial`、新颖 `Novelty`、
  廉价 tactic 兜底 `Trivial.tryCheapTactics`、软分测量 `Measure`、物化 `Materialize`、
  轨迹 `Trace`（含证明项常量，供复用测量）、stdio JSON 服务 `Server`
  （协议 v1.2，含 `cheap` / `cheap_verify` 命令；**一个作业一个 command**，心跳按条复位）
- **Python 侧**：模型代理与假服务、提示词与解析、需求挖掘、猜想、库读写、
  覆盖度与选择、**闭环编排**（十步一环）、**在线九步证明器**
  （`prover` · `retrieval` · `repair` + `scripts/prove.py` / `run_prover_eval.py`）
- **选择层真接线**：准入（探索额度）· 复用测量（按**不同目标**的**通过验收**证明计数）·
  复用/曝光落盘 · 僵尸淘汰（要求"被给过机会"）· 提示词注入（`reuse/cost` 密度贪心）
- **测量装置**：两臂覆盖测量，含环境预检与引用计数；批量评测报三个口径的 pass@k
- **公共入口**：`sgsr/utils/http_client.py`（HTTP）· `sgsr/verification/client.py`
  （Lean 常驻客户端 + 预检）· `sgsr/data/lean_parse.py`（声明→命题）
- **测试**：`scripts/run_closure_tests.py` 60 条断言（含准入/复用/物化往返）

**待建**（见思路文档第二版第 9 节）：

1. **P1 真模型数字**：装置已修好，但 pass@k 与成本要由能出网的进程重跑
   （旧的一份是 phase26 之前的产物，且库与评测集同源，不能当基线）
2. **P2 小实验台**：数据准备、难度标定、复用判据接线、库 ≤100 条
3. **P3 三组对照**：A（SGS 原样，LLM 打分）／B（复用判据 + 需求）／C（随机伪需求消融）
4. **P4 成本与复用分析**、**P5 写作**

**已知的最大工程缺口**：Lean 子进程仍不常驻——每批实验都要重付一次 Mathlib 导入
（实测 75 s 量级）。上游 SGS 用的是持久化 REPL，这是下一件首要工作。

参考基线：miniF2F **valid** 修正预算后（k=3）候选级 44/164、目标级 18/57 ≈ 32%；
"近失手"（解出率严格介于 0 与 1）约占一成，是唯一有增益信号的区间。

## 目录结构

```
.
├─ docs/                     项目级文档（三份 PDF 及 LaTeX 源码）
├─ AGENTS.md                 项目记忆与约束
└─ sgs-reap/
   ├─ sgsr/                  Python 包（对应上游 SGS 的 sgs/ 布局）
   │   ├─ models/            模型服务：真代理 / 假服务 / 提示词 / 后端 / 配置
   │   ├─ pipeline/          在线：prover · retrieval · repair（P1 骨架已建）
   │   │                     离线：demand · conjecture · library · coverage · runner
   │   ├─ verification/      Lean 服务常驻客户端
   │   ├─ data/ · utils/     数据模式与服务辅助
   ├─ sgslean/               Lean 实验库（Gate/Verify/Trivial/Novelty/Measure/Materialize/Trace/Server）
   ├─ reap-fork/             上游 reap 的本地 fork（验证内核与 MCTS）
   ├─ scripts/               实验入口：prove · run_prover_eval · run_round · run_gate_* · …
   ├─ tools/                 数据集转换与后端自检
   ├─ data/ · experiments/   数据集与实验报告
   └─ docs/                  方案、框架、数据协议、接口契约、阶段日志
```

## 快速开始（离线可复现）

```powershell
$py = "C:\Users\gaosen\anaconda3\python.exe"   # Python 3.12.4；lake 由 elan 提供

# Lean 侧：库 + 测试 + 服务（首次需要 Mathlib）
cd sgs-reap\sgslean
lake build SgsLean SgsLean.Test sgslean-server
lake build SgsLean.GeneratedLibrary   # 库目标不编这个模块，必须点名模块目标

# 离线闭环冒烟（假服务，无 Mathlib，快）
cd ..
& $py sgsr\models\mock_server.py --port 8765
& $py scripts\run_round.py --rounds 2 --target-limit 2 --k 1 --n 2 --imports none --expect-mock

# 闭环核心协议自检（纯 Python，秒级）
$env:PYTHONPATH = "D:\bianma\code\大创\sgs-reap"
& $py scripts\run_closure_tests.py --no-lean

# 真实模型（需网络权限；密钥在 sgsr\models\.env，不入库）
& $py sgsr\models\proxy.py --port 8770
& $py scripts\run_gate_g3_real.py --select nearmiss --limit 6 --k 4 --endpoint http://127.0.0.1:8770/solve
```

`/health` 正常但 `/solve` 返回 503，说明代理起在没有网络权限的进程里。

## 文档

| 文档 | 内容 |
|---|---|
| **SG-Lean 思路文档（第二版）** | **实现规格**：目标与指标、求解器配置、全部计算公式、数据规格、阶段计划、接口字段 |
| SG-Lean 思路汇报（第一版） | 理念记录：为什么这样做、与 SGS 的关系 |
| 库的相关工作 | 文献综述：前提选择与引理库演化两条技术线的现状与我们的位置 |

## 定位说明（诚实版）

这个方向已经相当拥挤，因此**明确写出我们不去声称什么**：

| 已被占据 | 代表工作 |
|---|---|
| 前提选择 / 检索 | LeanSearch v2、LeanExplore、LeanPremise、ReProver、Magnushammer |
| 引理库的演化与管理 | DreamProver（wake-sleep）、LEGO-Prover、MathlibLemma |
| 智能体证明器的成本-质量路由 | Optimizing the Cost-Quality Tradeoff of Agentic Theorem Provers |
| 编译器反馈驱动的精修 | Compile to Compress |
| 基准缺陷审计 | Faults in Our Formal Benchmarking |

**我们认为仍然空着的三处**（因此作为主攻方向）：

1. 判据面向**跨题复用**，而不是"对当前目标有没有用"——现有选择判据（LRU、语义聚类、
   结构相似度阈值）都不直接优化这个量；
2. 用**形式化可计算判据替代 LLM 打分**，并在同一预算下正面对照（SGS 论文自己把"用求解动态
   标注合成数据"列为 future work，但未见实现）；
3. 报**"每个可复用引理的 token 成本"**这一成本口径，把复用与成本放在同一张 Pareto 图上。

## 参考

- SGS: Scaling Self-Play with Self-Guidance — <https://arxiv.org/abs/2604.20209>
- DreamProver: Evolving Transferable Lemma Libraries — <https://arxiv.org/abs/2604.26311>
- LeanSearch v2: Global Premise Retrieval — <https://arxiv.org/abs/2605.13137>
- LeanDojo / ReProver — <https://arxiv.org/abs/2306.15626>
- LEGO-Prover: Neural Theorem Proving with Growing Libraries — <https://arxiv.org/abs/2310.00656>
- reap — <https://github.com/frenzymath/reap>
