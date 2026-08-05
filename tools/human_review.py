"""DAO-only human-review provenance for policy decisions.

A machine-authored contract must not be able to self-declare "a human decided
X" with a bare name string. Two decisions in the policy pipeline are, by their
nature, human calls that no automated check can substitute:

  - accepting a policy-audit finding as a residual risk (accepted_risk); and
  - verifying an unpaged physical page (a cover/blank with no printed logical
    number) as a genuine administrative exclusion.

Before this module, both were reachable by writing the right strings into a
normal write-contract JSON: `resolution_actor_type: "human"` +
`resolved_by: "any-name"`, or a `verified_by: "any-name"` on an unpaged page.
That is exactly the fabricated-human-review failure P7/D2 exist to prevent, at
a different gate.

The fix is a per-case ledger, _human_review_ledger.json, written ONLY through
`dao.py record-human-review` (never write-contract). Each record binds an
immutable content-derived review_uid to the EXACT on-disk bytes of the artifact
reviewed -- the DAO computes artifact_sha256 itself at record time, so an agent
can neither supply its own hash nor point a record at bytes it never saw. A
downstream contract that claims a human decision references the review_uid; the
verifier here re-checks that:

  1. the UID exists in the ledger;
  2. the record was filed for THIS artifact kind, document, and target key;
  3. the artifact's CURRENT bytes still hash to the recorded artifact_sha256
     (editing the audit/coverage after review invalidates the decision -- it
     must be re-made against the new bytes); and
  4. the recorded decision is one this reference is allowed to rely on
     (accepted_risk for a finding, verified for an unpaged exclusion).

This does not (and in a PoC cannot) cryptographically authenticate a human --
the CLI invocation is trusted to be a genuine human action, the same trust
model mark-human-review-complete and set-ledger-status already use. What it
removes is the ability to fabricate the decision inside a machine-written
contract with no DAO-mediated, hash-bound record behind it.
"""
from __future__ import annotations

import copy
import hashlib
import json

HUMAN_REVIEW_LEDGER_SCHEMA = "human_review_ledger.schema.json"

# Which decisions each artifact kind's downstream reference is allowed to rely
# on. A `rejected` record never satisfies a completion gate -- it is recorded
# so the rejection is durable, but it is not an acceptance.
ALLOWED_DECISION = {
    "policy_audit_finding": "accepted_risk",
    "unpaged_physical_exclusion": "verified",
    # A classification produced from PRE-REDACTION page text (checkpoint 1's
    # page_NNN.md, because no redacted_text.md existed yet). The classifier is
    # an analysis-side consumer, so this is unredacted text reaching a stage
    # that should never see it -- deliberate and permitted (a document not yet
    # redacted must stay classifiable), but not something a run may finalize
    # while nobody has looked. `verified` here means a human read the
    # classification and confirmed the label stands: the decisive question is
    # whether the evidence quote survives redaction, because if it does, the
    # same label is reachable from the redacted layer and no PII was
    # load-bearing.
    "classification_review": "verified",
}


# Fields that record a review's OWN disposition, not the substance a human
# reviews. They are REMOVED before hashing so the binding is invariant to the
# very decision the review sets -- otherwise a genuine open->accepted_risk
# transition (plus writing the returned UID) would change the bytes and
# invalidate the record it just produced. Everything else --
# finding identity, description, evidence, the audit's bound source hashes, the
# exclusion's reason/page -- stays in the hash, so any substantive edit after
# review still invalidates the human decision.
_DISPOSITION_FIELDS = frozenset({
    "status",
    "resolved_by",
    "resolution_note",
    "resolution_actor_type",
    "human_review_uid",
    "verified_by",
    "verified_at",
})


