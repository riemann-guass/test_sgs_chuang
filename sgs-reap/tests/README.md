# tests

- Lean 侧测试放在 `reap-fork/Reap/Test/` 下，跟随上游目录约定。
- Python 侧测试（服务层契约、解析器）放在本目录。

阶段 0 的验收命令：

```powershell
cd sgs-reap\reap-fork
lake build Reap Reap.Test     # 离线，不需要任何服务
```

阶段 1 的端到端联调（需要 Python，会自动启停假服务）：

```powershell
C:\Users\gaosen\anaconda3\python.exe sgs-reap\tests\run_e2e.py
```

它会依次执行 `reap-fork/tests/ConjectureE2E.lean`（真实 HTTP 往返）与
`reap-fork/tests/ConjectureDegrade.lean`（服务不可达时的降级），并校验
`e2e_wall_clock.jsonl` / `e2e_raw_tree.json` 两个可观测性产物。
