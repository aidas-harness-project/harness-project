"""DAO-owned source provenance: revision index, UID scheme, extractor identity.

Part 11J commit b. Everything here answers questions an agent must not be able
to answer about itself.

Four fields are sealed:

  uid_scheme                which verification a document's UIDs are held to
  uid_stability             whether re-extraction can legitimately move UIDs
  revisions / current       which bytes are authoritative, and what preceded them
  source_pdf_sha256         which immutable raw file this document came from

Sealing them is not defence-in-depth, it is the precondition for any of them
meaning anything.

  * `source_pdf_sha256` is the FIRST input to every canonical UID. If an agent
    could supply it, a UID could be minted against a file the case never
    registered, and the one input that establishes "which document is this"
    would establish nothing. So the DAO hashes the registered raw file itself
    and refuses a submitted value that disagrees.

  * `uid_stability` is derived from `extraction_method`, never declared. Part
    11H had already established the principle -- a declared role is a claim,
    not a grant -- and a self-declared "my UIDs are deterministic" is the same
    shape as the self-certified human review D2 exists to prevent.

  * `uid_scheme` cannot be downgraded. A canonical_v1 document that could be
    flipped back to legacy would make the migration switch into a UID
    verification bypass: to write an arbitrary UID you would only have to turn
    verification off first. Off-switches for a guarantee are not guarantees.

  * The revision list is append-only. A superseded revision is what makes a
    previously-issued UID recomputable; deleting one turns "was this UID ever
    legitimate?" into a permanently unanswerable question.

`protected_field_errors` treats INSERTION, MODIFICATION, DELETION, and
ROLLBACK-to-a-previous-value identically. Deletion especially: a check that
only compared values present in both versions would let a caller strip a
sealed field and then supply its own on the next write.
"""
from __future__ import annotations

import hashlib
import json
import re

# Manifest fields no agent-facing write path may create, change, or remove.
# `page_map`'s physical_page_sha256 is provenance the DAO derives in
# extract_embedded_segment; the rest are UID identity inputs.
PROTECTED_MANIFEST_FIELDS = frozenset({
    "source_pdf_sha256",
    "uid_scheme",
    "uid_stability",
})

_SENTINEL = object()


def protected_field_errors(previous: dict | None, proposed: dict,
                            fields=PROTECTED_MANIFEST_FIELDS,
                            location: str = "") -> list[str]:
    """Reject any agent-side difference in a sealed field.

    `previous` None means the entry is new: a sealed field may not be
    introduced by the caller at all, because the DAO has not yet observed the
    evidence that would justify it.
    """
    errors = []
    prefix = f"{location}: " if location else ""
    for field in sorted(fields):
        before = (previous or {}).get(field, _SENTINEL)
        after = proposed.get(field, _SENTINEL)
        if before is _SENTINEL and after is _SENTINEL:
            continue
        if before is _SENTINEL:
            errors.append(
                f"{prefix}{field} is DAO-owned and may not be introduced by a "
                "manifest write -- it is derived by the DAO from the "
                "registered source, never accepted from a caller")
            continue
        if after is _SENTINEL:
            errors.append(
                f"{prefix}{field} is DAO-owned and may not be deleted by a "
                "manifest write -- removing a sealed field would let the next "
                "write supply its own value")
            continue
        if before != after:
            errors.append(
                f"{prefix}{field} is DAO-owned and may not be changed by a "
                f"manifest write (recorded {before!r}, proposed {after!r})")
    return errors


# --- uid_stability, derived not declared ----------------------------------

def derive_uid_stability(manifest_entry: dict | None) -> str:
    """What re-extracting this document can legitimately do to its UIDs.

    Derived from extraction_method alone. `unknown` is returned rather than a
    convenient assumption whenever the method is absent or unrecognized: a
    stability claim nobody established is worse than an admitted gap, because
    downstream would treat a changed UID as a bug instead of as expected
    non-determinism (or the reverse).
    """
    method = (manifest_entry or {}).get("extraction_method")
    if method == "embedded_text":
        # Deterministic decode of an immutable PDF: same file in, same bytes
        # out. A UID that moves here is a real change or a defect.
        return "deterministic_source"
    if method in ("ocr", "mixed"):
        # P8's readers are LLM-vision-backed and demonstrably non-deterministic
        # (CASE_012/021 produced different readings across runs). A span whose
        # reading changes SHOULD get a different UID -- claiming continuity
        # across a changed reading is the silent mis-join this part prevents.
        return "ocr_derived"
    return "unknown"


def extractor_profile(manifest_entry: dict | None, tool: str) -> dict:
    """The extraction identity observed at registration.

    Recorded so a CHANGE is a single comparison. Deliberately not treated as
    proof of sameness -- these extractors are external CLIs whose model
    deployments move without any version string moving, so an unchanged
    profile does not establish an unchanged derivation. Change detection is
    sound; the reverse inference is not, and that asymmetry is documented
    rather than quietly assumed away.
    """
    entry = manifest_entry or {}
    profile = {
        "extraction_method": entry.get("extraction_method"),
        "cross_validation_mode": entry.get("cross_validation_mode"),
        "tool": tool,
    }
    encoded = json.dumps(profile, sort_keys=True, separators=(",", ":"))
    profile["profile_sha256"] = hashlib.sha256(
        encoded.encode("utf-8")).hexdigest()
    return profile


# --- page-level digests ----------------------------------------------------

