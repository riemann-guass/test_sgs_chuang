-- 测试聚合入口。这里刻意不用 `module`：测试文件是普通文件，
-- 而 `module` 文件不能导入非 `module` 模块（与 `reap-fork/Reap/Test.lean` 的做法一致）。
import SgsLean.Test.Gate
import SgsLean.Test.Verify
import SgsLean.Test.Trivial
import SgsLean.Test.Novelty
