# 阶段 27 执行日志（全仓库代码审计的整改：让核心判据真的在环里）

## 为什么做这一单元

2026-09-22 的一次全量代码检查（对照 `reap1`、上游 SGS 与 Lean 4.28 源码）发现：
**零件都能跑，接线是断的**。具体表现是三条"空转"——

1. **复用判据（项目唯一的方法性改动）四处断线**，后果是库永远长不大、也长不住；
2. **在线第 3 步"廉价 tactic 兜底"被自己的判定否掉**，零模型调用的那条路根本不存在；
3. `scripts/build_library.py` 被 phase26 新加的来源守卫**废掉**，却仍然报 `pass`。

这些都不是笔误，而是"每个模块各自有测试、模块之间的接口没有测试"的结构问题。
所以本单元的一条主线是：**把判定与证据链钉在能被反向对照抓住的地方**，
并把"同一件事有 N 份实现"收敛成一份。

## 交付物

| 文件 | 动作 | 内容 |
|---|---|---|
| `sgsr/utils/http_client.py` | 新增 | 全仓库唯一的 HTTP JSON 客户端（`post_json` 抛 `BackendUnavailable`；`soft_post_json` 收错），替掉 7 份副本 |
| `sgsr/data/lean_parse.py` | 新增 | 全仓库唯一的"Lean 声明 → 闭式命题"实现 + 注释/前导处理 |
| `sgsr/pipeline/coverage.py` | 重写 | 准入（探索额度）/ 注入（密度贪心 + 探索补位）/ 淘汰（要求"被给过机会"）/ 复用分布 |
| `sgsr/pipeline/runner.py` | 改 | 目标侧复用测量、复用与曝光落盘、后端错误单列、稳定名字、预检式工作目录 |
| `sgsr/pipeline/prover.py` | 改 | 兜底改用 `cheap_verify`（一处判定）、计费口径 `usage_total`、缓存命中不计费 |
| `sgsr/pipeline/{retrieval,library,conjecture,repair}.py` | 改 | 排序用 `reuse/cost` 密度、按内容生成稳定名字、错误不再被吞 |
| `sgsr/models/{backend,prompts}.py` | 改 | 缓存命中 usage 清零；`by_cases` 前缀修复；声明解析走共用实现 |
| `sgsr/verification/client.py` | 改 | 按**行数**判收齐（丢响应/重号当场报错）、心跳改为**单条作业**预算、环境预检 `preflight_imports` |
| `sgslean/SgsLean/Server.lean` | 改 | **一个作业一个 command**（心跳计数复位）、新增 `cheap_verify`、Mathlib 模式补 `open` |
| `sgslean/SgsLean/{Trace,Materialize}.lean` | 改 | 轨迹返回 `constants`（复用测量的原料）；引理名由调用方给出（内容哈希） |
| `scripts/{build_library,run_prover_eval,run_gate_g1,g2,g3_real,run_round,prove,solve_mock,verify_lemma_refs,run_materialize}.py` | 改 | 统一到两个公共入口；`pass@k` 分三个口径；写不进库不再算 pass |
| `tools/minif2f_to_jsonl.py` | 改 | 共用解析、去注释、单条作业心跳预算；重生成 D/T |
| `scripts/run_closure_tests.py` | 改 | 43 → **60 条断言**，新增准入/探索额度/复用测量/复用落盘，并去掉一处"逃生口"式断言 |
| `data/minif2f_{valid,test}.jsonl` | 重生成 | 229→244、236→244（**零丢失、零改写**，只增） |

## 逐条修复与实测证据

### P0-1 复用判据四处断线（致命的死锁）

**错在哪**（四处，缺任何一处都让判据失效）：

1. `runner.select` 把本轮新验证的候选一律记 `reuse=0`，而 `select_by_reuse` 先做
   `reuse >= threshold` 的准入门槛 ⟹ **新引理永远进不了库**；
2. `reuse` 是从**候选引理**的 `constants` 里数的，而候选引理是在 `library=None`
   的条件下求解的 ⟹ 结构上不可能出现 `sgs_lem_*`，**reuse 恒为 0**；
3. 测到的 `reuse` 只活在内存里，`commit` 不写它 ⟹ 淘汰读到"每条 reuse 都是 0"，
   检索也拿不到排序键；
