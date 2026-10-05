"""Run all brand scrapers and write output/refrigerator_specs.xlsx."""
import importlib
import sys
import traceback

from excel_writer import ROOT, write_excel

BRANDS = ("ge", "bosch")
OUTPUT = "output/refrigerator_specs.xlsx"
REQUIRED = ("price_usd", "width_in", "height_in", "depth_in", "capacity_total_cuft",
            "energy_kwh_year", "weight_lb")


def brand_status(product, modes) -> tuple[str, str]:
    """('ok'|'partial', detail) from still-missing required fields and mode count."""
    missing = [f for f in REQUIRED if getattr(product, f) is None]
    if not modes:
        missing.append("no modes")
    return ("partial", "missing: " + ", ".join(missing)) if missing else ("ok", "")


def main() -> int:
    products, documents, raw_specs, all_modes, run_log = [], [], [], [], []
    for name in BRANDS:
        try:
            module = importlib.import_module(name)  # lazy: one broken brand must not stop the other
            product, docs, specs = module.scrape()
        except Exception as exc:  # noqa: BLE001 - isolate per-brand failure
            traceback.print_exc()
            run_log.append((name, "failed", f"{type(exc).__name__}: {exc}"))
            print(f"[{name}] FAILED - {exc}")
            continue
        try:
            import modes as modes_mod
            modes = modes_mod.extract_modes(product, docs)
        except Exception as exc:  # noqa: BLE001 - modes are best-effort
            traceback.print_exc()
            print(f"[{name}] modes extraction failed - {exc}")
            modes = []
        products.append(product)
        documents.extend(docs)
        raw_specs.extend(specs)
        all_modes.extend(modes)
        status, detail = brand_status(product, modes)
        msg = f"{product.model_number}: {len(docs)} docs, {len(specs)} raw specs, {len(modes)} modes"
        if detail:
            msg += f" ({detail})"
        run_log.append((name, status, msg))
        print(f"[{name}] {status} - {msg}")

    failed = sum(1 for r in run_log if r[1] == "failed")
    target = ROOT / OUTPUT
    if failed == len(BRANDS):
        print(f"All brands failed; not overwriting {target}")
        return 1
    import pod
    path = write_excel(products, documents, raw_specs, target, run_log=run_log, modes=all_modes,
                       extra_sheets=lambda wb: pod.add_pod_sheets(wb, products))
    print(f"Wrote {path} ({len(products)} products, {len(all_modes)} modes)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
