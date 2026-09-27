# data/：三块数据，角色不可混

角色与纪律见 `docs/data-protocol.md`；这里是文件清单。

| 文件 | 角色 | 谁可以读 | 怎么重建 |
|---|---|---|---|
| `raw/` | 原始语料 | 转换器 | 见 `docs/history/phase16-log.md` 的克隆命令（不入库） |
| `lemmas_g1.jsonl` | **C1** 自建初等引理集 | 建库全流程 | 手写（63 条） |
| `minif2f_valid.jsonl` | **D** 开发集 | 调参、消融、看方向 | `tools/minif2f_to_jsonl.py`（冻结后不许改） |
| `minif2f_test.jsonl` | **T** 测试集 | **只在框架冻结后跑一次** | 同上（冻结后不许改） |
| `C.jsonl` | **C** 课程集（C1 + C2） | 建库全流程 | `tools/prepare_domain_corpus.py` |
| `C_build.jsonl` | 旧建库子集（near-miss + hard） | 迁移参考 | 正式三态库前将由稳定哈希重建 |
| `corpus_manifest.json` | C 的来源与同源检查记录 | 审计 | `tools/prepare_domain_corpus.py` 一并写出 |
| `workload.jsonl` | 早期工作负载 W（定题用） | 调参 | 手写，已被 D/C 取代 |

## 派生文件不入库

难度分档清单（`*__easy.jsonl` / `*__nearmiss.jsonl` / `*__hard.jsonl`）**随时可由一条命令重建**，
因此不提交：

```powershell
& $py scripts\run_prover_eval.py --set C --path data\C.jsonl --k 2 --tier-out data `
      --out experiments\results\calib_C_k2.json
```

分档口径写在 `run_prover_eval.classify_tier` 的 docstring 里：`easy` = 兜底命中或首轮 k 篇全过；
`nearmiss` = 首轮 0 < 通过 < k（**唯一有增益信号的档**）；`hard` = 首轮一篇都没过。

正式建库还要从 C 稳定派生 `C-build`、`C-measure`（可选 `C-audit`）。划分按内容哈希和
固定种子确定，不手工挑题；清单不入库，算法、种子、规模和指纹写入 manifest。
候选的 `source_target` 不得为自己的 reuse 贡献计数。

## 格式与约束

```json
{"id": "w01", "domain": "nat", "statement": "∀ (n : Nat), n + 0 = n", "note": "..."}
```

`statement` 必须是**闭式**的（只引用全局常量，或用 `∀`/`→` 自己引入变量）：
`SgsLean/Server.lean` 的协议不支持局部上下文透传。

历史遗留（2026-09-26 之前）的 `heldout.jsonl` 与 `workload_mathlib.jsonl` 已被删除：
前者是被 `docs/data-protocol.md` 的三块数据划分取代的旧划分，后者是 Mathlib 工程就绪前的
占位工作负载，两者在代码里**引用次数为 0**。
