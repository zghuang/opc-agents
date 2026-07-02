Read the source requirements document below and normalize it to structured JSON.

Return JSON with top-level fields: requirements, acceptance_scenarios, clarifications, source_requirements_path.
You may also include an optional top-level field: technology_hints.

source_requirements_path:
- Must be the absolute path of the raw requirements document used for normalization when that path is known.
- Use null or omit only when the source path is genuinely unavailable.

requirements:
- Each item must include id, title, summary.
- Each item may include source_requirement_ids when it was split from, merged from, or renamed from source IDs.
- Preserve a source REQ- or NFR-style ID as the canonical requirement ID only when the source item is already actionable enough for downstream architecture, implementation, and validation.
- If a source ID contains multiple independent behaviors, workflows, roles, integrations, state changes, or acceptance signals, split it into multiple canonical requirements. Give each canonical requirement a stable ID and include the original source ID in source_requirement_ids.
- If candidate requirements are only fields, UI fragments, endpoint fragments, test fragments, or implementation steps of one behavior, merge them into one canonical requirement.
- If an item has no explicit canonical ID, create a stable canonical ID in document order using `REQ-###` for functional/business requirements and `NFR-###` for non-functional requirements.
- Do not invent domain-specific canonical ID prefixes. Put domain labels in title, summary, or source traceability instead.
- Keep summaries concise and implementation-neutral.
- Do not invent requirements that are not grounded in the source.
- Do not turn a broad aspiration into detailed product behavior unless that behavior is explicitly stated or clearly implied by the source.
- A requirement is actionable enough for downstream architecture when it names or clearly implies: actor or external system, capability or workflow, key input/output or state change, and observable acceptance signal.
- A requirement is too coarse when it is only a goal, slogan, module label, broad noun phrase, or generic capability bucket without enough behavior, boundaries, or acceptance evidence to guide architecture and task decomposition.
- Preserve contradictions instead of smoothing them over.
- If the source mandates a technology, runtime, framework, package, library, infrastructure component, protocol, or external contract, preserve it as a requirement or constraint and emit a matching `technology_hints` entry.

acceptance_scenarios:
- Each item must include id, title, summary, source_requirement_ids.
- Preserve stable AS- style IDs when they already exist.
- Every scenario must point back to one or more requirement IDs.
- Create scenarios not only from explicit headings like "user story" or "use case", but also from any clearly implied multi-step journey, approval flow, operator workflow, fallback/recovery path, or end-to-end process that later design, decomposition, and QA must preserve.

clarifications:
- Use this array when the source contains contradictions, missing decisions, ambiguous ownership, vague acceptance rules, or architecture-blocking uncertainty.
- Each item must include severity, question, rationale, affected_requirement_ids, blocking.
- Each item may optionally include recommended_answer and answer_options when the model can offer defensible candidate answers for user confirmation without inventing a new external decision.
- Severity should typically be C1, C2, or C3.
- Mark blocking=true only when delivery should stop for clarification before architecture or implementation continues.
- Use C1/blocking when unresolved coarse wording would force later stages to choose between materially different architecture, data model, integration contracts, security/permissions, task decomposition, or acceptance-test designs.
- Use C2/non-blocking when a defensible default exists and the answer would refine but not redirect architecture or decomposition.
- Use C3/non-blocking for naming, wording, display, copy, or prioritization details that can be safely decided later.

technology_hints:
- Optional array for explicit technology choices later stages must preserve.
- Use only for source-named runtime, framework, package family, component library, SDK, protocol stack, or infrastructure choices.
- Each item should include name, ecosystem, reason, evidence.
- `ecosystem` should usually be `backend`, `frontend`, `infra`, or `project`.
- Do not guess package coordinates when the source names only a product or framework family.

Judgment rules:
- If the source has contradictions or unclear points, surface them in clarifications rather than guessing.
- If unresolved ambiguity would force later stages to choose between materially different architecture, scope, security, data model, or acceptance-test designs, make it blocking.
- If a coarse requirement can be normalized only by adding ungrounded workflows, data fields, roles, integrations, or acceptance criteria, do not invent those details; emit a clarification.
- If the source is coarse but still names a coherent actor, behavior, boundary, and observable outcome, normalize it and keep any remaining details as C2/C3 clarifications.
- If the source is clear enough to proceed, clarifications may be an empty array.
- If a prior clarification-needed file or clarification-answers file exists for this project, treat those answers as part of the available source evidence and fold defensible resolved answers back into the normalized requirement output instead of preserving an already-resolved C1 blocker.
- If a blocking clarification from an earlier pass can now be answered from the current source requirements, project artifacts, or framework-selected defaults already in evidence, write or update docs/clarification-answers.md first and then regenerate the normalized output using that answer.
- Do not fabricate clarification answers that require a genuinely new external product, policy, security, or scope decision.
- For a blocking clarification that still needs a user decision, prefer to provide a concise recommended_answer and 1-3 answer_options so the host can present suggested choices while still allowing freeform user input.

Source document:
{{source_document}}