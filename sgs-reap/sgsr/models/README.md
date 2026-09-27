# sgsr/models —— 模型服务层

对应上游 SGS 的 `sgs/models/`，但这里**只接外部 API**（不本地部署权重）。

| 文件 | 作用 |
|---|---|
| `../client.py` | 配置、OpenAI 兼容客户端、HTTP、缓存、重试和成本记账的公共入口 |
| `prompts.py` | 三个端点的提示词与解析器：`conjecture_prompt` / `solve_prompt` / `guide_prompt` |
| `proxy.py` | HTTP 服务：`POST /conjecture`、`POST /solve`、`POST /guide`（Guide 是 A 组对照） |
| `mock_server.py` | 确定性假服务，离线测试用（`--mode normal/noisy/empty`） |

```powershell
# 真代理（需要网络权限 + .env 里的 key）
python sgsr\models\proxy.py --port 8770
# 假服务（离线）
python sgsr\models\mock_server.py --port 8765
```

目标契约见 `../../docs/api-contract.md`。v2 要求 `/solve` 显式携带冻结快照哈希、相关 active 引理、
`measurement|product` 提示模式和重复采样盐；代码尚待迁移。服务端不判断证明真伪，
`sorry`、未闭合目标和错误常量都交给 Lean 终检。
