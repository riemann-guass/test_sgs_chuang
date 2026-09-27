# Lean 验证层

这里是 LeanReuse 的形式化裁判和引理库生成器。它负责：

- 检查输入文本是否是合法命题；
- 拒绝固定简单策略可以直接证明的平凡候选；
- 验证整篇证明并让 Lean 内核终检；
- 从通过验证的证明项中提取实际引用的常量；
- 把已发布引理写成可导入的 Lean 模块；
- 在一个常驻进程中批量处理请求，避免反复导入 Mathlib。

它不负责判断自然语言质量，也不把失败证明中的文本当作引理使用证据。

| 接口 | 含义 |
|---|---|
| `Gate.check` | 文本能否成为 Lean 命题 |
| `Trivial.isTrivial` | 固定简单策略是否可直接闭合 |
| `Verify.verify` | 完整证明是否通过 |
| `Trace.traceScript` | 通过证明使用了哪些常量 |
| `Materialize.emit` | 生成已发布引理模块 |

## 构建

项目固定保留 `reap-fork/.lake`。首次配置时可复用其中已经下载的依赖：

```powershell
robocopy ..\reap-fork\.lake\packages .lake\packages /E /NFL /NDL /NJH /NJS /NP
lake build SgsLean SgsLean.Test sgslean-server
lake build SgsLean.GeneratedLibrary
```

日常建议运行统一入口 `python scripts/check_project.py --skip-materialize`，而不是分别调用内部测试。