def _strip_disposition(obj):
    """Recursively REMOVE every disposition field from a copy of `obj`.

    Removal, not nulling. Nulling makes the hash depend on whether the key was
    present at all, which breaks the exact round trip this module exists to
    support: record a review against an artifact that has no human_review_uid
    yet, then write the returned UID into it. Under nulling the artifact goes
    from `{...}` to `{..., "human_review_uid": null}` -- different canonical
    bytes, so the record it just produced no longer verifies against the
    artifact it was made for.

    The two original kinds never exposed this: their artifacts always carry the
    disposition keys, so present-and-nulled was all that ever happened.
    classification_review adds the key on write, which is what surfaced it.
    Removing is invariant both ways -- absent and present-but-nulled now hash
    identically -- and is otherwise the same rule: the substance a human
    reviewed stays in the hash, the disposition their review sets does not.
    """
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key in _DISPOSITION_FIELDS:
                continue
            out[key] = _strip_disposition(value)
        return out
    if isinstance(obj, list):
        return [_strip_disposition(item) for item in obj]
    return obj


def _strip_disposition_legacy_null(obj):
    """The pre-2026-08-05 rule: null a disposition field rather than remove it.

    Retained ONLY so a review recorded before that change still verifies. Its
    hash is accepted as a fallback by verify_reference; nothing computes a new
    binding with it. See _strip_disposition for why nulling was wrong.
    """
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key in _DISPOSITION_FIELDS:
                out[key] = None
            else:
                out[key] = _strip_disposition_legacy_null(value)
        return out
    if isinstance(obj, list):
        return [_strip_disposition_legacy_null(item) for item in obj]
    return obj


