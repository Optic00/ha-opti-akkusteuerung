from pathlib import Path
from textwrap import dedent


REPO = Path(__file__).resolve().parents[1]


def _read(path):
    return (REPO / path).read_text(encoding="utf-8")


def _assert_canonical_document(path, expected):
    assert _read(path).strip() == dedent(expected).strip()


def test_policy_is_the_complete_normative_source():
    expected = """
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
    """

    _assert_canonical_document("REVIEW_POLICY.md", expected)


def test_agent_entrypoints_are_canonical_policy_references():
    expected_by_path = {
        "AGENTS.md": """
            # Repository Agent Instructions

            Before creating or merging any non-trivial pull request or substantial
            documentation pull request, agents MUST follow
            [`REVIEW_POLICY.md`](REVIEW_POLICY.md).

            A required independent cross-model review may only be skipped under the
            documented unavailability exception, and the concrete reason MUST be recorded
            in the pull request.
        """,
        "CLAUDE.md": """
            # Claude Repository Instructions

            Before creating or merging any non-trivial pull request or substantial
            documentation pull request, Claude MUST follow
            [`REVIEW_POLICY.md`](REVIEW_POLICY.md).

            A required independent cross-model review may only be skipped under the
            documented unavailability exception, and the concrete reason MUST be recorded
            in the pull request.
        """,
    }

    for path, expected in expected_by_path.items():
        _assert_canonical_document(path, expected)


def test_pr_template_records_one_review_status_and_all_evidence():
    expected = """
    ## Summary

    <!-- What changed and why? -->

    ## Verification

    <!-- Tests, privacy checks, configuration checks, and live evidence. -->

    ## Cross-model review

    Select exactly one status:

    - [ ] Required and completed
    - [ ] Exempt: editorial-only change with no technical, process, or
          operational meaning
    - [ ] Independent reviewer unavailable after a reasonable authentication or
          connectivity attempt

    Primary author/implementer model:

    Reviewer model:

    Review date:

    Commit range:

    Verdict:

    Findings and disposition:

    Unavailability reason:
    """

    _assert_canonical_document(".github/pull_request_template.md", expected)
