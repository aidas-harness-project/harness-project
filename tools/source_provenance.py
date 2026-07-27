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


# --- uid_scheme transitions ------------------------------------------------

def scheme_transition_errors(current: str | None, proposed: str) -> list[str]:
    """canonical_v1 is a one-way door."""
    if proposed not in ("legacy", "canonical_v1"):
        return [f"unknown uid_scheme {proposed!r}"]
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
