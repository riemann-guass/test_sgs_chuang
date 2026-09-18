# 上游来源与版本锁定

本仓库对上游代码只做最小 patch，所有改动集中在 `sgs-reap/reap-fork/`。

| 项目 | 本地路径 | 上游 | 锁定 commit |
|---|---|---|---|
| reap | `D:\bianma\code\reap`（只读参考） | <https://github.com/frenzymath/reap> | `147743970d0061ae23783820b4723658064665b9` (2026-08-06, "feat: add native selector support (#16)") |
| SGS | `D:\bianma\code\SGS`（只读参考） | <https://github.com/LukeBailey181/sgs> | 待补 |
| **Mathlib** | `sgs-reap/sgslean/.lake/packages/mathlib`（依赖，不入库） | <https://github.com/leanprover-community/mathlib4> | **tag `v4.28.0-rc1`** = `5352afccd6866369be9de43f5b7ec47203555f44` (2026-01-26, "chore: bump toolchain to v4.28.0-rc1") |

## Mathlib 版本为什么锁这个 tag（P1.2，2026-09-18）

`sgslean/lean-toolchain` 与 reap-fork 一致，都是 `leanprover/lean4:v4.28.0-rc1`。
**olean 与工具链绑定**，所以 Mathlib 必须取同名 tag；实测该 tag 存在，因此不需要把工具链
升级到正式版（也就避免了 reap-fork 跟着重编译）。

一致性检查由 mathlib 的缓存工具自己执行（`checkForToolchainMismatch`）：项目
`lean-toolchain` 与 Mathlib 的不一致时它会直接拒绝。

安装副作用（如实记录，均可还原）：

* 缓存/解压工具装在 `sgslean/.lake/mathlib-cache`（由 `MATHLIB_CACHE_DIR` 指定，不污染用户目录）；
* 用户级 curl 配置 `%APPDATA%\_curlrc` 被写入了 `--ssl-no-revoke`：本机 curl 走 Schannel，
  对 GitHub 的证书吊销检查失败（`CRYPT_E_NO_REVOCATION_CHECK`），而 lake 与 mathlib 缓存工具
  都会调用 curl。删掉该文件即可还原（见 `docs/phase6-log.md` 现象 2）。

## fork 说明

- `sgs-reap/reap-fork/` 是 reap 在上述 commit 处的完整拷贝，已删除上游 `.git` 目录，避免嵌套仓库。
- fork 内保留 `.lake/`（依赖与编译产物），仅用于本地加速编译，已被 `.gitignore` 排除，不入库。
- 上游 Toolchain：`leanprover/lean4:v4.28.0-rc1`；reap 只依赖 `batteries` / `openAI_client` / `requests`，**不依赖 mathlib**，因此阶段 0 无需下载 mathlib。

## 生成 patch

改动完成后用以下命令产出可对照上游的补丁：

```powershell
git diff --no-index --stat D:\bianma\code\reap\Reap D:\bianma\code\大创\sgs-reap\reap-fork\Reap
```
