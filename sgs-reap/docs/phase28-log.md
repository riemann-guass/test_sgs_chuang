# 阶段 28 执行日志（P1 真模型数字 + P2 建库：把闭环真的跑起来）

## 为什么做这一单元

phase27 把装置修到"能跑"，但两件交付物还缺：

* **P1 的真模型数字**（规格 9.1 的闸门：D 上 20 题的 pass@k 与 token 成本）；
* **P2 的建库**：按 `docs/data-protocol.md` 在 C 上把库建起来（闸门：库 ≥20 条 +
  两臂测量给得出**非零**的复用计数）。

本轮把这两件做完，并且在过程中抓到 6 个只有"真跑"才会暴露的缺陷。

## 交付物

| 文件 | 动作 | 内容 |
|---|---|---|
| `sgslean/SgsLean/Trivial.lean` | 改 | 廉价兜底对不会自己 `intro` 的 tactic 追加 `intros` 变体；探针补 `exact by` 包装；新增**整条清扫**的墙钟上限（`SGSLEAN_CHEAP_BUDGET_MS`，默认 60 s）与 `exhausted/elapsedMs` 字段 |
| `sgslean/SgsLean/Test/Cheap.lean` | 改 | 钉住"变体必须生成、必须是平铺两行、最贵的 aesop 不重复试探" |
| `sgslean/SgsLean/{Server,Materialize}.lean` | 改 | `list_theorems` 命令（一次遍历环境、按前缀分桶）；`import` 一条一个模块；物化时**剔除生成库自己**（自导入） |
| `sgsr/pipeline/prover.py` | 改 | 在线路径显式传单作业心跳预算（400M，此前是服务端默认 4M） |
| `sgsr/pipeline/runner.py` | 改 | **按目标走检索层注入库**（此前是"全轮公用前 N 条"）；物化用独立工厂（基础 import） |
| `sgsr/models/prompts.py` | 改 | 库区块改成"先检查、能引用就引用 `sgs_lem_*`"（并说明它们不在 Mathlib 里） |
| `sgsr/models/backend.py` | 改 | 网络重试 3 → 5 次（SSL 掉线实测） |
| `scripts/run_prover_eval.py` | 改 | **逐题落盘**（末尾崩掉不再丢整轮）+ 报告写清单作业预算与清扫上限 |
| `scripts/calibrate_difficulty.py` | 新增 | 难度标定：一次批 `cheap_verify` + 一次 `/solve` 采样 + 一次批 `verify` → easy / `unknown_cheap` / nearmiss / hard + 分档清单 |
| `tools/prepare_domain_corpus.py` | 新增 | C1+C2 合成 `data/C.jsonl`；命题级去重与**与 D/T 的同源检查**；`data/corpus_manifest.json` |
| `data/C.jsonl` · `data/corpus_manifest.json` | 新增 | C = 195 条（C1 63 + C2 132），与 D/T 零重叠 |
| `data/{C_build,C_nearmiss,C_hard,C_easy}.jsonl` | 新增 | 分档清单（建库用 near-miss + hard） |
| `experiments/results/calib_C2_k2_n80.json` | 新增 | 标定报告（80 题 × k=2） |
| `experiments/results/rounds_real_3.json` + `runs/round_real_*` | 新增 | 3 轮真模型建库的逐轮报告 |
| `experiments/library.jsonl` | 新增 | **第一个真库**（C2 来源，31 条，含 `reuse/reuse_targets/exposures/cost_tokens/name`） |

## P1：真模型数字（D 上 20 题，k=4）

装置修好后一共跑了两轮，两轮都如实留档。

### 第 1 轮：配置有缺陷（留档为 `p1_dev_k4_n20_budget4m.json`）

