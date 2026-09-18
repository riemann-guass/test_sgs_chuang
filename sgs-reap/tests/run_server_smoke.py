"""`SgsLean/Server.lean` 协议 v1 的离线冒烟测试（不联网、不花一分钱）。

用法：

    C:\\Users\\gaosen\\anaconda3\\python.exe tests\\run_server_smoke.py

做三件事：

1. 把一批请求一次性喂给 `lake exe sgslean-server`，用 `{"cmd":"flush"}` 取回本批响应，
   再发一条请求由 EOF 自动 flush，覆盖两条 flush 路径；
2. 校验 **stdout 是纯 JSONL**（每一行都能 `json.loads`，且行数恰好等于响应数）——
   这是协议能用的前提：Lean frontend 默认把诊断打到 stdout，一旦泄漏，Python 侧解析就崩；
3. 逐条校验判定语义与 P1.1 的库内路径一致（`Gate.check` / `Verify.verify` 的判定码）。

结果落到 `experiments/results/server_smoke.json`。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 cp936，中文报告会变乱码

ROOT = Path(__file__).resolve().parents[1]
SGSLEAN = ROOT / "sgslean"
RESULTS = ROOT / "experiments" / "results"
LAKE = os.environ.get("LAKE", "lake")

# 默认走**无 Mathlib** 的快速模式（本测试考的是协议与判定管线，不是 Mathlib 本身）；
# 想看生产配置就显式设 `SGSLEAN_IMPORTS=Mathlib`（一次 frontend ≈ 4–8 分钟，见 docs/phase6-log.md）。
# 哨兵值是 `none` 而不是空串：Windows 上空串环境变量等于删除（见 SgsLean/Server.lean 注释）。
if "SGSLEAN_IMPORTS" not in os.environ:
    os.environ["SGSLEAN_IMPORTS"] = "none"
MATHLIB_MODE = "Mathlib" in os.environ["SGSLEAN_IMPORTS"]

PROP = "∀ (n : Nat), n + 0 = n"

# (id, 请求行, 期望)
# 期望结构：{"ok": bool, "reason": str} 或 {"error": str}
REQUESTS: list[tuple[str, str, dict]] = [
    # ping 的结果里没有 `ok` 字段（它回的是**子进程**环境信息）：查 status 与 mathlib
    ("ping", '{"id":"ping","cmd":"ping"}',
     {"field": ("status", "ok"), "mathlib": MATHLIB_MODE}),
    (
        "check_ok",
        json.dumps({"id": "check_ok", "cmd": "check", "stmt": PROP}, ensure_ascii=False),
        {"result_ok": True, "reason": "ok", "isProp": True},
    ),
    (
        "check_not_prop",
        json.dumps({"id": "check_not_prop", "cmd": "check", "stmt": "Nat"}, ensure_ascii=False),
        {"result_ok": False, "reason": "not_a_prop", "isProp": False},
    ),
    (
        # 候选是证明项而不是命题（P1.1 的负例 1，这里是闭式写法）
        "check_proof_term",
        json.dumps(
            {"id": "check_proof_term", "cmd": "check", "stmt": "fun h : True ∧ True => h.right"},
            ensure_ascii=False,
        ),
        {"result_ok": False, "reason": "type_error"},
    ),
    (
        # ℕ 记法补丁的回归测试（走 JSON 往返，说明记法在 frontend 环境里生效）
        "check_unicode",
        json.dumps({"id": "check_unicode", "cmd": "check", "stmt": "∀ (n : ℕ), n ≤ n * n + 1"},
                   ensure_ascii=False),
        {"result_ok": True, "reason": "ok"},
    ),
    (
        "check_unknown",
        json.dumps({"id": "check_unknown", "cmd": "check", "stmt": "NotARealType"}),
        {"result_ok": False, "reason": "unknown_identifier"},
    ),
    (
        "verify_ok",
        json.dumps({"id": "verify_ok", "cmd": "verify", "stmt": PROP, "proof": "intro n\nrfl"},
                   ensure_ascii=False),
        {"result_ok": True, "reason": "ok", "finalChecked": True},
    ),
    (
        # CRLF 证明：行尾归一化的回归测试
        "verify_crlf",
        json.dumps({"id": "verify_crlf", "cmd": "verify", "stmt": PROP, "proof": "intro n\r\nrfl"},
                   ensure_ascii=False),
        {"result_ok": True, "reason": "ok"},
    ),
    (
        "verify_sorry",
        json.dumps({"id": "verify_sorry", "cmd": "verify", "stmt": PROP, "proof": "sorry"},
                   ensure_ascii=False),
        {"result_ok": False, "reason": "mvar_or_sorry"},
    ),
    (
        "verify_unclosed",
        json.dumps({"id": "verify_unclosed", "cmd": "verify", "stmt": PROP, "proof": "intro n"},
                   ensure_ascii=False),
        {"result_ok": False, "reason": "unclosed_goals"},
    ),
    (
        "verify_bad",
        json.dumps({"id": "verify_bad", "cmd": "verify", "stmt": PROP, "proof": "intro n\nexact 1"},
                   ensure_ascii=False),
        {"result_ok": False, "reason": "type_error"},
    ),
    (
        "verify_missing_field",
        json.dumps({"id": "verify_missing_field", "cmd": "verify", "stmt": PROP}, ensure_ascii=False),
        {"error": "invalid_params"},
    ),
    ("unknown_cmd", '{"id":"unknown_cmd","cmd":"solve"}', {"error": "bad_request"}),
    # 下面两条不是合法请求（前者连 JSON 都不是，后者不是对象）：
    # 服务端**立刻**回一条 id=null 的 bad_request，不进批，因此这里单独断言
    ("bad_json", '{"id":"bad_json","cmd":', {"immediate_error": "bad_request"}),
    ("not_object", "[1,2,3]", {"immediate_error": "bad_request"}),
]

FLUSH_ID = "flush_1"
TRAILING = (
    "eof_flush",
    json.dumps({"id": "eof_flush", "cmd": "check", "stmt": PROP}, ensure_ascii=False),
)

# Mathlib 模式专属用例：只有显式声明了 `SGSLEAN_IMPORTS=Mathlib` 才跑
# （每条都要等一次完整 Mathlib 导入，很慢；离线模式跑它们会直接判 unknown_identifier）。
MATHLIB_REQUESTS: list[tuple[str, str, dict]] = [
    (
        "check_mathlib_symbol",
        json.dumps({"id": "check_mathlib_symbol", "cmd": "check",
                    "stmt": "∀ (n : Nat), Even (n * (n + 1))"}, ensure_ascii=False),
        {"result_ok": True, "reason": "ok"},
    ),
    (
        "verify_ring",
        json.dumps({"id": "verify_ring", "cmd": "verify",
                    "stmt": "∀ (x y : ℝ), (x + y) ^ 2 = x ^ 2 + 2 * x * y + y ^ 2",
                    "proof": "intro x y\nring"}, ensure_ascii=False),
        {"result_ok": True, "reason": "ok", "finalChecked": True},
    ),
    (
        # omega 证不出这条（会如实给出反例约束），用来验证"失败也是有效结论"
        "verify_omega_fails",
        json.dumps({"id": "verify_omega_fails", "cmd": "verify",
                    "stmt": "∀ (n : Nat), 3 ∣ n ^ 3 + 2 * n",
                    "proof": "intro n\nomega"}, ensure_ascii=False),
        # 判定码不稳定：omega 失败时实测既可能是 `type_error`（给出反例约束），
        # 也可能是 `mvar_or_sorry`（它内部留下了 sorryAx）。两者都表示"拒"。
        {"result_ok": False, "reason_any": ["type_error", "mvar_or_sorry"]},
    ),
]

if MATHLIB_MODE:
    REQUESTS = REQUESTS + MATHLIB_REQUESTS


def run_server(lines: list[str], timeout: float = 1800.0) -> tuple[list[str], str, float]:
    payload = "\n".join(lines) + "\n"
    started = time.perf_counter()
    proc = subprocess.run(
        [LAKE, "exe", "sgslean-server"],
        cwd=str(SGSLEAN),
        input=payload.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    elapsed = time.perf_counter() - started
    return (
        proc.stdout.decode("utf-8", errors="replace").splitlines(),
        proc.stderr.decode("utf-8", errors="replace"),
        elapsed,
    )


def main() -> int:
    requests = [line for _, line, _ in REQUESTS]
    batched = [r for r in REQUESTS if "immediate_error" not in r[2]]
    immediate = [r for r in REQUESTS if "immediate_error" in r[2]]
    expected_count = len(batched) + len(immediate) + 1 + 1  # + flush 响应 + EOF 那条
    lines = requests + [json.dumps({"id": FLUSH_ID, "cmd": "flush"}), TRAILING[1]]

    stdout_lines, stderr_text, elapsed = run_server(lines)
    failures: list[str] = []
    records: list[dict] = []

    # ① 协议纯度：每一行都必须是 JSON，且行数恰好等于期望
    parsed: list[dict] = []
    for idx, line in enumerate(stdout_lines):
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError as exc:
            failures.append(f"stdout 第 {idx + 1} 行不是 JSON（协议被污染）: {line[:200]!r} ({exc})")
    if len(stdout_lines) != expected_count:
        failures.append(f"stdout 行数应为 {expected_count}，实际 {len(stdout_lines)}")

    by_id = {str(item.get("id")): item for item in parsed if isinstance(item, dict)}

    # ② 逐条判定语义
    for rid, _line, want in REQUESTS:
        if "immediate_error" in want:
            continue
        item = by_id.get(rid)
        if item is None:
            failures.append(f"{rid}: 没有响应")
            continue
        if "error" in want:
            got = (item.get("error") or {}).get("code")
            if item.get("ok") is not False or got != want["error"]:
                failures.append(f"{rid}: 期望 error={want['error']}，实际 ok={item.get('ok')} error={got}")
        else:
            result = item.get("result") or {}
            if item.get("ok") is not True:
                failures.append(f"{rid}: 协议层应为 ok=true，实际 {item.get('ok')} ({item.get('error')})")
            elif "result_ok" in want and result.get("ok") is not want["result_ok"]:
                failures.append(f"{rid}: 判定 ok 应为 {want['result_ok']}，实际 {result.get('ok')}"
                                f"（reason={result.get('reason')}）")
            elif "reason" in want and result.get("reason") != want["reason"]:
                failures.append(f"{rid}: reason 应为 {want['reason']}，实际 {result.get('reason')}"
                                f"（detail={str(result.get('detail'))[:160]}）")
            elif "reason_any" in want and result.get("reason") not in want["reason_any"]:
                failures.append(f"{rid}: reason 应属于 {want['reason_any']}，实际 {result.get('reason')}"
                                f"（detail={str(result.get('detail'))[:160]}）")
            elif "isProp" in want and result.get("isProp") is not want["isProp"]:
                failures.append(f"{rid}: isProp 应为 {want['isProp']}，实际 {result.get('isProp')}")
            elif "mathlib" in want and result.get("mathlib") is not want["mathlib"]:
                failures.append(f"{rid}: mathlib 应为 {want['mathlib']}，实际 {result.get('mathlib')}"
                                f"（importedModules={result.get('importedModules')}）")
            elif "finalChecked" in want and result.get("finalChecked") is not want["finalChecked"]:
                failures.append(f"{rid}: finalChecked 应为 {want['finalChecked']}，实际 {result.get('finalChecked')}")
            elif "field" in want:
                key, value = want["field"]
                if result.get(key) != value:
                    failures.append(f"{rid}: result.{key} 应为 {value}，实际 {result.get(key)}")
            records.append({"id": rid, "result": result})

    # 立刻回的那几条：id 一定是 null（非法行里取不到 id），且必须是 bad_request
    null_id = [item for item in parsed if isinstance(item, dict) and item.get("id") is None]
    if len(null_id) != len(immediate):
        failures.append(f"id=null 的响应应有 {len(immediate)} 条（非法请求行），实际 {len(null_id)}")
    for item in null_id:
        code = (item.get("error") or {}).get("code")
        if item.get("ok") is not False or code != "bad_request":
            failures.append(f"非法请求行应回 bad_request，实际 ok={item.get('ok')} code={code}")

    flush_item = by_id.get(FLUSH_ID)
    if flush_item is None or (flush_item.get("result") or {}).get("flushed") != len(batched):
        failures.append(f"{FLUSH_ID}: 应回报 flushed={len(batched)}，实际 {flush_item}")
    frontend_ms = (flush_item or {}).get("result", {}).get("frontend_ms")
    trailing_item = by_id.get(TRAILING[0])
    if trailing_item is None or (trailing_item.get("result") or {}).get("ok") is not True:
        failures.append(f"{TRAILING[0]}: EOF 自动 flush 未回响应：{trailing_item}")

    # ③ stderr 只允许放诊断；正常情况下应当是空的（泄漏会被上一步抓住，这里再确认一次）
    stderr_nonempty = bool(stderr_text.strip())

    RESULTS.mkdir(parents=True, exist_ok=True)
    report = {
        "elapsed_s": round(elapsed, 2),
        "frontend_ms": frontend_ms,
        "per_request_ms": round(frontend_ms / len(batched), 1) if isinstance(frontend_ms, int) and batched else None,
        "requests": len(REQUESTS) + 1,
        "batched_requests": len(batched),
        "immediate_errors": len(immediate),
        "stdout_lines": len(stdout_lines),
        "stderr_nonempty": stderr_nonempty,
        "stderr_head": stderr_text[:400],
        "failures": failures,
        "results": records,
    }
    (RESULTS / "server_smoke.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"[server-smoke] 批大小 {len(REQUESTS) + 1}，stdout {len(stdout_lines)} 行，{elapsed:.1f}s")
    print(f"[server-smoke] frontend_ms={frontend_ms}（含 {len(batched)} 条作业的整批 elaboration）")
    if stderr_nonempty:
        print(f"[server-smoke] 注意：stderr 非空（前 200 字）: {stderr_text[:200]!r}")
    if failures:
        print(f"[server-smoke] FAIL，{len(failures)} 处：")
        for f in failures:
            print("  -", f)
        return 1
    print(f"[server-smoke] PASS（报告写入 {RESULTS / 'server_smoke.json'}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
