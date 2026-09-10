# Requested-review workflow

Run one private, report-only review cycle.

## 1. Prepare the assigned claim

A dispatched worker receives one exact claim key in its initiating prompt. Run:

```sh
python3 bin/review_queue.py prepare --key '<exact claim key>'
```

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
      "review_comment": "Concise, self-contained GitHub-ready comment explaining the concern, consequence, concrete example, and an illustrative solution shape."
    }
  ],
  "previous_findings": [
    {
      "claim_key": "exact earlier claim key",
      "finding_id": "F-01",
      "status": "resolved",
      "note": "What changed and where it was verified."
    }
  ]
}
```

Use stable IDs `F-01`, `F-02`, and so on within each round. `kind` is `defect` or `maintainability`. `safeguard_kind` is `implementation` or `regression_test`; use `implementation` for pseudocode, responsibility splits, timelines, data examples, and diagrams as well as literal patch sketches. Omit `start_line` and `end_line` only when the concern cannot be anchored to a changed line; such a finding becomes part of the review body instead of an inline comment. The JSON must contain only findings that pass the applicable review gate. `previous_findings` must reconcile every carried finding from `previous_review`.

## 4. Complete the cycle

Run:

```sh
python3 bin/review_queue.py complete --key '<claim key>' --report '<report path>' --findings '<findings path>'
```

Return a concise outcome and the report to the Scheduled inbox. The final line of the inbox message must be `[Open PR #123](...) · [Open ISSUE-ID](...)`. If no Linear issue was found, use `[Open PR #123](...) · Linear issue: not found`. Do not post anything to GitHub.

If preparation or analysis fails, run `python3 bin/review_queue.py fail --key '<claim key>' --reason '<concise reason>'` and report the failure privately. If a candidate was identified, still end the failure message with its PR link and either the primary Linear issue link or `Linear issue: not found`.
