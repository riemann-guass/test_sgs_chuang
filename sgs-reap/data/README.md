# data/：工作负载与 held-out

| 文件 | 内容 | 用途 |
|---|---|---|
| `workload.jsonl` | 工作负载 W（本轮要攻的目标） | P1.3 (G1 闸门)、P2 的轨迹采集、P3 的覆盖度 `cover(S)` |
| `heldout.jsonl` | held-out 评测集 | **只**用于 P4 的 pass@k 对照；任何轮次的库构建/需求挖掘都不许读它 |

格式（一行一条）：

```json
{"id": "w01", "domain": "nat", "statement": "∀ (n : Nat), n + 0 = n", "note": "..."}
```

`statement` 必须是**闭式**的（只引用全局常量，或用 `∀`/`→` 自己引入变量），
因为 `SgsLean/Server.lean` 的协议 v1 不支持局部上下文透传（见 `docs/implementation-blueprint.md`）。

`w09`/`w10` 是**故意**放进去的不可解目标：它们让 solve_rate 分布不是恒 1，
G1 闸门要看的正是这种分布。

当前这批是**流水线标定用**的初等目标（无 Mathlib 依赖），不代表领域难度；
Mathlib 工程就绪后（P1.2 剩余部分）会替换为 Mathlib 级的引理。
