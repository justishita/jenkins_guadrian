# Guardrails: what they do, how well, and where they stop

**Owner:** P2 evaluation (Week 8) of `common/guardrails.py` (P3's module, hardened in Week 8, see "What changed"). Covers course
criterion 8. The numbers come from `python scripts/evaluate_guardrails.py` and are reported as measured, including the bad ones.

## Three layers

| Layer | Function | Protects against |
|---|---|---|
| **Input** | `screen_untrusted_text` | prompt injection and abuse in text an attacker or an angry person controls: build logs, commit messages, PR comments, job names. Matches are blanked, the text is wrapped in `<untrusted_data>` so a model is told it is data, and a flag is posted to the blackboard. Evidence is never dropped. Clean text is passed on exactly as it was. |
| **Output** | `evidence_guardrail` (a CrewAI task guardrail) | an agent writing a secret, advising something forbidden (skip or delete tests, bypass security, turn off the policy gate), or a confidence outside 0-1. The agent is asked to retry. |
| **Tool** | `validate_tool_output` | a tool returning something that is not its declared Pydantic model (missing field, wrong type, prose or truncated JSON where an object belongs). |

These are the cheap, always-on checks. The expensive protections are structural: agents run with read-only tools, the policy gate
(OPA) checks every proposed change, a human approves it, and re-validation checks the result.

## How it was tested (variance in structure and in content)

`tests/security/adversarial_cases.yaml` (the **dev** set, 78 cases) and `adversarial_cases_heldout.yaml` (the **held-out** set,
50 cases) hold the same kinds of case in different forms:

- **content** variants: the same attack in other words, other languages, or obfuscated (zero-width characters, full-width letters,
  look-alike letters from another alphabet, leetspeak, spaced or dotted letters, base64);
- **structure** variants: the attack hidden in JSON, a markdown table or fence, an HTML comment, a YAML comment, a log line, a
  multi-line message;
- **personas**: an attacker, an angry developer, a harasser, an agent, a tool;
- **benign controls**: text that looks like an attack and must pass ("an idiot-proof wrapper", "report success rate: 98%",
  "Do not skip tests").

Expectations describe what a good guardrail should do, not what the code does.

**The dev set was used to design the patterns, so its score is a regression check and not an accuracy.** The held-out set was
written after the hardening and was not used to tune anything. It is the honest number.

## Results (`results/guardrails_eval.md`)

| | Attacks stopped | Benign text wrongly stopped |
|---|---|---|
| Original guardrails, dev set (`results/guardrails_eval_before_hardening.md`) | 36/61 = 59% | 4/17 |
| Hardened, dev set (contaminated: a regression check) | 61/61 = 100% | 0/17 |
| **Hardened, held-out set (the honest estimate)** | **7/33 = 21%** (95% CI 11-38%) | **4/17 = 24%** (95% CI 10-47%) |

What the numbers say, plainly:

1. **Pattern matching does not generalise.** Patterns written against one list of attacks caught almost none of a fresh list. The
   100% above is the patterns remembering their own homework.
2. **Hiding the attack in a container makes no difference** (structure variants were all caught): a regex does not care where text
   sits. Rewording is what defeats it.
3. **What helped in general** (and is worth keeping): normalising text before matching (zero-width, full-width, look-alike letters,
   non-breaking spaces), a leetspeak reading, decoding base64, and not rejecting advice that is *negated* ("Do not skip tests").
   These turn whole families of obfuscation into one match rather than adding another phrase.
4. **Three false positives on the held-out set come from patterns added in Week 8** (`New instructions:`, `Call the X tool with`,
   `From now on, you will`): they match ordinary README text. A fourth is the older `remove tests?` pattern matching
   `Remove tests/__pycache__`. Tightening them now would turn the held-out set into a dev set, so they are left, reported, and the
   cost is small (a matched span is blanked, the text is still wrapped and flagged, nothing is dropped).
5. Obfuscations still missed: spaced letters (`i g n o r e`), rot13, URL-encoding, hyphenated letters. Toxicity is the weakest
   area: a word list cannot find insults it does not contain (muppet, pathetic, "nobody wants you here", "go to hell").

## What this means for the system

Do not rely on the input screen as a wall. It reduces noise and catches the lazy attack; a determined one gets through. The system is
designed so that getting through is not enough:

- an injected instruction in a log can change an agent's *opinion* but cannot make it act: agents have read-only tools;
- an opinion becomes a change only through the policy gate and a human approval, and the report must be **complete and confident**
  (an investigation with a missing or failed agent is never actionable);
- the output guardrail blocks the most dangerous advice and every secret;
- everything is in the audit trail (`docs/audit_workflow.md`).

The honest residual risk is a successful injection that skews a diagnosis *and* a reviewer who approves a bad fix without reading it.

## What changed in `common/guardrails.py` (P3 to review)

Additive, with every existing test still passing (`tests/security`, `tests/jenkins_agent`, `tests/code_agent`):
`normalize_text`, base64 decoding, a leetspeak reading, more injection / toxicity / forbidden-advice patterns, a narrow
negation rule, and `report success` no longer matches `report success rate`. Clean text is still returned untouched.

## Next steps if this matters more

1. Move the held-out cases into the dev set and write a fresh held-out set (the held-out score only counts once).
2. Replace the toxicity word list with a classifier or a moderation API; keep the regexes for the cases that must never pass.
3. Add an LLM-as-judge screen for injections and measure it on the same sets (costs quota; not done).
4. Narrow the three new patterns that fire on README text.

## Reproduce

```bash
python scripts/evaluate_guardrails.py     # results/guardrails_eval.md and .json
python -m pytest tests/security -q
```
