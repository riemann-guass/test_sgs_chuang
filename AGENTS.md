# AGENTS.md —— 项目记忆与约束

> 进入本仓库先读本文件。**实现规格以 `docs/SG-Lean思路文档第二版.pdf` 为准**（源码为同名 `.tex`），
> 写代码时按它的第 3 至 5 节步骤规格与第 9 节阶段计划执行。
> 数据角色见 `sgs-reap/docs/data-protocol.md`；文献基线与"哪些已被别人做过"见
> `docs/库的相关工作.pdf`；工程细节与踩过的坑在 `sgs-reap/docs/phase*-log.md`，需要时再查。
> 与本文件冲突的旧文档，以本文件与第二版文档为准。

---

## 一、目标与评判指标

**做一个证明器**：输入一条数学命题的 Lean 形式化陈述，输出一段通过 Lean 内核终检的证明脚本。
未通过验证的候选证明**不出现在输出中**。

| 项 | 内容 |
|---|---|
| 输入 | 一个 `.lean` 文件（含 `theorem`/`example ... := by sorry`），或一条闭式命题字符串 |
| 输出 | 通过内核终检的 tactic 脚本 + 验证记录 + 成本记录 |

**三个评判指标（所有取舍按此顺序裁决）：**

1. **正确率**（主指标）：在冻结测试集上的 **pass@1 / pass@k**，目标级统计。
2. **成本**：`CostPerSolved` / `CostPerLemma` / **`CostPerReusable`**（建库总 token 除以"被至少两个不同目标引用过"的引理数，本项目最看重的成本量）。
3. **轻量化**：**不是优化对象，是可交付性的硬约束**——0 可训练参数、0 GPU、
   单次实验墙钟不超过 12 小时、单次实验 API 不超过 500 元、一条命令可复现。违反即不合格。

**冲突裁决规则（写死，避免临时争论）**：
正确率有显著差异则选正确率高的；置信区间重叠则选 `CostPerSolved` 低的；成本也接近则选实现更简单的；
任何方案若违反轻量化硬约束直接淘汰，无论正确率多高。

**明确不做的事**：

| 不做 | 理由 | 替代 |
|---|---|---|
| 不训练、不微调任何模型 | 无 GPU；项目定位是"模型之上的脚手架" | 换更好的 API 模型（改一行配置） |
| 不训练检索器 | 这条赛道已被 LeanSearch v2、LeanExplore、LeanPremise 等充分占据 | 直接调用现成检索工具 |
| 不用 miniF2F 作主基准 | 已被做到 96% 至 99%，天花板效应吞掉一切真实增益 | 用有余量的分域集合，见第二版文档 7.4 节 |
| 不重造库的演化与管理 | DreamProver 已完整实现（聚类抽象、LRU 淘汰、树编辑距离去重） | 沿用其方法，只改选择判据 |
| 不做自然语言到 Lean 的自动形式化 | 属另一课题 | 输入直接要求是 Lean 命题 |

---

## 二、思路：SG-Lean（复用导向的轻量自博弈）

**保留 SGS 的三角色结构与循环形状，替换其中两样东西**：

| SGS（训练期） | SG-Lean（推理期） | 说明 |
|---|---|---|
| 求解者 π：整篇生成证明 | **保留**，仍是语言模型 | Lean 是**裁判**不是求解者 |
| 出题者 g：条件化在未解目标上 | **保留结构，扩展输入** | 额外喂入"跨题重复出现的子目标需求" |
| Guide ρ：LLM 打分 | **替换为形式化复用判据** | 本项目唯一的方法性改动 |
| 奖励 × 权重更新 | **替换为库更新 + 提示词注入** | 我们不做梯度更新 |
| 评测在训练题上 | **留出集协议** | 建库集与报告集不同源，比 SGS 更严格 |

### 2.1 核心判据（唯一的方法性改动）

```
reuse(l) = l 被多少个【不同目标】的【通过验收的】证明实际引用
cost(l)  = l 进入提示词的 token 数
score(l) = reuse(l) / cost(l)
```

用途：**准入**（过硬门且 `reuse>=1`）、**排序**、**淘汰**（`reuse=0` 且超过 R 轮则移入冷存）。

