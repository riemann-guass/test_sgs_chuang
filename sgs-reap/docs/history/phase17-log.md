# 阶段 17 执行日志（P4 准备：miniF2F 上的 G1 —— **闸门不过，停机问**）

## 一、结果（真实输出）

```powershell
# 起代理（带网络权限）
python tests\run_gate_g1.py --file data\minif2f_valid.jsonl --limit 60 --k 3 --chunk 60 ^
       --endpoint http://127.0.0.1:8770/solve
```

```
[g1] 验证批 1: 60 条（frontend 106624ms，累计 795s）
[g1] 验证批 2: 60 条（frontend 90626ms，累计 888s）
[g1] 验证批 3: 44 条（frontend 85380ms，累计 977s）
[g1] k=3 引理 60 条 / 候选 164 篇 / 通过 17 篇
[g1] solve_rate: mean=0.0944  min=0.0  max=1.0  非零解占比=0.1333
[g1] 分布直方图={'部分': 5, '0': 52, '1': 3}
[g1] 失败原因={'exception': 122, 'parse_error': 8, 'unknown_identifier': 1,
              'mvar_or_sorry': 6, 'type_error': 5, 'unclosed_goals': 5}
[g1] 判定：fail_no_signal
stats: {"calls":60,"prompt_tokens":17638,"completion_tokens":209510,
        "total_latency_ms":683359,"avg_latency_ms":11389}
```

**闸门 G1：不过**（判据跑前定死：非零解目标占比需 ≥ 20%；实测 **13.3%**）。
按预案，这是**停机点**——不擅自换主线。

对比两次 G1（这才是重点）：

| 数据集 | 引理数 | 通过/候选 | mean solve_rate | 非零解占比 | 判定 |
|---|---|---|---|---|---|
| G1 初等引理集 | 63 | 164/188 | 0.873 | 98.4% | 通过 |
| **miniF2F valid（前 60）** | 60 | **17/164** | **0.094** | **13.3%** | **不过** |

## 二、失败原因构成里有个必须先查清的问题

`exception` 占了 **122/164 = 74%**。这不是"题证不出"的判定码，而是**验证过程本身抛异常**
（`tacticException`，含心跳耗尽 / 解释执行超时那一类）。所以：

> **现在这个 0.094 还不能当成"模型不会做 miniF2F"的结论。**
> 至少有相当一部分候选是**被我们的验证预算掐死的**，不是模型证错了。

旁证：同一次跑的 `frontend` 时间（106 s / 90 s / 85 s 每批）与我们之前 M1 的观测一致——
Mathlib 级 tactic 在 `lean` 驱动里是**解释执行**，开销比编译版高一个量级；
miniF2F 的语句又是大项（`Real.log`、求和、`∧` 嵌套），`defaultHeartbeats = 4_000_000`
很可能不够。此外模型输出也变长（completion token 从 G1 初等引理的 ~4.3k 涨到 **209.5k**，
平均每次调用 3,500 token、11.4 s），说明它在写长证明——长证明更容易个别 tactic 超时。

## 三、结论与停机问（按项目协议）

**G1 在 miniF2F 上不过，且不过的原因里混着工具性问题。** 按预案有三个方向，
但在选之前必须先做一次**诊断**，否则等于拿一个被污染的数去决策：

1. **先诊断（我建议**，免费或极低成本）：把那 122 篇 `exception` 里的样本（比如 10–20 篇）用
   **大得多的预算**（心跳 ×10、单 tactic 墙钟 600 s）重测，看有多少从 `exception` 变成
   真判定（`ok` / `type_error` / `unclosed_goals`）。这会直接回答"0.094 里有多少是假的"。
   代价：需要给 `SgsLean` 加一个预算开关（现在是编译期常量），再跑一两批。
2. **换 Solver**（预案选项）：本地 REAL-Prover 7B / Kimina 7B——它们本来就是证明器，
   比通用 `deepseek-flash` 更该做这件事；代价是部署成本（显存/时间）。
3. **退域 / 降级**：把工作负载退回更初级的题（我们的初等引理集 G1 通过），
   或把"整篇证明生成"降级为"骨架 + 搜索（MCTS）"——后者要接 P4 的闭环。

顺带记一笔：本轮的"防覆盖"改动生效了——报告写进了 `g1_solver_capability_real_k3_n60.json`，
**没有**覆盖 G1 初等引理集的正式报告 ✓。

## 四、附带产出

* `experiments/results/g1_solver_capability_real_k3_n60.json`：miniF2F 子集的逐条 solve_rate 与逐篇判定码；
* `experiments/runs/g1_20260919T131818Z/traces.jsonl`：164 篇候选的原始判定记录（诊断的输入）；
* `experiments/minif2f_proxy_stats.json`：本次 API 的真实 token/延迟账单。
