# Cross-Model Review Policy

## Requirement

Before a non-trivial pull request or substantial documentation change is
merged, it MUST receive an independent review from a different model family
or provider than the model primarily responsible for the change, when such a
reviewer is available.

## Scope

The requirement covers code, tests, configuration, Home Assistant
automations, deployment, security, privacy, persistence, and substantial
documentation about architecture, installation, operation, recovery, or the
review process.

Only small editorial changes, such as typo fixes, formatting, or rewording
without technical, process, or operational meaning, are exempt.

## Review package and evidence

The reviewer receives the base and head commits, requirements, complete diff,
test evidence, and live evidence for runtime or deployment changes. The
review is read-only.

The pull request records both the primary author/implementer model and the
reviewer model, review date, commit range, verdict, and findings disposition.

## Finding gates

Critical and Important findings MUST be fixed and submitted for re-review
before merge. Minor findings MUST be fixed or explicitly documented in the
pull request.

## Availability exception

An installed but logged-out reviewer is not yet unavailable. Make one
reasonable authentication or connectivity attempt first.

If no independent reviewer can be used, record the concrete technical or
organizational reason in the pull request. This exception does not waive
same-model review, tests, privacy checks, or other repository gates.
