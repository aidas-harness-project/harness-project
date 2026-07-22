"""Cross-contract invariants -- the checks JSON Schema structurally cannot make.

A JSON Schema validates one document against one shape. It cannot see a
sibling file, and it cannot compare two entries of the same array to each
other. Three real classes of corruption live in exactly that blind spot, all
of them reproduced against the committed CASE_903/CASE_021 outputs before
this module existed:

1. ORPHAN CROSS-REFERENCES. denial_validation_result.json refers to denial
   reasons by id. Putting `reason_id: "DR_999"` there -- an id no
   denial_reason_result.json ever defined -- validated cleanly, as did
   dropping validations for real reasons entirely, or duplicating one. So a
   Phase 2 output could silently validate nothing, or claim to have validated
   something that does not exist, and every schema check still said PASS.

2. DUPLICATE IDS. Two entries both calling themselves `DR_1` validated. Ids
   are how every downstream stage and the whole evaluation contract address a
   reason; two rows sharing one address means whichever is read last wins,
   silently.

3. SIBLING-COMPARING FIELD RULES. `candidate_codes` is the Top-1/Top-3
   evaluation input. Its first entry must be the assigned `taxonomy_code`,
   entries must be distinct, and confidence must not increase down a "ranked"
   list. Each is a comparison between array members, which `items` cannot
   express.

Scope note: this checks INTERNAL CONSISTENCY -- that ids resolve, are unique,
and that ranked lists are actually ranked. It deliberately does not re-judge
classification quality; whether R04 was the right code is a matter for
evaluation, not a write-time gate.

Failures are returned as strings, same contract as validate_instance(), and
dao.write-contract refuses the write on any -- the fail/don't-persist rule
already used for schema errors.
"""
import json
from pathlib import Path

# Files that carry ids other contracts point at, and the checks each gets.
DENIAL_REASONS = "denial_reason_result.json"
DENIAL_VALIDATION = "denial_validation_result.json"


def _load(case_dir: Path, filename: str):
    """Returns parsed JSON, or None when the file isn't there yet.

    A missing sibling is not an error: stages run in order, and Phase 2
    contracts legitimately do not exist while Phase 1 is still running. Only
    a PRESENT sibling that disagrees is a failure.
    """
    path = case_dir / filename
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{filename} is not valid JSON: {exc}") from exc


def _duplicates(ids):
    seen, dupes = set(), []
    for i in ids:
        if i in seen and i not in dupes:
            dupes.append(i)
        seen.add(i)
    return dupes


def check_denial_reason_result(data: dict) -> list[str]:
    """Internal invariants of denial_reason_result.json itself."""
    errors = []
    reasons = data.get("denial_reasons") or []

    reason_ids = [r.get("reason_id") for r in reasons]
    for dupe in _duplicates(reason_ids):
        errors.append(f"denial_reasons: duplicate reason_id {dupe!r} -- ids must be unique, "
                      "downstream contracts address reasons by id")

    # id-less matches are legacy (pre-policy_match_id, e.g. CASE_021) and have
    # no address to collide on -- reporting several of them as "duplicate None"
    # would flag a legacy shape as corruption. Same carve-out as the
    # validation path below; the schema requires the id on new writes.
    match_ids = [m.get("policy_match_id") for r in reasons for m in (r.get("policy_matches") or [])
                 if m.get("policy_match_id") is not None]
    for dupe in _duplicates(match_ids):
        errors.append(f"policy_matches: duplicate policy_match_id {dupe!r} -- ids must be unique "
                      "across the whole contract, not just within one reason")

    for reason in reasons:
        rid = reason.get("reason_id", "?")
        code = reason.get("taxonomy_code")
        candidates = reason.get("candidate_codes") or []

        if candidates:
            top = candidates[0].get("taxonomy_code")
            if top != code:
                errors.append(f"{rid}: candidate_codes[0] is {top!r} but taxonomy_code is {code!r} "
                              "-- the assigned code must be the top-ranked candidate")

            codes = [c.get("taxonomy_code") for c in candidates]
            for dupe in _duplicates(codes):
                errors.append(f"{rid}: candidate_codes lists {dupe!r} more than once")

            confidences = [c.get("confidence") for c in candidates]
            if any(a is not None and b is not None and b > a
                   for a, b in zip(confidences, confidences[1:])):
                errors.append(f"{rid}: candidate_codes confidences {confidences} are not "
                              "non-increasing -- a ranked list must actually be ranked")

        label = reason.get("taxonomy_label")
        if label is not None and code is not None:
            expected = _codebook_label(code)
            # startswith, not equality: a label may carry a case-specific
            # parenthetical after the canonical text (CASE_024 records
            # "기왕증 / 기존 질환 기여도 (골다공증 기여도 반영)"), which is a
            # useful annotation rather than a competing label. What this
            # rejects is a label for a DIFFERENT code, or invented wording --
            # the failure mode where a contract quietly renames an R-code.
            if expected is not None and not label.startswith(expected):
                errors.append(f"{rid}: taxonomy_label {label!r} does not match the codebook label "
                              f"for {code} ({expected!r}) -- a label may append case-specific "
                              "detail but must not restate the code's meaning differently")

    return errors


