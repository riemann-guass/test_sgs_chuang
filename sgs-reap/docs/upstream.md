# 上游来源与版本锁定

本仓库对上游代码只做最小 patch，所有改动集中在 `sgs-reap/reap-fork/`。

| 项目 | 本地路径 | 上游 | 锁定 commit |
|---|---|---|---|
| reap | `D:\bianma\code\reap`（只读参考） | <https://github.com/frenzymath/reap> | `147743970d0061ae23783820b4723658064665b9` (2026-08-06, "feat: add native selector support (#16)") |
| SGS | `D:\bianma\code\SGS`（只读参考） | <https://github.com/LukeBailey181/sgs> | 待补 |

## fork 说明

- `sgs-reap/reap-fork/` 是 reap 在上述 commit 处的完整拷贝，已删除上游 `.git` 目录，避免嵌套仓库。
- fork 内保留 `.lake/`（依赖与编译产物），仅用于本地加速编译，已被 `.gitignore` 排除，不入库。
- 上游 Toolchain：`leanprover/lean4:v4.28.0-rc1`；reap 只依赖 `batteries` / `openAI_client` / `requests`，**不依赖 mathlib**，因此阶段 0 无需下载 mathlib。

## 生成 patch

改动完成后用以下命令产出可对照上游的补丁：

```powershell
git diff --no-index --stat D:\bianma\code\reap\Reap D:\bianma\code\大创\sgs-reap\reap-fork\Reap
```
