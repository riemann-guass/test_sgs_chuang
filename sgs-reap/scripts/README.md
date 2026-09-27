# 命令行入口

这里保留五个公开入口。业务逻辑位于 `sgsr/`，脚本只负责参数、流程编排和报告落盘。

| 脚本 | 作用 | 是否会调用模型 |
|---|---|---|
| `prove.py` | 证明一条命题 | 是 |
| `build_library.py` | 建立、试用并发布引理库 | 是 |
| `evaluate.py` | 批量计算正确率、成本和失败类型 | 是 |
| `compare_library.py` | 对同一批题比较无库与有库 | 是 |
| `check_project.py` | 检查数据、接口、库规则和 Lean 服务 | 否 |

新能力优先作为现有入口的参数或子命令实现，不再增加新的顶层脚本。

## 环境

```powershell
cd sgs-reap
$env:PYTHONPATH = (Get-Location).Path
```

真实模型代理需要把 `sgsr/models/.env.example` 复制为不入库的 `.env` 并填写配置：

```powershell
python -m sgsr.models.proxy --port 8770
```

离线流程检查可以使用确定性假服务：

```powershell
python -m sgsr.models.mock_server --port 8765
```

## 常用命令

```powershell
# 不调用 Lean 的快速检查
python scripts\check_project.py --no-lean

# 加入 Lean 常驻服务和非平凡性检查
$env:ELAN_HOME = "$env:USERPROFILE\.elan"
python scripts\check_project.py --skip-materialize

# 单题证明
python scripts\prove.py --statement "∀ (n : Nat), n + 0 = n" --k 4

# 假服务下跑两个极小建库轮，仅检查流程
python scripts\build_library.py --rounds 2 --target-limit 2 `
  --measure-target-limit 2 --k 1 --n 2 --imports none --expect-mock

# 开发集评测
python scripts\evaluate.py --set D --k 4 --library none `
  --out experiments\results\dev_evaluation.json

# 正式库建立后比较有库与无库
python scripts\compare_library.py --targets data\minif2f_dev.jsonl --k 4

# 只重新生成和编译当前已发布引理
python scripts\build_library.py --materialize-only
```

最终测试集 T 需要 `evaluate.py` 的显式一次性确认参数，并有运行台账保护。不要把测试集用于
调参、流程试跑或建库。

## 共享实现

- `sgsr/client.py`：HTTP 请求、模型后端、缓存、重试和成本记录；
- `sgsr/lean.py`：常驻 Lean 验证服务；
- `sgsr/pipeline/prover.py`：生成并验证证明；
- `sgsr/pipeline/selection.py`：相关引理检索；
- `sgsr/pipeline/library.py`：引理状态和快照；
- `sgsr/pipeline/runner.py`：离线建库流程。
