Read the clarification document below and produce defensible resolution answers so planning can continue.

Return JSON with a top-level field: resolved.

resolved:
- Must be an array of objects.
- Each object must include question and answer.
- Reproduce each blocking clarification question exactly enough to match the source wording.
- Provide the narrowest defensible assumption that lets delivery continue.
- Prefer implementation-neutral product/contract assumptions over low-level technical guesses.
- When the source is silent, make the assumption explicit in the answer instead of hiding uncertainty.
- Answers must be concise but specific enough that architecture, task decomposition, and QA can consume them.

Judgment rules:
- Do not leave any C1 question unanswered.
- If there are non-blocking C2/C3 questions present, include answers only when they materially reduce architecture or task-planning ambiguity.
- Keep answers consistent with the existing requirements and acceptance scenarios.

Clarification document:
{{clarification_needed_md}}