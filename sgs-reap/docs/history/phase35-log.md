# 阶段 35：项目公开整理

日期：2026-09-27。

## 目标

在不运行建库和评测实验的前提下，把仓库从内部研究记录整理成外部读者可以理解、可以自检、
不会误读旧结果的项目。

## 名称和定位

- 公开名称统一为 **LeanReuse**。
- 一句话定位改为：带可复用引理库、所有输出都经 Lean 内核验证的轻量证明器。
- SGS 只保留为研究启发和后续 Guide 对照，不再进入项目名称或主叙事。
- `sgsr`、`SgsLean`、`sgs_lem_*`、`sgs-reap` 暂时保留为兼容实现名，避免破坏导入路径。

## 文档整理

- 重写根 `README.md` 和 `AGENTS.md`，先用普通语言解释项目，再给代码字段。
- 新增 `docs/architecture.md` 作为权威设计说明。
- 新增 `docs/project-status.md`，明确已经完成、尚未完成和下一阶段。
- 新增两级文档导航，明确历史日志不是当前规格。
- 重写数据、脚本、实验目录、Lean 层和模型服务的 README。
- 删除两套已经被 Markdown 取代的旧 SG-Lean 报告及 PDF。
- 相关工作源码改名为 `docs/related-work.tex`；内置 LaTeX 编译器仍因平台标准目录不可用
  无法生成 PDF，因此不提交可能与源码不一致的旧 PDF。

## 入口和代码可读性

公开入口从阶段代号改成用途名称：

| 旧名 | 新名 |
|---|---|
| `run_round.py` | `build_library.py` |
| `run_prover_eval.py` | `evaluate.py` |
| `run_gate_g3_real.py` | `compare_library.py` |
| `run_closure_tests.py` | `check_project.py` |

`compare_library.py` 不再依赖已退役的难度报告和 `targets_hard.jsonl`，默认直接读取开发集，
只做同一批题的有库/无库配对比较。模块说明、命令帮助和关键注释删除了 P0/P1/G1/G3 等
阶段代号，保留必要的代码字段并补充中文含义。

## 文件清理

- 删除旧开发实验 JSON、旧归档 JSON、旧难度派生数据和失效结果文件；Git 历史仍可恢复。
- 当前 `experiments/library.jsonl` 与 `library_cold.jsonl` 重置为空。
- `SgsLean.GeneratedLibrary` 重置为可编译的空模块。
- 项目自检报告改名为 `experiments/results/project_check.json`。

这样，当前工作区不再同时展示“尚未建立正式库”和“35 条旧开发引理”两个互相冲突的状态。

## 验收

```text
python -m compileall -q sgsr scripts tools             PASS
五个公开入口 --help                                    PASS
python scripts/check_project.py --no-lean              PASS（95 条）
python scripts/check_project.py --skip-materialize     PASS（100 条）
lake build SgsLean.GeneratedLibrary                    PASS（空库模块）
```

反向对照仍包括：开发集改名后进入建库必须失败、C-build/C-measure 文件互换必须失败、失败证明
引用不得计数、高复用无关引理不得进入提示词、`1 = 1` 必须被非平凡性检查拦截。

本阶段没有调用模型，没有运行正式建库、开发集求解或最终测试集。