4. `retrieve()` 最后按 `score`（符号重叠）重排，把库层按密度排好的顺序丢掉。

**实测（修复前）**：库空 + 2 条新候选，`select_by_reuse(..., threshold=2)` 返回
`chosen=['sgs_lem_1']`（只有已在库的那条）、`blocked_by_threshold=2`；
threshold 改成 0 也一样（`reuse_cost_greedy` 还会滤掉 `reuse=0`）。

**改法**：

* 准入与"复用证据"解耦：新引理走 `coverage.exploration_admission`（库容 + 探索额度）；
* `reuse` 在**目标侧**测：`Trace` 现在一并返回证明项里的 `constants`，
  `RoundRunner.measure_reuse` 按**不同目标**对**通过验收**的证明去重计数；
* `reuse` / `reuse_targets` / `cost_tokens` / `exposures` 由
  `RoundRunner.update_library` 一次性落盘（全库重写只在这一处）；
* 提示词集合由 `coverage.select_by_reuse` 选（密度贪心 + 探索期补位），不再按位置截断；
* 淘汰要求"**被给过机会**"（`exposures > 0`）：没进过提示词的引理不淘汰。

**实测（修复后，假服务闭环，2 轮）**：

```
[round 0] 入库 1 条 → 库 1 条；漏斗 {'passed_hard_gates': 1, 'proof_candidates': 1,
          'verified': 1, 'library_written': 1, 'library_size': 1, 'generated_build_ok': True}
[round 1] 库 1 条；提示词注入 1 条；本轮候选被 novelty 挡下（已与库中引理重复）
        round 0: 库 1 条（可复用 0、被用过一次 0、未用 0）
        round 1: 库 1 条（可复用 0、被用过一次 0、未用 1）
```

库行（磁盘上）：

```
{"stmt": "∀ (a b : Nat), a + b = b + a", "proof": "intro a b\nexact Nat.add_comm a b",
 "verified": true, "source": "round0:d02#0", "source_target": "d02", "source_corpus": "C1",
 "constants": ["Nat", "Nat.add_comm"], "proof_steps": 2, "added_round": 0,
 "reuse": 0, "reuse_targets": [], "exposures": 1,
 "name": "sgs_lem_27bd2969", "cost_tokens": 14}
```

### P0-2 廉价兜底被自己的判定否掉

**错在哪**：旧实现把 `verify` 与 `cheap` 塞进**同一批**，但发批时还不知道命中哪条
tactic，于是只能送 `proof=""`；判定却拿这份空证明的结果去裁定命中的 tactic。

**实测（修复前）**：

```
cheap            -> {"hit": true, "proof": "simp", "tactic": "simp"}
verify(proof="") -> {"ok": false, "reason": "parse_error", "goalsLeft": 1}
verify("simp")   -> {"ok": true, "finalChecked": true}
```

也就是说：命中被否掉、白跑一条作业、每次掉回模型路径，`cheap_hits` 结构性恒为 0。

**改法**：服务端新增 `cheap_verify`——先试三批廉价 tactic，**命中就当场把那条
tactic 送去内核终检**，一次作业返回 `{cheap, verify}`。判定必须发生在拿到 tactic 之后。
客户端保留"老服务端退回两步式"的兼容路径（多付一次导入，但不变式不破）。

**实测（修复后）**：

```
cheap_verify -> {"cheap": {"hit": true, "tactic": "simp"},
                 "verify": {"ok": true, "finalChecked": true, "goalsLeft": 0}}
$ python scripts\prove.py --statement "∀ (n : Nat), n + 0 = n" --k 2 --imports none --endpoint .../solve
  "solved": true, "path": "cheap", "proof": "simp", "model_calls": 0, "notes": []
```

### P0-3 `build_library.py` 被来源守卫废掉却报 pass

**错在哪**：phase26 让 `library.add_many` 强制要求 `source_target` / `source_corpus`，
而 `build_library.py` 写的条目**没有这两个字段**。

**实测（修复前）**：`add_many` 返回 `(0, [{'stmt': 'S', 'reason': '缺 source_target'}])`；
而判定只看 `funnel["verified"] > 0` ⟹ 报 `pass`，实际一条没入库。

