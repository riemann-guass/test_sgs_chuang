# service

薄代理层，向 Lean 侧提供两个 HTTP 端点：

- `POST /conjecture`：输入当前证明状态与目标，输出候选辅助引理。
- `POST /guide`：输入目标与候选引理，输出 SGS rubric 三维评分与合成分数。

后端使用 OpenAI 兼容协议，先接远端模型，后续可通过配置切到本地 vLLM。
接口契约见 `../docs/api-contract.md`（阶段 1 冻结）。
