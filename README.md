# SG-Lean：复用引导的双尺度 Lean 4 证明器

SG-Lean 输入一条 Lean 命题，输出一段经过 Lean 内核终检的 tactic 脚本。
项目不训练或微调模型，而是把计算分成两个时间尺度：在线解决单题，离线建立可复用引理库。

本项目受 [Scaling Self-Play with Self-Guidance（SGS）](https://arxiv.org/abs/2604.20209)
启发，但不是 SGS 的训练复现。SGS 通过训练更新 Solver；SG-Lean 用经过验证和筛选的
**冻结引理库快照**承载离线积累。

## 核心架构

```text
在线：命题 → 门检 → 从冻结活动库检索 → 生成候选证明 → Lean 内核终检 → 输出
                         ↑
                  LibrarySnapshot
                         ↑
离线：C-build → 需求与候选 → 硬门与证明 → probation
      → C-measure 公平曝光 → reuse/cost → active → 物化、编译、冻结
```

两条路只通过 `LibrarySnapshot` 相连。在线求解不修改库；离线候选必须先进入试用池，
获得跨目标复用证据后才能进入正式活动库。

## 复用判据

```text
reuse(l) = l 被多少个不同的、非来源目标的、通过验收的证明实际引用
cost(l)  = l 进入提示词的 token 成本
score(l) = reuse(l) / cost(l)
```

引用必须来自 Lean 证明项中的常量集合。失败证明里写过库名、同一目标重复引用、
以及候选自己的来源目标，都不计入 `reuse`。

`reuse` 表示“被使用”，不等于“产生因果帮助”。正式实验还会用冻结的有库/无库配对
测量真实 pass@k 增益，并审计引用代理与真实增益是否一致。

## 库生命周期

| 状态 | 含义 | 是否进入正式在线证明器 |
|---|---|---|
| `probation` | 已证明、等待跨目标试用 | 否 |
| `active` | 公平曝光后达到复用门槛 | 是 |
| `cold` | 曝光充分但无复用证据，或已淘汰 | 否 |

检索先按当前目标做相关性召回，再在相关候选中按 `reuse/cost` 排序。这样既避免
无关的全局高频引理占满提示词，也避免新引理永远得不到试用机会。

## 指标

1. **正确率**：冻结测试集上的目标级 `pass@1` / `pass@k`。
2. **成本**：`CostPerSolved`、`CostPerLemma`、`CostPerReusable`。
3. **轻量化硬约束**：0 可训练参数、0 GPU、单实验 ≤12 小时、API ≤500 元。

正式方法比较使用首轮、模型生成、无 repair、无廉价兜底的严格 pass@k。
廉价 tactic 与 repair 是产品增强，单独报告，避免把库效果和其他能力混在一起。

## 数据纪律

| 数据 | 用途 | 禁止 |
|---|---|---|
| C | 离线建库，内部划分 C-build / C-measure | 与 D/T 同源 |
| D（miniF2F valid） | 调参、debug、难度与预算选择 | 入库、需求挖掘、候选来源 |
| T（miniF2F test） | 框架冻结后一次性最终评测 | 调参、建库、试跑 |

## 实验设计

- A：真实需求 + SGS Guide 选择；
- B：真实需求 + `reuse/cost` 选择；
- C：随机等量伪需求 + `reuse/cost` 选择。

A/B 共用候选池，只比较选择判据；B/C 保持选择和预算不变，只比较需求信号。
所有组使用同一模型、k、温度、token 上限、验证器和在线检索配置。

## 当前状态

已经具备 Gate、内核终检、证明项依赖抽取、库物化、常驻 Lean 服务、在线证明器、
离线闭环、数据角色守卫和逐题报告。纯 Python 唯一测试入口当前为 72 条断言。

2026-09-27 完成外部评审后的文字架构重定稿。代码尚未全部迁移到新规格，因此：

- 当前 35 条库是开发期资产，全部 `reuse=0`，并含平凡条目，不用于正式结论；
- 旧“两臂 14/24 引用”统计包含失败证明的文本命中，不是正式 reuse；
- P1 报告仍是 9 题的部分报告；
- 在库三态、统一执行引擎、正确引用统计和硬门修复完成前，不运行正式 P3 或 T。

## 仓库结构

```text
docs/                         权威思路文档、汇报与相关工作
AGENTS.md                     当前项目记忆、硬约束与迁移状态
sgs-reap/
  sgsr/                       Python 服务与两条流程
  sgslean/                    Lean Gate / Verify / Trace / Materialize / Server
  scripts/                    现有五个公共入口
  data/                       C / D / T 数据
  experiments/                库、运行记录与结果
  docs/                       数据协议、接口契约、坑清单与历史日志
```

## 快速自检

```powershell
$py = "C:\Users\gaosen\anaconda3\python.exe"
$env:PYTHONPATH = "D:\bianma\code\大创\sgs-reap"
cd D:\bianma\code\大创\sgs-reap
& $py scripts\run_closure_tests.py --no-lean
```

详细规格见 [SG-Lean 思路文档第二版](docs/SG-Lean思路文档第二版.pdf)，数据协议见
[data-protocol.md](sgs-reap/docs/data-protocol.md)。
