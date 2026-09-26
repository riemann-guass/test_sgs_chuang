# 阶段 18 执行日志（P4 前置：判定预算运行时化 + `exception` 诊断）

## 本单元要回答的问题

`docs/phase17-log.md`：miniF2F 上的 G1 不过（非零解 13.3%），但**74% 的失败是 `exception`**
（`tacticException`，含心跳耗尽）。所以那个 0.094 **不能**当成"模型不会做 miniF2F"。
本单元把判定预算做成运行时可配置，并做一次**不花 API 钱**的对照诊断。

## 交付物

| 文件 | 作用 |
|---|---|
| `sgslean/SgsLean/Basic.lean`（改） | `heartbeatsRef` + `getHeartbeats`：环境变量 `SGSLEAN_HEARTBEATS` 覆盖心跳预算 |
| `sgslean/SgsLean/Trivial.lean`（改） | `trivialHeartbeatsRef` + `getTrivialHeartbeats`：`SGSLEAN_TRIVIAL_HEARTBEATS`（"非平凡"判据的**定义参数**，必须可标定） |
| `sgslean/SgsLean/Gate.lean`、`Verify.lean`、`Measure/Dependency.lean`、`Trace.lean`（改） | 四处 `defaultHeartbeats` → `(← getHeartbeats)` |
| `sgslean/SgsLean/Server.lean`（改） | 协议 **v1.2**：`ping` 回报 `heartbeats`/`trivialHeartbeats`/`tacticTimeoutMs`；片段按环境变量注入 `set_option maxHeartbeats` 与 `set_option reap.timeout` |
| `tests/lean_server.py`（新） | **常驻** `sgslean-server` 客户端：一个进程处理多批，避免每个 chunk 重付一次 Mathlib 导入 |
| `tests/diagnose_exceptions.py`（新） | 把某次轨迹里的 `exception` 样本用放大预算重测，输出翻转矩阵 |
| `experiments/results/diagnose_exceptions.json` | 诊断报告 |

## 验收命令与真实输出

```powershell
cd sgs-reap\sgslean
lake build SgsLean            # Build completed successfully (208 jobs).
lake build sgslean-server     # Build completed successfully (412 jobs).

cd sgs-reap
$env:SGSLEAN_IMPORTS='none'; python tests\lean_server.py
```

```
[lean-server] ping: {"heartbeats": 4000000, "importedModules": 2078, "mathlib": false,
                     "status": "ok", "tacticTimeoutMs": 200000,
                     "trivialHeartbeats": 200000, "version": "v1.2"}
[lean-server] t: {"ok": true,  "reason": "ok",            "finalChecked": true, ...}
[lean-server] f: {"ok": false, "reason": "mvar_or_sorry", ...}
[lean-server] PASS
```

## 诊断结果（这是本单元的重点）

```powershell
python tests\diagnose_exceptions.py --limit 6 --heartbeats 40000000 --timeout-ms 300000
```

```
[diag] exception 样本共 122 条，抽样 6 条；放大预算 = 心跳 40,000,000 / 墙钟 300000 ms
[diag] ping：{"heartbeats": 40000000, "importedModules": 9873, "mathlib": true,
              "tacticTimeoutMs": 300000, "version": "v1.2"}
[diag] 翻转后判定分布：{'unknown_identifier': 1, 'unclosed_goals': 2, 'ok': 3}
[diag] 脱离 exception 的样本 6/6（100%），其中直接判 ok 的 3 条
       aime_1984_p5#0:                        exception -> unknown_identifier
       algebra_amgm_prod1toneq1_sum1tongeqn#0: exception -> unclosed_goals
       algebra_sqineq_36azm9asqle36zsq#2:      exception -> ok
       amc12_2001_p9#2:                        exception -> ok
       amc12a_2008_p2#0:                       exception -> ok
       amc12a_2010_p22#0:                      exception -> unclosed_goals
```

**读法（关键）**：

* **6/6 脱离了 `exception`** —— 说明那 74% 的异常桶是**预算伪影**，不是模型能力问题；
* 其中 **3 条直接判 `ok`**：这些证明**本来就是对的**，被心跳上限掐死了；
* 另 3 条变成了真实的失败码（`unclosed_goals` ×2、`unknown_identifier` ×1），这才是模型的问题。

**量级推算**：若这 6/6 的比例在全量 122 条上成立，则
`17 + 122 × (3/6) ≈ 78 / 164`，非零解占比会从 **13.3% 升到远超 20%**——
也就是说 **G1 在 miniF2F 上很可能本来就是通过的**，phase17 的"不过"是工具造成的假阴性。

（样本 n=6，只用于定性；定量要以放大预算重跑 G1 为准。下一步。）

## 顺带查出的两个工具问题

**1. `frontend_ms` 名不副实。** `runBatch` 返回的是**子进程从 spawn 到 wait 的总耗时**
（`Server.lean`），它包含 Mathlib 导入 **与本批作业的全部执行时间**。
本次诊断 `frontend_ms = 812112`，其中导入只占一部分，剩下是 6 次判定的执行。
所以 `docs/phase6-log.md` 里"批处理固定成本 67 s（热）/ 246–493 s（冷）"这个说法
把 import 与作业时间混在一起了——**真实 import 成本要靠"空批"单独测**。

