# 阶段 1 执行日志

## 步骤 1：接口契约 + 确定性假服务

- `docs/api-contract.md`：冻结 `/conjecture` 与 `/guide` 的字段、错误码、超时、缓存键，
  以及 Guide 分数 → 搜索先验的换算约定 `prior_i = β · ln softmax(review_i / T)`。
- `service/mock_server.py`：标准库实现，`normal` / `noisy` / `empty` 三种模式，零第三方依赖。
- 冒烟测试通过：`/health`、`/conjecture`（3 条候选，review 7/4/4）、`/guide`、
  非法请求体 → 422、未知路径 → 404、noisy 模式按预期注入非法候选。

## 步骤 2：Lean 侧客户端与包装器

新增/修改的文件（全部在 fork 内）：

| 文件 | 作用 |
|---|---|
| `Reap/Conjecture/API.lean`（新增） | 线上类型、`ConjectureClient` / `GuideClient`、缓存、`conjecturePriors` |
| `Reap/Tactic/Conjecture.lean`（新增） | `generatePolicyValueWithConjecture` 包装器、猜想动作生成、路径标记 |
| `Reap/Options.lean`（修改） | `reap.conjecture_*` 六个 option（默认关闭） |
| `Reap/Basic.lean`（修改） | 挂载新模块 |
| `Reap/Test/Tactic/Conjecture.lean`（修改） | 追加先验换算与契约解析单元测试 |

验证：`lake build Reap Reap.Test` → `Build completed successfully (209 jobs)`。

### 关键设计决定

**「每条路径最多猜想一次」由上下文标记实现。** `have sgs_aux_i : P := ?_` 会把该假设带进
本分支后续所有节点的局部上下文，因此"上下文中存在 `sgs_aux_` 前缀的假设"等价于"这条路径已经猜想过了"。
这条规则替代了原计划里的深度阈值，好处是**不需要改动 MCTS 核心**（`PolicyValueEval` 签名不含深度）。
代价是无法按深度精细分层，已在代码注释与 `depth` 字段说明中标注，阶段 3 再通过扩展签名补上。

**软失败语义。** 服务不可用、返回非法 JSON、候选为空、打分失败，一律退回纯 policy 行为，
不中断搜索。这是与 `PremiseSelectionClient` 一致的原则。

### 本轮发现并修掉的 bug

1. **`PolicyValueEval` 未导入 → 被 autoImplicit 吞掉。** `Reap.Tactic.Generator` 并不导入
   `Reap.Tactic.TreeSearch`，于是 `PolicyValueEval` 被当成隐式绑定变量，报错是高度误导的
   `fun goals ↦ ?m.4 ... but is expected to have type PolicyValueEval`。修法是显式
   `import Reap.Tactic.TreeSearch`。教训：Lean 里"未知标识符 + autoImplicit"会伪装成类型错误。
2. **`meta` 是关键字**，不能做结构体字段名（`unexpected token ':'`）。字段改名 `metaJson`，
   线上 JSON 键仍为 `meta`。
3. **`Array.get!` 需要 `Inhabited`**（`candidates[i]!`）。给 `ConjectureCandidate` / `GuideScore`
   加 `deriving Inhabited`。
4. **`String.trimAscii` 返回 `Slice`**，没有 `isEmpty`，需要 `.toString`。
5. **`getTypeCleanup` 来自 Batteries**（`Batteries/Lean/Meta/Basic.lean`，namespace `Lean.MVarId`），
   需显式导入 `Batteries.Lean.Meta.Basic`。
6. **推导的 `FromJson` 对缺失字段会失败。** 改为手写实例：`candidates` / `scores` 缺失时退回空数组，
   因此服务端可以用 `{}` 表示"本次无猜想"，而不是让客户端抛异常。
7. **`Float` 无法用于 `native_decide` 的等式判定**（`failed to synthesize Decidable`），
   测试改用 `Bool` 比较（`==` 产生 `Bool` 后再判定）。

### 测试有效性的反向证据

把"空对象应解析成空列表"的断言写成 `some 0` 时（当时实现返回 `none`），
`native_decide` 直接报 `evaluated that the proposition ... is false` —— 说明这些断言确实在执行求值，
不是空转。

## 下一步

阶段 1 第三步：端到端联调——启动假服务，让 Lean 侧 `generatePolicyValueWithConjecture`
经真实 HTTP 取回候选并进入 MCTS，断言动作进入树、脚本可过终检；再验证服务关闭时的降级行为。
