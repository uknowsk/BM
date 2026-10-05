"""Live check (not a unit test): bind the sample cooking form to the GE PTS9200SNSS and KitchenAid KOEC730SWH fixtures
using the REAL LM Studio embeddings and LLM, and print how each form item was matched.

Usage:
  python template_check.py                 live bind + per-item table + bind stats (embeddings / LLM reachable, calls, cache hits)
  python template_check.py --offline       embeddings / LLM disabled, for comparison
  python template_check.py --probe ITEM    diagnostic: the competitor rows nearest to ITEM by the real embeddings (cosine, section, values)
"""
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import compare_model  # noqa: E402
import make_template_sample  # noqa: E402
import template  # noqa: E402
import test_ge_us  # noqa: E402
import test_kitchenaid_us  # noqa: E402


def fixtures():
    ge, ka = test_ge_us._rec("wall_double"), test_kitchenaid_us._rec("KOEC730SWH")
    return [ge[0], ka[0]], list(ge[1]) + list(ka[1])  # fixtures return (product, raw_specs[, docs]); index 1 is the RawSpec list


def probe(items: list[str]) -> None:
    import llm
    products, raw = fixtures()
    rows = compare_model.build_compare(products, None, raw, [], [[] for _ in products]).get("cooking", [])
    labels = [(r.get("key_en") or r.get("key_ko") or "", r) for r in rows]
    vecs = llm.embed(items + [lab for lab, _ in labels])
    if not vecs:
        print("embeddings unavailable (is LM Studio running with an embedding model?)")
        return
    cos = lambda a, b: sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))  # noqa: E731
    q, c = vecs[:len(items)], vecs[len(items):]
    print("compare rows:", len(rows))
    for qi, name in enumerate(items):
        print(f"\n== {name}")
        for score, lab, r in sorted(((cos(q[qi], c[i]), labels[i][0], labels[i][1]) for i in range(len(labels))), key=lambda t: -t[0])[:8]:
            print(f"  {score:.3f}  {lab[:46]:46} section={r.get('section')}  values={[str(v)[:28] for v in r.get('values', [])]}")


def main() -> None:
    if "--probe" in sys.argv:
        probe(sys.argv[sys.argv.index("--probe") + 1:] or ["마이크로웨이브"])
        return
    offline = "--offline" in sys.argv
    sample = ROOT / "data" / "template_sample.xlsx"
    if not sample.exists():
        make_template_sample.main()
    tpl = template.parse_form(sample.read_bytes(), sample.name)
    print(f"form: {len(tpl.items)} items, warnings: {tpl.warnings[:3]}")
    products, raw = fixtures()
    print("products:", [(p.brand, p.model_number) for p in products])
    kwargs = {"embed_fn": None, "llm_fn": None} if offline else {}
    t = time.time()
    bound = template.bind(tpl, products, raw, [], [[] for _ in products], cache_path=ROOT / "data" / "templates" / "_check_cache.json", **kwargs)
    print(f"bind took {time.time() - t:.1f}s ({'offline' if offline else 'LIVE embeddings + LLM'})")
    print("counts:", bound.counts())
    print("match methods:", bound.stats["methods"])
    print(bound.stats["line_ko"])
    for bs in bound.sheets:
        print(f"\n== sheet {bs.sheet.name}")
        for row in bs.rows:
            cells = [f"{c.status[:5]}:{str(c.display or c.status)[:22]}" for c in row.cells]
            flag = "REVIEW" if row.needs_review else "      "
            print(f"  {flag} {str(row.item.label)[:34]:34} <- {str(row.matched_label or '-')[:30]:30} [{row.method}/{row.score}] " + " | ".join(cells))
        print("uncovered:", len(bs.uncovered))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
