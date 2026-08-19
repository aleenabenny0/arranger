"""Print a run log in a readable form.

    python show_run.py runs/bohemian2.json

Exists because inspecting a run is something you do constantly, and one-liners
with nested quotes are a fight on Windows. It also answers the question that
actually matters after a run: not "did it succeed" but "what did it decide,
and did the decisions differ across the piece?"
"""

import json
import sys
from pathlib import Path

if len(sys.argv) < 2:
    print("usage: python show_run.py runs/somefile.json")
    raise SystemExit(2)

run = json.loads(Path(sys.argv[1]).read_text())

print(f"{run['title']}")
print(f"source had {run['baseline_hard']} hard violations")
print()

print("ATTEMPTS")
for a in run["attempts"]:
    sections = len(a["plan"]["sections"]) if a.get("plan") else 0
    if a.get("error"):
        print(f"  {a['number']}: rejected - {a['error'][:70]}")
    else:
        print(
            f"  {a['number']}: {a['hard']:>4} hard, {a['strain']:>3} strain, "
            f"{sections} sections, {a['seconds']:.1f}s"
        )
print()

plan = run.get("best_plan")
if not plan:
    print("no usable plan produced")
    raise SystemExit(1)

print(f"BEST PLAN — {run['best_hard']} hard violations")
print(f"  target skill: {plan.get('target_skill')}")
print(f"  {len(plan['sections'])} sections")
print()
print(f"  {'bars':>12}  {'pattern':<14} {'voices':>6} {'fold':>5} {'shift':>6}  label")
for s in plan["sections"]:
    bars = f"{s['start_bar']}-{s['end_bar']}"
    print(
        f"  {bars:>12}  {s['lh_pattern']:<14} {s['lh_voices']:>6} "
        f"{s.get('melody_fold_window', 0):>5} {s.get('melody_shift', 0):>6}  "
        f"{s.get('label', '')}"
    )

# The question the whole M5 entry turned on: did the model treat different
# regions differently? Uniform settings mean it found nothing brute force
# could not, however good the score.
varied = {
    field: len({s.get(field) for s in plan["sections"]})
    for field in ("lh_pattern", "lh_voices", "melody_fold_window", "melody_shift")
}
print()
print("VARIATION ACROSS SECTIONS (1 = uniform, brute force could match it)")
for field, count in varied.items():
    print(f"  {field:<20} {count} distinct value(s)")

if plan.get("reductions"):
    print()
    print("REDUCTIONS")
    for r in plan["reductions"]:
        print(f"  bars {r['start_bar']}-{r['end_bar']} {r['kind']}: {r.get('rationale','')}")

if plan.get("notes"):
    print()
    print("MODEL'S NOTES")
    print(f"  {plan['notes']}")

print()
print(f"tokens: {run['input_tokens']} in, {run['output_tokens']} out")
print("escalated" if run["escalated"] else "playable")
