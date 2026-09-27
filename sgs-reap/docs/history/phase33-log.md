# 阶段 33 执行日志（修复非平凡硬门）

## 问题

旧 `Trivial.isTrivial` 只尝试 `decide/simp/aesop`，没有尝试最便宜也最直接的 `rfl`。
因此 `1 = 1`、`a = a` 一类定义等式可能被错当成“非平凡”，污染 probation。

## 修复

- 唯一 Lean 实现的探针顺序改为 `rfl/decide/simp/aesop`，命中即停。
- Lean 单文件测试明确断言 `1 = 1` 的 `byTactic` 必须是 `rfl`。
- 闭包测试通过常驻服务再走一次真实 `trivial` 协议，防止只改源文件却没进执行路径。
- `∀ (n : Nat), 2 ∣ n ^ 2 + n` 等边界例仍需跑完四条探针且判为非平凡。

## 验收

```text
lake build SgsLean.Trivial                         PASS（13 s，本地增量）
lake env lean SgsLean/Test/Trivial.lean            PASS
python scripts/run_closure_tests.py --skip-materialize
                                                   PASS（95 条断言）
```

第一次直接运行 Lean 测试时读到了旧 `.olean`；显式增量构建 `SgsLean.Trivial` 后重跑通过。
第一次闭包测试因当前 shell 未传 `ELAN_HOME` 而在启动前失败；补入本机现有工具链环境后通过，
该失败与代码判定无关。

本阶段没有调用模型、没有物化往返、没有运行 P3/T。

## 下一步

重整 C 的 near-miss 题面并重建正式库；在此之前旧 35 条开发库仍只能用于迁移诊断。
