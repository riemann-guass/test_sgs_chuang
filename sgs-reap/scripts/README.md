# scripts —— 实验入口（5 个，不再增长）

对应上游 SGS 的 `scripts/`：每个脚本是一个**可独立运行、自带判定准则**的实验单元。
所有编排逻辑都在 `sgsr/` 包里，这里只做参数、流程串联与报告落盘。

权威规格是 `docs/SG-Lean思路文档第二版.pdf`；本文件只说明"哪个脚本干哪件事"。

> **新能力一律做成现有入口的子命令，不新开脚本。** 2026-09-27 的精简删掉了 12 个
> 僵尸入口（17 → 5），每一条的去处见文末"删掉的入口去了哪"。

| 脚本 | 作用 |
|---|---|
| `prove.py` | **单题入口**：一条命题进，一篇过内核终检的证明出 |
| `run_prover_eval.py` | 批量评测：严格 pass@k、产品增强口径、成本与难度分档；带环境预检与 T 一次性守卫 |
| `run_round.py` | 离线建库入口。迁移目标是 C-build 生成、C-measure 试用、三态库与冻结快照 |
| `run_gate_g3_real.py` | 有库／无库配对测量。当前引用统计仍是旧文本口径，修复前只作开发诊断 |
| `run_closure_tests.py` | **唯一测试入口**（当前 95 条纯 Python 断言，另有常驻会话与物化往返） |

## 常用命令

```powershell
cd sgs-reap
$py = "C:\Users\gaosen\anaconda3\python.exe"
$env:PYTHONPATH = "D:\bianma\code\大创\sgs-reap"   # 直接跑脚本时 sgsr 包不在 sys.path 上

# 自检（秒级，不用 Lean；加 --skip-materialize 还会跑常驻会话冒烟）
& $py scripts\run_closure_tests.py --no-lean
& $py scripts\run_closure_tests.py --skip-materialize

# 离线：闭环冒烟（假服务，无 Mathlib，快）
& $py sgsr\models\mock_server.py --port 8765
& $py scripts\run_round.py --rounds 2 --target-limit 2 --k 1 --n 2 --imports none --expect-mock

# 真跑：先起真代理（需网络权限 + sgsr\models\.env）
& $py sgsr\models\proxy.py --port 8770
& $py scripts\run_round.py --rounds 3 --k 2 --n 3

# 单题 / 批量（批量带分档与逐题落盘）
& $py scripts\prove.py --statement "∀ (n : Nat), n + 0 = n" --k 4
& $py scripts\run_prover_eval.py --set D --k 4 --limit 20 --library none `
      --out experiments\results\p1_dev_k4_n20.json --tier-out data

# 配对测量（当前仅作开发诊断；正式版本将统一到通过证明的 Lean constants）
& $py scripts\run_gate_g3_real.py --select nearmiss --limit 6 --k 4 --endpoint http://127.0.0.1:8770/solve

# 库没变但物化文件过时
& $py scripts\run_round.py --materialize-only
```

> `python sgsr\models\proxy.py` 直接跑时，Python 会把**脚本所在目录**（`sgsr\models`）放进
> `sys.path`，于是 `from sgsr import client` 报 `ModuleNotFoundError`。
> 起服务前把仓库根加到 `PYTHONPATH`（如上），或用 `python -m sgsr.models.proxy`。

## 删掉的入口去了哪（2026-09-27）

| 删掉 | 去处 |
|---|---|
| `build_library.py` · `run_materialize.py` | 建库与物化只有一条路：`run_round.py`（后者另有 `--materialize-only`） |
| `calibrate_difficulty.py` | `run_prover_eval.py --tier-out`（分档直接写进逐题记录与报告） |
| `run_gate_g1.py` | `run_prover_eval.py`（求解器能力就是它的 pass@k） |
| `run_gate_g2.py` | `run_closure_tests.py` 的需求挖掘断言（分层 + 分桶） |
| `run_conjecture.py` | `run_round.py`（出题者本来就在离线闭环里） |
| `run_solve_mock.py` | `run_round.py --expect-mock`（假服务模式） |
| `run_server_smoke.py` | `run_closure_tests.py` 的常驻会话断言（协议已改成常驻，旧冒烟测的是已删掉的 stdin 路径） |
| `diagnose_exceptions.py` | `run_prover_eval.py` 报告里的 `heartbeats_per_job` + `SGSLEAN_HEARTBEATS` 显式重测 |
| `verify_lemma_refs.py` | `run_closure_tests.py` 的"物化 → 编译 → import 往返" |
| `test_prompts.py` | `run_closure_tests.py` 的提示词区块断言 |
| `run_m1_calibration.py` | 无替代（M1 标定是已被淘汰的仪器，结论留在 `docs/history/`） |

## 约定

* **判定准则写在脚本里、跑之前定死**（每个脚本的文件头都有），避免事后解释。
* 报告一律带 `mode` 与规模后缀，防止小规模重跑覆盖正式报告。
* 正式方法主指标是首轮模型生成、无 cheap、无 repair 的严格 pass@k；增强口径另报。
* 在线、离线与配对实验最终必须共用一个生成/检索/验证执行引擎，不允许脚本各自解释引用。
* Lean 侧测试在 `sgslean/SgsLean/Test/` 与 `reap-fork/Reap/Test/`，用 `lake build` 跑。
* **数据准备在 `tools/prepare_domain_corpus.py`**：miniF2F valid 确定性切成
  C-build=122、C-measure=61、D=61，写 `data/dataset_manifest.json`；miniF2F test 保持为 T。

## 公共入口与迁移目标

| 公共入口 | 唯一实现 | 谁在用 |
|---|---|---|
| `sgsr/client.py` | 对外说话的唯一入口：HTTP（`post_json` 抛错 / `soft_post_json` 收错）+ 服务配置 + OpenAI 兼容 chat 后端 | prover · conjecture · runner · 真代理 · 各实验脚本 |
| `sgsr/lean.py` | `sgslean-server` 的**常驻**客户端（跨批复用 + 环境预检 `preflight_imports`） | prove · run_prover_eval · run_round · closure_tests · run_gate_g3_real · tools/ |

新增能力请直接复用这两处；不要再写第 N 份 `http_post` / `subprocess.run([lake, exe, ...])`。

当前 `Prover`、`Runner.collect` 与 `run_gate_g3_real` 仍存在重复的求解/验证流程，且后者把
失败证明的文本命中也算“引用”。下一阶段要把三者收敛到同一执行引擎；在此之前，
`g3_c_device_n12_k2.json` 的 14/24 不得作为正式 reuse 证据。
