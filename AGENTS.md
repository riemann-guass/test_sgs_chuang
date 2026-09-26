# AGENTS.md —— 项目记忆与约束

> **实现规格**：`docs/SG-Lean思路文档第二版.pdf`（源码 `.tex`），第 3–5 节是步骤规格、
> 第 9 节是阶段计划。**数据角色**：`sgs-reap/docs/data-protocol.md`。
> **还生效的坑**：`sgs-reap/docs/pitfalls.md`。**接口字段**：`sgs-reap/docs/api-contract.md`。
> 其余历史材料在 `sgs-reap/docs/history/`。冲突时以规格与本文件为准。

## 一、目标与评判指标

**做一个证明器**：输入一条 Lean 命题，输出一段**通过内核终检**的 tactic 脚本。
未通过验证的候选证明不出现在输出里。

1. **正确率**（主指标）：冻结测试集上的 pass@1 / pass@k，**目标级**统计。
2. **成本**：`CostPerSolved` / `CostPerLemma` / **`CostPerReusable`**（建库总 token ÷ 被至少
   两个不同目标引用过的引理数）。
3. **轻量化**（硬约束，不是优化对象）：0 可训练参数、0 GPU、单次实验 ≤12 h、API ≤500 元、
   一条命令可复现。违反即不合格。

**裁决**：正确率有显著差异取高者；置信区间重叠取 `CostPerSolved` 低者；成本接近取实现更简单者；
违反轻量化硬约束者直接淘汰。

**不做**：不训练/不微调模型；不训练检索器；不用 miniF2F 作主基准；不重造库的演化与管理
（沿用 DreamProver 的方法、只换选择判据）；不做自然语言到 Lean 的自动形式化。

## 二、唯一的方法性改动

```
reuse(l) = l 被多少个【不同目标】的【通过验收的】证明实际引用
cost(l)  = l 进入提示词的 token 数
score(l) = reuse(l) / cost(l)
```

准入 / 排序 / 淘汰三件事都用它，但**用法必须分开**：新引理按构造 `reuse=0`，所以
**不能**拿"没复用证据"来拒它（走探索额度），也不能拿"超龄即杀"来淘汰（要求 `exposures>0`）。
实现只有一处：`sgsr/pipeline/selection.py`。三个必须照做：按**不同目标**计数、只统计
**通过验收**的证明、每次实验前做**环境预检**。

三个角色与两个尺度：在线只有求解者（`sgsr/pipeline/prover.py`），出题者与评审者只存在于
离线闭环（`sgsr/pipeline/runner.py`）；两个尺度只通过**库**这一个接口相连。

## 三、数据角色（不可混）

| 数据 | 角色 | 谁可以读 | 禁止 |
|---|---|---|---|
| **C** 课程集（`data/C.jsonl`） | 建库：需求 → 猜想 → 硬门 → 求解 → 入库 | 全流程 | — |
| **D** 开发集（`data/minif2f_valid.jsonl`） | 调参、debug、看方向 | 测量与消融 | 进库；进需求挖掘；当候选来源 |
| **T** 测试集（`data/minif2f_test.jsonl`） | 最终评测 | **框架冻结后只跑一次** | 任何调参/建库/需求/"看看效果" |

库里**绝不允许**出现 `source_target` 落在 D 或 T 上的引理（判来源，不判文本相似度）。
`run_round.py` 有硬守卫（文件名 + 内容指纹），`run_prover_eval.py` 有 T 的一次性台账。

## 四、硬约束

1. **数据角色不可混**（见上表）。
2. **绝不输出未验证的证明**：必须过 `Verify.verify` 的内核终检（`checkProof`）。
3. **不提交密钥**：`sgsr/models/.env` 不入库。
4. **`/guide`、GuideClient、rubric 保留**——它们是对照组 A（SGS 原样）。
5. **不做梯度训练**；可训练参数恒为 0。
6. **报告文件名带 mode 与规模后缀**，正式数字必须能由 `experiments/results/` 回溯。
7. **库版本冻结**：评测时库不再更新；在线现场生成的临时引理不进全局库。
8. **`reap-fork/.lake` 不删**。
9. **一个概念只有一条实现路径**：在线求解、批量评测、建库、语料准备、选材各一条。
10. **不新开入口**：新能力做成现有入口的子命令；派生数据（分档清单、manifest）不入库。

## 五、当前状态（2026-09-27）

**已具备**：门检 `Gate` / 内核终检 `Verify` / 非平凡 `Trivial`（含廉价兜底）/ 新颖 `Novelty` /
轨迹 `Trace`（含 `constants`，复用测量的唯一来源）/ 物化 `Materialize`；在线九步证明器
（`prover` + `selection` + repair）；离线十步闭环（`runner`）；两臂测量；难度分档；
库 `experiments/library.jsonl` **35 条**（C2 来源，内容哈希命名）；唯一测试入口 **70 条断言**。