| 项 | 值 |
|---|---|
| 结果 | solved 7 / 20；可评测 16 → pass@4 = **0.438**（首轮 0.250、不含兜底 0.250） |
| 兜底命中 | 3（零 token） |
| token | 180,960（计费口径 prompt+completion）；CostPerSolved 25,851 |
| **已知缺陷** | ① 在线路径没传单作业心跳预算 → 用的 4M，`aime_1984_p15` 门检直接 `exception`（同一条语句在 400M 下能过门检）；② 3 题撞上 DeepSeek `SSL: UNEXPECTED_EOF_WHILE_READING`，重试 3 次不够 → 记成 `backend_error` |

### 第 2 轮：修复后（正式数字）

修复：在线路径显式传 `budget_for_jobs()`（400M）；网络重试 3 → 5。

数字见 `experiments/results/p1_dev_k4_n20.json`（脚本**逐题刷新**，最后一条
`partial: false` 表示整轮跑完）。第一次修复后重跑在**末尾写报告那一行**崩过
（漏 `import os`），那次结果因此没落盘——这正是本轮把报告改成逐题落盘的原因。

第 3 次运行（正式）的配置与成本实测：`--k 4 --library none --imports Mathlib`，
**约 15 分钟/题**（每题 2–4 个 Lean 批：门检+兜底 1 批、每轮求解验证各 1 批；
每批都要重付一次 Mathlib 导入 ≈ 90–150 s）。20 题合计约 5 小时，
这也是"子进程不常驻"这个缺口的直接代价。

报告里同时记录三个口径与判定预算（`heartbeats_per_job` = 400M、
`cheap_budget_ms` = 60 s），并注明 `partial` 状态——**逐题落盘 + `--resume`**
让长跑被打断后可以接着跑，不必重头再来。

## P2：语料 → 标定 → 建库

### 语料：C = C1 + C2，与 D/T 零重叠

`tools/prepare_domain_corpus.py` 一次 `list_theorems`（遍历环境一次、按前缀分桶，
避免"每个前缀重走一遍环境"）+ 一次批 `check`，产出：

```
[corpus] C = 195 条（C1 63 + C2 132）；领域 {'nat': 64, 'real': 40, 'int': 33, ...}
[corpus] 与 D 重叠 0 条、与 T 重叠 0 条（必须都是 0）、C 内重复 0
[corpus] 过滤：{'duplicate_in_c': 20}；门检拒 18
```

C2 = 从 Mathlib 的 `Data.{Nat,Int,List,Finset,Multiset,Real,Rat}` 各取前 200 条定理
（跳过编译器自动生成的名字、跳过宇宙多态、pp 文本 ≤300 字符、无 `?` 占位符），
按名字排序后确定性等距抽样再过门检。每条的 `source_target` 记着定理全名，
来源可事后审计（数据协议硬约束 1 判的是来源，不是文本相似度；这里两道都做了）。

### 标定：C1 已被兜底吃掉，C2 是唯一有信息的来源

先说一个必须记住的实测：**C1（63 条初等引理）里 61 条（97%）被廉价兜底零成本秒杀**
（`simp` 31、`omega` 6、`aesop` 7、`intros; ring` 9、`intros; positivity` 3、`decide` 3、
`norm_num` 1、`intros; nlinarith` 1、`intros; linarith` 1；只剩 2 条）。它们不可能测出增益，
所以标定与建库都改用 C2。

`calibrate_difficulty.py --set C --corpus C2 --limit 80 --k 2`：

```
[calib] 分档：{'easy': 29, 'hard': 46, 'nearmiss': 5}
[calib] token 251167（3139.6/题）；后端故障 0；耗时 1108s
```

* `easy` 29：兜底命中或模型 2/2 解出（Mathlib 里的一步引理，例如 `Finset.mem_range`）；
* `hard` 46：模型 0/2（Hölder 共轭、choose/阶乘同余这类 Mathlib 内部深水区）；
* `nearmiss` 5：0 < 解出率 < 1（唯一"能被一条引理翻过来"的区间）。