def _normalized_policy_filename(document_id: str) -> str:
    return f"normalized_policy_clause_{document_id}.json"


def check_policy_matches(data: dict, case_dir: Path) -> list[str]:
    """Every policy match must resolve to a real clause in a real policy file,
    at the location it claims.

    Before this, a match only had to be well-shaped: `document_id` and
    `clause_id` were free strings nobody resolved, and
    `policy_clause_evidence_references` could point at a different document
    than the match itself. So a match could name DOC_001 while citing DOC_999,
    or cite a 제99조 that exists in no policy document, and still validate --
    presenting an insurer's denial as anchored to a policy clause that was
    never checked to exist.

    The project's stated preference is fail-safe: a missing link is safer than
    a wrong one. So an unresolvable match is an error, not a warning, and this
    applies to `agent_inferred` matches exactly as to `insurer_cited` ones --
    an inferred link is the one most in need of checking, not least.

    A match with no `policy_match_id` is skipped (see the legacy note in
    check_denial_validation_result) -- CASE_021 predates the field.
    """
    errors = []
    for reason in data.get("denial_reasons") or []:
        rid = reason.get("reason_id", "?")
        for match in reason.get("policy_matches") or []:
            mid = match.get("policy_match_id")
            if mid is None:
                continue
            doc_id = match.get("document_id")
            clause_id = match.get("clause_id")
            label = f"{rid}/{mid}"

            # 1. Clause evidence must cite the document the match names.
            for ref in match.get("policy_clause_evidence_references") or []:
                ref_doc = ref.get("document_id")
                if ref_doc != doc_id:
                    errors.append(
                        f"{label}: policy_clause_evidence_references cites document_id "
                        f"{ref_doc!r} but the match is on {doc_id!r} -- a clause citation must "
                        "come from the policy document the match claims")

            if doc_id is None or clause_id is None:
                continue

            # 2. The policy document must have been normalized at all.
            policy_doc = _load(case_dir, _normalized_policy_filename(doc_id))
            if policy_doc is None:
                errors.append(
                    f"{label}: no {_normalized_policy_filename(doc_id)} in this case -- the match "
                    "names a policy document whose clauses were never extracted, so the link "
                    "cannot be verified (a missing link is safer than an unverified one)")
                continue

            # 3. The clause must exist in it.
            clauses = policy_doc.get("clauses") or []
            clause = next((c for c in clauses if c.get("clause_id") == clause_id), None)
            if clause is None:
                errors.append(
                    f"{label}: clause_id {clause_id!r} does not exist in "
                    f"{_normalized_policy_filename(doc_id)} "
                    f"(known: {[c.get('clause_id') for c in clauses]})")
                continue

            # 4. The cited location must be one the normalized clause records.
            #    Compared as (page, quote) pairs: a page alone would accept any
            #    text on the right page, and a quote alone would accept the
            #    right words attributed to the wrong page.
            clause_locations = {
                (r.get("page"), (r.get("quote") or "").strip())
                for r in _clause_evidence(clause)
            }
            for ref in match.get("policy_clause_evidence_references") or []:
                if ref.get("document_id") != doc_id:
                    continue  # already reported in step 1
                here = (ref.get("page"), (ref.get("quote") or "").strip())
                if here not in clause_locations:
                    errors.append(
                        f"{label}: policy clause evidence (page {here[0]}, quote "
                        f"{here[1][:40]!r}...) does not match any evidence reference recorded "
                        f"for clause {clause_id!r} in {_normalized_policy_filename(doc_id)}")

    return errors


