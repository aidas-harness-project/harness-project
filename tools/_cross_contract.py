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

    match_ids = [m.get("policy_match_id") for r in reasons for m in (r.get("policy_matches") or [])]
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
            if expected is not None and label != expected:
                errors.append(f"{rid}: taxonomy_label {label!r} does not match the codebook label "
                              f"for {code} ({expected!r})")

    return errors


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
    # Only id-bearing matches participate. Pre-policy_match_id contracts
    # (CASE_021 was written before the field existed) have nothing to address,
    # so demanding a verification for them would report a legacy shape as
    # corruption. The schema is what requires the id on new writes; this layer
    # resolves ids that exist rather than retro-failing ones that never did.
    known_match_ids = [m.get("policy_match_id")
                       for r in reasons for m in (r.get("policy_matches") or [])
                       if m.get("policy_match_id") is not None]

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

    seen_match_ids = [pmv.get("policy_match_id")
                      for v in validations for pmv in (v.get("policy_match_validations") or [])
                      if pmv.get("policy_match_id") is not None]

    for orphan in [i for i in seen_match_ids if i not in known_match_ids]:
        errors.append(f"policy_match_validations: policy_match_id {orphan!r} does not exist in "
                      f"{DENIAL_REASONS} (known: {known_match_ids})")
    for dupe in _duplicates(seen_match_ids):
        errors.append(f"policy_match_validations: policy_match_id {dupe!r} verified more than once")
    for missing in [i for i in known_match_ids if i not in seen_match_ids]:
        errors.append(f"policy_match_validations: policy match {missing!r} has no verification -- "
                      "an unverified match must not be presentable as checked")

    return errors


def check_screening_report(data: dict, case_dir: Path) -> list[str]:
    """screening_report's reason_ids must resolve to real denial reasons."""
    reasons_doc = _load(case_dir, DENIAL_REASONS)
    if reasons_doc is None:
        return []
    known = [r.get("reason_id") for r in (reasons_doc.get("denial_reasons") or [])]

    errors = []
    for ref in _collect_reason_ids(data):
        if ref not in known:
            errors.append(f"screening_report references reason_id {ref!r}, which does not exist "
                          f"in {DENIAL_REASONS} (known: {known})")
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
        return check_denial_reason_result(data)
    if base == DENIAL_VALIDATION:
        return check_denial_validation_result(data, case_dir)
    if base.startswith("screening_report"):
        return check_screening_report(data, case_dir)
    return []
