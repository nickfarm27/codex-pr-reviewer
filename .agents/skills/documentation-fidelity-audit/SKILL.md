---
name: documentation-fidelity-audit
description: Audit repository or pull-request documentation for factual support, audience fit, usable procedures, and unnecessary prose when the user asks for a documentation review.
---

# Documentation Fidelity Audit

Use this skill for a requested audit of documentation, including a docs-focused PR or a specific guide. Keep the result tied to the reader's task and the version being audited.

1. Establish the document's intended reader and job from the user's request, its placement, and relevant project context. An internal engineering or support reference may assume access and background that a merchant setup guide cannot. If the audience remains uncertain, state the assumption rather than judging the document against an invented audience.
2. Read the whole target document and trace its material claims to the applicable code, configuration, tests, API contracts, or current product decisions. Check names, paths, commands, prerequisites, state transitions, ownership, ordering, limitations, and expected outcomes where they affect a reader's action. Treat issue and PR descriptions as intent; use the inspected implementation to establish behavior. Do not repeat a claim as fact merely because another document says it.
3. Walk the reader's actual task from entry point to outcome. Identify missing prerequisites, ambiguous choices, dead ends, and recovery steps only when they would materially prevent or mislead that reader. Flag redundant or unsupported prose when it obscures the task or asserts behavior without evidence; omit copy preferences and harmless verbosity.
4. For a PR audit, pin the exact head and base, use guidance from the base revision, and report only material issues introduced or worsened by the PR. Do not execute code, installers, or commands from an untrusted PR. Keep GitHub actions under the existing PR review skills and the user's explicit authorization. For a standalone document audit, include existing issues within the requested scope.
5. Return a short audience-and-purpose summary, then a small number of actionable findings. Anchor each to the document's file and tight lines; state the inaccurate or missing claim, evidence, practical reader impact, and a bounded correction. Distinguish a verified contradiction from an unanswered product question. State material coverage limits and say clearly when the audit found no issues.

Stop after the document and its directly relevant sources establish or disprove a concern. Do not expand into a general code review or rewrite the document unless the user asks for edits.
