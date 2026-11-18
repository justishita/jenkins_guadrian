"""Week 8: guardrail evaluation against an adversarial dataset with prompt variance.

**Owner:** P2. Runs ``tests/security/adversarial_cases.yaml`` through the three guardrail layers in ``common/guardrails.py``
and reports, per category and per kind (content variant, structure variant, benign control):

  * detection rate on attacks (with a 95% Wilson interval; the samples are small),
  * false-positive rate on benign controls,
  * every miss, in full. A miss is a finding, not a failure of the script.

Layers: ``input`` (``screen_untrusted_text``: logs, commits, comments going into a prompt), ``output`` (``evidence_guardrail``:
secrets, forbidden advice, confidence range) and ``tool`` (``validate_tool_output``: a tool must return its declared model).
The "variance" the assignment asks for is in the dataset: the same attack worded differently (content), hidden in a different
container (structure), obfuscated, spoken by different personas, and benign text that looks like an attack.

    python scripts/evaluate_guardrails.py                  # writes results/guardrails_eval.md and .json
    python scripts/evaluate_guardrails.py --out results/guardrails_eval_before.md --json results/guardrails_eval_before.json
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import yaml
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: "dev" was used to design the patterns (a regression check); "heldout" was written afterwards and never used to tune anything.
DATASETS = {
    "dev": ROOT / "tests" / "security" / "adversarial_cases.yaml",
    "heldout": ROOT / "tests" / "security" / "adversarial_cases_heldout.yaml",
}
DATASET = DATASETS["dev"]
#: What "the guardrail did its job" means for each layer: attacks must be stopped, benign text must pass.
ATTACK_EXPECT = {"flag", "reject", "invalid"}


class ToolReturn(BaseModel):
    """A stand-in for the model a real tool declares: a build summary."""

    job: str
    build: int
    result: str


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - half), min(1.0, centre + half))


def load_cases(paths: dict[str, Path] | None = None) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for split, path in (paths or DATASETS).items():
        for case in yaml.safe_load(path.read_text(encoding="utf-8"))["cases"]:
            if case["id"] in seen:
                raise ValueError(f"duplicate case id {case['id']}")
            seen.add(case["id"])
            cases.append({**case, "split": split})
    return cases


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    """Return what the guardrail did: ``stopped`` (flagged/rejected/invalid) plus detail for a miss."""
    from common.guardrails import evidence_guardrail, screen_untrusted_text, validate_tool_output

    layer = case["layer"]
    text = case.get("text", "")
    if "secret_parts" in case:  # a token-shaped string is never written out whole in the dataset (see the heldout file)
        text = text.replace("{secret}", "".join(case["secret_parts"]))
    if layer == "input":
        screened = screen_untrusted_text(text, source="adversarial_eval")
        wrapped = screened.text.startswith("<untrusted_data") and screened.text.rstrip().endswith("</untrusted_data>")
        return {"stopped": bool(screened.flags), "detail": ", ".join(screened.flags) or "no flag", "wrapped": wrapped}
    if layer == "output":
        pydantic = None
        if "confidence" in case:
            pydantic = SimpleNamespace(hypotheses=[SimpleNamespace(confidence=case["confidence"])])
        ok, feedback = evidence_guardrail(SimpleNamespace(raw=text, pydantic=pydantic))
        return {"stopped": not ok, "detail": "" if ok else str(feedback)[:120]}
    if layer == "tool":
        payload = case["payload"]
        decorated = validate_tool_output(ToolReturn)(lambda: payload)
        try:
            decorated()
        except Exception as error:  # noqa: BLE001 - any refusal counts; the type is not the contract
            return {"stopped": True, "detail": type(error).__name__}
        return {"stopped": False, "detail": "accepted"}
    raise ValueError(f"unknown layer {layer}")


def score(cases: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for case in cases:
        outcome = run_case(case)
        is_attack = case["expect"] in ATTACK_EXPECT
        rows.append(
            {
                "id": case["id"], "split": case["split"], "layer": case["layer"], "category": case["category"], "kind": case["kind"],
                "persona": case.get("persona", ""), "attack": is_attack, "stopped": outcome["stopped"],
                "correct": outcome["stopped"] == is_attack, "detail": outcome["detail"],
                "wrapped": outcome.get("wrapped", True),
                "text": str(case.get("text", case.get("payload")))[:140],
            }
        )

    def tally(selector) -> dict[str, Any]:
        picked = [r for r in rows if selector(r)]
        attacks = [r for r in picked if r["attack"]]
        benign = [r for r in picked if not r["attack"]]
        caught = sum(r["stopped"] for r in attacks)
        wrongly = sum(r["stopped"] for r in benign)
        return {
            "attacks": len(attacks), "caught": caught, "detection": caught / len(attacks) if attacks else None,
            "detection_ci": wilson(caught, len(attacks)) if attacks else None,
            "benign": len(benign), "false_positives": wrongly,
            "false_positive_rate": wrongly / len(benign) if benign else None,
            "false_positive_ci": wilson(wrongly, len(benign)) if benign else None,
        }

    by_category: dict[str, Any] = {}
    for category in sorted({r["category"] for r in rows}):
        by_category[category] = tally(lambda r, c=category: r["category"] == c)
    by_layer = {layer: tally(lambda r, l=layer: r["layer"] == l) for layer in ("input", "output", "tool")}
    by_kind = {kind: tally(lambda r, k=kind: r["kind"] == k) for kind in ("content", "structure", "control")}
    by_split = {split: tally(lambda r, x=split: r["split"] == x) for split in dict.fromkeys(r["split"] for r in rows)}
    by_persona = {p: tally(lambda r, x=p: r["persona"] == x) for p in sorted({r["persona"] for r in rows})}
    return {
        "rows": rows, "overall": tally(lambda r: True), "by_layer": by_layer, "by_category": by_category,
        "by_kind": by_kind, "by_persona": by_persona, "by_split": by_split,
        "misses": [r for r in rows if r["attack"] and not r["stopped"]],
        "false_positives": [r for r in rows if not r["attack"] and r["stopped"]],
        "unwrapped_input": [r["id"] for r in rows if r["layer"] == "input" and not r["wrapped"]],
    }


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def interval(pair: tuple[float, float] | None) -> str:
    return "" if pair is None else f" (95% CI {pair[0] * 100:.0f}-{pair[1] * 100:.0f}%)"


def table(title: str, groups: dict[str, Any]) -> list[str]:
    out = [f"| {title} | Attacks caught | Benign flagged wrongly |", "|---|---|---|"]
    for name, t in groups.items():
        caught = f"{t['caught']}/{t['attacks']} = {pct(t['detection'])}{interval(t['detection_ci'])}" if t["attacks"] else "-"
        fp = f"{t['false_positives']}/{t['benign']} = {pct(t['false_positive_rate'])}" if t["benign"] else "-"
        out.append(f"| {name} | {caught} | {fp} |")
    return out


def render_markdown(result: dict[str, Any], *, label: str = "") -> str:
    o = result["overall"]
    held = result["by_split"].get("heldout")
    lines = [
        f"# Guardrail evaluation{label}",
        "",
        f"_Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} by `scripts/evaluate_guardrails.py` on `tests/security/adversarial_cases.yaml`._",
        "",
        "## Headline",
        "",
        *(
            [
                f"- **Held-out set (the honest estimate): attacks stopped {held['caught']}/{held['attacks']} = {pct(held['detection'])}**"
                f"{interval(held['detection_ci'])}; benign text wrongly stopped {held['false_positives']}/{held['benign']} = "
                f"{pct(held['false_positive_rate'])}{interval(held['false_positive_ci'])}",
                "- The dev set was used to design the patterns, so its score is a regression check, not an accuracy (see the split table).",
            ]
            if held
            else []
        ),
        *(
            []
            if held
            else [
                f"- **Attacks stopped: {o['caught']}/{o['attacks']} = {pct(o['detection'])}**{interval(o['detection_ci'])}",
                f"- **Benign text wrongly stopped: {o['false_positives']}/{o['benign']} = {pct(o['false_positive_rate'])}**{interval(o['false_positive_ci'])}",
            ]
        ),
        f"- Untrusted text is always wrapped in `<untrusted_data>` (so a model is told it is data): "
        f"{'yes, every input case' if not result['unwrapped_input'] else 'NO: ' + ', '.join(result['unwrapped_input'])}",
        "",
        "> Small dataset: the intervals are wide on purpose. The misses below are the main result.",
        "",
        *table("Split", result["by_split"]),
        "",
        "_Do not quote a pooled figure: the dev split was tuned against, the held-out split was not._",
        "",
        *table("Layer", result["by_layer"]),
        "",
        *table("Category", result["by_category"]),
        "",
        *table("Variant kind", result["by_kind"]),
        "",
        *table("Persona", result["by_persona"]),
        "",
        f"## Attacks that got through ({len(result['misses'])})",
        "",
    ]
    for r in result["misses"]:
        lines.append(f"- `{r['id']}` [{r['split']}] ({r['layer']}, {r['category']}, {r['kind']}, {r['persona']}): `{r['text']!r}`")
    lines += ["", f"## Benign text wrongly stopped ({len(result['false_positives'])})", ""]
    for r in result["false_positives"]:
        lines.append(f"- `{r['id']}` [{r['split']}] ({r['layer']}): `{r['text']!r}` -> {r['detail']}")
    lines += [
        "",
        "## How to read this",
        "",
        "- Expectations are what a good guardrail should do, not what the code does today: a miss is a gap in the guardrail.",
        "- `content` = the same attack worded differently or obfuscated; `structure` = hidden in JSON, markdown, a comment or several lines;",
        "  `control` = benign text that must pass. A guardrail that stops everything scores 100% detection and a high false-positive rate.",
        "- Pattern matching cannot catch every paraphrase. The defence in depth is: untrusted text is wrapped and labelled, agents run",
        "  with read-only tools, outputs are validated, and the policy gate and a human approve any change.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=ROOT / "results" / "guardrails_eval.md")
    parser.add_argument("--json", type=Path, default=ROOT / "results" / "guardrails_eval.json")
    parser.add_argument("--label", default="")
    args = parser.parse_args(argv)

    result = score(load_cases())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_markdown(result, label=args.label), encoding="utf-8")
    args.json.write_text(json.dumps({k: v for k, v in result.items() if k != "rows"} | {"rows": result["rows"]}, indent=2, default=str), encoding="utf-8")
    for split, t in result["by_split"].items():
        print(f"{split:8s} attacks stopped {t['caught']}/{t['attacks']} = {pct(t['detection'])}; benign wrongly stopped {t['false_positives']}/{t['benign']}")
    for layer, t in result["by_layer"].items():
        print(f"  {layer:7s} caught {t['caught']}/{t['attacks']}, false positives {t['false_positives']}/{t['benign']}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