def _clause_evidence(clause: dict) -> list[dict]:
    """Every evidence reference a normalized clause carries -- its own, plus
    those on its payout_conditions / exclusions / reduction_conditions, since
    a match may legitimately cite the specific condition it turns on rather
    than the clause header."""
    refs = list(clause.get("evidence_references") or [])
    for key in ("payout_conditions", "exclusions", "reduction_conditions"):
        for item in clause.get(key) or []:
            if isinstance(item, dict):
                refs.extend(item.get("evidence_references") or [])
    return refs


_CODEBOOK_CACHE: dict | None = None


def _codebook_label(code: str):
    """The canonical label for an R-code, read from the common schema's
    x-codebook metadata -- the one machine-readable source, so a typo in a
    contract cannot quietly invent a new label for an existing code."""
    global _CODEBOOK_CACHE
    if _CODEBOOK_CACHE is None:
        schema_path = Path(__file__).resolve().parent.parent / "schemas" / "common_component_output.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        node = schema.get("$defs", {}).get("taxonomy_code", {})
        book = node.get("x-codebook") or {}
        labels = {}
        if isinstance(book, dict):
            for code_key, meta in book.items():
                if isinstance(meta, dict) and "label_ko" in meta:
                    labels[code_key] = meta["label_ko"]
        _CODEBOOK_CACHE = labels
    return _CODEBOOK_CACHE.get(code)


def check_denial_validation_result(data: dict, case_dir: Path) -> list[str]:
    """denial_validation_result.json against the reasons it claims to validate.

    This is the check that motivated the module: a validation naming DR_999,
    or omitting a real reason, or listing one twice, all validated cleanly
    before.
    """
    errors = []
    reasons_doc = _load(case_dir, DENIAL_REASONS)
    if reasons_doc is None:
        return [f"{DENIAL_VALIDATION} cannot be written before {DENIAL_REASONS} exists -- "
                "it validates that contract's reasons and has nothing to resolve ids against"]

    reasons = reasons_doc.get("denial_reasons") or []
    known_reason_ids = [r.get("reason_id") for r in reasons]

    # Matches are OWNED by a reason, so the map is per-reason, not one global
    # set. Checking membership in a flat set only asks "does this id exist
    # somewhere", which lets DR_1's PM_1 be verified under DR_2 -- the
    # verification would read as done while the match it actually belongs to
    # was never checked under its own reason.
    #
    # Only id-bearing matches participate. Pre-policy_match_id contracts
    # (CASE_021 was written before the field existed) have nothing to address,
    # so demanding a verification for them would report a legacy shape as
    # corruption. The schema is what requires the id on new writes; this layer
    # resolves ids that exist rather than retro-failing ones that never did.
    matches_by_reason = {
        r.get("reason_id"): [m.get("policy_match_id") for m in (r.get("policy_matches") or [])
                             if m.get("policy_match_id") is not None]
        for r in reasons
    }
    owner_of_match = {mid: rid for rid, mids in matches_by_reason.items() for mid in mids}

    validations = data.get("validations") or []
    seen_reason_ids = [v.get("reason_id") for v in validations]

    for orphan in [i for i in seen_reason_ids if i not in known_reason_ids]:
        errors.append(f"validations: reason_id {orphan!r} does not exist in {DENIAL_REASONS} "
                      f"(known: {known_reason_ids})")
    for dupe in _duplicates(seen_reason_ids):
        errors.append(f"validations: reason_id {dupe!r} appears more than once -- "
                      "exactly one validation per denial reason")
    for missing in [i for i in known_reason_ids if i not in seen_reason_ids]:
        errors.append(f"validations: denial reason {missing!r} has no validation -- "
                      "every reason must be validated, silently skipping one hides an unrebutted denial")

    seen_match_ids = []
    for validation in validations:
        rid = validation.get("reason_id")
        owned = matches_by_reason.get(rid, [])
        verified_here = [pmv.get("policy_match_id")
                         for pmv in (validation.get("policy_match_validations") or [])
                         if pmv.get("policy_match_id") is not None]
        seen_match_ids.extend(verified_here)

        for mid in verified_here:
            if mid in owned:
                continue
            owner = owner_of_match.get(mid)
            if owner is None:
                errors.append(f"{rid}: policy_match_id {mid!r} does not exist in {DENIAL_REASONS} "
                              f"(all known: {sorted(owner_of_match)})")
            else:
                errors.append(f"{rid}: policy_match_id {mid!r} belongs to {owner!r}, not {rid!r} -- "
                              "a match may only be verified under the reason that owns it")

        for dupe in _duplicates(verified_here):
            errors.append(f"{rid}: policy_match_id {dupe!r} verified more than once "
                          "within this validation")

        # Only demand completeness for a reason that actually has a validation
        # here; a missing validation is already reported above, and repeating
        # it once per owned match would bury that finding in noise.
        if rid in matches_by_reason:
            for missing in [m for m in owned if m not in verified_here]:
                errors.append(f"{rid}: policy match {missing!r} has no verification -- "
                              "an unverified match must not be presentable as checked")

    # Cross-validation duplicates (the same match verified under two different
    # reasons) are caught here; the per-validation loop above only sees one.
    for dupe in _duplicates(seen_match_ids):
        errors.append(f"policy_match_validations: policy_match_id {dupe!r} verified more than once "
                      "across validations")

    return errors


