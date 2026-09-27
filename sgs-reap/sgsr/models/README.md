# 模型服务

模型服务把外部语言模型包装成三个简单接口：

- `/solve`：为完整命题生成证明脚本；
- `/conjecture`：为未解目标提出候选辅助引理；
- `/guide`：给候选辅助引理评分，仅用于后续研究对照。

服务只生成和整理文本，不能宣布证明正确。所有证明仍必须交给 Lean 验证。

| 文件 | 作用 |
|---|---|
| `../client.py` | 后端配置、HTTP、缓存、重试和 token 记账 |
| `prompts.py` | 提示词和响应解析 |
| `proxy.py` | 真实模型代理 |
| `mock_server.py` | 不联网的确定性假服务 |

```powershell
$env:PYTHONPATH = (Get-Location).Path
python -m sgsr.models.proxy --port 8770
python -m sgsr.models.mock_server --port 8765
```

真实代理从不入库的 `sgsr/models/.env` 读取密钥。字段定义见 `../../docs/api-contract.md`。