选择问题 = 提示词预算约束下的**单调子模最大化** → 密度贪心（边际增益除以成本），
与最优单条取较优后有 `1/2(1-1/e)` 的近似保证。

**三个必须照做的设计决定**：
① 按"不同目标"计数，不按引用次数；② 只统计通过验收的证明里的引用；
③ 每次实验前做**环境预检**（确认处理臂真的能 import 到库）。

### 2.2 在线：一道题

```
命题 -> 门检 -> 廉价 tactic 兜底 -> 分层检索 -> 求解 k 篇 -> 内核验证
                                            |                    |
                                  库(精排) + Mathlib(兜底)   通过 -> 输出 + 记账
                                                              全败 -> repair -> 回到求解
```

### 2.3 离线：一批课程集（库的建立与管理）

```
采轨迹 -> 挖需求 -> 出题者出引理 -> 硬门筛选 -> 求解验证
       -> 复用判据打分 -> 选择入库 -> 物化 -> 下一轮
```

两个尺度只通过**库**这一个接口相连。**在线只有求解者出场；出题者与评审者只存在于离线**。

---

## 三、文件结构

```
D:\bianma\code\大创\
├─ AGENTS.md                    本文件（记忆与约束）
├─ README.md                    GitHub 首页介绍
├─ docs\                        项目级文档
│   ├─ SG-Lean思路文档第二版.pdf/.tex   【实现规格，权威】目标指标、求解器、公式、数据、阶段计划
│   ├─ sg-lean-report.pdf/.tex          《SG-Lean 思路汇报》（第一版，理念记录）
│   └─ 库的相关工作.pdf/.tex            前提选择与引理库演化的文献综述
└─ sgs-reap\
   ├─ sgsr\                     【Python 侧】对应上游 SGS 的 sgs/
   │   ├─ models\               proxy.py(真代理 /solve /conjecture /guide) · mock_server.py(假服务) ·
   │   │                        prompts.py(提示词与解析) · backend.py(OpenAI 兼容) · config.py · .env(不入库)
   │   ├─ pipeline\             demand(N1) · conjecture · library · coverage · runner(闭环编排)
   │   │                        prover.py · retrieval.py · repair.py（P1 骨架已建，待实现）
   │   ├─ verification\         client.py：sgslean-server 的常驻客户端（批处理 + 环境预检）
   │   ├─ data\                 schema.py（轨迹/需求/库的数据模式）·
   │   │                        lean_parse.py（"声明 → 闭式命题"的唯一实现）
   │   └─ utils\                service.py（起服务、日志尾巴、端口解析）·
   │                            http_client.py（全仓库唯一的 HTTP JSON 客户端）
   ├─ sgslean\                  【Lean 侧】Gate · Verify · Trivial · Novelty · Measure · Materialize ·
   │                            Trace · Server(协议 v1.2) · GeneratedLibrary.lean(自动生成，勿手改)
   ├─ reap-fork\                reap 的本地 fork（上游 commit 1477439）
   │                            **验证内核是地基**：sgslean 直接 import Reap.Tactic.{Conjecture,Step,TreeSearch}
   │                            MCTS 全套（Generator/TreeSearch/Options）已就绪、默认关闭
  ├─ scripts\                  实验入口：prove(单题) · run_prover_eval(批量) · run_round(闭环) ·
  │                            build_library · run_conjecture · run_gate_g1/g2/g3_real ·
   │                            run_closure_tests · diagnose_exceptions · run_server_smoke ·
   │                            run_m1_calibration …
   ├─ tools\                    数据集转换与后端自检：minif2f_to_jsonl.py · check_backend.py
   ├─ data\                     数据集（角色见第四节）
   ├─ experiments\              results\(入库的报告) + runs\(逐条轨迹，可再生产物，不入库)
   └─ docs\                     proposal · framework · data-protocol · implementation-blueprint ·
                                api-contract · upstream · phase*-log(历史)
```

---

## 四、硬约束

