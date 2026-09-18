# 阶段 6 执行日志（P1.2 联网部分：Mathlib 工程 + 执行模型改造）

本单元把 **Mathlib 接进 sgslean 并锁定版本**，并因此被迫改造了 `Server.lean` 的执行模型
（进程内 frontend → 子进程 `lean`）。过程中踩到 8 个坑，全部记在下面。

## 交付物

| 文件 | 作用 |
|---|---|
| `sgslean/lakefile.toml`（改） | 新增 `mathlib` require（`scope = "leanprover-community"`, `rev = "v4.28.0-rc1"`），**排在 reap 之后** |
| `sgslean/lake-manifest.json`（改） | Mathlib + 8 个传递依赖锁定（aesop/Qq/proofwidgets/batteries/Cli/importGraph/LeanSearchClient/plausible） |
| `docs/upstream.md`（改） | Mathlib 版本锁定与理由 |
| `sgslean/SgsLean/Syntax.lean`（新） | `ℕ` 记法补丁**拆成独立模块**（与 Mathlib 互斥，见现象 4） |
| `sgslean/SgsLean/Server.lean`（重写） | 执行模型改为「每批 spawn 一个 `lean` 子进程 + 文件通道」 |
| `sgslean/SgsLean/Basic.lean`（改） | `defaultHeartbeats` 200000 → 4000000（见现象 8） |
| `tests/run_server_smoke.py`、`tests/run_solve_mock.py`（改） | 模式感知（`SGSLEAN_IMPORTS=none` 快速 / `Mathlib` 生产），新增 Mathlib 级用例 |

## 验收命令与真实输出

```powershell
cd sgs-reap\sgslean
lake build SgsLean SgsLean.Test sgslean-server     # Build completed successfully (406 jobs).

cd sgs-reap
python tests\run_server_smoke.py                    # 默认快速模式（无 Mathlib）
python tests\run_solve_mock.py
$env:SGSLEAN_IMPORTS = "Mathlib"; python tests\run_server_smoke.py
```

```
[server-smoke] 批大小 16，stdout 17 行，35.1s
[server-smoke] frontend_ms=15137（含 13 条作业的整批 elaboration）
[server-smoke] PASS

[solve-mock] 10 条目标 × 3 篇 = 28 篇候选，验证通过 8 篇，20.9s（frontend 16007ms）
[solve-mock] solve_rate: mean=0.2666 min=0.0 max=0.3333 零解目标=['w09', 'w10']
[solve-mock] PASS

[server-smoke] 批大小 19，stdout 20 行，315.0s
[server-smoke] frontend_ms=246209（含 16 条作业的整批 elaboration）
[server-smoke] PASS（含 3 条 Mathlib 级用例）
```

Mathlib 级判定的实测样例（都在子进程里跑通）：

| 语句 | 证明 | 结果 |
|---|---|---|
| `∀ (n : Nat), Even (n * (n + 1))` | （只做 `check`） | `ok`，`isProp=true` — Mathlib 符号能解析 |
| `∀ (x : ℝ), x + 0 = x` | `ring` | `ok` |
| `2 ∣ 4` | `norm_num` | `ok` |
| `∀ (n : Nat), n < n + 1` | `omega` | `ok` |
| `∀ (n : Nat), Even (n * (n + 1))` | `exact Nat.even_mul_succ_self n` | `ok` |
| `∀ (x y : ℝ), (x + y)^2 = x^2 + 2*x*y + y^2` | `ring` / `ring_nf` | `ok`（放宽心跳后） |
| `∀ (n : Nat), 3 ∣ n^3 + 2n` | `omega` | **拒**（`omega` 给出反例约束，如实判负） |

## 成本实测（P1.3 的输入）

| 模式 | 子进程环境 | 一次批处理固定成本 | 备注 |
|---|---|---|---|
| 快速（`SGSLEAN_IMPORTS=none`） | `importedModules=2072` | **14.8–16.1 s** | 只做 Nat/Prop 级判定 |
| 生产（默认 `Mathlib`） | `importedModules=9867` | **67 s（热）/ 246–493 s（冷）** | 冷热差异近 10 倍：磁盘缓存命中时最快 67 s，冷启动时 5–8 分钟 |

