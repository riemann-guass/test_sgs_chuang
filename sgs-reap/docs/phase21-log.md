# 阶段 21 执行日志（阶段 D：把 `cover` 换成真定义）

## 本单元做了什么

旧的 G3 是**空转**的：候选池被定义成"目标轨迹签名集合的并集"，于是每条候选按构造至少覆盖一个目标，
贪心比 1.000、子模性 0 违例都是数学必然（审计结论）。本单元换成真定义：

```
在裸解不出的目标集上，同一个 Solver、同样的 k：
  基线臂（不给库）          → proved_cover(∅)
  处理臂（给库 + 环境 import）→ proved_cover(S)
  增益 = 二者之差
```

## 交付物

| 文件 | 作用 |
|---|---|
| `service/prompts.py`（改） | `solve_prompt` 新增 `library` 参数 → `AVAILABLE LEMMAS` 区块（空列表 = 不出现，天然构成两臂对照） |
| `service/proxy.py`（改） | `/solve` 透传 `library`，meta 回报 `library_used` |
| `service/mock_server.py`（改） | 回显 `library_used`（离线断言用） |
| `sgslean/SgsLean/GeneratedLibrary.lean`（新，自动生成） | 物化目标改到 **lib 内部**，这样 `import SgsLean.GeneratedLibrary` 能解析——处理臂的验证环境必须真的能看到这些引理，否则证明里引用 `sgs_lem_i` 会报 `unknown identifier` |
| `tests/build_library.py`（改） | 物化后跑 `lake build SgsLean`（产出 olean）；新增 `--from-library` 从已有库重新物化 |
| `tests/run_gate_g3_real.py`（新） | 两臂实验 harness |

## 实测结果

```powershell
python tests\run_gate_g3_real.py --limit 12 --k 2 --endpoint http://127.0.0.1:8770/solve
```

```
[g3r] 目标 12 条；库 4 条；k=2
[g3r] ==== 基线臂（不给库） ====      22 篇候选证明 → 验证 22 篇
[g3r] ==== 处理臂（给库 + import 库）==== 18 篇候选证明 → 验证 18 篇
[g3r] proved_cover(∅)=0  proved_cover(S)=0  增益=0
[g3r] 判定：no_solvable_targets
```

**两臂都是 0：这 12 条目标一条都没证出来。**

## 为什么是 0——工作负载选错了（这是本单元真正的结论）

`data/targets_hard.jsonl` 的定义是"**修正后 solve_rate = 0** 的目标"，也就是
**恰好是 Solver 最不擅长的那些**。在这样的集合上测"库能不能帮着多证出题"，
等于在最难的题上要求 4 条引理实现 0→1 的翻转——留不出任何可测量的余量。

修正后的 G1 给出完整的目标难度分布（k=3）：

| 难度 | 目标数 | 含义 |
|---|---:|---|
| `solve_rate = 0` | 39 | 完全解不出（`targets_hard` 就取自这里） |
| `0 < solve_rate < 1` | **6** | **近失手**：偶尔能解出——这才是 `cover` 有信号的地方 |
| `solve_rate = 1` | 12 | 稳定解出（可用于验证"给库不会变差"） |

6 条近失手目标：

```
aime_1983_p9 (1/3)          algebra_2varlineareq_xpeeq7_2xpeeq3_eeq11_xeqn4 (1/3)
algebra_amgm_sumasqdivbsqgeqsumbdiva (1/3)   amc12_2001_p9 (2/3)
amc12a_2013_p7 (2/3)        amc12a_2016_p3 (1/3)
```

**次要观察**：处理臂的候选证明反而更少（18 vs 22，其中 `aime_1994_p4` 从 2 篇掉到 0）。
提示词变长（多了库区块）会改变模型的输出分布——这一点在后续做 H2 时必须用同预算、多次采样来控制。

## 下一步（阶段 D v2）

1. **换工作负载**：`W` = 6 条近失手 + 12 条稳定可解（后者用来验证"给库不会让成绩变差"），
   而不是"39 条最难的"。
2. **把 `cover` 改成**评分制**而不是 0/1 翻转**：`cover(S) = Σ_w [ solve_rate_with(S,w) − solve_rate_without(S,w) ]`。
   在近失手集合上，0/1 的翻转太稀疏（k=2 时一条目标要么 0 要么 0.5），评分制才有分辨率。
3. **提高 k**（例如 4–8）以降低采样噪声——否则"给库后掉了一篇"这种波动会被误读成因果。
4. 库还太小（4 条、只覆盖 2–3 个目标），在要求增益之前应先把库扩到 15–30 条。

## 现在在哪

**阶段 A/B/C 完成；阶段 D 的"测量装置"已就位（两臂、真 cover 定义、库可 import），
但第一次测量暴露的是工作负载设计问题，不是结果。** 这是一次有价值的负结果：
它把"该在什么集合上测"这件事钉死了。