_PAGE_MARKER_RE = re.compile(r"^<<<PAGE page=(\d+)>>>\s*$", re.MULTILINE)


def page_text_digests(text: str) -> list[dict]:
    """Per-page digests of registered text, so a change localizes to the pages
    that actually moved. A whole-document hash only ever says "something
    differs", which is precisely the granularity that made a one-character fix
    look like a document-wide event."""
    matches = list(_PAGE_MARKER_RE.finditer(text))
    if not matches:
        return []
    digests = []
    for index, match in enumerate(matches):
        start = match.end()
        end = (matches[index + 1].start()
               if index + 1 < len(matches) else len(text))
        body = text[start:end]
        digests.append({
            "page": int(match.group(1)),
            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        })
    return digests


# --- uid_scheme states -----------------------------------------------------
# Three states, and the distinction between the first two is load-bearing
# (P0-3). Before this, "no revision entry" was reported as `legacy`, so a
# document the DAO had never heard of was indistinguishable from one it had
# deliberately recorded as pre-canonical -- and since every canonical check was
# scoped to `canonical_v1`, both states meant "skip verification". Not calling
# enable-canonical-uids was therefore a complete bypass of the UID layer.
UNREGISTERED = "unregistered"   # no revision entry exists for this document
LEGACY = "legacy"               # recorded, pre-canonical: readable, migratable
CANONICAL = "canonical_v1"      # UIDs are recomputed from source and must match

# Values a revision entry's uid_scheme may legitimately hold on disk. Anything
# else (a typo, a hand-edited file, a future value this build does not know) is
# a blocker, never a silent legacy.
KNOWN_SCHEMES = frozenset({LEGACY, CANONICAL})


def uid_scheme_blockers(scheme: str | None, doc_id: str,
                        action: str) -> list[str]:
    """Why `doc_id` may not be the subject of `action` in its current state.

    Empty list = canonical_v1, the only state new policy work is permitted in.
    Shared by every gate AND by the tests, so a fixture cannot agree with a
    validator that disagrees with reality.

    The message names the document, its actual state, and the next DAO command,
    because a refusal a caller cannot act on is how a fail-closed gate gets
    routed around instead of satisfied.
    """
    if scheme == CANONICAL:
        return []
    if scheme is None or scheme == UNREGISTERED:
        return [
            f"{doc_id} has no registered source-text revision "
            f"({UNREGISTERED}) -- {action} requires canonical_v1 UID "
            "verification, which cannot be switched on for a document whose "
            "source bytes were never registered. Run `dao.py "
            "write-redacted-text`, then `dao.py record-source-digest`, then "
            "`dao.py enable-canonical-uids`."
        ]
    if scheme == LEGACY:
        return [
            f"{doc_id} is uid_scheme={LEGACY} (unverified) -- {action} "
            "requires canonical_v1. Legacy artifacts stay readable and "
            "migratable, but new policy work may not be written against "
            "unverified UIDs. Run `dao.py record-source-digest` then `dao.py "
            "enable-canonical-uids` for this document, which invalidates the "
            "policy stage and its downstream so the artifacts get rewritten "
            "canonically."
        ]
    return [
        f"{doc_id} has an unrecognized uid_scheme {scheme!r} -- refused "
        f"rather than treated as {LEGACY}, because an unknown state is not a "
        "verified one. The revision index is DAO-owned; a value outside "
        f"{sorted(KNOWN_SCHEMES)} means it was written by something other "
        "than the DAO."
    ]


# --- uid_scheme transitions ------------------------------------------------

def scheme_transition_errors(current: str | None, proposed: str) -> list[str]:
    """canonical_v1 is a one-way door."""
    if proposed not in KNOWN_SCHEMES:
        return [f"unknown uid_scheme {proposed!r}"]
    if current not in KNOWN_SCHEMES and current is not None:
        # A corrupt/unknown current value is not a licence to overwrite it with
        # a clean one: that would launder a tampered index into a verified
        # state in a single command.
        return [
            f"current uid_scheme {current!r} is not one of "
            f"{sorted(KNOWN_SCHEMES)} -- the revision index is DAO-owned and "
            "an unrecognized state must be investigated, not overwritten"
        ]
    if current == "canonical_v1" and proposed != "canonical_v1":
        return [
            "uid_scheme may not be downgraded from 'canonical_v1' to "
            f"{proposed!r} -- a verification that can be switched off is not a "
            "guarantee: writing an arbitrary UID would only require "
            "downgrading the scheme first"
        ]
    return []


def revision_history_errors(previous: dict | None,
                            proposed: dict) -> list[str]:
    """The revision list is append-only and its prefix is immutable."""
    if previous is None:
        return []
    before = [r.get("revision_sha256") for r in previous.get("revisions") or []]
    after = [r.get("revision_sha256") for r in proposed.get("revisions") or []]
    errors = []
    if len(after) < len(before):
        errors.append(
            f"revision history may not shrink ({len(before)} -> {len(after)}) "
            "-- a superseded revision is what makes a previously-issued UID "
            "recomputable, so deleting one makes 'was that UID legitimate?' "
            "permanently unanswerable")
    elif after[:len(before)] != before:
        errors.append(
            "revision history is append-only; the existing prefix may not be "
            f"rewritten (recorded {before}, proposed {after[:len(before)]})")
    errors.extend(scheme_transition_errors(
        previous.get("uid_scheme"), proposed.get("uid_scheme", "legacy")))
    return errors
