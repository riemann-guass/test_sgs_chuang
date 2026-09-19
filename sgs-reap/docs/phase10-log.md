# 阶段 10 执行日志（P3：硬门第一件——非平凡性）

硬门第一件是**非平凡**：替代 SGS rubric 里那句"如果平凡就给低分"，由 Lean 直接判，
不靠 LLM 猜。判据是"`decide` / `simp` / `aesop` 能不能在预算内**秒杀**这条语句"。

## 交付物

| 文件 | 作用 |
|---|---|
| `sgslean/SgsLean/Trivial.lean` | `Trivial.isTrivial (stmt) : TacticM TrivialResult`；三条探针按便宜→贵顺序试，任一闭合即判平凡 |
| `sgslean/SgsLean/Test/Trivial.lean` | 离线测试（10 条断言，跑在无 Mathlib 环境） |
| `sgslean/SgsLean/Server.lean`（改） | 协议新增 `trivial` 命令（便于批量标定） |
| `sgslean/SgsLean.lean`（改） | 挂载 `SgsLean.Trivial` |

实现要点：造孤立义务目标（与 `Gate`/`Verify` 同源），每条探针都在 `withoutModifyingState` 里跑；
心跳预算 `Trivial.defaultHeartbeats = 200000`（千次），比 `Verify` 的默认预算紧一档——
"秒杀"本来就该便宜，墙钟由 `reap.timeout` 兜底。`tried` 字段如实记录每条探针的结果，
便于标定与诊断（不是只回一个布尔）。

## 标定记录（Mathlib 模式，8 条探针的真实输出）

```powershell
cd sgs-reap\sgslean
# 一次性把 8 条语句喂给 server 的 trivial 命令
```

| 语句 | 判定 | 秒掉它的 tactic | 备注 |
|---|---|---|---|
| `True` | 平凡 | `decide` | 符合预期 |
| `1 = 1` | **非平凡** | — | **反直觉**：`decide`/`simp`/`aesop` 都没闭合它 |
| `∀ (n : Nat), n + 0 = n` | 平凡 | `simp` | 加零是 simp 引理 |
| `∀ (a b : Nat), a + b = b + a` | **非平凡** | — | **反直觉**：`Nat.add_comm` 没被 `simp` 用上 |
| `∀ (n : Nat), 2 ∣ n ^ 2 + n` | 非平凡 | — | 期望行为（需要真引理） |
| `∀ (n : Nat), n ≤ n * n + 1` | 非平凡 | — | 期望行为 |
| `NotARealType` | 非平凡（`detail` 为空） | — | **autoImplicit 把它当成自由变量**，所以连 elaborate 都没失败；拦它的是 `Gate`，不是这里 |
| `Nat` | 非平凡 | — | 类型不是命题，三条探针都不可能闭合 |

成本：8 条语句一个 Mathlib 批 **176 s**（固定导入成本为主）。

> 两条"反直觉"结果是**判据偏保守**的证据：`1 = 1` 与交换律明明可证，却因为三条 tactic 的
> 具体能力而没被秒掉。这对我们**是安全方向**（宁可漏判平凡、也不要错杀），但要记住：
> 「非平凡」在本项目里的定义就是**相对这三条探针的**，不是"绝对不难"。
> 以后若要加 `norm_num` / `omega` 之类探针，等于**改定义**，必须重跑标定并更新本表。

## 离线测试与反向对照

`SgsLean/Test/Trivial.lean` 三条 example、10 条断言，跑在 lake 的构建环境（无 Mathlib），
因此只断言**跨模式稳健**的部分：`True` 与加零判平凡；`2 ∣ n^2+n`、`n ≤ n*n+1`、`Nat` 判非平凡
且 `tried` 恰好记录 3 条探针；`∀ (P : Prop), P` 不可能平凡。

```powershell
cd sgs-reap\sgslean
lake build SgsLean SgsLean.Test sgslean-server   # Build completed successfully (411 jobs).
```

**反向对照**：把 `Trivial.probes` 改成空数组（一条探针都不跑）后重建：

```
error: SgsLean/Test/Trivial.lean:41:2: 「True」应判平凡，实际 trivial=false，tried=[]
error: SgsLean/Test/Trivial.lean:48:2: 「∀ (n : Nat), 2 ∣ n ^ 2 + n」应记录 3 条探针，实际 0：[]
error: Lean exited with code 1
```

随后还原 `probes`，重建恢复 411 jobs 全绿。

> **教训（差点让反向对照假通过）**：我第一次用的是 `lake build SgsLean Test`，而测试库的名字是
> **`SgsLean.Test`**——`Test` 并不是那个 target，结果"构建通过"了、反向对照看起来也通过了。
> 改成 `lake build SgsLean.Test` 之后反向对照才如期失败。**用一个不存在的 target 名做反向对照，
> 等于什么都没验**；以后反向对照必须先确认它编译的确实是被改的那个模块。

## 现在在哪 / 下一步

硬门三件里第一件（非平凡）落地并有测试与反向对照。下一步：

1. `SgsLean/Novelty.lean`：**新颖**——不 α-等价于已有引理/目标 + 库检索未命中（硬门第二件）；
2. `SgsLean/Measure/{Compression,Dependency}.lean` + `Verify.materialize`：Δlen 与依赖抽取；
3. `graph/library.py` + `graph/coverage.py`：`cover(S)` 与子模贪心 → 闸门 **G3**。
