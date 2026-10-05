"""Optional LIVE quality check of canon.py (not a unit test): runs tests/fixtures/canon_cases.json through the REAL LM Studio
embeddings (+ the local LLM for the grey zone) in a throw-away registry and prints precision / recall and every mistake.

    python canon_check.py              # embeddings + LLM (needs LM Studio on :1234 with text-embedding-bge-m3)
    python canon_check.py --offline    # deterministic fallback only (no LM Studio)

Both modes end with the per-method tally and the '임베딩 단계: 사용됨 / 사용 불가' line (the same one the Mapping sheet shows).
"""
import collections
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import canon  # noqa: E402


def main() -> int:
    offline = "--offline" in sys.argv
    cases = json.loads((ROOT / "tests" / "fixtures" / "canon_cases.json").read_text(encoding="utf-8"))["cases"]
    with tempfile.TemporaryDirectory() as d:
        shutil.copy(ROOT / "data" / "canon_seed.json", d)
        cz = canon.Canonicalizer(d, offline=offline, persist=False)
        cz.begin()
        methods = collections.Counter()
        tp = fp = fn = 0
        bad = []
        t0 = time.time()
        for case in cases:
            vals = case.get("values") or [None] * len(case["labels"])
            fun = cz.canonicalize_item if case["space"] == "item" else cz.canonicalize
            res = [fun(lab, category=case["category"], value=v) for lab, v in zip(case["labels"], vals)]
            methods.update(r.method for r in res)
            for i in range(len(res)):
                for j in range(i + 1, len(res)):
                    same = res[i].id == res[j].id
                    if case["same"] and same:
                        tp += 1
                    elif case["same"]:
                        fn += 1
                        bad.append(f"MISSED  {case['labels'][i]!r} / {case['labels'][j]!r} -> {res[i].id} / {res[j].id}")
                    elif same:
                        fp += 1
                        bad.append(f"MERGED  {case['labels'][i]!r} / {case['labels'][j]!r} -> {res[i].id}")
        print("\n".join(bad))
        print(f"cases {len(cases)}  pair precision {tp / max(tp + fp, 1):.3f}  recall {tp / max(tp + fn, 1):.3f}  "
              f"(tp {tp} fp {fp} fn {fn})  {time.time() - t0:.1f}s")
        print("resolved by:", dict(methods), "| embed calls", cz.stats["embed_calls"], "llm calls", cz.stats["llm_calls"])
        print(cz.run_stats()["line_ko"])  # embedding stage visibility: used / unavailable (deterministic fallback) / offline
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
