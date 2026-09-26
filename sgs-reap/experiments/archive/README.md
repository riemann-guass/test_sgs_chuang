# archive/：由已退役仪器产生的旧报告

这些 JSON 是**历史结论的证据**，不是当前数字的来源。它们对应的脚本在 2026-09-27 的
精简里被删除或吸收（见 `sgs-reap/scripts/README.md` 的"删掉的入口去了哪"），**不删除**
是为了保住"报告可回溯"这条硬约束：旧日志里引用的数字仍能在这里找到原文件。

| 前缀 | 原仪器 | 现在怎么复现同类数字 |
|---|---|---|
| `g1_*` | 求解器能力检定（`run_gate_g1.py`） | `run_prover_eval.py` 的三个口径 pass@k |
| `g2_*` | 需求挖掘检定（`run_gate_g2.py`） | `run_closure_tests.py` 的需求断言 |
| `conj_*` / `conjecture_*` | 猜想器单测（`run_conjecture.py`） | `run_round.py`（出题者本来就在闭环里） |
| `build_library_*` | 单轮漏斗（`build_library.py`） | `run_round.py` |
| `g3_real_*` / `g3_selection` | phase20/21 的两臂测量（装置未修好时的版本） | `run_gate_g3_real.py` |
| `diagnose_exceptions*` / `reverify_all_*` / `repro_check` / `p1_probe_phase27` | 判定预算与可复现性的诊断 | 报告里的 `heartbeats_per_job` + `SGSLEAN_HEARTBEATS` 重测 |
| `materialize_check` / `lemma_refs_check` / `minif2f_import` / `solve_smoke` / `server_smoke` | 单项冒烟 | `run_closure_tests.py` |
| `rounds_mock_2` | 假服务闭环冒烟 | `run_round.py --expect-mock` |

`experiments/results/` 里现在只留能回溯**当前**结论的 6 份报告：

| 报告 | 对应结论 |
|---|---|
| `p1_dev_k4_n20.json` · `p1_dev_k4_n20_budget4m.json` | P1 的 D 集数字；`budget4m` 那份是**配置有缺陷**的留档（4M 心跳 + SSL 故障），用于说明"报告必须记判定预算" |
| `calib_C2_k2_n80.json` | C2 的难度分布（80 题 × k=2） |
| `rounds_real_3.json` | C 上 3 轮真模型建库 |
| `g3_c_device_n12_k2.json` | 两臂装置检查（处理臂 14/24 篇引用库引理） |
| `closure_tests.json` | 唯一测试入口的最新一次运行 |