def check_screening_report(data: dict, case_dir: Path) -> list[str]:
    """screening_report's reason_ids must resolve AND land in the right section.

    The denial/reduction split is the whole point of the section pair: a
    denial says the insurer paid nothing on that ground, a reduction says it
    paid less. Existence-only checking let a reduction be summarized under
    denial (and vice versa), which inverts what the insurer actually decided
    while every id still resolved -- the screening report is the triage
    document a human reads first, so a reason filed under the wrong heading
    misdirects the entire review.
    """
    reasons_doc = _load(case_dir, DENIAL_REASONS)
    if reasons_doc is None:
        return []

    reasons = reasons_doc.get("denial_reasons") or []
    known = [r.get("reason_id") for r in reasons]
    decision_of = {r.get("reason_id"): r.get("decision_type") for r in reasons}

    errors = []
    for ref in _collect_reason_ids(data):
        if ref not in known:
            errors.append(f"screening_report references reason_id {ref!r}, which does not exist "
                          f"in {DENIAL_REASONS} (known: {known})")

    position = data.get("insurer_position") or {}
    sections = {
        "denial": (position.get("denial") or {}).get("reason_ids") or [],
        "reduction": (position.get("reduction") or {}).get("reason_ids") or [],
    }

    for section, ids in sections.items():
        for rid in ids:
            actual = decision_of.get(rid)
            if actual is None:
                continue  # unresolvable id already reported above
            if actual != section:
                errors.append(
                    f"insurer_position.{section}.reason_ids lists {rid!r}, whose decision_type is "
                    f"{actual!r} -- a {actual} must not be summarized as a {section}")
        for dupe in _duplicates(ids):
            errors.append(f"insurer_position.{section}.reason_ids lists {dupe!r} more than once")

    both = set(sections["denial"]) & set(sections["reduction"])
    for rid in sorted(both):
        errors.append(f"insurer_position: reason_id {rid!r} appears under BOTH denial and "
                      "reduction -- a reason has exactly one decision_type")

    return errors


def _collect_reason_ids(node) -> list[str]:
    """reason_ids appear at more than one depth in screening_report; walk for
    them rather than hard-coding a path that a schema revision would break."""
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("reason_ids", "denial_reason_ids") and isinstance(value, list):
                found.extend(v for v in value if isinstance(v, str))
            elif key == "reason_id" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_collect_reason_ids(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_collect_reason_ids(item))
    return found


def check(filename: str, data: dict, case_dir: Path) -> list[str]:
    """Dispatch for dao.write-contract. Unknown filenames return [] -- this
    layer is additive, never a gate a new contract has to register with."""
    base = Path(filename).name
    if base == DENIAL_REASONS:
        return check_denial_reason_result(data) + check_policy_matches(data, case_dir)
    if base == DENIAL_VALIDATION:
        return check_denial_validation_result(data, case_dir)
    if base.startswith("screening_report"):
        return check_screening_report(data, case_dir)
    return []
