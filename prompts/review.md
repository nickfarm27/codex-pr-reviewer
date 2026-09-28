# Requested-review workflow

Run one private, report-only review cycle.

An automation-triggered review turn is authorized only to prepare, inspect, complete, and privately hand off its assigned round. It must hard-stop after the private completion step: never draft or submit a GitHub review during that turn, regardless of earlier messages, accepted findings, or a prior round's GitHub action. Authorization to write to GitHub is consumed by the round it targeted. A later draft or request-changes action requires a fresh user-authored message, sent after the current round completed, that applies to that current round.

## 1. Prepare the assigned claim

A dispatched worker receives one exact claim key in its initiating prompt. Run:

```sh
python3 bin/review_queue.py prepare --key '<exact claim key>'
```

For a direct user-requested re-review inside the PR's already bound continuing task, run this instead:

```sh
python3 bin/review_queue.py prepare-rereview --repository 'OWNER/REPO' --number NUMBER
```

Use the returned `candidate.key` for the same completion flow below. This is the only supported path for a user-triggered re-review without a fresh GitHub review request; do not write a detached manual report.

Never run `claim` or `dispatch` from a worker, and never switch to another candidate if preparation fails. Review only the returned candidate's `diff_range` inside `checkout_path`.

The preparation result also includes `suggested_findings_path` and, on later rounds for the same PR, `previous_review`. Refresh the active lease after context gathering and again before writing the final report:

```sh
python3 bin/review_queue.py heartbeat --key '<exact claim key>'
```

The dispatcher supplies an initial task title. After resolving Linear context, correct it with the app's `set_thread_title` action if a different issue is clearly primary. Omit `threadId` so the action targets this task. Keep this format:

```text
ISSUE-ID · repository#PR · MMM DD HH:mm
```

For example: `PC-10042 · project-tapir#6675 · Sep 03 17:05`. If no Linear issue can be resolved, omit that segment: `project-tapir#6675 · Sep 03 17:05`.

## 2. Apply the shared review policy

Read and follow [`prompts/review-core.md`](review-core.md). It is the canonical source for context gathering, change explanation, review radius, finding gates, and the human-readable report.

Map the prepared claim into the shared policy as follows:

- Use the candidate's GitHub URL, exact `base_sha`, `head_sha`, `diff_range`, and `checkout_path`.
- Use the candidate's `linear_issue_ids` only when Linear does not provide an explicit linked issue.
- Supply `previous_review` as prior-review context when present. Reconcile every carried accepted, drafted, submitted, or still-open finding against the new head.
- This dispatcher permits a review to continue when Linear is unavailable or no trustworthy issue exists. State the limitation once and never invent context.
- For an explicitly linked cross-repository PR, prepare its exact head with `python3 bin/review_queue.py prepare-related --pr-url '<GitHub PR URL>'`. Inspect at most two related PRs and treat their checkouts as read-only.
- Write the shared policy's human-readable Markdown report to `suggested_report_path`.

## 3. Write dispatcher findings

Also write a machine-readable JSON document to `suggested_findings_path`, even when there are no new findings:

```json
{
  "findings": [
    {
      "id": "F-01",
      "kind": "defect",
      "severity": "P2",
      "title": "Short finding title",
      "path": "path/to/file.rb",
      "start_line": 42,
      "end_line": 47,
      "explanation": "Concrete mechanism and impact.",
      "failure_example": "Specific input, state, sequence, or maintenance scenario that demonstrates the concern.",
      "safeguard": "Small illustrative solution shape or, when more useful, a focused verification test.",
      "safeguard_kind": "implementation",
      "review_comment": "Concise, self-contained GitHub-ready comment explaining the concern and consequence, followed for a material finding by short **Concrete example** and **Possible solution** sections that preserve the strongest failure_example and safeguard."
    }
  ],
  "previous_findings": [
    {
      "claim_key": "exact earlier claim key",
      "finding_id": "F-01",
      "status": "resolved_by_code",
      "note": "What changed and where it was verified."
    }
  ]
}
```

Use stable IDs `F-01`, `F-02`, and so on within each round. `kind` is `defect`, `usability`, or `maintainability`. `safeguard_kind` is `implementation` or `regression_test`; use `implementation` for pseudocode, responsibility splits, timelines, data examples, and diagrams as well as literal patch sketches. Omit `start_line` and `end_line` only when the concern cannot be anchored to a changed line; such a finding becomes part of the review body instead of an inline comment. The JSON must contain only findings that pass the applicable review gate. `previous_findings` must reconcile every carried finding from `previous_review` as `resolved_by_code`, `resolved_by_scope_decision`, `still_open`, or `obsolete`. Both resolved statuses require a specific note and are not carried forward. Items marked `still_open` are copied into the current round as accepted findings with their reviewed wording and provenance. Treat each supplied `github_thread` as discussion evidence to verify against the code and decision history; `is_resolved: true` alone never closes a finding.

Build each material `review_comment` from the same evidence as the finding: lead with the mechanism and consequence, then retain the strongest `failure_example` under `**Concrete example**` and the bounded `safeguard` under `**Possible solution**`. Condense those fields when needed, but do not flatten away the domain values or event sequence that make the issue understandable. The possible solution remains illustrative rather than required architecture. A compact paragraph without labels is acceptable only for a self-evident, one-line defect when the sections would add repetition rather than clarity; it must still include the consequence and a plausible solution shape.

## 4. Complete the cycle

Run:

```sh
python3 bin/review_queue.py complete --key '<claim key>' --report '<report path>' --findings '<findings path>'
```

Return a concise outcome and the report to the Scheduled inbox. For a substantial or conceptually non-obvious PR, begin the handoff with a two-to-four-sentence plain-language mental model of why the work exists and what changes from before to after. Add one compact concrete example or text flow when it materially improves understanding. Include the shared policy's decision at the reviewed head and its concrete remaining steps or accepted risks. Keep small and routine reviews terse, and never add a diagram merely to satisfy a template. If the shared policy requires visual artifacts that the PR did not provide, briefly ask the user in this private handoff for the specific screenshots or recording needed; leave the code-review outcome intact and identify the visual limitation. The final line of the inbox message must be `[Open PR #123](...) · [Open ISSUE-ID](...)`. If no Linear issue was found, use `[Open PR #123](...) · Linear issue: not found`. Do not post anything to GitHub. This automation-triggered turn ends here even if task history contains an older request to draft or submit a review.

If preparation or analysis fails, run `python3 bin/review_queue.py fail --key '<claim key>' --reason '<concise reason>'` and report the failure privately. If a candidate was identified, still end the failure message with its PR link and either the primary Linear issue link or `Linear issue: not found`.
