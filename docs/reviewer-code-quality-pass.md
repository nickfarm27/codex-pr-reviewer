# Proposal: make structural code quality a deliberate review pass

Accepted for implementation by Nicholas on 2026-10-01.

## Observation and limit

A manual provider-integration audit identified a consequential rule repeated across many services and forms. The shared reviewer policy already mentions design and maintainability, but the structural check is brief enough to be easy to overlook. No matching automated review round was recorded, so this is **not** evidence that the automation missed that exact finding.

## Proposed change

Make one bounded structural pass explicit in the shared review core, with [Refactoring.Guru's code smells](https://refactoring.guru/refactoring/smells) as diagnostic prompts. Point to its [refactoring techniques](https://refactoring.guru/refactoring/techniques) for small illustrative remedies and its [design patterns](https://refactoring.guru/design-patterns) only as optional vocabulary. Preserve the existing maintainability gate: report only PR-introduced, material, repository-evidenced maintenance costs with a bounded improvement. A catalog item, large file, or missing named pattern is not itself a finding.

Expected benefit: substantial structure regressions receive conscious scrutiny even when no runtime defect is apparent. Risk: a catalog can produce noisy textbook critiques; the bounded pass and existing finding gate must prevent that.

Regression scenario: a PR adds a long but cohesive method, or repeats a harmless one-off expression. The reviewer may inspect it but reports no maintainability finding without a demonstrated cost compared with the base revision and repository design.

## Implementation and release plan

1. Update `prompts/review-core.md` only for the new structural pass; avoid changing finding schema, severity, permissions, or review lifecycle.
2. Copy that exact core into the peer skill and bump its release version from 0.4.2 to 0.4.3 in all five version locations.
3. Add a focused prompt invariant test. Run prompt and peer tests, the full unit suite, peer package validation, and skill validation. Review the diff for overfitting and weakened gates.
4. Open a PR, wait for CI, inspect the final PR diff and release metadata, then merge. Confirm the automatic `peer-v0.4.3` tag, GitHub Release, and assets. Stop and report any failed check rather than forcing a release.
