# 阶段 26 执行日志（审计整改：闭环的 P0 阻断项 + 协议守卫 + 测量装置）

## 为什么做这一单元

2026-09-22 收到一份独立审计（`docs/` 之外的评审意见）。它的结论是：

> 外围基础设施已经比较完整，但"复用判据驱动的库增长"这一核心贡献尚未在代码中真正闭环，
> 而且现有闭环存在数个会让实验结论失真的严重错误。**如果现在直接跑正式实验，
> 最可能得到的是"增益很低或为零"，但无法判断究竟是方法无效，还是工程错误造成的。**

我逐条核对过：**审计的 P0 六条全部成立**，其中两条（目标身份、`verified/ok` 字段）
会直接把核心判据变成废纸。本单元就是按它的优先级清单做的整改。

## 交付物与逐条整改

### P0-1 目标身份：候选 id 被当成目标 id

**错在哪**：`schema.trace_from_job` 只存候选作业的 `id`，`demand.mine` 直接把它当目标用。
于是一条目标的 k 篇候选被算成 k 个"不同目标"，`min_targets` 那道跨目标门槛永远能过——
"需求"退化成"同一目标的候选里反复出现的子目标"。

**改法**：
* `schema.py` 新增 `candidate_id()` / `target_of()` 与**必填字段 `target`**；
  `validate_trace` 会抓"缺 target"与"target 等于 id"；
* `demand.py` 的 `freq` 改成**按不同目标计数**：`freq_total = len(targets)`，
  另存 `occurrences`（总出现次数，只作诊断）；`cost` 也从 `goalsLeft`
  改成规格 5.2 节要求的**出现位置均值**；
* `runner.collect` 传 `target`，`run_round` 用 `t["target"]` 判"已解出的目标"。

### P0-2 `dependencies` 的 `verified` 字段被当成 `ok`

**错在哪**：闭环用 `dependencies` 命令跑验证（省一次 Mathlib 导入），它的字段是
`verified`（来自 `Measure.Dependency`）；而 `runner` 检查 `result["ok"]`
（那是 `verify` 命令的字段）。结果是**通过验证的候选全部被判成不可证**——库根本长不大。

**改法**：`runner._verify_ok()` 同时接受 `ok` 与 `verified`；
两者都缺时返回 `None` 并记 `protocol:missing_ok_or_verified`——
**协议错误与"判定为假"必须分开**，否则工具坏了会伪装成"模型证不出"。

### P0-3 物化后没编译、没检查返回

**错在哪**：`commit` 只调 `materialize`，既不 `lake build SgsLean.GeneratedLibrary`，
也不看返回。下一轮提示词里给了 `sgs_lem_i` 的名字，环境里却没有对应的 olean
→ 模型引用时 `unknown identifier` → 表现成"库没用"。phase22 已在两臂测量里踩过一次。

**改法**：`commit` 现在走完"入库 → 复核来源 → 物化 → **编译** → 失败即抛"，
并把 `generated_build_ok` / `jobs` 写进漏斗报告。
顺带修掉一个同类字段错位：`MaterializeResult` 没有 `ok` 字段（是 `written`/`skipped`/`names`），
旧代码按 `ok` 判会永远失败——这条是 B 组测试抓出来的。

### P0-4 数据角色守卫可绕过 + 库没有来源字段

**错在哪**：`assert_buildable()` 定义了但**从没被调用**；`run_round` 只看文件名里有没有
`minif2f`。把 D 复制成别的名字就能进建库流程。库记录里也没有 `source_target`/`source_corpus`，
硬约束 1（库里绝不许出现 D/T 来源的引理）**无法事后审计**。

**改法**（三道）：
1. `add_many` **拒收**缺 `source_target` / 非法 `source_corpus` 的条目，返回 `(写入数, 拒收明细)`；
2. `assert_clean_sources()` 在读库时复核（挡"用别的工具写进来的"历史数据）；
3. `assert_buildable()` 真正被调用，并且**既比解析后的绝对路径、又比内容 sha256**——
   改名换目录但内容还是 D/T 的，一样拒。

### P0-5 `.lean` 文件解析不可靠

**错在哪**：正则直接把整段文本当声明，`import Mathlib` / `namespace` / 文档注释
会被拼进命题里。

**改法**：新增 `_strip_prelude`（剔 import/open/namespace/set_option/块注释）与
`_find_top_level_colon`（找**不在括号内**的冒号，否则 `theorem f (n : Nat) : P`
会在绑定变量的冒号处被切错）；文件路径补行首换行再匹配声明。

### P1-1 复用判据没接线（项目唯一的方法性改动）