1. **数据角色不可混**：建库集 `C` 与报告集 `T` **必须不同源**；`D` 可反复用于调参；
   `T` 在框架冻结后**只跑一次**。`scripts/run_round.py` 有硬守卫。
   库中**绝不允许**出现 `source_target` 落在 `D` 或 `T` 上的引理。
2. **绝不输出未验证的证明**：必须过 `Verify.verify` 的内核终检。注意本版 Lean 的 `sorry`
   语法节点匹配不到上游守卫所比的名称，真正拦住它的是 `checkProof`。
3. **不提交密钥**：`sgsr/models/.env` 不入库，任何输出里不得出现 API key。
4. **`/guide`、GuideClient、rubric 必须保留**——它们是三组对照里的 **A 组（SGS 原样）**。
5. **不做梯度训练**；可训练参数恒为 0。
6. **报告文件名带 mode 与规模后缀**，正式数字必须能由 `experiments/results/` 回溯。
7. **库版本冻结**：评测时库不再更新；在线现场生成的临时引理不进全局库。
8. **`reap-fork/.lake` 不删**（复制 fork 时排除全部 `.git` 会破坏 lake 的依赖校验）。

---

## 五、当前进度（截至 2026-09-21）

**已具备**（都有离线测试与真实输出）：

| 部件 | 位置 | 状态 |
|---|---|---|
| 门检、内核验证、非平凡、新颖 | `sgslean/SgsLean/{Gate,Verify,Trivial,Novelty}.lean` | ✅ 含负例与反向对照 |
| 轨迹层（子目标签名 v1.1） | `SgsLean/Trace.lean` + `sgsr/data/schema.py` | ✅ |
| 需求挖掘 `d(g)=freq×cost` | `sgsr/pipeline/demand.py` | ✅ 38 条轨迹挖出 4 条需求 |
| 软分测量（压缩、依赖） | `SgsLean/Measure/` | ✅ 已实现，接线中 |
| 选择与物化入库 | `sgsr/pipeline/{coverage,library}.py` + `Materialize.lean` | ✅ 物化文件可编译 |
| 闭环编排（十步一环） | `sgsr/pipeline/runner.py` + `scripts/run_round.py` | ✅ 离线假服务 2 轮冒烟通过 |
| 两臂覆盖测量装置 | `scripts/run_gate_g3_real.py` | ✅ 含环境预检与引用计数 |
| 模型服务与协议 v1.2 | `sgsr/models/` + `SgsLean/Server.lean` | ✅ |
| 廉价 tactic 兜底（在线第 3 步） | `SgsLean/Trivial.tryCheapTactics` + `Server` 的 `cheap` 命令 | ✅ 三批清单，Mathlib/快速双模式实测 |
| 证明器本体（在线九步） | `sgsr/pipeline/{prover,retrieval,repair}.py` + `scripts/{prove,run_prover_eval}.py` | ✅ 离线假服务全链路；**真模型数字待跑**（phase25） |
| 闭环核心协议（审计 P0/P1 整改） | `schema` 目标身份 · `runner` 真复用判据与来源守卫 · `coverage` 密度贪心与淘汰 | ✅ phase26 |
| 选择层真接线（准入/复用测量/落盘/淘汰/注入） | `coverage` + `runner` + `library.name_for` | ✅ phase27（假服务实测：空库 → 一轮后 1 条 → 下轮注入 1 条） |
| 在线兜底（命中即终检） | `Server.lean` 的 `cheap_verify` + `prover` | ✅ phase27（`path=cheap` 且 `model_calls=0`） |
| 判定执行模型（每作业一个 command） | `Server.lean` 的 `runJob`/`snippetSource` | ✅ phase27（心跳不再按批累计） |
| 公共入口收敛（HTTP / Lean 客户端 / 语句解析） | `utils/http_client.py` · `verification/client.py` · `data/lean_parse.py` | ✅ phase27（替掉 7 份 HTTP、5 份 Lean 驱动、2 份解析） |
| 闭环核心测试 | `scripts/run_closure_tests.py`（**60 条断言**，含准入/探索额度/复用测量与落盘、物化→编译→import 往返） | ✅ 60/60（A 组）+ B 组 |
| D/T 语料 | `data/minif2f_{valid,test}.jsonl` | ✅ phase27 重生成：244/244（旧 229/236，**零丢失零改写**） |
| P1 真模型数字（D 20 题 k=4） | `experiments/results/p1_dev_k4_n20.json` | ✅ phase28（逐题落盘；含三个口径的 pass@k 与 token） |
| P2 语料与标定 | `tools/prepare_domain_corpus.py` · `scripts/calibrate_difficulty.py` · `data/C.jsonl` | ✅ phase28（C = C1 63 + C2 132，与 D/T 零重叠；分档 easy/hard/nearmiss） |
| P2 建库（真模型 3 轮） | `experiments/library.jsonl` | ✅ phase28（**35 条**，C2 来源，含 reuse/exposures/内容哈希名） |
| 两臂装置检查（库真被引用） | `scripts/run_gate_g3_real.py` | ✅ phase28（处理臂 **14/24 篇**引用库引理；评分制 cover 2.5→3.5） |

