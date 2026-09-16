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

## 步骤 3：端到端联调（Lean ⟷ 假服务）

新增文件：

| 文件 | 作用 |
|---|---|
| `reap-fork/tests/ConjectureE2E.lean` | 真实 HTTP 往返：候选引理进入动作集并解出目标 |
| `reap-fork/tests/ConjectureDegrade.lean` | 服务不可达时的降级行为 |
| `tests/run_e2e.py` | 启动/关闭假服务、跑两个 Lean 文件、校验可观测性产物 |

两个 Lean 文件都**不在** lake 的 glob 范围内（位于 `reap-fork/tests/` 而非 `Reap/`），
因此 `lake build` 不会构建它们，离线构建保持绿色。

为了能脱离真实 model 服务测试，包装器重构为：

```lean
def generatePolicyValueWithConjectureUsing (base : PolicyValueEval) : PolicyValueEval
def generatePolicyValueWithConjecture : PolicyValueEval :=
  generatePolicyValueWithConjectureUsing TacticGenerator.generatePolicyValue
```

端到端测试把 `base` 注入成纯本地策略，并把 `policy` / `value` 端点指向不可达端口——
于是**搜索里任何动作都只可能来自 `/conjecture`**，证明成功即证明 HTTP 链路在工作。

运行结果：

```
[run_e2e] mock server healthy on 127.0.0.1:8765
[run_e2e] PASS  tests/ConjectureE2E.lean
[run_e2e] PASS  tests/ConjectureDegrade.lean
[run_e2e] wall-clock records: {'tactic_eval_pre': 7, 'tactic_eval': 7, 'conjecture': 4, 'guide': 4}
[run_e2e] ALL PASS
```

### 本轮发现并修掉的 bug

**1. 辅助引理的子目标上会发起"自指猜想"。**

首轮 e2e 的断言（整棵树应恰有 1 条猜想边）拿到 2 条。原因是 `have sgs_aux_0 : P := ?_`
拆分出的第二个 focus 子目标是 `⊢ P`，而 `sgs_aux_0` **不在**该子目标的局部上下文里
（这正是它的证明义务），所以"上下文标记"没能拦住它——于是又对 `P` 猜了一次 `P`。

修法是在客户端侧加目标同型过滤：`normalizeProp` 折叠空白后，丢弃与当前目标文本相同的候选。
这与 SGS 的 Guide rubric 一致（对与目标等价的引理直接给 0 分），属于通用改进而非补丁。

**2. 服务不可达时一次搜索从 13 秒劣化到 137 秒。**

`Requests.post` 直接 shell 出 curl，没有超时设置，每个节点都要跑满重试。
加入进程级熔断：连续失败 3 次后跳过后续请求。实测同一测试的耗时从 **137 秒降到 13.4 秒**，
且语义不变（无服务时不产生猜想动作，退回基础策略）。

### 测试有效性的反向证据

关掉假服务后跑主链路测试，两条断言都如期失败：

```
tests/ConjectureE2E.lean:45:2: error: expected MCTS to solve the goal through the HTTP conjecture service
tests/ConjectureE2E.lean:64:2: error: expected exactly 1 conjecture edge in the whole tree, got 0
```

### 已知成本特征（留给阶段 3）

一次搜索产生 2 次猜想调用（根节点一次，辅助引理的证明子目标一次）。日志统计的是**调用次数**
而非服务命中数——同一目标状态的重复请求由客户端缓存挡掉，不会再发 HTTP。
阶段 3 需要补：全局调用预算 + 新增 `sgsMCTS` tactic 在每次搜索开始时重置预算与熔断计数。

## 阶段 1 完成

`lake build Reap Reap.Test` 全绿（209 jobs），`run_e2e.py` 全通过，
离线与联网两条路径都有可复现的验收命令。

## 下一步

阶段 2：真实模型接入——实现薄代理 `service/proxy.py`（OpenAI 兼容后端 + 缓存 + 重试 + token 统计），
搬 SGS 的 conjecturer prompt 与 guide rubric/解析，在 20 道题上标定候选利用率、延迟与 token。
