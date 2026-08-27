# Is the fidelity pipeline reproducible and robust? — measured 2026-08-27

Everything below was run, not reasoned about. Where a property was not tested,
it says so.

## What is reproducible

**The arithmetic and the record.** Recomputing CASE_705 from the same inputs
produced data identical to the stored result (`data equal: True`; the bytes
differ only because the DAO serialises with its own formatting). The weights,
the weighted total, the verdict thresholds and the schema conditions are code.

**The gates.** 57 tests across the four fidelity suites pass — stage gate,
review token, filename restriction, traversal, leak boundary, F4 exclusion,
core-field verdict pinning.

**The values read out of the answer key.** The keys are scanned PDFs with no
text layer, so every read is a fresh vision transcription — and the text is not
stable: three reads of CASE_710's key gave 1932 / 2068 / 1965 characters, with
char-similarity 0.9350 and 0.9151 against the first, and 35–59 of ~100 lines
differing.

But the numbers are stable. Across all three reads the numeric token multiset
was **identical** (60 tokens each, zero tokens present in one read and absent
from another). Layout, line breaks and spacing move; dates, codes, amounts and
percentages did not. Measured on one 3-page key; the two 12-page keys were not
tested this way.

## What is not reproducible

**The scoring is a single-rater judgement, and the rater chose the denominators.**
The rubric fixes the weights and the arithmetic. It does not fix the item set:
which fields get compared, how many issues the answer key is said to contain,
which documents count as required. Those were the scorer's calls, made after
reading both documents, by the same agent that wrote the rubric.

Recomputed under other defensible calls:

| Variant | CASE_705 | CASE_711 |
| --- | --- | --- |
| as scored | **68.7** partial | **50.1** divergent |
| key-absent field counted as a miss | 62.7 | 48.2 |
| disability block scored as one field, not four | 65.9 | 43.2 |
| answer key enumerated with 2 more issues | 65.7 | 47.9 |
| answer key enumerated with 2 fewer issues | 73.7 | 53.9 |
| 사고경위서 not treated as required | 71.7 | 53.1 |
| *(discretionary folded into the headline — the rubric forbids this)* | *56.4, verdict flips* | *40.8* |

Excluding the variant the rubric already rules out, CASE_705 ranges **62.7–73.7**
and CASE_711 **43.2–53.9**. Neither verdict flips inside that range, but an
11-point and a 10.7-point spread come from choices no rule constrains. The one
variant that does flip a verdict is the one the rubric explicitly closes — which
is evidence the exclusion of discretionary agreement was load-bearing, not
decorative.

**There is no re-executable scoring run.** The scorer is an agent procedure, not
a program. The scripts that produced these two results were ad hoc files under
the session scratchpad; the repository holds the rubric, the contract and the
gates, but nothing that re-runs a scoring from the case and the key.

**Test–retest and inter-rater agreement are unknown.** Each case was scored once,
by one rater, in one context. No second pass has been run, so the spread above is
a sensitivity analysis, not a measured variance.

**A rescore silently overwrites.** Writing `screening_fidelity_result_v1.json`
again for CASE_705 succeeded with no warning and no history. The `_v1` in the
name is the result-format version, not a run counter, so two scoring runs of the
same case cannot be compared after the fact — the first is simply gone.

## What would close each gap

1. **Fix the item universe.** The rubric should name a required field list per
   case type, with anything extra recorded separately. Until then the F1
   denominator is the scorer's, and the score is not comparable across cases —
   CASE_705 was scored over 11 fact fields and CASE_711 over 13, chosen by the
   same rater with no rule to appeal to.
2. **Name the issue and document sets the same way**, or record explicitly that
   the denominator was scorer-enumerated.
3. **Make a rescore additive**: include the `run_id` in the filename, or refuse
   to overwrite a result whose `target_report_sha256` matches.
4. **Record what was read**: store a hash of the transcription and of its numeric
   multiset in the result, so a later run can tell whether the key it read was
   the key the score was based on. Cheap, and it does not persist any answer-key
   text.
5. **Run test–retest** — the same case scored twice in context-isolated passes,
   and the disagreement reported. That needs a second agent, which this session
   does not dispatch without being asked.
