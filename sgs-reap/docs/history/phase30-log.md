# 阶段 30 执行日志（统一执行入口与 Mathlib 会话）

## 为什么做

新版规格要求在线、离线和两臂测量共享求解、检索、验证语义。审计发现离线一轮仍在
轨迹、硬门、候选验证三个阶段分别启动 Lean；三个 CLI 又各自判断是否导入生成库。
前者重复支付 Mathlib 导入，后者容易制造“提示词有库、验证环境没库”的假实验。

## 改动

- 新增唯一 `solve_candidates`，在线 Prover、离线 Runner、两臂脚本共同使用。
- 新增 `resolve_imports` / `materialize_imports`，三个 CLI 不再复制 import 判断。
- `RoundRunner` 一轮只启动一个主 Lean 会话，轨迹、硬门、验证跨批复用；物化仍使用
  排除 `SgsLean.GeneratedLibrary` 的独立基础环境。
- 检索改为相关性过滤优先，`reuse/cost` 只在相关候选内排序。
- 增加 `product` / `measurement` 两种求解提示词；测量模式中性陈列库，不诱导引用。
- `sample_salt` 真正进入提示词，避免重复轮被代理缓存变成同一批样本。
- 两臂引用统计改为 `dependencies.constants`，只数通过内核验收的证明。
- 删除 Runner 与 G3 中重复的 HTTP `/solve` 客户端及已失效注释。

## 验收

```text
python -m compileall -q sgsr scripts                         PASS
python scripts/run_closure_tests.py --no-lean                PASS（82 条断言）
run_round --rounds 1 --target-limit 1 --k 1 --n 1
          --imports none --expect-mock                       PASS
```

小型冒烟只跑 1 个目标、1 轮、假模型，不导入 Mathlib。报告确认
`lean_main_sessions=1`、`lean_batches=1`。首次运行因执行环境缺少 `ELAN_HOME` 退出；
显式使用本机既有 `C:\Users\gaosen\.elan` 后通过，这不是项目逻辑失败。

## 反向对照

- 非空库显式请求 `Mathlib`（漏掉生成库）必须抛错；测试已通过。
- 构造 `reuse=100/cost=1` 但与目标无关的引理，检索结果必须排除它；测试已通过。
- 测量提示词不得含产品模式的 `Check them FIRST`；测试已通过。
- 离线一轮的工厂事件必须严格等于一次 `enter`、一次 `exit`；测试已通过。

## 下一步

实现 probation/active/cold 三态与冻结 `LibrarySnapshot`。当前 35 条开发库仍不升级为
正式活动库，也不运行大规模实验。