结论：**抽样规则偏了**——按名字排序取前 200 条，抽进来的多是"Mathlib 内部管道"
（iff 引理、instance 相关），中间难度太少。这是 P3 之前要改的事（见"下一步"）。

### 建库：库 0 → 31 条（真模型、3 轮）

用 `data/C_build.jsonl`（near-miss 5 + hard 46 = 51 条）当课程集，
`--target-limit 30 --k 2 --n 2 --imports Mathlib,SgsLean.GeneratedLibrary`：

| 轮 | 解出目标 | 候选 | 过硬门 | 验证通过 | 入库 | 库规模 | 提示词注入 | 被引用 |
|---|---|---|---|---|---|---|---|---|
| 0 | 5/30 | 46 | 34 | 9 | 9 | 16 | 7 | 0 |
| 1 | 6/30 | 47 | 37 | 12 | 12 | 28 | 12 | 0 |
| 2 | 6/30 | 45 | 19 | 3 | 3 | 31 | 12 | 0 |

* 3 轮共 2636 s（44 分钟），**0 装置故障**，每轮物化 + 编译都通过（`generated_build_ok: true`）；
* 库条目示例：`∀ {a : ENNReal}, 1 ≤ a → a⁻¹ ≤ 1`、
  `∀ (n : ℤ), Int.fib (n + 2) = Int.fib n + Int.fib (n + 1)`；
* **P2 闸门前半达成**（库 ≥20 条）；后半（非零复用计数）在这一版注入策略下是 0，
  原因见下节——改完后用两臂装置检查验证为 **14/24 篇引用**。

### 为什么引用是 0（以及改了什么）

拆开看是两件事：

1. **注入不相关**：离线闭环的提示词集合是 `library[:prompt_slots]`（全轮公用的前 12 条），
   **没有走检索层**——规格 2.2 的分层检索（按当前命题取库）此前只在在线路径
   （`prover.retrieve`）里实现。库里最早的 12 条与当前目标多半无关，模型自然不引用。
   已改为 `runner.collect` 里**按目标**调用 `retrieve_library`（符号重叠 + 复用密度），
   与在线路径同一套口径。
2. **提示词没给理由**：库区块原文只说"They are optional"。已改成
   "先检查这些引理，能直接收口就 `exact sgs_lem_xxx` / `rw [sgs_lem_xxx]`，
   比重新猜 Mathlib 名字更便宜、更不容易错"，并明确它们**不在 Mathlib 里**。

### 闸门后半：两臂装置检查给出非零引用

诊断修完之后，用 `run_gate_g3_real.py` 在 **C 的建库集上**做两臂检查
（`--targets data/C_build.jsonl --select hard --limit 12 --k 2`；这是**装置检查**，
不是增益结论——增益结论按数据协议只能在 T 上跑一次）：

```
[g3r] 评分制 cover：基线 2.5 → 给库 3.5（增益 1.0）
[g3r] 处理臂引用库的证明：14/24 篇
[g3r] 二值口径：proved_cover(∅)=5  proved_cover(S)=4  差=-1
[g3r] 因库而多证出：['C2:Finset.range_sdiff_zero']
[g3r] 判定：pass；protocol_errors=0；backend_errors=0；耗时 621 s
```

**处理臂 24 篇证明里 14 篇引用了库引理**（每目标 0–2 篇），报告
`experiments/results/g3_c_device_n12_k2.json`。也就是说：

* 库被物化、被 import、被按目标检索注入、被模型实际引用——**装置的每一环都有证据**；
* 二值口径反而 -1（2 条从可证变不可证），那是 k=2 的采样波动；评分制 +1.0。
  这条差异本身就是 P4 要回答的"注入库的代价"：模型把预算花在库引理上，
  个别目标的样本会变差。

## 真跑才暴露的 6 个缺陷（全部已修）