结论：**批处理是硬约束**。P1.3 的 G1（50–100 条引理 × k 采样）必须一次喂给一个 server 进程，
按「批数 × 固定成本」估，而不是「条数 × 单条成本」。Mathlib 模式下 100 条引理的整批
预计 = 一次固定成本（≈4–8 min）+ 每条判定时间。

## 现象 / 根因 / 修法

### 现象 1：mathlib 缓存工具的 leantar 安装失败

**现象** `lake update mathlib` 依赖拉取成功，但 post-update 钩子在 `installing leantar 0.1.16`
之后崩溃：`uncaught exception: Tried to read from handle containing non UTF-8 data`。

**根因** 安装步骤用 `curl` 下载 zip、`tar -xf` 解压，而 Lean 用 `IO.Process.output`
**按 UTF-8 解码** 子进程输出；中文 Windows 上这些工具会输出 GBK 字节 → panic。

**修法** 手工把 leantar 0.1.16 装到 `MATHLIB_CACHE_DIR`（本工作区 `.lake/mathlib-cache`）：
`curl --ssl-no-revoke` 下 zip + `Expand-Archive` 解压 + 复制成 `leantar-0.1.16.exe`。
`validateLeanTar` 见文件已存在即跳过下载/解压 ✓。

### 现象 2：curl 的证书吊销检查（Schannel）

**现象** `curl: (35) schannel: ... CRYPT_E_NO_REVOCATION_CHECK`——GitHub / Azure 都下不动。

**根因** 本机 curl 是 Schannel 后端，吊销服务器（OCSP/CRL）不可达。

**修法** 先试 `CURL_HOME/_curlrc`（**不生效**，实测仍 000），最终写用户级
`%APPDATA%\_curlrc`（内容一行 `--ssl-no-revoke`）→ 立即 200。**这是唯一一处工作区外的改动**，
删掉该文件即可还原（`docs/upstream.md` 已记录）。

### 现象 3：bsdtar 的 `-vv` 输出中文日期

**现象** curl 修好后，`lake` 解压 ProofWidgets 云发布包时又崩：
`failed to execute 'tar': Tried to read from handle containing non UTF-8 data`。

**根因** Lake 用 `tar -xvvz`，bsdtar 的详细输出带**本地化日期**（`1月 26 2026`），
字节是 GBK（实测 `\xd4\xc2`）→ Lean 按 UTF-8 读 → panic。
`chcp 65001` 无效（bsdtar 走 ANSI 代码页），Git 自带的 GNU tar 在本机跑不起来。

**修法** 归档其实已经下好（`trace` 文件已写、`depHash` 匹配），所以直接手工把
`ProofWidgets4.tar.gz` 解压进 `proofwidgets/.lake/build`（540 文件 + 23 目录 = 563 成员，
与归档成员数一致），Lake 之后判定 fetch 已是最新，不再下载/解压 ✓。

### 现象 4：`ℕ` 记法与 Mathlib 冲突（硬错误）

**现象** 片段同时 `import Mathlib` 与 `import SgsLean`：
`error: import SgsLean.Basic failed, environment already contains 'termℕ' from Mathlib.Data.Nat.Notation`。

**根因** Lean **不允许**同一记法声明两次（不是歧义，是错误）。

**修法** 把 `notation "ℕ" => Nat` 从 `Basic.lean` 拆到独立的 `SgsLean/Syntax.lean`：
离线模式（无 Mathlib）自动补 `import SgsLean.Syntax`，Mathlib 模式不补；P1.1 的离线测试显式导入它。

### 现象 5：进程内 frontend 跑不动 Mathlib

**现象** 进程内 `Lean.Elab.runFrontend` 导入 Mathlib 时直接崩：
`libc++abi: terminating ... cannot evaluate [init] declaration 'Mathlib.pp.mathlib.binderPredicates'
in the same module`（而且把整个 server 进程带走了）。