**改法**：补来源三件套（`--source-corpus`，默认 C1）与稳定名字；判定改为
「`library_written ≥ 1` 才 pass」，并新增 `fail_not_written`（验证过却没写进库）
与 `fail_name_mismatch`（物化时名字对不上）两个明确失败态。

### P0-4 引理名按位置编号 ⟹ 淘汰会让名字整体错位

`Materialize` 曾按数组下标命名 `sgs_lem_<i+1>`，而淘汰会从库中间删条目：
第 5 条被冷存后，原来的第 6 条就成了 `sgs_lem_5`，历史 `constants`、提示词里给的名字、
已验证的证明文本一起张冠李戴。

**改法**：名字由**内容**决定（`library.name_for` = 归一化语句的 sha256 前 8 位），
在 `add_many` **唯一一处**生成并写进库行；`Materialize.emit` 用调用方给的名字，
缺名字会记进 `missingName`（调用方应当当错误处理）。

### P1-1 心跳按 command 累计（自造的问题）

**错在哪**：一整批作业挤在一个 `run_tac` 里，Lean 的心跳计数器**按 command** 累计
（`Lean.Elab.Command` 在每个 command 开头记 `initHeartbeats`），于是批次尾部集体报
`maximum number of heartbeats`。历史上的缓解是"预算按批大小放大"
（`4M + 200k × n`），那是拿经验公式硬撑。

**改法**：服务端改成**一个作业一个 command**（`runJob i` + 片段里重复 `example`），
计数器自动复位；预算随之改为**单条作业**的固定值（400M），
`budget_for_jobs(n)` 保留旧签名但不再依赖 `n`。真正的上界回到墙钟
（`reap.timeout` 200 s/条 + 客户端的批超时）。

**实测**：Mathlib 模式下 5 条混合作业一批（含 `check` / `cheap_verify` / `trace` /
`verify`，最后一条是最重的 `Even (n * (n+1))`）全部正常返回；D/T 重生成时
244 条一次全喂也没有出现 `exception` 集群。

### P1-2 `LeanServer.batch` 用字典长度判收齐 ⟹ 丢响应会阻塞到超时

**错在哪**：`while len(responses) < expected`。服务端把同一个 id 回两次（或漏回一条）时，
字典长度永远到不了 `expected`，于是**阻塞到 7200 s 才报超时**——现象是"批处理卡死"，
而不是"丢了一条响应"。phase26 记的"没有解释的丢响应"很可能就是这个形态。

**改法**：按**行数**判收齐；重号当场抛 `LeanServerError`；收齐后再核对
"每条作业都有响应""flush 响应存在"。非 JSON 行也给明确的错误。

**实测**：故意发两条同 id 的作业 →
`LeanServerError: server 重复回了 id=dup 的响应（协议违规）；已收到 2/3 行`（立即返回，
不再等 2 小时）。

### P1-3 离线闭环吞掉后端错误

`runner._post` 把网络/HTTP 故障变成 `{"error": …}`，`solve()` 只取 `proofs`
就把它丢了；`conjecture.generate` 的 `error` 也被忽略。于是一次 503 在报告里表现为
"模型什么都没产出"——与在线侧刻意区分 `backend_error` 的做法自相矛盾。

**改法**：`runner.solve` 遇错抛 `BackendUnavailable`，`collect`/`conjecture` 把故障
收进 `backend_errors` 并写进报告的 `reasons["backend_error:*"]` 与漏斗。

### P1-4 同一件事的 N 份实现

7 份 `http_post`、5 份 Lean 批驱动（`subprocess.run(input=…)`），语义还不一致
（有的抛错、有的吞错）。新增 `sgsr/utils/http_client.py`（两种**显式命名**的语义）
与既有的 `sgsr/verification/client.py`，把 `prover` / `repair` / `conjecture` /
`runner` / `build_library` / `run_gate_g1,g2,g3` / `run_solve_mock` /
`verify_lemma_refs` / `run_materialize` 全部接过去；
`tools/minif2f_to_jsonl.py` 也从裸 `subprocess.run` 换成常驻客户端。
`run_server_smoke.py` 例外：它测的**就是** stdio 协议本身（含"非 JSON 行立刻回
`bad_request`""EOF 自动 flush"），用客户端会把这些细节藏起来。

