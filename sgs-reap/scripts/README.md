# scripts —— 实验入口

对应上游 SGS 的 `scripts/`：每个脚本是一个**可独立运行、自带判定准则**的实验单元。
所有编排逻辑都在 `sgsr/` 包里，这里只做参数、流程串联与报告落盘。

## 按"思路框架的四层"分组

| 层 | 脚本 | 作用 |
|---|---|---|
| **闭环（建库）** | `run_round.py` | SG-Lean 主入口：多轮闭环（采轨迹→需求→猜想→门检→硬门→求解→验证→选择→入库）。**只接受课程集 C 作为 `--curriculum`** |
| | `build_library.py` | 单轮漏斗：候选 → 硬门 → 求解 → 验证 → 物化 → 入库（`--from-library` 可只重新物化） |
| | `run_conjecture.py` | N1 下游：需求 → 候选引理 → 门检（`--no-demand` 是 H1 的反向对照） |
| **判据层自检** | `test_prompts.py` | 提示词离线单测（空需求必须等于没有需求，否则 H1 对照不干净） |
| | `run_server_smoke.py` | `sgslean-server` 协议冒烟（`ping`/`check`/`verify`/`trace`/预算） |
| | `run_solve_mock.py` | `/solve` → 验证 → 落轨迹的离线链路 |
| | `run_materialize.py` | 物化 + 编译校验 |
| | `verify_lemma_refs.py` | 校验 G1 引理集里的参考证明 |
| **闸门与评测** | `run_gate_g1.py` | G1：Solver 能力（solve_rate 分布，支持 `--file/--budget/--imports`） |
| | `run_gate_g2.py` | G2：需求挖掘（分层统计 + 四条分桶） |
| | `run_gate_g3_real.py` | **G3（真定义）**：两臂对照 `proved_cover(S) − proved_cover(∅)`，`--select {hard,nearmiss,easy,mixed}` 选工作负载 |
| | `diagnose_exceptions.py` | 判定预算诊断：把 `exception` 样本用大预算重测，输出翻转矩阵 |
| **P0 期仪器** | `run_e2e.py` / `run_real_e2e.py` / `run_proxy_smoke.py` / `run_m1_calibration.py` | 假服务链路、真实模型端到端、代理冒烟、M1 标定（利用率/token/延迟）。结论已入 `docs/phase0..2-log.md` |

## 常用命令

```powershell
cd sgs-reap
$py = "C:\Users\gaosen\anaconda3\python.exe"

# 离线：闭环冒烟（假服务，无 Mathlib，快）
$py sgsr\models\mock_server.py --port 8765 &        # 另开一个终端
$py scripts\run_round.py --rounds 2 --target-limit 2 --k 1 --n 2 --imports none --expect-mock

# 真跑：先起真代理（需网络权限 + sgsr\models\.env）
$py sgsr\models\proxy.py --port 8770 &
$py scripts\run_round.py --rounds 5 --k 3 --n 3

# 两臂评测（只能在开发集 D 上调参；测试集 T 只跑一次）
$py scripts\run_gate_g3_real.py --select nearmiss --limit 6 --k 4 --endpoint http://127.0.0.1:8770/solve
```

## 约定

* **判定准则写在脚本里、跑之前定死**（每个脚本的文件头都有），避免事后解释。
* 报告一律带 `mode/规模` 后缀，防止小规模重跑覆盖正式报告。
* Lean 侧测试在 `reap-fork/Reap/Test/` 与 `sgslean/SgsLean/Test/`，用 `lake build` 跑。
