# 阶段 34 执行日志（miniF2F 数据协议重整）

## 方向变化

用户明确决定项目改用 miniF2F 数据集。原“自建 C1 + Mathlib C2、miniF2F valid 只作 D”协议
随之废止。当前唯一题目来源为 miniF2F；这是经用户授权的数据协议重大变化。

## 当前划分

miniF2F valid 244 题按固定种子 `sg-lean-minif2f-v1` 和
`sha256(seed, id, normalized_statement)` 排序后精确切分：

- C-build：122 题，候选与需求来源；
- C-measure：61 题，只做中性曝光、reuse 计数和晋升；
- D：61 题，只做调参、装置检查与忠实度审计；
- T：miniF2F test 原始 244 题，未读取求解结果、未运行，仍是最终一次性测试集。

完整算法、来源与分区哈希写入 `data/dataset_manifest.json`。三个 valid 分区完整覆盖源集，
目标身份两两不相交；valid 与 test 的目标身份和归一化命题重叠均为 0。

## 工程改动

- `tools/prepare_domain_corpus.py` 改为纯 Python 的 miniF2F 确定性划分器，不再导入 Mathlib、
  启动 Lean 或抽取 Mathlib 定理。
- `run_round.py` 默认显式读取新的 C-build/C-measure；`run_prover_eval.py --set D` 改读 61 题 D。
- 引理来源只允许 `MF_VALID_C`；`MF_VALID_D`、原始未分区 valid 和 T 均被守卫拒绝。
- 删除当前工作树中的旧 `C.jsonl`、`C_build.jsonl`、`lemmas_g1.jsonl` 和旧 manifest；
  它们仍可从 Git 历史恢复，但不再占据当前正式数据目录。
- 删除 Lean 服务中只供旧 Mathlib C2 抽取使用、已无调用方的 `list_theorems` 命令。
- 更新两份报告源码、项目记忆、数据协议和入口说明。

## 验收与反向对照

```text
python -m compileall -q sgsr scripts tools            PASS
python tools/prepare_domain_corpus.py                 PASS（122/61/61）
相同种子连续生成，四个输出 SHA-256 全部不变          PASS
python scripts/run_closure_tests.py --no-lean         PASS（95 条断言）
python scripts/run_closure_tests.py --skip-materialize PASS（100 条断言，含 Lean 常驻会话与 rfl 反例）
python scripts/run_round.py --rounds 0 ...            PASS（默认双输入可装载）
```

反向对照包括：D 文件直接作为 C 输入必须失败；D 文件复制改名后仍由内容指纹拒绝；
三个 valid 分区一旦出现交叉、漏题、非法来源标签或 C-build/C-measure 文件互换，
闭包测试必须失败。

首次 Lean 冒烟因当前进程没有 `ELAN_HOME` 而退出；显式指向已有 `.elan` 后 100 条断言全过，
不是代码失败。内置 LaTeX 编译器尝试编译两份报告 `.tex` 时都返回平台标准目录不可用，未得到 PDF 编译确认；
源码已保留并在编辑器中打开。没有安装工具链，也没有改用大规模工程构建。

本阶段没有调用模型、没有运行正式建库、没有运行 D 求解，更没有运行 T。

## 下一步

以新的 C-build/C-measure 重建正式 probation/active 库；在此之前旧 35 条开发库继续只作迁移诊断。