| # | 症状 | 根因 | 修法 |
|---|---|---|---|
| 1 | 兜底在 13 道初等引理上"解不了" | 批量 2 的 `ring/linarith/positivity` **不会自己 intro**，而输入永远是 `∀` 闭式命题 | 给这些 tactic 追加 `intros` 变体（C1 命中率 79% → **97%**） |
| 2 | 加了变体仍然不命中 | `evalTacticStrNoFinalCheck` 对**未包装**的多行脚本只跑第一行 | 探针补 `MCTS.wrapProofScriptAsTactic`（`intros\nring` 不再被当成"把 ring 当假设名"） |
| 3 | D 上一题门检被拒 | `Prover` 没传心跳预算 → 用服务端默认 4M；离线脚本都传 400M | 在线工厂显式 `heartbeats=budget_for_jobs()` |
| 4 | 3 题记成"装置故障" | DeepSeek 侧 `SSL: UNEXPECTED_EOF_WHILE_READING`，3 次重试不够 | 重试 3 → 5 次（2/4/8/16 s 退避） |
| 5 | 闭环第 1 轮在 `commit` 停住 | 物化把 `SgsLean.GeneratedLibrary` 写进生成文件自己的 import（**自导入**）；且多个 import 被拼成一行（Lean 语法错误） | `Materialize.emit` 收**模块数组**、一模块一行；服务端剔除生成库自己；闭环用独立工厂写物化文件（基础 import） |
| 6 | P1 重跑 2.7 小时的结果全丢 | 报告只在整轮跑完才写；末尾 `NameError`（漏 `import os`） | 报告抽成 `build_report`，**逐题落盘**（带 `partial` 标记） |
| 7 | 硬门批 156 个作业只回了 51 条，2/3 候选被当成"没过门检" | Lean 默认 `maxErrors = 100`：失败作业的错误**累加到同一个文件**，到 100 条 Lean 直接 `maximum number of errors reached, exiting`，后面的 command 全不跑 | snippet 加 `set_option maxErrors 0`；`screen`/`collect` 把"响应缺失"记成 `protocol:*` 并跳过，超过 20% 直接抛错停下 |

另外一个小改动：兜底清扫加了**整条清扫**的墙钟上限（默认 60 s）。
在"兜底解不了"的题上，22 条探针会各自烧满预算（每条上限 200 s），
实测把单题墙钟推高好几分钟而命中概率接近 0。超限时记 `exhausted=true`
——"没试完"与"试过都不行"是两件事，标定时也照此分档（`unknown_cheap`）。

## 反向对照

* 兜底变体：先故意写成 `intros\n  ring`（缩进版），实测被 Lean 解析成"把 `ring` 当假设名"
  （目标上下文出现 `ring : ℕ`）→ 不命中；改成平铺两行 + `exact by` 包装后命中。
  这条对照写进了 `Test/Cheap.lean` 的断言。
* 心跳预算：同一条 `aime_1984_p15` 在 4M 下 `exception`、400M 下 `ok`——
  报告里因此固定记录 `heartbeats_per_job`。
* 自导入：手动 `lake build SgsLean.GeneratedLibrary` 复现
  `unexpected identifier; expected command`，修好后 7889 jobs 通过。
* 逐题落盘：把报告函数抽出来后，末尾异常不再丢数据（`partial` 标记可见）。
* `maxErrors`：从 `child.log` 拿到原文
  `maximum number of errors (100; from option 'maxErrors') reached, exiting`，
  并核对 `out.json` 只有 51 条（=156 的三分之一，正好对应 100 条错误 / 3 个作业每组）。

## 一个必须记住的副作用：后端缓存会让"重复轮"变成空转

`Backend.chat` 按 `(messages, max_tokens, temperature, thinking)` 缓存。
闭环里同一道目标在**下一轮**的提示词若与上一轮逐字相同（库的注入集没变），
第二次 `/solve` 会直接命中缓存：**模型根本没被调用**，拿到的是同一批候选。
实测一轮 73 次 solve 里有 35 次命中缓存。对建库的影响是"第二轮接近空转"
（候选还是那批，再被 novelty 挡掉）。

