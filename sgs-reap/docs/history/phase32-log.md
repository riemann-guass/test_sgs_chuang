# 阶段 32 执行日志（C-build / C-measure 数据隔离）

## 目标

把原先同一组 C 目标同时用于需求生成和 reuse 测量的混合流程拆开，确保候选的
来源集合不参与晋升测量。

## 交付物

- `RoundRunner` 必须接收独立的 `c_build` 和 `c_measure`；身份或命题内容交叉立即拒绝。
- C-build 只产生基线轨迹、需求与候选；C-measure 只做中性曝光和 constants 计数。
- reuse 必须同时满足：C-measure 目标、通过验收、constants 引用、该引理确实对该目标曝光。
- `run_round.py` 可用固定种子和内容哈希自动划分 C，也可显式接收两个输入；
  每次运行把划分算法、种子、规模和指纹写入运行目录 manifest。

## 验收与反向对照

```text
python -m compileall -q sgsr scripts              PASS
python scripts/run_closure_tests.py --no-lean     PASS（90 条断言）
```

反向对照包括：C-build 目标复制进 C-measure 必须拒绝；未对某测量目标曝光的引理即使
出现在 constants 中也不计 reuse；旧的非 C-measure 复用和曝光目标会在迁移时被清除。

没有调用模型，没有运行大规模 Lean、P3 或 T。

## 下一步

阶段 33 修复非平凡硬门，先用 `rfl` 可直接闭合的命题做反向对照，不重建正式库。
