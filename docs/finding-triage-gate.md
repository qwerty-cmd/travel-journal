# Finding Triage Gate

**This is the canonical location of the gate. The rule text below is user-authored and fixed —
transcribe it, cite it, link to it; do not redraft, condense, reorder or improve it.** If it needs to
change, that is a user decision, not an agent edit.

It applies to every agent that can discover an issue — `qa` classifying findings in its report,
`dev` deciding whether a finding belongs in the patch it is writing, `ba` deciding whether a finding
becomes a task, and the orchestrator deciding what gets dispatched next.

Why it exists, and the reasoning it replaced, are recorded in `docs/decision-log.md` **Entry 12**.
Read that entry before arguing for an exception — the exception that gets reached for every time
("it's a small follow-up, just build it now") is precisely the one the gate was written to refuse,
and the evidence against it was measured, not asserted.

---

A discovered issue is not automatically work. Before implementing any finding, classify it using the gate below.

**1. CURRENTLY BROKEN** — Does the current implementation violate an explicit specification requirement, invariant, security/access-control rule, data-integrity requirement, or existing behavioural contract? **YES → FIX NOW.** The violation must be supported by concrete evidence from the current code, tests, specification, or runtime path.

**2. CURRENTLY OBSERVABLE** — If the system is not currently violating a requirement, does any client, test, handler, integration, queue, background job, or other runtime path that exists today actually exercise or depend on the affected behaviour? **YES → FIX NOW.** "Could consume it" is not sufficient. There must be a current consumer.

**3. TRIGGERED DEBT** — If neither condition above applies, is there a concrete future event after which the finding must be addressed? Examples: before the first stops handler; with the first write endpoint; when the first route-level `dependencies=[...]` kwarg is introduced; when the next endpoint requiring this convention is added. **YES → FILE DEBT WITH THE TRIGGER. DO NOT IMPLEMENT NOW.**

**4. ORDINARY DEBT** — If there is no current violation, current consumer, or concrete promotion trigger: **FILE DEBT. DO NOT IMPLEMENT NOW.**

**Evidence Requirement.** Every QA finding must include: `Finding:` / `Evidence:` / `Gate classification:` / `Current consumer: <specific consumer or none>` / `Promotion trigger: <specific trigger or none>`. The classification must be based on observable evidence, not hypothetical future behaviour. Use these distinctions strictly: `Could happen ≠ Does happen`, `Could break ≠ Is broken`, `Could consume ≠ Currently consumes`, `Future risk ≠ Current defect`. Potential future breakage is not current breakage.

**Scope Rule.** A finding discovered while completing task X does not automatically become work for task X. The sequence is: one task → one patch → verify → next story. For every newly discovered finding: (1) apply the gate; (2) if CURRENTLY BROKEN or CURRENTLY OBSERVABLE, make the smallest necessary fix; (3) if TRIGGERED DEBT or ORDINARY DEBT, record it and return to the original task; (4) do not investigate or implement deferred findings unless their promotion trigger fires or new evidence establishes a current violation/consumer. Do not turn a discovered issue into an unbounded investigation.

**Stop Condition.** Once a finding has been classified as debt, stop investigating it. Do not: modify production code; add speculative tests; refactor surrounding code; redesign the affected behaviour; investigate hypothetical consumers; explore unrelated edge cases — unless evidence emerges that the finding satisfies Gate 1 or Gate 2. The correct outcome of a finding can be: "Not relevant to the current system. File it and move on."

**QA Classification.** QA should classify each finding against this gate in its report. QA classification is evidence, not unquestionable authority. If implementation disagrees with QA's classification, the disagreement must be explicit: `QA classification:` / `Implementation classification:` / `Reason for disagreement:` / `Evidence:`. Do not silently re-triage and begin implementation.

**Debt Promotion.** Debt is reviewed at story boundaries, not continuously. A debt item carries its promotion condition with it. A triggered debt is promoted only when its stated trigger occurs. Do not use calendar-based review as a substitute for a concrete trigger unless the team explicitly chooses one.

---

## Where filed debt lives

A filed debt item is a task in `docs/progress.json` with its detail — evidence, gate classification,
current consumer, promotion trigger — in `docs/progress-notes.md` under the task's ID. The promotion
trigger travels with the item: it goes in the note, in the words that let a future reader recognise
the trigger when it fires. "Revisit later" is not a trigger; "when the stops router registers
`GET`/`POST` on that path" is.

An explicit disagreement with a QA classification is a contested call. It goes in
`docs/decision-log.md` with both classifications stated, per that file's convention.