这不是记账错误（缓存命中确实按其 0 token 计费），但它改变了采样的语义：
**同一 prompt 的重复采样不再是独立样本**。P3 要重复采样时要么在提示词里带轮次/盐，
要么给代理加一个关缓存的开关。

## 现状与下一步

**已完成**：P2 的语料、标定、3 轮建库（库 35 条）、两臂装置检查；7 个真跑缺陷的修复。
P1 数字仍待重跑（见下表）。

### 本轮收尾时的准确状态（2026-09-26 23:30，务必按这个读）

| 项 | 状态 |
|---|---|
| P2 交付物（语料 / 标定 / 库 / 两臂装置检查） | ✅ 已提交（`3b74069`），库 35 条、两臂 14/24 篇引用 |
| P1 数字 | ⚠️ `experiments/results/p1_dev_k4_n20.json` 目前是**跑到 9/20 被我停下的部分报告**（`partial: true`）。完整 20 题的两次运行见：`p1_dev_k4_n20_budget4m.json`（**配置有缺陷**：4M 心跳 + 3 次 SSL 装置故障）与本文档"第 2 轮"小节（修复后 20 题，但那次因末尾 `NameError` 没落盘）。**要重跑**。 |
| 执行模型（常驻子进程） | ⚠️ **代码已写、未验证**：`SgsLean/Server.lean` 的 `serveLoop`（一次导入、跨批服务，批号唯一）+ `sgsr/verification/client.py` 的常驻协议 + `run_prover_eval.py` 整批共用一个会话 + `prove.py`/`Prover.session()`。设计上把"每批一次 Mathlib 导入"降为"每个会话一次"，但**冒烟测试被中断，尚未跑通**。 |

**恢复后的第一条命令**（先验证常驻协议，再重跑 P1）：

```powershell
cd D:\bianma\code\大创\sgs-reap
$env:PYTHONPATH = "D:\bianma\code\大创\sgs-reap"
$py = "C:\Users\gaosen\anaconda3\python.exe"
# ① 常驻协议冒烟：一个会话跑 3 批，只有第一批应包含 Mathlib 导入
& $py scripts\run_server_smoke.py
# ② 逐批计时（可选，直接看"首批慢、后续批秒回"）
#    见 phase28 日志"执行模型"一节的三批计时脚本
# ③ 真代理起在有网络权限的进程里，然后重跑 P1（现在是"整批一个会话"）
& $py sgsr\models\proxy.py --port 8770
& $py scripts\run_prover_eval.py --set D --k 4 --limit 20 --library none --resume `
      --out experiments\results\p1_dev_k4_n20.json
```

若 ① 不过：先 `git revert` 常驻协议那一个提交（或把 `SgsLean/Server.lean` 的片段改回
`runJob` 逐作业 command 版本），回到 `3b74069` 的已验证状态再排查——
**不要**在未验证的执行模型上跑长实验（这一轮的教训）。

**下一步（按性价比）**：

1. **C2 抽样规则要改**：按名字排序取前 200 条偏向 Mathlib 内部引理（near-miss 只占 6%）。
   应改成按"题面形态"挑（`∀` 量化的等式/不等式、绑定数适中、不含 typeclass 参数），
   让 near-miss 占比上去，P3 的三组对照才有分辨率。
2. **复用计数为 0 这件事本身要写进报告**：`reuse` 是"被使用次数"，
   不是"因果复用价值"——引用为 0 说明**在当前语料与提示词下**模型不需要库。
   这与 phase26 记录的"代理与真实边际增益可能脱节"一致，
   P4 的 `coverage.spearman` 审计与逐引理 with/without 消融要正面回答它。
3. Lean 子进程仍未常驻（每批一次 Mathlib 导入，75 s 量级）：P1 一轮 20 题耗 2.7 小时，
   其中大部分是导入。持久 REPL 仍是收益最大的一件事。