**待建**（第二版文档第 9 节的 P1 至 P5）：

| 阶段 | 内容 | 交付 |
|---|---|---|
| P1 | 证明器本体：`prover.py` + 兜底臂 + 检索层 + `repair.py` + `scripts/prove.py` | ✅ phase28：真模型数字已跑（D 20 题 k=4） |
| P2 | 小实验台：数据准备、难度标定、复用判据接线、库 ≤100 条 | ✅ phase28：库 35 条、两臂检查给出 14/24 篇引用 |
| P3 | 三组对照（A：SGS 原样／B：复用判据＋需求／C：随机伪需求） | 可比较的同格式报告 |
| P4 | 成本与复用分析 | 论文主图（复用率对成本） |
| P5 | 写作 | 大创报告 + 论文短文 |

**审计遗留（2026-09-26 更新，见 `sgs-reap/docs/phase28-log.md`）**：

1. **Lean 子进程仍不常驻**——每批仍要付一次 Mathlib 导入（实测 75 s 量级；P1 一轮 20 题
   因此耗 2–4 小时）。phase27 已去掉"按批放大心跳"的绕路，但"同一份 Mathlib 装了 N 遍"
   还在。上游 SGS 用持久化 REPL（`query_repl`），仍是收益最大的一件事。
2. **三条表述要改**（改主张属"重大方向改变"，须先问用户）：
   "自博弈"命名偏强；子模性保证不适用于真实 pass@k；`reuse` 是"被使用次数"不是"因果复用价值"。
3. 成本口径按实测：**约 7,500 token/调用**（不是 1,500）；计费口径 = `prompt + completion`
   （`reasoning_tokens` 是 completion 的子集，不能相加）；报告固定记录 `heartbeats_per_job`
   与 `cheap_budget_ms`（判定预算一变，判定本身会变——实测 `aime_1984_p15` 在 4M 下
   门检 `exception`、400M 下 `ok`）。
4. **C2 抽样规则偏了**：按名字排序取前 200 条 ⇒ 抽进来的多是 Mathlib 内部管道引理
   （80 题里 near-miss 只有 5 条，46 条是模型 0/2 的深水区）。P3 之前要按"题面形态"
   重挑（`∀` 量化的等式/不等式、绑定数适中、不含 typeclass 参数）。
5. **代理的后端缓存会让"重复轮"空转**：同一 prompt 第二次 `/solve` 直接命中缓存，
   模型不被调用、拿到同一批候选（实测一轮 73 次调用里 35 次命中）。P3 要重复采样时
   需在提示词里带轮次/盐，或给代理加关缓存的开关。
6. **`reuse` 的忠实度仍未审计**：两臂装置检查给出了 14/24 篇引用（装置通了），
   但"引用计数代理"与真实边际增益的相关性要等 P3/P4 的 `coverage.spearman` 与
   逐引理 with/without 消融。

**参考基线**：miniF2F valid 修正预算后（k=3）候选级 44/164、目标级 18/57 ≈ 32%；
近失手（解出率严格介于 0 与 1）约占一成，是唯一有增益信号的区间。

