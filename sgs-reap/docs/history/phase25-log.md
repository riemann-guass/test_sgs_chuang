# 阶段 25 执行日志（P1 证明器本体：在线九步）

## 为什么做这个单元

`AGENTS.md` 把 P1 列为下一个待建单元。审计的结果很直接：`prover.py` / `retrieval.py` /
`repair.py` 三个文件**只有签名与 docstring，方法体全是 `NotImplementedError`**——
也就是说，规格文档 9.1 节写的验收命令

```powershell
python scripts\prove.py --statement "forall (n : Nat), n + 0 = n" --k 4
```

当时跑不出任何东西。在此之前"在线"只存在于文档里：`runner.py` 的 `collect` 是自己拼的
简化版，不是规格第 3 节的九步。

本单元补上的就是这条路：**一条命题进，一篇过内核终检的证明出**。

## 交付物

| 文件 | 动作 | 内容 |
|---|---|---|
| `sgslean/SgsLean/Trivial.lean` | 改 | 新增 `cheapBatches` / `CheapResult` / `tryCheapTactics`；`closesGoal` 拆出带预算版本 `closesGoalWithBudget` |
| `sgslean/SgsLean/Server.lean` | 改 | 新增 `cheap` 命令；`ping` 回报 `cheapBatches` 三批大小 |
| `sgslean/SgsLean/Test/Cheap.lean` | 新 | 兜底的离线测试（命中 / 未命中试满三批 / 不可解析 / 反向对照） |
| `sgsr/pipeline/prover.py` | 重写 | 九步主线；`Budget` / `Attempt` / `ProofResult`；`parse_input` 闭包声明 |
| `sgsr/pipeline/retrieval.py` | 重写 | 分层检索：库（符号重叠，带复用密度优先）+ 外部（可降级），token 截断 |
| `sgsr/pipeline/repair.py` | 重写 | 失败记录 → `repair_prompt` → 新一轮候选；`RepairBackendError` 与"没生成出东西"分开 |
| `sgsr/models/prompts.py` | 改 | 新增 `repair_prompt`（含 `REPAIR` 区块与 `mvar_or_sorry` 硬指令） |
| `sgsr/models/proxy.py` | 改 | `/solve` 支持调用方直接送 `prompt`（repair 入口） |
| `sgsr/models/mock_server.py` | 改 | 同步支持 `prompt` 入口，让离线链路能把 repair 走完 |
| `scripts/prove.py` | 重写 | 单题 CLI（`--statement` / `--file` / `--k` / `--library` / `--no-cheap` / `--out`） |
| `scripts/run_prover_eval.py` | 重写 | 批量评测：pass@k、三条成本量、兜底命中率、分域分桶、T 的一次性守卫 |

### 九步的落点

```
① parse_input      正则定位 theorem/lemma/example → 绑定变量闭包成 ∀（协议 v1.2 要闭式命题）
② Gate.check       与 ③ 合批（一次 Mathlib 导入最贵，能合就合）
③ cheap            三批 tactic：5 + 7 + 1 条，预算 3× / 10× / 50× 秒杀预算
④ retrieval        库层 + Mathlib 层，1200 token 预算内按分数收入
⑤ /solve           k 篇候选；503/超时**重试一次**，仍失败记 backend_error
⑥ Verify.verify    一批提交 k 对；含 checkProof 内核终检
⑦ repair           把失败码 + 错误原文回灌，最多 R 轮（默认 2）
⑧ ProofResult      RunRecord：path / attempts / usage / lean_ms / wall_ms / retrieval
⑨ 预算与停止       token 预算耗尽 或 某步命中
```

**兜底命中的 tactic 也要过 `Verify.verify`。** 这是刻意的：如果"廉价兜底"自己就是放行口，
那么一条带残余元变量的 `decide` 会绕过整个验收层，而"输出的一定是验证过的"这条不变量
就有了两个实现点。现在两条路（兜底、求解）汇合在同一个验证步骤，不变量只有一个出口。

## 验收命令与真实输出

### Lean 侧

