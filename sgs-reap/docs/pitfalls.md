# 坑清单（还生效的那些）

只写**现在仍会咬人**的坑。每条都对应过一次真实假结论或一次真实返工；历史细节在
`docs/history/phase*-log.md`，这里只留结论与判据。

## 一、判定预算：变了，判定就变了

* Lean 的心跳按 **command** 累计。老实现把整批作业塞进一个 `run_tac`，于是批次尾部集体
  报 `maximum number of heartbeats`，看起来像"模型证不出"。
* 现在只有一条常驻路径（`Server.serveLoop`），整条循环在一个 command 里 ⟹ 必须
  `maxHeartbeats 0`（不限制），真正的上界是**每条 tactic 的墙钟** `reap.timeout`（默认 200 s）。
* 实测：`aime_1984_p15` 在 4M 心跳下门检 `exception`、400M 下 `ok`。
  **报告必须记 `heartbeats_per_job` 与 `cheap_budget_ms`**，否则数字不可比。

## 二、`maxErrors`：失败作业会静默吃掉后面的作业

Lean 默认 `maxErrors = 100`。失败作业的错误累加到同一张消息表，一旦到 100 就直接
`maximum number of errors reached, exiting`，**后面的 command 一条都不跑**。
实测：156 个作业的硬门批只回了 51 条，2/3 的候选被当成"没过门检"。

判据：片段固定带 `set_option maxErrors 0`；客户端对"响应缺失"记
`protocol:*` 并跳过，缺失超过 20% 直接抛错停下——**不许**把缺响应当成"这条不成立"。

## 三、常驻会话：父与子只靠三个文件说话

一次 `lean` 子进程导入一次 Mathlib，**跨批服务**（实测：首批 577 s，第二批 0.1 s）。
时序纪律（phase26 在这里踩过四个坑，都是"对同一个固定文件的读写时序假设"）：

1. 完成判据是**响应条数**，不是退出码、也不是标记文件；
2. **批号唯一 ⟹ 文件名唯一**（`out.<n>.json` 在开批前不存在），消掉"把上一批残留当本批"；
3. 读 `request.json` 失败只是"还没准备好"，退避重试，不当错误；
4. 父进程在子进程退出前**不能**返回，否则子进程成孤儿、继续占着 Mathlib 的内存。

## 四、物化：生成库不能 import 自己

`Materialize.emit` 若把 `SgsLean.GeneratedLibrary` 写进生成文件的 import，就是自我循环导入，
编译必失败（实测：闭环第一轮停在 commit；库已写入但下一轮 import 必然失败，表现成"库没用"）。
客户端与服务端两侧都要剔掉它；`lake build` 必须点名**模块目标**
（`lake build SgsLean` 不编这个模块）。

## 五、路径层数随文件移动而变

`sgsr/lean.py` 的 `ROOT = parents[1]`、`sgsr/pipeline/runner.py` 定位 lake 工程根，
都依赖"文件在包的哪一层"。移动模块时必须一起改——指错一层的症状是
`lake exe` 在不存在的 cwd 里跑、子进程立刻 `exit=1`、报告全空（不是崩溃信息）。
同理：Lean 子进程的工作目录**必须留在 `.lake/` 里**，否则拿不到 lake 的搜索路径，
退化成 `unknown module prefix 'SgsLean'`。

## 六、报告必须逐题落盘

整轮跑完才写报告 ⟹ 末尾一个异常（实测：漏 `import os` 触发 `NameError`）把几小时的结果全丢。
现在 `run_prover_eval` 每题刷一次盘，文件里带 `partial` 标记；`--resume` 接着跑。

## 七、装置故障 ≠ 模型能力

后端 503 / DNS / 非 JSON 响应、门检拒绝、协议缺响应，都**不是**"这道题没解出"：
* HTTP 只有两套显式语义（`post_json` 抛 `BackendUnavailable` / `soft_post_json` 收错），
  调用方必须选一个；
* 评测时把它们从分母剔除并单列（`excluded_gate_rejected` / `excluded_backend_errors`）；
* 环境预检（`preflight_imports`）失败就直接退出，**不让假数据进报告**。

## 八、代理缓存会让"重复轮"空转

`Backend.chat` 按 `(messages, max_tokens, temperature, thinking)` 缓存：同一 prompt 第二次
`/solve` 直接命中缓存，模型不被调用、拿到同一批候选（实测一轮 73 次调用里 35 次命中）。
P3 要重复采样时必须在提示词里带轮次/盐，或给代理加关缓存的开关。

## 九、写了库名不等于通过证明实际复用

旧两臂脚本用 `"sgs_lem" in proof` 统计引用，失败证明、拼错名字和未闭合证明都会计入。
因此 `g3_c_device_n12_k2.json` 的“14/24”只能说明模型尝试写过库名，不能作为 reuse 证据。

唯一合法口径：证明先通过 `Verify.verify`，再从 Lean 证明项的 `constants` 抽取稳定库名，
按不同目标去重，并排除引理自己的 `source_target`。

## 十、强提示会污染复用测量

“先检查这些引理”“引用更便宜、更不容易错”会提高模型写库名的概率，但这混入了提示服从性。
产品提示词可以这样写；probation 曝光和 A/B/C 测量必须使用中性 `measurement` 模式。
产品模式产生的引用不回写 reuse。

## 十一、可证不等于值得入库

开发期 35 条库中出现了 `a = a`、集合基数等于自身等 `rfl` 平凡命题，说明旧非平凡门失效。
硬门测试必须包含这些反向样例；正式库必须在修复后重建。旧库仅用于迁移和装置诊断。

## 十二、复用排序不能先于相关性

全局按 `reuse/cost` 排序会让已获得曝光的高频引理持续占据提示词，即使与当前目标无关，
形成“越常出现越容易继续被计数”的反馈偏差。正确顺序是先相关性召回/过滤，再在相关候选内
按复用密度排序，最后做 token 截断并保留明确的 probation 探索槽。