def _encode_canonical(canonical) -> bytes:
    return json.dumps(
        canonical, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")


def legacy_null_artifact_sha256(artifact: dict) -> str:
    """The pre-2026-08-05 canonical hash (disposition fields nulled, not
    removed). Verification-only: accepted as a fallback so a review recorded
    under the old rule keeps verifying. Never used to bind a new record."""
    return hashlib.sha256(_encode_canonical(
        _strip_disposition_legacy_null(copy.deepcopy(artifact)))).hexdigest()


def canonical_artifact_sha256(artifact: dict) -> str:
    """SHA-256 of the artifact in its canonical form: every review-disposition
    field (see _DISPOSITION_FIELDS) REMOVED, keys sorted, compact separators,
    UTF-8. This is the single hashing rule both record-human-review and every
    verifier use, so the two never disagree about what 'the reviewed substance'
    was -- and it is invariant to the disposition the review itself sets,
    including whether the disposition key is present at all."""
    canonical = _strip_disposition(copy.deepcopy(artifact))
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_review_uid(
        case_id: str,
        artifact_kind: str,
        artifact_id: str,
        target_key: str,
        artifact_sha256: str,
        reviewed_at: str) -> str:
    """Deterministic HR- identifier bound to the review's own content.

    Includes artifact_sha256 and reviewed_at so re-reviewing the same target
    after the artifact changed (or at a later time) yields a distinct UID --
    a review is of specific bytes at a specific moment, never a reusable token.
    """
    parts = "|".join([
        case_id, artifact_kind, artifact_id, target_key,
        artifact_sha256, reviewed_at,
    ])
    return "HR-" + hashlib.sha256(parts.encode("utf-8")).hexdigest()[:16]


def find_record(ledger: dict | None, review_uid: str) -> dict | None:
    if not ledger:
        return None
    for record in ledger.get("records", []):
        if record.get("review_uid") == review_uid:
            return record
    return None


def verify_reference(
        ledger: dict | None,
        review_uid: str | None,
        *,
        expected_kind: str,
        expected_artifact_id: str,
        expected_target_key: str,
        current_artifact_sha256: str | None,
        legacy_artifact_sha256: str | None = None) -> list[str]:
    """Return blocker strings (empty = the referenced human decision is valid).

    current_artifact_sha256 is the canonical hash of the artifact's bytes AS
    THEY ARE NOW (see canonical_artifact_sha256). A None value means the
    artifact does not exist to hash -- itself a failure, since a reference
    cannot outlive its target.
    """
    if not review_uid:
        return [
            f"a human decision on {expected_kind} {expected_target_key!r} is "
            "claimed but no human_review_uid is provided -- record it via "
            "dao.py record-human-review; a name string alone is not a review"
        ]
    record = find_record(ledger, review_uid)
    if record is None:
        return [
            f"human_review_uid {review_uid!r} does not exist in "
            "_human_review_ledger.json (no such DAO-recorded review)"
        ]
    errors: list[str] = []
    if record.get("artifact_kind") != expected_kind:
        errors.append(
            f"human_review_uid {review_uid!r} was recorded for artifact_kind "
            f"{record.get('artifact_kind')!r}, not {expected_kind!r}")
    if record.get("artifact_id") != expected_artifact_id:
        errors.append(
            f"human_review_uid {review_uid!r} was recorded for document "
            f"{record.get('artifact_id')!r}, not {expected_artifact_id!r}")
    if record.get("target_key") != expected_target_key:
        errors.append(
            f"human_review_uid {review_uid!r} was recorded for target "
            f"{record.get('target_key')!r}, not {expected_target_key!r}")
    allowed = ALLOWED_DECISION.get(expected_kind)
    if record.get("decision") != allowed:
        errors.append(
            f"human_review_uid {review_uid!r} carries decision "
            f"{record.get('decision')!r}; only {allowed!r} satisfies a "
            f"{expected_kind} reference (a 'rejected' review never does)")
    recorded_hash = record.get("artifact_sha256")
    if current_artifact_sha256 is None:
        errors.append(
            f"human_review_uid {review_uid!r}: the reviewed artifact no longer "
            "exists to hash -- the review cannot be verified against it")
    elif recorded_hash != current_artifact_sha256 and (
            legacy_artifact_sha256 is None
            or recorded_hash != legacy_artifact_sha256):
        # The legacy fallback accepts a record bound under the pre-2026-08-05
        # nulling rule against unchanged bytes. It is strictly a compatibility
        # path: it never widens what counts as unchanged, since both hashes are
        # computed from the artifact as it is now.
        errors.append(
            f"human_review_uid {review_uid!r} was recorded against artifact "
            f"bytes {recorded_hash!r} but the artifact now hashes to "
            f"{current_artifact_sha256!r} -- it changed after review; the human "
            "decision must be re-made against the current bytes")
    return errors


def check_audit_human_provenance(
        audit: dict,
        ledger: dict | None,
        artifact_id: str) -> list[str]:
    """Every finding that claims a human decision must resolve to a valid,
    hash-current, accepted_risk ledger record for that finding_uid.

    The record is bound to the audit's own canonical (UID-stripped) bytes --
    the finding lives inside the audit file. An accepted_risk finding whose
    human_review_uid does not verify is a blocker, exactly as if it were still
    open.
    """
    current_sha = canonical_artifact_sha256(audit)
    legacy_sha = legacy_null_artifact_sha256(audit)
    errors: list[str] = []
    for index, finding in enumerate(audit.get("findings") or []):
        claims_human = (
            finding.get("status") == "accepted_risk"
            or finding.get("resolution_actor_type") == "human"
        )
        if not claims_human:
            continue
        finding_uid = finding.get("finding_uid")
        errors.extend(
            f"findings[{index}] ({finding_uid}): {error}"
            for error in verify_reference(
                ledger,
                finding.get("human_review_uid"),
                expected_kind="policy_audit_finding",
                expected_artifact_id=artifact_id,
                expected_target_key=finding_uid,
                current_artifact_sha256=current_sha,
                legacy_artifact_sha256=legacy_sha,
            )
        )
    return errors


def check_unpaged_human_provenance(
        coverage: dict,
        ledger: dict | None,
        artifact_id: str) -> list[str]:
    """Every administrative_excluded unpaged physical page must resolve to a
    valid, hash-current, verified ledger record for that physical page. The
    record is bound to the parent-coverage's canonical (UID-stripped) bytes."""
    current_sha = canonical_artifact_sha256(coverage)
    legacy_sha = legacy_null_artifact_sha256(coverage)
    errors: list[str] = []
    for page in coverage.get("unpaged_physical_pages") or []:
        if page.get("disposition") != "administrative_excluded":
            continue
        physical = page.get("physical_page")
        target_key = f"physical:{physical}"
        errors.extend(
            f"unpaged physical page {physical}: {error}"
            for error in verify_reference(
                ledger,
                page.get("human_review_uid"),
                expected_kind="unpaged_physical_exclusion",
                expected_artifact_id=artifact_id,
                expected_target_key=target_key,
                current_artifact_sha256=current_sha,
                legacy_artifact_sha256=legacy_sha,
            )
        )
    return errors
