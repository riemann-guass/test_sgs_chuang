# LeanReuse（基于SGS的复用引理库解题方法）

LeanReuse 是一个轻量的 Lean 4 自动证明项目。输入一条完整的 Lean 命题，系统生成候选
证明，并且只返回通过 Lean 内核检查的证明。

项目的核心想法很简单：平时解决单题，空闲时整理一套经过验证、确实被其他题目使用过的
引理库。在线求解只能读取已经冻结的引理库，不能临时修改它。

## 它解决什么问题

语言模型可以生成 Lean 证明，但生成结果可能有语法错误、未完成目标或者引用不存在的定理。
LeanReuse 把语言模型当作候选生成器，把 Lean 内核当作最终裁判：

```text
输入命题 → 找相关引理 → 生成候选证明 → Lean 验证 → 只返回通过项
```

离线阶段负责建立引理库：

```text
从建库题中发现需求 → 生成并证明候选引理 → 在另一批题上试用
→ 保留有跨题使用证据的引理 → 编译并冻结新版本
```

项目不训练或微调模型，不需要 GPU。模型参数始终冻结，离线积累只通过引理库进入在线系统。

## 四份数据各做什么

miniF2F 是当前唯一题目来源。valid 的 244 题按固定规则拆成三份，test 的 244 题保留为
最终测试集。

| 通俗名称 | 代码名称 | 数量 | 用途 |
|---|---|---:|---|
| 建库集 | `C-build` | 122 | 发现需求、生成和证明候选引理 |
| 复用测量集 | `C-measure` | 61 | 检查候选引理是否真的被其他题使用 |
| 开发集 | `D` | 61 | 调参数、排查故障、比较方案 |
| 最终测试集 | `T` | 244 | 所有内容冻结后只运行一次 |

建库集不能给自己生成的引理贡献正式复用记录；开发集和最终测试集绝不参与建库。

## 引理如何进入正式库

每条候选引理都要先通过语法、非平凡性、新颖性和可证明性检查，然后经历三个状态：

| 中文含义 | 文件字段 | 说明 |
|---|---|---|
| 待观察 | `probation` | 已经证明，但还没有足够的跨题使用证据 |
| 已发布 | `active` | 可以进入在线证明器的冻结库 |
| 已停用 | `cold` | 试用后没有证据，或后来被淘汰 |

“被使用”只认通过验证的 Lean 证明项，并按不同题目去重。失败证明中出现引理名字不算，
候选引理自己的来源题也不算。正式效果最终还要通过同一批题的有库/无库配对测试确认。

## 当前状态

已经完成：

- 在线单题求解、批量评测和离线建库主流程；
- Lean 命题检查、证明验证、依赖提取、引理库生成和常驻验证服务；
- 建库集、复用测量集、开发集和最终测试集的强制隔离；
- 待观察、已发布、已停用三种库状态，以及冻结快照校验；
- 95 条纯 Python 检查和 5 条 Lean 集成检查。

尚未完成：基于新 miniF2F 划分重建正式引理库，以及随后在开发集和最终测试集上的正式实验。
仓库中的正式库当前应视为空，历史开发结果不代表项目最终性能。

## 快速自检

需要 Windows PowerShell、Python 和项目 `lean-toolchain` 指定的 Lean 版本。

```powershell
cd sgs-reap
$env:PYTHONPATH = (Get-Location).Path
python scripts\check_project.py --no-lean
```

运行包含 Lean 服务的轻量检查：

```powershell
$env:ELAN_HOME = "$env:USERPROFILE\.elan"
python scripts\check_project.py --skip-materialize
```

这两个命令不会调用模型，也不会运行正式实验。

## 五个公开入口

| 命令 | 用途 |
|---|---|
| `scripts/prove.py` | 证明一条命题 |
| `scripts/build_library.py` | 多轮建立并发布引理库 |
| `scripts/evaluate.py` | 在指定数据集上批量评测 |
| `scripts/compare_library.py` | 对同一批题比较有库与无库 |
| `scripts/check_project.py` | 运行项目自检 |

## 目录

```text
docs/                         面向读者的设计、状态与相关工作
sgs-reap/
  data/                       miniF2F 数据与可复现划分清单
  scripts/                    五个公开命令行入口
  sgsr/                       Python 实现
  sgslean/                    Lean 验证器和生成库
  experiments/                当前库、运行中间记录和结果
  docs/                       接口、数据规则、工程注意事项和历史日志
  reap-fork/                  保留的上游研究代码，不是日常入口
```

## 文档导航

- [设计说明](docs/architecture.md)
- [当前状态与后续工作](docs/project-status.md)
- [数据划分规则](sgs-reap/docs/data-protocol.md)
- [模型服务接口](sgs-reap/docs/api-contract.md)
- [工程注意事项](sgs-reap/docs/pitfalls.md)
- [阶段历史](sgs-reap/docs/history/README.md)

## 名称说明

项目公开名称是 **LeanReuse**。代码中的 `sgsr`、`SgsLean`、`sgs_lem_*` 和目录
`sgs-reap` 是早期实现标识，为避免破坏已有 Lean 模块、数据和脚本导入路径而暂时保留，
不代表项目仍在复现 SGS。SGS 只作为研究启发和后续对照方法出现。