> 顺带踩到一个坑并修掉：新模块最初叫 `sgsr/models/http.py`，与标准库 `http` 撞名——
> 直接 `python sgsr\models\proxy.py` 会以 `ModuleNotFoundError: No module named
> 'http.client'; 'http' is not a package` 崩掉（脚本目录进了 `sys.path`）。
> 已改名 `sgsr/utils/http_client.py`，并实测 `mock_server.py` 能直接启动。

### P1-5 指标口径

* `run_prover_eval` 现在报**三个口径**：`pass_at_k`（含兜底与 repair）、
  `pass_at_k_first_round`、`pass_at_k_model_only`；分母剔除门检拒绝与装置故障
  （`excluded_gate_rejected` / `excluded_backend_errors`）；
* **计费口径** = `prompt + completion`（`reasoning_tokens` 是 completion 的子集，
  三类相加会把推理 token 算两遍）；
* 缓存命中不再回上一次的 usage（旧实现让重跑的成本翻倍），单列 `cache_hits`；
* `run_prover_eval` 与 `run_gate_g3_real` 都改用公共的 `preflight_imports`：
  处理臂的 `import SgsLean.GeneratedLibrary` 不可用时**直接退出**，不让假数据进报告。

实测（C1 前 3 条，假服务，imports=none）：

```
[eval] 环境预检通过（imports=none）
[eval] 可评测 3/3（门检拒绝 0、装置故障 0 已剔除）
[eval] pass@2 = 1.000（3/3）；首轮 1.000；不含兜底 0.000
[eval] 兜底命中 3；总 token 0（计费口径 = prompt+completion）；CostPerSolved 0.0
```

这三个数一起看才说明问题：simple 题被兜底整体吸走，**模型这一档的贡献是 0**。

### P2-1 `extract_proofs` 削掉 `by_cases` 的前两个字母

实测：`by_cases h : n = 0` → `_cases h : n = 0`（`startswith("by")` 的无边界匹配）。
一条合法证明就这么被判成"模型证不出"。改成 `^by\b`（后接词边界）。

### P2-2 "声明 → 命题"的三份实现

`prover.close_declaration`（`∀ n : Nat,`）、`prompts._strip_declaration`
（`(n : Nat) →`）、`tools/minif2f_to_jsonl.convert`（保留括号形态）三份。
现在前两者共用 `sgsr/data/lean_parse.py`；转换器保留自己的括号形态（这是刻意的，
让 D/T 题目文本稳定），但**括号计数、顶层冒号、注释与前导处理**共用同一份实现。

### P2-3 D/T 的 23 条丢失（判定环境缺 `open`）

**根因**：miniF2F 的题面假定文件头的 `open BigOperators Real Nat Topology Rat` 生效，
而判定片段没有这些 `open`（题面文本里也没有）⟹ `π` 报 `unknown_identifier`、
`∑` 报 `parse_error`；另外旧转换器先折叠空白再匹配，会把行尾 `--` 注释后面的内容
整段吃掉。**那是判定环境的问题，不是数学错。**

**改法**：Mathlib 模式的片段补上 `open BigOperators Real Nat Topology Rat`
（无 Mathlib 模式不加）；转换器先去注释再折叠、并改用共享的冒号切分。

**实测**：

```
修复前：valid 244 题 → 过 229 / 拒 15（parse_error 8、unknown_identifier 6、exception 1）
       test  244 题 → 过 236 / 拒 8
修复后：valid 244 题 → 过 244 / 拒 0
       test  244 题 → 过 244 / 拒 0
与 git HEAD 的旧文件比对：valid 229→244、test 236→244，**丢失 0、改写 0**（纯超集）
```

顺带一个标定：`aime_1984_p15` 的题面在 52.8M 心跳下 `whnf` 超时、400M 下通过——
单条作业的预算就是按它标定的。

### P2-4 其它小项

* `Materialize` 的文档说 `sgs_lem_0001`、代码是 `sgs_lem_1`；来源注释拼在证明最后一行
  行尾（已改为独立一行放在定理前）；
* `run_gate_g3_real` 的协议错误检测是死代码（响应缺失时 `reason=None`，永远匹配不到
  那两个字符串），并且打印的路径 `tests\build_library.py` 早已不存在；
* `run_solve_mock` 自己又写了一份 `wait_health` 轮询（改用 `sgsr/utils/service.py`）；
* `SgsLean/Server.lean` 的片段在 Mathlib 模式下不再需要"按批放大预算"的注释与逻辑。