**库的现状（2026-09-26 更新）**：phase26 那 4 条 D 来源的开发期引理仍在
`experiments/library_cold.jsonl` 冷存（来源守卫会直接拒收它们）。
**活动库 `experiments/library.jsonl` 是 phase28 在 C 上建的：35 条，全部
`source_corpus: "C2"`（来源是 Mathlib 定理，`source_target` 记定理全名），
带 `name`（内容哈希）/`reuse`/`reuse_targets`/`exposures`/`cost_tokens`。**
两臂装置检查（C 的建库集、12 题 k=2）显示处理臂 14/24 篇证明引用了库引理。

---

## 六、常用命令

```powershell
$py = "C:\Users\gaosen\anaconda3\python.exe"        # Python 3.12.4；lake 由 elan 提供

# Lean 侧
cd D:\bianma\code\大创\sgs-reap\sgslean
lake build SgsLean SgsLean.Test sgslean-server
lake build SgsLean.GeneratedLibrary     # 库目标不编这个模块，必须点名模块目标

# 离线假服务闭环
cd D:\bianma\code\大创\sgs-reap
& $py sgsr\models\mock_server.py --port 8765
& $py scripts\run_round.py --rounds 2 --target-limit 2 --k 1 --n 2 --imports none --expect-mock

# 真实模型（另开终端，需网络权限；密钥在 sgsr\models\.env）
# 注意：直接跑脚本时 Python 只把脚本目录放进 sys.path，先设 PYTHONPATH 指向 sgs-reap
$env:PYTHONPATH = "D:\bianma\code\大创\sgs-reap"
& $py sgsr\models\proxy.py --port 8770

# P1：单题 / 批量（真模型数字要在这里跑）
& $py scripts\prove.py --statement "∀ (a b : Nat), a + b = b + a" --k 4
& $py scripts\run_prover_eval.py --set D --k 4 --limit 20 --library none `
      --out experiments\results\p1_dev_k4_n20.json          # 基线臂（不带库）；逐题落盘
& $py scripts\run_gate_g3_real.py --select nearmiss --limit 6 --k 4 --endpoint http://127.0.0.1:8770/solve

# P2：语料 → 难度标定 → 建库（真模型）
& $py tools\prepare_domain_corpus.py --c2-limit 200 --sample 150 `
      --out data\C.jsonl --manifest data\corpus_manifest.json
& $py scripts\calibrate_difficulty.py --set C --corpus C2 --limit 80 --k 2 `
      --endpoint http://127.0.0.1:8770/solve --out experiments\results\calib_C2_k2_n80.json --tier-dir data
& $py scripts\run_round.py --rounds 3 --curriculum data\C_build.jsonl --target-limit 30 `
      --k 2 --n 2 --imports Mathlib,SgsLean.GeneratedLibrary --library-budget 80 `
      --source-corpus C2 --solve-endpoint http://127.0.0.1:8770/solve `
      --conjecture-endpoint http://127.0.0.1:8770/conjecture

# 自检
& $py -m compileall -qf sgsr scripts tools
& $py scripts\run_closure_tests.py --no-lean    # 闭环核心协议：60 条断言，秒级

# 编译项目文档（需 xelatex，连编两遍）
cd D:\bianma\code\大创\docs
xelatex "SG-Lean思路文档第二版.tex"
```

`/health` 正常但 `/solve` 返回 503 → 代理起在没有网络权限的进程里。

---

## 七、协作约定

* **一次只推进一个可验收单元**，六步走：状态检查 → 写码 → 跑 → **反向对照** → 记日志 → 提交。
  "反向对照"指故意把断言写错、确认测试真的失败，防止装置空转。
* 每个阶段写 `sgs-reap/docs/phase<N>-log.md`：为什么做 / 交付物 / 真实输出 / 踩到的坑 / 现状与下一步。
  日志里不许只写结论，验收必须能由命令复现。
* 提交信息：`feat(phaseN): …` / `fix(phaseN): …` / `docs: …`；分支前缀 `codex/`。
* 实测推翻既有假设时，**同时改文档与代码**，不要让文档与实际相反。
* **重大方向改变**（换主线、放弃某条主张、动数据协议、改指标优先级）先问用户，不要自行决定。
