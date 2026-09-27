# 阶段 31 执行日志（审查修复：库三态与冻结快照）

## 修复范围

根据 phase30 代码审查，修复六项会污染实验或浪费固定成本的问题：

1. 新候选只进入 `probation`；达到跨目标门槛且有曝光后才晋升 `active`；在线 Prover、
   import 决策、G3 和物化只读取 active。旧无状态 35 条库按 probation 处理。
2. reuse 落盘前删除引理自己的 `source_target`，来源题不再贡献复用。
3. 检索有正符号重叠时过滤零重叠噪声；全池零重叠时保留回退，避免 α 改名误杀。
4. 发布活动库时写 `<library>_snapshot.json`，校验 active 内容哈希与生成源码哈希；
   在线入口在模型调用前拒绝缺失或陈旧快照。
5. G3 活动库为空立即退出；处理臂预检与正式验证复用同一个 Lean 会话。
6. `--generated` 只允许规范模块路径，避免“写到自定义文件但 import 固定模块”的假接口。

## 验收

```text
python -m compileall -q sgsr scripts                         PASS
python scripts/run_closure_tests.py --no-lean                PASS（87 条断言）
run_gate_g3_real.py --limit 1 --library experiments/library.jsonl
                                                              exit=2（活动库为空）
```

没有运行真实模型、Mathlib 大构建或大规模实验。

## 反向对照

- reuse 输入显式包含 `source_target=g01`，落盘后必须没有 g01。
- 旧无状态库必须解析为 probation，不能触发生成库 import。
- 篡改 `GeneratedLibrary.lean` 后 snapshot manifest 校验必须失败。
- `a,b` 与 `x,y` 的 α 改名加法命题必须仍能召回。
- G3 空活动库必须在读取旧难度报告、启动 Lean、调用模型之前退出。

## 尚未完成

Runner 还没有把 C-build 与 C-measure 做成两个独立输入。本阶段保证状态与快照语义正确，
下一阶段再拆数据流；在拆分完成并修复非平凡门之前，不重建正式库、不跑 P3/T。
