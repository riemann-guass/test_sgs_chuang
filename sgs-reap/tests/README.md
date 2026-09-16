# tests

- Lean 侧测试放在 `reap-fork/Reap/Test/` 下，跟随上游目录约定。
- Python 侧测试（服务层契约、解析器）放在本目录。

阶段 0 的验收命令：

```powershell
cd sgs-reap\reap-fork
lake build Reap Reap.Test
```
