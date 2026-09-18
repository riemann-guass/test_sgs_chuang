/-
# `ℕ` 记法补丁（**只在没有 Mathlib 的模式下导入**）

Mathlib 自带 `Mathlib.Data.Nat.Notation:termℕ`。Lean **不允许**同一个记法被声明两次——
重复声明是硬错误：

```
error: import SgsLean.Basic failed, environment already contains 'termℕ' from Mathlib.Data.Nat.Notation
```

所以这一行必须与 Mathlib 的 import 互斥，故单独成模块：

* `SgsLean/Server.lean` 的片段在**未**导入 Mathlib 时会自动补 `import SgsLean.Syntax`；
* 离线测试 `SgsLean/Test/*.lean` 显式导入本模块；
* 导入 Mathlib 的模式下**不要**导入本模块（`ℕ` 由 Mathlib 提供）。

它存在的原因（阶段 2 的实测）：本项目环境不含 Mathlib 时 `ℕ` 不在记法表里，而 Lean 的
`autoImplicit` 会把它当成隐式绑定变量，把「记法缺失」伪装成 `failed to synthesize HMul ℕ ℕ ?m`
之类的实例错误——模型输出里的 `ℕ` 级语句会被误判成数学错误。
-/
import SgsLean.Basic

notation "ℕ" => Nat
