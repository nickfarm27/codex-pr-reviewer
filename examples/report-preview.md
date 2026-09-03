# Format preview: acme/widgets#417 — Give an OAuth connection its own row

> Fictional example showing the report format. It is not a review of a real pull request.

[Open PR](https://github.com/acme/widgets/pull/417) · `main ← a187ddc937e5` · 21 files, +684/−212

## At a glance

**Review result:** Changes recommended

The PR replaces several implicit OAuth-credential relationships with one explicit connection record, but deleting a staff user can leave a connection that the revocation path cannot retire.

## Why this exists

- The Remote Assistant project lets retailers securely connect the product to AI clients. This work belongs to its OAuth and client-interoperability milestone.
- Grants and tokens currently imply a connection without one row owning its retailer, state, locking, or revocation. That repeats the same rules across credential tables.
- This needs to land before enablement, while the OAuth tables are still empty; later it would require a riskier live-data backfill.

Context: [ENG-142](https://linear.app/acme/issue/ENG-142) · [Remote Assistant](https://linear.app/acme/project/remote-assistant) · [Architecture plan](https://linear.app/acme/document/remote-assistant-architecture)

## What this PR changes

- Before: a connection was inferred from an OAuth application, owner, grants, and tokens. After: `Oauth::Connection` owns the retailer, subject, lifecycle state, and revocation.
- Grants, tokens, and connection events now point at that row, so locking and revocation can operate on one durable object.
- The retailer association moves to the connection and remains immutable, preserving the existing tenant boundary.
- The preceding OAuth foundation remains a prerequisite; this PR changes the connection model rather than the public tool surface.

```text
Staff user + OAuth app
          ↓
      Connection ── retailer + state + revocation
       ↙      ↘
    Grants    Tokens
```

## Findings

### P2 — Keep orphaned connections revocable

`app/models/oauth/connection.rb:13`

`subject` is required, but the polymorphic owner can be hard-deleted without database cleanup. A later `Connection#revoke!` validates the missing association and raises before the connection and credentials are marked revoked, so token exchange or cleanup can return a 500 instead of retiring the orphan.

**Example failure**

A staff user authorizes an AI client and is later hard-deleted. When token exchange discovers the orphaned connection and calls `revoke!`, the missing required `subject` makes the update fail validation; the connection and credentials remain active instead of being retired.

**Example regression test**

```ruby
test "an orphaned connection can still be revoked" do
  connection = create_connection
  connection.subject.destroy!

  assert_nothing_raised { connection.reload.revoke!(reason: :subject_deleted) }
  assert connection.reload.revoked?
end
```

One valid fix is to revoke connections as part of subject deletion. Another is to let historical connections tolerate a missing subject during revocation; the test captures the required behavior without prescribing which design to use.

## Fastest review path

1. `app/models/oauth/connection.rb` — verify the new row's identity, locking, tenancy, and lifecycle invariants.
2. The connection migration — verify uniqueness, foreign keys, nullability, and the assumption that credential tables are empty.
3. OAuth authorization and token controllers — trace open, refresh, subject-move, and revoke behavior through the new connection.
4. Connection and revocation tests — check races, reconnects, owner deletion, and credential cleanup rather than only happy paths.

## Merge readiness

- **CI:** Minitest is red at this snapshot, but the surfaced failures are unrelated integration tests.
- **Dependencies:** The preceding OAuth work has merged.
- **Coverage:** The full diff, important callers, and existing tests were inspected. No pull-request code or tests were executed locally in this unattended review.

[Open PR #417](https://github.com/acme/widgets/pull/417) · [Open ENG-142](https://linear.app/acme/issue/ENG-142)