**错在哪**：`coverage.py` 只有 `parent_cover` 代理与条数预算，规格 5.5 节的
`reuse/cost` 密度贪心、门槛准入、僵尸淘汰**都不在代码里**。

**改法**：`coverage.py` 重写为
* `reuse_cost_greedy(pool, ctx_budget)`：**token 预算**下的密度贪心；
* `select_by_reuse(pool, ctx_budget, threshold)`：先按 `reuse >= threshold` 过门槛
  （门槛默认 2，与 `CostPerReusable` 的分母口径**一致**）再贪心；
* `evict(library, round, evict_after)`：`reuse=0` 且超龄 → 冷存，新引理有探索额度；
* `spearman()`：给规格要求的"代理忠实度审计"用。

`runner.select` 改成用真判据：从**通过验收的证明**里抽 `constants`（不是对证明文本做子串搜索），
按**不同目标**去重后算 `reuse`，再交给密度贪心。

**这一条里有一个我自己引入又自己修掉的 bug**：第一版"与最优单条取较优"拿单条的
`reuse` **标量**去比贪心的**并集增益**——两个不同的量比大小，于是一条 reuse=5 的引理
会把真正更优的两条组合顶掉，还会选出**超出预算**的单条。测试抓到了它。

### P1-2 相关度硬门缺失

**改法**：`runner.screen` 增加相关度检查（候选必须与父目标共有至少一个符号），
不过就记 `relevance:no_symbol_overlap`。phase19 的"候选原样抄需求"就靠这道门挡。

### P1-3 在线顺序与规格不一致

**改法**：`prover.prove` 把检索挪到门检**之后**（规格 3.4 节的顺序），
不合法输入不再先去打一次外部检索；同时把兜底的终检并进第一批作业。

### P1-4 报告缺元数据、T 的一次性只是开关

**改法**：
* 报告加 `commit` / `library_hash` / `env`（模型名、imports、预算口径）；
  `ProofResult.meta` 也带这些，单题报告同样可回溯；
* T 的守卫变成**两道**：命令行确认 + `experiments/results/test_runs_ledger.jsonl` 台账。
  台账里有记录就直接拒绝（`--force-test-rerun` 才能覆盖，且会**追加**一条，不删历史）。
  按解析后的绝对路径判断"这次是不是在动 T"，`--path` 指向同一个文件也算。

### 并发与可复现性

* `LeanServer` 默认工作目录从**全项目共享**的 `.lake/sgslean-server-work`
  改成**每实例独立**的 `.lake/sgslean-server-work/<pid>-<序号>`（审计"并发与稳定性"一节）。
  目录必须留在 `.lake/` 里：子进程靠"工作目录在项目内"才拿到 lake 的搜索路径。
* `.gitignore`：`experiments/*.jsonl`（引理库与版本快照）与
  `experiments/results/*.json`（正式报告）**改为入库**。审计指出"正式数字必须能回溯"
  却把库和报告挡在版本库外，这两条直接矛盾。

## 新增的测试（审计点名"缺失自动测试"的那一节）

`scripts/run_closure_tests.py`：46 条断言，每条都对应"错了会怎样"。

```
python scripts\run_closure_tests.py --no-lean   # A 组：43 条，纯 Python，秒级
python scripts\run_closure_tests.py             # A + B（B 要 Lean，约 30 分钟）
```

真实输出（A 组）：

```
[closure-tests] PASS（43 条断言）
```

覆盖：目标身份（含"单目标 5 篇候选不构成需求"这条最危险的失真）、
`ok`/`verified` 两种字段名、库来源守卫（写入侧 + 读取侧 + 内容指纹）、
角色守卫（直接路径 / 改名 / 反向对照 / role 字段）、
`.lean` 解析（import/namespace/文档注释/example/无证明体）、
选择与淘汰（门槛、预算、同目标函数比较、探索额度）、Spearman、物化往返。

**这些测试当场抓到 8 条真问题**，包括上面那个"不可比量"的选择 bug、
`MaterializeResult` 的字段错位、以及 `parse_input` 的路径分支漏了行首换行。

## 实测成本标定（审计要求"按实测重新标定"）

单题（`prove.py`，k=4，Mathlib 模式）：

| 项 | 实测 |
|---|---|
| 模型调用 | 中位 **6.9 s** / 次，最长 61.7 s |
| 单次 API token | 约 1,800 prompt + 5,600 completion（20 题 33 次调用合计 246,647） |
| 单次 Mathlib 导入 | **75.6 s**（冷后第二次 74.2 s ⇒ 不是缓存问题） |
| 单题墙钟 | 约 4.75 分钟，其中 **88% 在 Lean 侧** |
| 每题的 Mathlib 导入次数 | 约 3.3（门检+兜底 1、验证 1、repair 1–2） |