**2. 每个 chunk 起一个服务进程 = 白付多次 Mathlib 导入。**
`run_gate_g1.py` 的 `run_lean_batch` 每批都 `subprocess.run(lake exe sgslean-server)`；
三次 chunk 就是三次导入（phase17 里 106 s / 90 s / 85 s 的那三次）。
`tests/lean_server.py` 改成一个常驻进程 + 多次 flush，导入只付一次。

## 现在在哪 / 下一步

阶段 A 的第一半完成：预算可配置、诊断链路可复跑、结论明确（预算伪影）。
剩余：

1. **加大样本量**（n=20–30）把"3/6 判 ok"这个比例收紧；
2. 用放大后的预算**重跑 miniF2F 的 G1**，拿到诚实的 solve_rate，并同时产出
   `data/targets_hard.jsonl`（裸解不出的目标集）——那才是 N2 的 W；
3. 顺带测一条**预算曲线**（心跳 8M / 16M / 40M），确定"最便宜且不失真"的设置，
   否则重跑 G1 的时间成本会失控（本次 6 条就花了 817 s）。

---

# 阶段 18 续：全量重判（修正后的 G1）+ 两个必须记录的发现

## 全量重判结果

```powershell
python tests\diagnose_exceptions.py --mode all --limit 0 --heartbeats 40000000 --timeout-ms 300000 \
       --out experiments\results\reverify_all_n164.json
```

```
[diag] 修正后 G1：目标 57 / 候选 164 / 通过 44 / 非零解目标 18（31.6%）/ 判定 pass
[diag] hard 目标 37 条写入 data/targets_hard.jsonl
[diag] 翻转后判定分布：{'type_error': 68, 'ok': 44, 'unknown_identifier': 6, 'parse_error': 11,
                        'unclosed_goals': 17, 'exception': 11, 'protocol_error:internal_error': 7}
[diag] 耗时 2549s
```

**闸门 G1（miniF2F 子集）：从"不过（13.3%）"翻转为"通过（31.6%）"。**

| 指标 | phase17（原报告） | phase18（修正后） |
|---|---|---|
| 候选通过 | 17 / 164 | **44 / 164** |
| 非零解目标比例 | 13.3% | **31.6%** |
| `exception` | 122 / 164 | **11 / 164** |
| 判定 | fail_no_signal | **pass** |

`type_error` 抽查确认是**真实的模型失败**（`linarith failed to find a contradiction`、
`introN failed: There are no additional binders...`），不是工具问题。

## 发现 1：判定是可复现的，之前的不一致来自"片段语法错误"

两次全量重判的判定分布对不上（`mvar_or_sorry` 23 → 0、`type_error` 24 → 68），
这本来会动摇所有 G1 数字。于是做了一个对照：用同一子集、同样预算重跑一次，逐条比对。

```
一致 19 / 不一致 1 （共 20 条）
（唯一不一致：amc12a_2008_p2#0，全量那次是 protocol_error，重跑判 ok）
```

**结论：判定是稳定的**。两轮分布差异来自**第一次全量重判时片段还带着语法错误**
（`set_option linter.unusedTactic false` 被插在 tactic 块内部，见"发现 3"）——
那个错误会污染判定码。修好之后分布稳定。

## 发现 2：有些候选会**把验证子进程打崩**（不是判负）

7 条 `protocol_error:internal_error`，集中在 3 个目标（`amc12a_2008_p2/p4/p15`）。
而且它是**偶发**的——`amc12a_2008_p2#0` 在全量那次崩了、在重跑那次判 `ok`。

两个后果：

* **修正后的 44 仍然偏低**：这 7 条里已知至少 1 条其实是 `ok`，其余 6 条未知；
* 需要一个**重试策略**：把 `protocol_error` 的条目单独用小批（每批 1 条）重判一次，
  而不是当成"失败"计入 solve_rate。

（这次的逐条落盘修复把损失从 40 条压到 7 条，说明那个修复有效。）

## 发现 3：两个已修的工具 bug

**3.1 `out.json` 攒到最后写 → 一条候选崩掉，整批 40 条响应全丢。**
改为**逐条落盘**后，同类情况的损失从 40 条降到 7 条。

**3.2 生成的 Lean 片段本身有语法错误。**
`set_option linter.unusedTactic false` 原本被插在 tactic 块**内部**（`run_tac` 之后），
Mathlib 模式下片段报 `unexpected identifier; expected 'in'`。移到 header 后 `child.log` 干净，
判定分布也随之稳定（见发现 1）。

## 阶段 A 完成

**产出**：

* 判定预算运行时可配（协议 v1.2，`ping` 回报实际生效值）；
* `data/targets_hard.jsonl`：**37 条**"修正后裸解不出"的目标——这是 N2 的工作负载 W；
* 修正后的 G1 报告（`experiments/results/reverify_all_n164.json`）与可复现性对照（`repro_check.json`）。

**下一步（阶段 B）**：把 `demand` 接上猜想器。审计已确认 `/conjecture` 在 P1–P3 的任何
harness 里都没被调用过——这是与 SG-Lean 设计最根本的一条偏离。