## 反向对照（六步走里的第 4 步）

新断言必须能被故意写坏的实现打红，否则就是空转。实际执行：

| 反向对照 | 故意改坏的东西 | 结果 |
|---|---|---|
| 1 | 让 `exploration_admission` 恒返回空（旧代码的死锁语义） | 6 条断言里 **3 条红**（"准入 2 条"、"被额度挡下 1 条"、"库容只剩 1 条时准入 1 条"） |
| 2 | 把复用测量改成"数引用次数"而不是"数不同目标" | 4 条里 **2 条红**（"按不同目标去重"、"失败证明里的引用不算"） |
| 3 | 淘汰不看 `exposures`（旧语义：超龄即杀） | 15 条里 **2 条红**（含"没进过提示词的引理不淘汰"） |
| 4 | 兜底路径端到端 | 修复前 `path=solve, model_calls=1`；修复后 `path=cheap, model_calls=0` |

另外删掉了一处"逃生口"式断言：`test_selection_and_eviction` 里
`... or len(chosen2) == 1` 让那条永远为真——现在写成写死的期望值，
并补了一条"密度贪心真正该赢"的对照池（两条便宜的组合 > 一条贵的单条）。

## 验收命令与真实输出

```powershell
# Lean 侧（含全部离线测试）
cd sgs-reap\sgslean
lake build SgsLean SgsLean.Test sgslean-server      # Build completed successfully (423 jobs)
lake build SgsLean.GeneratedLibrary                 # 7889 jobs（空库也要能编）

# 闭环核心协议测试（纯 Python，秒级）
$env:PYTHONPATH = "D:\bianma\code\大创\sgs-reap"
& $py scripts\run_closure_tests.py --no-lean        # [closure-tests] PASS（60 条断言）

# 单题（假服务）：兜底路径必须零模型调用
& $py scripts\prove.py --statement "∀ (n : Nat), n + 0 = n" --k 2 --imports none `
      --endpoint http://127.0.0.1:8791/solve

# 批量评测（三口径 + 环境预检）
& $py scripts\run_prover_eval.py --set C1 --limit 3 --k 2 --imports none --library none `
      --endpoint http://127.0.0.1:8791/solve --out experiments\results\p1_probe_phase27.json

# 闭环（假服务）：空库 → 一轮后库里 1 条 → 下一轮提示词里注入 1 条
& $py scripts\run_round.py --rounds 2 --curriculum experiments\runs\closure_scratch\demo_curriculum.jsonl `
      --k 1 --n 1 --imports none --expect-mock

# 自检
& $py -m compileall -qf sgsr scripts tools
```

## 现状与下一步

**已完成**：上面 P0/P1/P2 全部落码，`scripts/run_closure_tests.py --no-lean` 60/60；
Lean 侧 423 jobs 全过；D/T 重生成为 244/244 且与旧文件是纯超集关系；
假服务闭环实测"库能长大、能被注入、能被曝光记账"。

**仍未做（诚实记录）**：

1. **真模型数字仍未跑**（本会话沙箱不通网）。P1 闸门命令已就绪：
   `scripts\run_prover_eval.py --set D --k 4 --limit 20 --out experiments\results\p1_dev_k4_n20.json`。
   注意旧的 `p1_dev_k4_n20.json` 是 phase26 之前的产物，且当时代理里挂的库来自 D 的三个目标、
   评测集也是 D（自我循环），**不能当基线**。
2. **Lean 子进程仍未常驻**：每批仍要付一次 Mathlib 导入（实测 75 s 量级）。
   本单元把"按批放大心跳"这个绕路去掉了，但"同一份 Mathlib 装了 N 遍"还在。
   上游 SGS 用的是持久化 REPL（`query_repl`），这是下一件最值钱的工作。
3. **两条描述性主张待用户批准后修改**（属"重大方向改变"，见 phase26 日志）：
   "自博弈"命名偏强；子模性保证不适用于真实 pass@k；`reuse` 是"被使用次数"
   而非因果复用价值。
4. **`reuse` 的忠实度仍未审计**：`coverage.spearman` 已就绪，但"引用计数代理"
   与真实边际增益的相关性要等真模型数据。
