# SGS → Tactic：把自博弈的评价机制搬进 Lean4 证明搜索

本项目研究一个可证伪的问题：**SGS 在训练期学到的 Guide（给合成题打分）迁移到推理期后，能否有效预测"这条引理对当前目标有没有用"，并真正改进 MCTS 证明搜索？**

## 项目定位

SGS 的训练期自博弈无法搬进 tactic —— tactic 活在单次 elaboration 内，没有跨 episode 的模型更新。因此本项目不做"SGS 的轻量化复现"，而是把 SGS 的**三角色结构搬到推理期**：

| SGS 训练期角色 | 推理期对应物 | 作用 |
|---|---|---|
| Solver | reap 已有的 policy 端点 | 生成 tactic |
| Conjecturer | 新增猜想端点 | 针对当前目标生成辅助引理 |
| Guide | 新增评审端点 | 给候选引理打分，转成搜索先验 |
| Lean 验证器 | 不变 | 唯一的地面真值 |

## 技术支点

方案成立的关键事实（已在 `frenzymath/reap` 源码中核实）：

1. reap 的动作来源只有一个接口 `PolicyValueEval`，新增动作**不需要改 MCTS 核心**。
2. `have aux : P := ?_` 可以作为 MCTS 动作，被组装成 bullet 证明并通过 kernel 终检 —— `Reap/Test/Tactic/MCTS.lean` 的 `deferredHavePolicyValue` 用例已覆盖。
3. reap 的可靠性护栏现成：禁 `sorry`/`admit`/`?` 结尾 tactic，proof script 重放 + pre-definition kernel 检查。
4. endpoint / model / api_key 全部是 `register_option`，外部 API 天然支持。

## 阶段计划

| 阶段 | 内容 | 闸门 |
|---|---|---|
| 0 | 环境与风险闸门：fork reap，验证 `have ?_` 猜想动作全链路 | M0：不成立则退回一次性分解 tactic |
| 1 | 冻结接口契约 + 假服务端到端 | 可无网络重复运行 |
| 2 | 真实模型接入 + 20 题标定利用率/延迟/token | M1：引理利用率过低则先换模型或加强格式约束 |
| 3 | 接入 MCTS，Guide → 先验，消融开关全部走 option | 四组配置零改码可切换 |
| 4 | 评测集与四组对照实验 | M2：劣于等预算对照则降级为实证评测 |
| 5 | 交付：最小 patch、复现脚本、技术报告 | — |

实验对照固定为四组：reap 原版 / reap + 等额额外采样 / 猜想无 Guide / 猜想 + Guide。

## 目录规划

```
sgs-reap/
├─ reap-fork/     reap 的本地 fork，patch 控制在 3 个文件内
├─ service/       薄代理：/conjecture 与 /guide，OpenAI 兼容后端
├─ tests/         Lean 侧与 Python 侧测试
├─ experiments/   评测脚本、数据、日志
└─ docs/          设计文档、接口契约、实验设计、技术报告
```

`D:\bianma\code\reap` 与 `D:\bianma\code\SGS` 原仓库保持只读，所有改动只发生在本仓库。

## 参考

- reap：<https://github.com/frenzymath/reap>
- REAL-Prover：<https://arxiv.org/abs/2505.20613>
- SGS: Scaling Self-Play with Self-Guidance：<https://arxiv.org/abs/2604.20209>