```powershell
cd sgs-reap\sgslean
lake build SgsLean SgsLean.Test sgslean-server      # Build completed successfully (423 jobs)
```

通过服务实测 `cheap`（`SGSLEAN_IMPORTS=none`，快速模式）：

```
ping: {"cheapBatches": [5, 7, 1], "heartbeats": 4000000, "mathlib": false,
       "trivialHeartbeats": 200000, "tacticTimeoutMs": 200000, "version": "v1.2"}
[0] hit=True  tactic='decide' tried=2  :: True
[1] hit=True  tactic='rfl'    tried=1  :: 1 = 1
[2] hit=True  tactic='simp'   tried=3  :: forall (n : Nat), n + 0 = n
[3] hit=False tactic=''       tried=13 :: forall (P : Prop), P
```

第 3 条正是分档预算的证据：`rfl`/`decide` 都没秒掉加零，`simp` 在第 3 条候选上成功，
于是 `tried=3` 且**没有**继续试第二、三批。

### 反向对照（六步走里的第 4 步）

把 `tryCheapTactics` 的命中分支故意改成 `return { hit := false, ... }`，重跑 `lake build SgsLean.Test`：

```
error: SgsLean/Test/Cheap.lean:66:2: 「True」应被兜底命中，实际 hit=false，
        tried=[(rfl, (false, 600000)), (decide, (true, 600000))]
error: SgsLean/Test/Cheap.lean:95:2: 「Nat」应被 constructor 秒掉（这正说明它不该当负例），实际 hit=false
```

改回正确实现后 423 jobs 全过。两条断言都真的抓到了，装置没有空转。

### Python 侧：离线（假服务）全链路

```powershell
python sgsr\models\mock_server.py --port 8766
python scripts\run_prover_eval.py --set probe --path <4 条探针> --k 3 ^
       --library none --imports none --no-cheap --endpoint http://127.0.0.1:8766/solve ^
       --out experiments\_probe_eval_out.json
```

```
pass@k=0.750 solved=3 cheap=0 paths={'solve': 3, 'failed': 1}
  s1 solved=True  path=solve   attempts=[('solve',0,'ok'), ('solve',0,'unclosed_goals'), ('solve',0,'mvar_or_sorry')]
  s2 solved=True  path=solve   attempts=[('solve',0,'ok'), ...]
  s3 solved=True  path=solve   attempts=[('solve',0,'ok'), ...]
  s4 solved=False path=failed  attempts=[solve×3, repair×3]   # R=2 走满
```

三种判定码都出现且被如实分类（`ok` / `unclosed_goals` / `mvar_or_sorry`）；
`s4` 在第二轮 repair 之后停止——*步 9 的停止条件真的在起作用*。

### Python 侧：真模型（**本次没跑成，如实记录**）

```
python scripts\prove.py --statement "∀ (a b : Nat), a + b = b + a" --k 3 --no-cheap ...
→ solved=False，attempts=[{reason: "backend_error",
   detail: "HTTP 503 ... 网络错误: <urlopen error [WinError 10061] 由于目标计算机积极拒绝>"}]
```

`/health` 正常（`{"status":"ok","model":"deepseek-flash"}`）而 `/solve` 503——
**正是 `AGENTS.md` 记的那个坑**：代理起在没有网络权限的进程里。
本次会话的沙箱不允许出网，所以 P1 的真模型数字（规格 9.1 的闸门：D 上 20 道）
留待有网络权限的进程重跑，命令已经就绪。

顺带暴露一个小坑：`python sgsr\models\proxy.py` 直接跑时，Python 把 `sgsr/models/`
放进 `sys.path`，`from sgsr.models import config` 会 `ModuleNotFoundError`。
本次用 `PYTHONPATH=<repo root>` 绕过；**这条只影响启动方式，不影响代码**，
但 `AGENTS.md` 第六节的常用命令建议补一句。

### 顺带修掉：`sgsr/models/` 被 `.gitignore` 吞掉（**代码从未入库**）

