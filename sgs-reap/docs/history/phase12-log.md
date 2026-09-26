# 阶段 12 执行日志（P3：软分第一件——压缩收益 Δlen）

概念前身是 Colton 等人 2000 年的 compression-interestingness："好的引理让证明变短"。
本项目把它做成**可计算的软分**：同一条语句，比较"不借助目标引理的证明（long）"与
"借助之后的证明（short）"。

## 交付物

| 文件 | 作用 |
|---|---|
| `sgslean/SgsLean/Measure/Compression.lean` | `Measure.compression (stmt) (longProof) (shortProof)`：给出步数差与字符数差 |
| `sgslean/SgsLean/Test/Compression.lean` | 离线测试：步数/字符数差必须**恰好**等于真实差值；交换两条证明 Δ 必须变号 |
| `sgslean/SgsLean/Server.lean`（改） | 协议新增 `compression` 命令（Python 侧做 G3 时会用） |

## 两个 v1 度量（都从真实 elaboration 取，不估算）

1. **步数**：复用 `Trace.traceScript` 记录到子目标清零为止的 tactic 步数；顺带给出
   `longOk` / `shortOk`——**两条证明都必须真的通过**，否则 Δlen 没有意义；
2. **字符数**：证明脚本的字符数（token 数的廉价代理；中文环境里比分词器更稳）。

`Δlen = long − short`，正数表示"用了引理之后变短"。

## 离线测试（无 Mathlib）

```powershell
cd sgs-reap\sgslean
lake build SgsLean SgsLean.Test sgslean-server   # Build completed successfully (417 jobs).
```

用例：语句 `∀ (n : Nat), n + 0 = n`

| | 证明 | 步数 | 通过 |
|---|---|---|---|
| long | `intro n` / `have h : n + 0 = n := rfl` / `exact h` | 3 | ✓ |
| short | `intro n` / `rfl` | 2 | ✓ |

断言：`longSteps=3`、`shortSteps=2`、`deltaSteps=1`、`deltaChars>0`；
**交换两条后 `deltaSteps` 必须变号（= −1）**——这条让"Δlen 方向"也被钉住。

## 反向对照

把 `compression` 里的长证明换成短证明（模拟"Δlen 算了个常 0"）后重建 `SgsLean.Test`：

```
error: SgsLean/Test/Compression.lean:18:2: 步数应为 3 / 2，实际 2 / 2
error: Lean exited with code 1
```

还原后重建 `417 jobs` 全绿。（这轮同样确认了 target 名是 `SgsLean.Test`。）

## 局限（v1，写清楚）

* 步数用"逐行 tactic"计数，`Trace` 的 v1 局限同样适用（bullet / 多行结构会被切错）；
* 字符数不是 token 数；真正的 token 数要等接上分词器（属 P5 成本核算）；
* 这里只是**单题局部**收益。`cover(S)` 那个"并集收益"（子模性、贪心 (1−1/e)）在
  `graph/coverage.py` 里算，是闸门 G3 的对象。

## 现在在哪 / 下一步

硬门三件（非平凡 / 新颖 / 可证）+ 软分第一件（Δlen）都落地了。P3 剩：

1. `SgsLean/Measure/Dependency.lean`：从证明里抽常量、判断"最终证明是否真的依赖该引理"；
2. `Verify` 增 `materialize`：把已验证引理落成**有名常量**（依赖与压缩可测的前提）；
3. `graph/library.py` + `graph/coverage.py`：`cover(S)` 与子模贪心 → 闸门 **G3**
   （cover 有没有便宜且仍具子模性的代理）。
