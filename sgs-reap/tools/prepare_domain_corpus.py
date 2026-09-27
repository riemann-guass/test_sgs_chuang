"""Prepare the formal miniF2F data protocol without calling Lean or a model.

miniF2F valid is deterministically partitioned into disjoint C-build, C-measure and D.
miniF2F test remains the untouched, one-shot T split.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sgsr.data import normalize_sig  # noqa: E402

DATA = ROOT / "data"
VALID = DATA / "minif2f_valid.jsonl"
TEST = DATA / "minif2f_test.jsonl"
OUTPUTS = {
    "C-build": DATA / "minif2f_c_build.jsonl",
    "C-measure": DATA / "minif2f_c_measure.jsonl",
    "D": DATA / "minif2f_dev.jsonl",
}
MANIFEST = DATA / "dataset_manifest.json"
DEFAULT_SEED = "sg-lean-minif2f-v1"


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def family_of(target_id: str) -> str:
    """Coarse source family for balance diagnostics; it does not affect membership."""
    return target_id.replace("-", "_").split("_", 1)[0]


def partition_score(row: dict, seed: str) -> str:
    identity = f"{row.get('id', '')}\0{normalize_sig(str(row.get('statement') or ''))}"
    return hashlib.sha256(f"{seed}\0{identity}".encode("utf-8")).hexdigest()


def prepare(valid_rows: list[dict], test_rows: list[dict], seed: str) -> tuple[dict[str, list[dict]], dict]:
    partitions: dict[str, list[dict]] = {name: [] for name in OUTPUTS}
    seen_ids: set[str] = set()
    seen_statements: set[str] = set()
    normalized: list[dict] = []
    for raw in valid_rows:
        target_id = str(raw.get("id") or "").strip()
        statement = normalize_sig(str(raw.get("statement") or ""))
        if not target_id or not statement:
            raise ValueError("miniF2F valid contains a row without id or statement")
        if target_id in seen_ids or statement in seen_statements:
            raise ValueError(f"duplicate valid target: {target_id}")
        seen_ids.add(target_id)
        seen_statements.add(statement)
        normalized.append({
            "id": target_id,
            "statement": statement,
            "source": "miniF2F/valid",
            "family": family_of(target_id),
        })

    ranked = sorted(normalized, key=lambda row: partition_score(row, seed))
    build_n = len(ranked) // 2
    measure_n = (len(ranked) - build_n) // 2
    for index, row in enumerate(ranked):
        role = "C-build" if index < build_n else (
            "C-measure" if index < build_n + measure_n else "D"
        )
        partitions[role].append(row | {
            "source_corpus": "MF_VALID_C" if role.startswith("C-") else "MF_VALID_D",
            "partition": role,
        })

    test_ids = {str(row.get("id") or "") for row in test_rows}
    test_statements = {normalize_sig(str(row.get("statement") or "")) for row in test_rows}
    if seen_ids & test_ids or seen_statements & test_statements:
        raise ValueError("miniF2F valid and test overlap by id or normalized statement")

    membership = [set(row["id"] for row in partitions[name]) for name in OUTPUTS]
    if any(membership[i] & membership[j] for i in range(3) for j in range(i + 1, 3)):
        raise ValueError("derived miniF2F partitions overlap")
    if set().union(*membership) != seen_ids:
        raise ValueError("derived miniF2F partitions do not cover valid")

    manifest = {
        "schema": 2,
        "protocol": "miniF2F-valid->C-build/C-measure/D; miniF2F-test->T",
        "algorithm": "sort-by-sha256(seed\\0id\\0normalized_statement), then exact 50/25/25 slices",
        "seed": seed,
        "ratios": {"C-build": "50%", "C-measure": "25%", "D": "25%"},
        "source": {
            "valid": {"path": "data/minif2f_valid.jsonl", "rows": len(valid_rows)},
            "test": {"path": "data/minif2f_test.jsonl", "rows": len(test_rows)},
        },
        "partitions": {},
        "checks": {
            "valid_partition_id_overlap": 0,
            "valid_test_id_overlap": 0,
            "valid_test_statement_overlap": 0,
            "valid_coverage": len(set().union(*membership)),
        },
    }
    for role, rows in partitions.items():
        manifest["partitions"][role] = {
            "path": str(OUTPUTS[role].relative_to(ROOT)).replace("\\", "/"),
            "rows": len(rows),
            "families": dict(sorted(Counter(row["family"] for row in rows).items())),
        }
    manifest["partitions"]["T"] = {"path": "data/minif2f_test.jsonl", "rows": len(test_rows)}
    return partitions, manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="deterministically partition miniF2F valid")
    parser.add_argument("--valid", default=str(VALID))
    parser.add_argument("--test", default=str(TEST))
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument("--manifest", default=str(MANIFEST))
    args = parser.parse_args()

    valid_path = Path(args.valid)
    test_path = Path(args.test)
    valid_rows = read_jsonl(valid_path)
    test_rows = read_jsonl(test_path)
    partitions, manifest = prepare(valid_rows, test_rows, args.seed)
    manifest["source"]["valid"]["sha256"] = file_hash(valid_path)
    manifest["source"]["test"]["sha256"] = file_hash(test_path)
    for role, rows in partitions.items():
        path = OUTPUTS[role]
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        manifest["partitions"][role]["sha256"] = file_hash(path)

    manifest_path = Path(args.manifest)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    counts = {role: len(rows) for role, rows in partitions.items()}
    print(f"[dataset] miniF2F valid {len(valid_rows)}: {counts}; T {len(test_rows)}")
    print(f"[dataset] manifest {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