提交前核对 `git status` 时发现：本次改的 `proxy.py` / `mock_server.py` 没有出现在改动列表里。
查下去是 `.gitignore` 第 39 行的 `models/`——那条规则本意是挡**模型权重目录**，
但它同时匹配了 `sgs-reap/sgsr/models/`。也就是说**整个模型服务层从未进过版本库**
（`git log --all -- sgs-reap/sgsr/models/` 为空），phase24 把 `service/` 搬过来时连带丢了历史。

后果本来会很严重：`proxy.py` / `mock_server.py` / `prompts.py` / `backend.py` / `config.py`
只存在于本机工作区，任何人 clone 下来都拿不到"怎么调模型"这一半代码，
而 README 的快速开始又明写要起代理。

处理：在 `.gitignore` 里加 `!sgs-reap/sgsr/models/` 例外，并把这一层补进首次提交。
**密钥仍然安全**：`.env` 由 `.gitignore` 第 26 行的 `.env` 规则挡住（已验证
`git check-ignore -v sgs-reap/sgsr/models/.env` 命中），入库的只有 `.env.example`。

## 两个必须记住的观察

**观察 1：廉价兜底把简单题从模型路径上整体吸走。** 第一轮探针里 `True`、`1 = 1`、
`∀ (n:Nat), n + 0 = n`、`∀ (P : Prop), P → P` **四条全部 `path=cheap`**，
零模型调用。这既是好消息（不花钱的解出）也是坏消息（在这些题上模型完全不参与，
两臂对照测不到任何东西）。所以：

* `run_prover_eval.py` 把 `cheap_hits` 与 `cost_per_solved` **分开报**，
  否则兜底会以 0 token 把 `CostPerSolved` 拉低，而那是记账口径问题不是真的便宜；
* P2 选工作负载时必须先看兜底命中率：**兜底能秒掉的题不该进增益测量的集合**
  （那里一定测出 0）。

**观察 2：repair 真的接着错误走，而不是重新采样。** `s4` 的轨迹是
`solve(unclosed_goals, mvar_or_sorry)` → `repair(unclosed_goals, mvar_or_sorry)`：
第二轮报的是**同样的**失败码，因为假服务复现不了"针对错误重写"。
但编排的三个不变量都被验证了：失败码回灌、轮数上限、token 记账（`usage` 缺失时留痕）。

## 遗留（明确没做的）

1. **真模型数字未跑**（无网络）。需要：`proxy.py` 在有网络权限的进程里起，
   然后 `scripts\run_prover_eval.py --set D --k 4 --limit 20 --out experiments\results\p1_dev_k4_n20.json`。
2. **`repair` 还没有"失败记录 ≥2 轮"的对照实验**：规格 3.7 的停止条件是轮数或预算，
   但"R=0 / R=1 / R=2 的 pass@k 差"要等真模型才能测。
3. **压缩收益 Δlen 仍未接线**（phase23 就留下的）：它需要"有/无该引理"的配对证明。
4. **外部检索层没有端点可测**：`retrieve_mathlib` 的降级路径已实现并默认生效
   （`degraded=True`），但"配了端点之后能不能拿到东西"要等 LeanSearch/LeanExplore 的接入。
5. **`Prover.verify` 方法目前没有被主线使用**（主线在同一个 `lean.batch` 里直接发
   `verify` 作业，为的是复用同一次 Mathlib 导入）。它是给外部批量调用留的入口；
   若长期没人用应当删掉，别让"两套验证路径"并存。

## 现状与下一步

**P1 的骨架已经完整**：九步全部落地、每一件都有离线测试与真实输出、
兜底与验证的接口在真服务上跑过、批量评测的四项指标都能算出来。
缺的只有"真模型下的那一组数字"，而它被网络权限挡住——这是环境问题不是实现问题。

下一步按规格第 9 节是 **P2（小实验台）**：`tools/prepare_domain_corpus.py`、
`scripts/calibrate_difficulty.py`、`coverage.reuse_cost_greedy`、
把 `runner` 的选择层从 `parent_cover_greedy` 换成复用密度贪心。
但在动 P2 之前，建议先用有网络的进程把 P1 的闸门跑掉——那是 P2 一切测量的基线。