**结论**：审计说的"1,500 token/调用的成本估计与实测差 5 倍"是对的——
实测约 7,500 token/调用。预算要按这个数重算：20 题约 25 万 token（约 0.25 元），
D 集 229 题约 280 万 token + 约 18 小时墙钟。

**时间瓶颈的根因不是 Mathlib 本身，而是"同一份 Mathlib 装了 66 遍"**
（20 题的 94.8 分钟里 83.6 分钟是导入）。唯一有效的解法是让
`lean` 子进程**跨 flush 常驻**。

## 试图做但**回退了**的改动（如实记录）

我花了很多时间尝试把子进程改成常驻（一次导入跑所有批次）。它一路踩到四个坑：

1. 父等"退出码"、子等"停止标记"→ **死锁**（完成判据只能是 `out.json`）；
2. 子进程开工前写的空 `out.json` 被父进程当成本批结果 → 整批响应丢失；
3. 上一批残留的 `batch=true` 被当成"本批已完成" → 需要**批序号**才分得清；
4. 子进程启动可能比父进程写 `jobs.json` 还快 → 读到旧内容，需要"等请求到达"的重试。

修完四个坑之后仍然观察到一批准时返回 `batch=false` 的**未解释**行为。
我没有继续硬啃，而是**把 `Server.lean` 整文件回退到上次提交**（`git checkout`），
只保留本轮**已证实**的那条修复（每实例独立工作目录）。

理由：项目自己写的协作约定是"一次只推进一个可验收单元……验收必须能由命令复现"。
一个我自己都解释不清、又会静默丢响应的服务端改动，不该混进这批整改里——
否则审计指出的"装置坏了却伪装成结果"会再来一遍。

常驻化仍是最值得做的一件事（预计把每题的固定成本从 3.3 次导入降到 1 次，
20 题从 95 分钟压到 15 分钟上下），但它应该是**独立的下一个单元**，
带自己的冒烟、反向对照与"导入次数前后对比"。

## 与前一份审计的交待

审计还提了三条**表述层面**的意见，本轮未改代码，但记在这里以免遗忘：

1. **"自博弈"命名偏强**：本项目的循环不更新模型参数，准确说法是
   "受 SGS 启发的推理期自改进引理库循环"；
2. **子模性保证不适用于真实 pass@k**：它只对"每条引理有冻结覆盖集合、目标函数是并集"
   的代理成立。真实成功率不保证单调、不保证子模、不保证稳定；
   应按消融实验验证相关性，**不主张理论保证**；
3. **引用数 ≠ 因果复用价值**：`reuse(l)` 是"被使用次数"，不是"促成求解次数"，
   需要逐引理消融做 `with − without` 的配对，并审计代理与真实边际增益的相关性
   （`coverage.spearman` 已经备好）。

这三条要落到 `docs/SG-Lean思路文档第二版.tex` 与 `AGENTS.md` 的措辞里——
**改主张属于"重大方向改变"，按约定要先问用户**，所以本轮只记录，不动文档。

## 现状与下一步

**已完成**：审计 P0 六条 + P1 四条 + 缺失测试 + 部分可复现性/并发问题。
`scripts\run_closure_tests.py --no-lean` 43/43 通过；服务端冒烟 PASS；
`lake build SgsLean SgsLean.Test sgslean-server` 423 jobs 通过。

**库的处置**：现有 4 条开发期引理由 `experiments/library.jsonl` 移入
`experiments/library_cold.jsonl`（带 `source_corpus: "D"` 与冷存原因）。
它们的来源目标（`aime_1984_p5` / `aime_1988_p3` / `aime_1990_p2`）**确认落在 D 上**，
按 `docs/data-protocol.md` 第五节必须换掉；新的来源守卫也会直接拒收它们。

**下一步（按性价比）**：

1. **常驻子进程**（独立单元，含前后对比）——不做这个，P2 的每一轮建库都要再付 1.5 小时；
2. 用 `scripts\run_prover_eval.py --set D --k 4 --limit 20` 复跑，确认装置修好后
   的 pass@4 落在什么位置（预测 8–20%），以及 `gate: unknown` 那 6 题是否消失；
3. 准备 C/D/T：C 按 `docs/data-protocol.md` 的三个候选来源定，
   D 用 miniF2F valid、T 用 test（**台账已就绪，只跑一次**）；
4. 再进 P2 的 `calibrate_difficulty` / `reuse_cost_greedy` 接线与库增长实验。