**执行模型（2026-09-27 定稿）**：`lake exe sgslean-server` 起**一个**常驻 `lean` 子进程，
跨批服务（`request.json` / `out.<n>.json` / `stop.flag`）。实测 Mathlib 首批 577 s、
第二批 0.1 s。旧的 stdin/`jobs.json` 逐批 spawn 路径**已删除**。

**未完成**：

1. **P1 正式数字**：`experiments/results/p1_dev_k4_n20.json` 目前是 `partial: true` 的部分报告
   （9/20 可评测、解出 2、token 66,157）。**要重跑**：`run_prover_eval --resume`（现在整批一个会话）。
2. **P3 三组对照**：A（SGS 原样，LLM 打分）／B（复用判据＋需求）／C（随机伪需求）。
   前置：C2 抽样要按"题面形态"重挑（现在 near-miss 只占 6%）；重复采样要带轮次盐
   （代理缓存会让重复轮空转，见 `docs/pitfalls.md` 第八条）。
3. **P4 成本与复用分析**（论文主图）、**P5 写作**。
4. **`reuse` 忠实度未审计**：活动库里 35 条的 `reuse` **全是 0**（两臂装置检查里处理臂
   14/24 篇确实引用了库引理）。"引用计数"与"真实边际增益"的相关性要等 P3/P4 的
   `coverage.spearman` 与逐引理 with/without 消融。
5. **三条表述待用户批准才能改**（属重大方向改变）：`自博弈`命名偏强；子模性保证不适用于
   真实 pass@k；`reuse` 是"被使用次数"而非因果复用价值。

## 六、常用命令

```powershell
$py = "C:\Users\gaosen\anaconda3\python.exe"        # Python 3.12.4；lake 由 elan 提供
$env:PYTHONPATH = "D:\bianma\code\大创\sgs-reap"     # 直接跑脚本时必须设

# Lean 侧
cd D:\bianma\code\大创\sgs-reap\sgslean
lake build SgsLean SgsLean.Test sgslean-server
lake build SgsLean.GeneratedLibrary     # 库目标不编这个模块，必须点名模块目标

# 自检（唯一测试入口）
cd D:\bianma\code\大创\sgs-reap
& $py scripts\run_closure_tests.py --no-lean            # 纯 Python，秒级
& $py scripts\run_closure_tests.py --skip-materialize   # 加常驻会话冒烟，约 20 s

# 离线闭环冒烟（假服务，无 Mathlib）
& $py sgsr\models\mock_server.py --port 8765
& $py scripts\run_round.py --rounds 2 --target-limit 2 --k 1 --n 2 --imports none --expect-mock

# 真模型（另开终端；密钥在 sgsr\models\.env）
& $py sgsr\models\proxy.py --port 8770
& $py scripts\prove.py --statement "∀ (a b : Nat), a + b = b + a" --k 4
& $py scripts\run_prover_eval.py --set D --k 4 --limit 20 --library none --resume `
      --out experiments\results\p1_dev_k4_n20.json

# P2 语料与建库（真模型）
& $py tools\prepare_domain_corpus.py --c2-limit 200 --sample 150 `
      --out data\C.jsonl --manifest data\corpus_manifest.json
& $py scripts\run_round.py --rounds 3 --curriculum data\C_build.jsonl --target-limit 30 `
      --k 2 --n 2 --imports Mathlib,SgsLean.GeneratedLibrary --library-budget 80 `
      --source-corpus C2 --solve-endpoint http://127.0.0.1:8770/solve `
      --conjecture-endpoint http://127.0.0.1:8770/conjecture

# 编译项目文档（需 xelatex，连编两遍）
cd D:\bianma\code\大创\docs
xelatex "SG-Lean思路文档第二版.tex"
```

`/health` 正常但 `/solve` 返回 503 → 代理起在没有网络权限的进程里。

## 七、协作约定

* **一次只推进一个可验收单元**，六步走：状态检查 → 写码 → 跑 → **反向对照** → 记日志 → 提交。
  "反向对照"指故意把断言写错、确认测试真的失败，防止装置空转。
* 每个阶段写 `sgs-reap/docs/history/phase<N>-log.md`：为什么做 / 交付物 / 真实输出 / 坑 / 下一步。
  验收必须能由命令复现。
* 提交信息：`feat(phaseN): …` / `fix(phaseN): …` / `docs: …`；分支前缀 `codex/`。
* 实测推翻假设时**同时改文档与代码**，不要让文档与实际相反。
* **重大方向改变**（换主线、放弃主张、动数据协议、改指标优先级）**先问用户**。
