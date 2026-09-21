# sgsr/models —— 模型服务层

对应上游 SGS 的 `sgs/models/`，但这里**只接外部 API**（不本地部署权重）。

| 文件 | 作用 |
|---|---|
| `config.py` | 读同目录 `.env`（`DEEPSEEK_API_KEY` / `BASE_URL` / `MODEL`） |
| `backend.py` | OpenAI 兼容客户端：缓存、重试、token/延迟记账、`thinking` 开关 |
| `prompts.py` | 三个端点的提示词与解析器：`conjecture_prompt` / `solve_prompt` / `guide_prompt` |
| `proxy.py` | HTTP 服务：`POST /conjecture`、`POST /solve`、`POST /guide`（**Guide 是 H2 的对照组**） |
| `mock_server.py` | 确定性假服务，离线测试用（`--mode normal/noisy/empty`） |

```powershell
# 真代理（需要网络权限 + .env 里的 key）
python sgsr\models\proxy.py --port 8770
# 假服务（离线）
python sgsr\models\mock_server.py --port 8765
```

契约见 `docs/api-contract.md`；接口的**意图**（为什么有 `/guide`、为什么服务端不过滤 `sorry`）
见 `docs/framework.md` 与 `docs/phase5-log.md`。