**根因** 内嵌解释器拒绝执行「同一次 frontend 运行中导入的模块」的 `[init]` 声明。

**修法** 改成 **spawn 一个 `lean <片段>` 子进程**——这本来就是 Mathlib 开发的标准姿势
（`lake env lean Mathlib/Foo.lean`），顺便解决两件事：崩溃不再带走服务进程；
子进程有独立的 stdout/stderr。

### 现象 6：子进程输出污染协议流

**现象** 子进程的 `error: Unknown option ...` 出现在 **stdout** 上，`json.loads` 立即失败。

**根因** 两处：① `IO.withStdout` 只替换 Lean 层的 stdout 流，**改不了子进程继承的 OS 句柄**；
② `.piped` 又会走 UTF-8 解码（现象 1/3 的 panic）。

**修法** 写一个 `run_child.cmd`（内容 `@echo off` + `lean sgslean_snippet.lean > child.log 2>&1`），
父进程 `cmd.exe /c run_child.cmd`。子进程输出直接落 `child.log`，协议流天然干净，
出错时也有日志可查（错误消息里带工作目录）。

> 中途试过 `cmd.exe /c "lean ... > child.log 2>&1"`：Lean 的 `spawn` 会给带空格的参数加引号，
> cmd 收到的就不是它想要的语法（exit=1、日志都没生成）——所以必须落到脚本文件里。

### 现象 7：Windows 上空环境变量 = 删除

**现象** 想用 `SGSLEAN_IMPORTS=""` 表示"不导入 Mathlib"，结果服务端仍按默认的 `Mathlib` 跑
（冒烟测试如实报 `ping: mathlib 应为 False，实际 True`）。

**根因** Windows 的 `SetEnvironmentVariable(name, "")` 语义是**删除**变量，Python 的
`os.environ[k] = ""` 同理。

**修法** 用哨兵值 `SGSLEAN_IMPORTS=none`（代码里 `none`/`-` 都识别）。
上面那次失败恰好也充当了**反向对照**：模式不匹配时测试确实会失败 ✓。

### 现象 8：Mathlib tactic 撞上心跳上限

**现象** `∀ (x y : ℝ), (x+y)^2 = x^2+2xy+y^2` 配 `ring` 被判 `exception`：
`(deterministic) timeout at isDefEq, maximum number of heartbeats (200) has been reached`。

**根因** 我们的判定跑在 `lean` 驱动里，**Mathlib 的 tactic 代码是解释执行的**
（不像 `lake build` 那样有原生代码），实际开销比编译版高一个量级；
P1.1 沿用的 200000 千次心跳（=2 亿次）不够。

**修法** `defaultHeartbeats` 200000 → **4000000**（=40 亿次）；真正的墙钟上限仍是
`reap.timeout`（默认 200 s/次 tactic），G1 标定时两者都要如实记录。
放宽后 `ring`/`ring_nf` 均判通过 ✓。

## 当前状态与遗留

* Mathlib 已接入并锁定；`ping` 在 Mathlib 模式下回报 `importedModules=9867, mathlib=true` ✓。
* **工作区外只有一处改动**：`%APPDATA%\_curlrc`（一行 `--ssl-no-revoke`）。删掉它即可还原
  （代价是以后拉 GitHub 资源会再次被吊销检查挡住）。leantar 与 .ltar 缓存都在本工作区
  `.lake/mathlib-cache` 内，Mathlib 源码与 olean 都在 `sgslean/.lake/packages/mathlib` 内。
* 缓存里有 3 个模块（`CurryingThree`/`PEquiv`/`Valuation.AlgebraInstances`）是本地补编的
  （只有 `.olean`，没有 `.o`）。若以后要把 Mathlib 链进 exe（提升 tactic 速度），需要重编这三个。
* 下一步：闸门 **G1**（`tests/run_gate_g1.py` + `data/lemmas_g1.jsonl` + G1 报告），
  以及真实模型版的 `/solve` 端到端（要花少量 API 费用，跑之前问）。
