#!/usr/bin/env python3
"""evaluate.py -- score a model OCR run against the ground truth.

Compares model output records ``{"class": ..., "scores": ...}`` (produced by the
routed / classifier / extractor pipelines) against the ground-truth records built
by ``build_ground_truth.py``, and reports per-form accuracy.

Modes
-----
``extractor``   Only the ``scores`` fields are scored (the extractor is run on a
                known form type, so classification is not in play). Ground-truth
                ``other`` pages are out of scope here (no extractor handles them)
                and are excluded, not counted as missing.

``e2e``         Both classification and extraction. A per-class precision /
                recall / F1 report and a confusion matrix are produced, and
                extraction is *gated* on the class: if a document's predicted
                class is wrong the whole document scores 0 (per-doc field
                accuracy = 0.0); if the class is right, each field is compared.
                (Classifier quality is read from an e2e run -- the pipeline's
                step-1 classification is written into each output's ``class`` --
                so there is no separate classifier mode.)

Field accuracy is measured **per document**: each form gets its own accuracy
(matched fields / its fields), and per form we report the **distribution** of
those per-doc accuracies -- mean, std, median, Q1, Q3 -- plus the share of
"perfect" docs (accuracy == 1.0). ``other`` pages have no fields and are excluded
from the field distribution (classification accuracy still covers them).

Scoring is **exact string match**, field by field (matched by question label, or
by key for the Mini-Cog clock object). ``null`` matches only ``null`` -- e.g. a
gold ``"illegible"`` vs a predicted ``"3"`` is wrong, and the MoCA MEMORY row
(gold ``null``) matches only a predicted ``null``. The one exception is the pair
of Mini-Cog clock time fields, where "no time here" has two spellings on the two
sides of the comparison; see ``NA_EQUIVALENT_FIELDS``.

Outputs
-------
``--out-dir/<run_name>.txt``   Human-readable per-form report. ``<run_name>`` is
                               the last path component of ``--model``.
``--output-error/<stem>.json`` One file per document that had any error
                               (class mismatch and/or field mismatches), holding
                               gold vs pred for later error analysis.

Usage
-----
    python scripts/evaluate.py \
        --gold  ground_truth \
        --model ocr/gpt-5.4-mini_e2e_v0 \
        --mode  e2e \
        --out-dir ./evaluation \
        --output-error ./evaluation/errors/gpt-5.4-mini_e2e_v0

This script only reads the JSON records; it never opens the scanned images.
"""
import argparse
import glob
import json
import os
from collections import OrderedDict, defaultdict

import numpy as np
from scipy import stats

# Canonical form order for the report (gold "class" values). Any other class seen
# in the gold set is appended after these.
FORM_ORDER = ["MMSE", "MoCA", "Mini-Cog:Instruction", "Mini-Cog:Clock_Drawing", "other"]

# Short column headers for the confusion matrix (full names stay on the rows).
CLASS_ABBREV = {
    "MMSE": "MMSE",
    "MoCA": "MoCA",
    "Mini-Cog:Instruction": "MC-Inst",
    "Mini-Cog:Clock_Drawing": "MC-Clock",
    "other": "other",
}

_MISSING = object()   # a field absent from the prediction (distinct from a null value)

# Fields where "there is no time here" is spelled two different ways on the two
# sides of the comparison, so a null-vs-"na" difference is a vocabulary artifact
# rather than an extraction error:
#
#   * the Mini-Cog clock labeling config tells annotators to type "NA" when a time
#     cannot be read, and does so for BOTH time fields;
#   * the clock extractor prompt asks for "na" on ``clock_time`` but ``null`` on
#     ``written_time`` (the one field it allows to be null).
#
# So a page where gold and model agree that no time is present can still be scored
# wrong ("na" vs null). On these two fields only, every "no time" spelling is
# folded to null before comparing. Both the annotation set and the model runs are
# final, so this is fixed here rather than upstream. Every other field -- including
# the MMSE / MoCA "TOTAL" rows, where both sides are asked for "na" -- is still
# compared verbatim, so a model emitting null where "na" was specified is wrong.
NA_EQUIVALENT_FIELDS = {"clock_time", "written_time"}
NA_TOKENS = {"", "na", "n/a", "none"}


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_records(dir_path):
    """stem -> record for every ``*.json`` under dir_path (searched recursively,
    so the classifier's ``<run>/<TYPE>/<stem>.json`` layout works too)."""
    records = {}
    for path in glob.glob(os.path.join(dir_path, "**", "*.json"), recursive=True):
        stem = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path) as f:
                records[stem] = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"WARNING: could not read {path}: {e}")
    return records


def scores_to_fields(scores):
    """Normalize a record's ``scores`` to an ordered {field: value} dict.

    * ``null``           -> {} (no scorable fields, e.g. the "other" class)
    * list of {question, points} -> {question: points}
    * dict (clock page)  -> the dict itself
    * anything else (a raw unparsed string) -> None, meaning "malformed": the
      extractor output could not be parsed, so no field can match.
    """
    if scores is None:
        return OrderedDict()
    if isinstance(scores, list):
        # The Mini-Cog clock object is a bare dict, but the model's JSON mode may
        # wrap it in a one-element array ([{...}]); unwrap that to the dict.
        if len(scores) == 1 and isinstance(scores[0], dict) and "question" not in scores[0]:
            return OrderedDict(scores[0])
        out = OrderedDict()
        for item in scores:
            if isinstance(item, dict) and "question" in item:
                out[item["question"]] = item.get("points")
        return out
    if isinstance(scores, dict):
        return OrderedDict(scores)
    return None


def canonical_na(value, field):
    """Fold an NA-equivalent field's "no value here" spellings to null.
    Any other field, and any real value, is returned untouched."""
    if field not in NA_EQUIVALENT_FIELDS or value is None:
        return value
    return None if str(value).strip().lower() in NA_TOKENS else value


def values_equal(gold_v, pred_v, field=None):
    """Exact-match one field. null matches only null; everything else is compared
    as whitespace-stripped strings (values are already strings or null). On an
    ``NA_EQUIVALENT_FIELDS`` field the "no value" spellings are canonicalized
    first, so gold "na" and a predicted null count as a match."""
    if pred_v is _MISSING:
        return False
    gold_v = canonical_na(gold_v, field)
    pred_v = canonical_na(pred_v, field)
    if gold_v is None or pred_v is None:
        return gold_v is None and pred_v is None
    return str(gold_v).strip() == str(pred_v).strip()


# --------------------------------------------------------------------------- #
# Per-document comparison
# --------------------------------------------------------------------------- #
def compare_doc(gold_rec, pred_rec):
    """Compare one document. Returns a dict with the raw comparison (no gating):

        class_correct : bool
        gold_fields   : {field: value}          (the canonical field set)
        n_fields      : int
        field_matches : {field: bool}           (per-field exact match, class-blind)
        mismatches    : [{field, gold, pred}]   (fields that did not match)
    Gating (e2e's "class wrong -> 0") is applied later, in the aggregator.
    """
    gold_class = gold_rec.get("class")
    pred_class = pred_rec.get("class")
    class_correct = (gold_class == pred_class)

    gold_fields = scores_to_fields(gold_rec.get("scores"))
    pred_fields = scores_to_fields(pred_rec.get("scores"))
    pred_malformed = pred_fields is None
    lookup = OrderedDict() if pred_malformed else pred_fields

    field_matches = OrderedDict()
    mismatches = []
    for field, gold_v in gold_fields.items():
        pred_v = lookup.get(field, _MISSING)
        ok = values_equal(gold_v, pred_v, field)
        field_matches[field] = ok
        if not ok:
            mismatches.append({
                "field": field,
                "gold": gold_v,
                "pred": None if pred_v is _MISSING else pred_v,
            })
    return {
        "gold_class": gold_class,
        "pred_class": pred_class,
        "class_correct": class_correct,
        "gold_fields": gold_fields,
        "n_fields": len(gold_fields),
        "field_matches": field_matches,
        "mismatches": mismatches,
        "pred_malformed": pred_malformed,
    }


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
class FormStats:
    def __init__(self):
        self.n_docs = 0
        self.class_correct = 0
        self.field_accs = []     # one per-document field accuracy (docs with >=1 field)


def aggregate(stats, cmp, mode):
    """Fold one document's comparison into its form's stats, applying mode gating."""
    s = stats[cmp["gold_class"]]
    s.n_docs += 1
    if cmp["class_correct"]:
        s.class_correct += 1

    n_fields = cmp["n_fields"]
    if n_fields == 0:
        return                                  # e.g. "other": no extraction fields

    if mode == "e2e" and not cmp["class_correct"]:
        acc = 0.0                               # class wrong -> whole doc scores 0
    else:
        acc = sum(cmp["field_matches"].values()) / n_fields
    s.field_accs.append(acc)


def mean_ci(a, conf=0.95):
    """Two-sided t-based confidence interval for the mean of sample ``a``."""
    n = len(a)
    m = float(a.mean())
    if n < 2:
        return m, m
    se = a.std(ddof=1) / np.sqrt(n)
    tcrit = stats.t.ppf(0.5 + conf / 2, df=n - 1)
    return m - tcrit * se, m + tcrit * se


def wilson_ci(k, n, conf=0.95):
    """Wilson score confidence interval for a proportion k/n. Robust near 0 and 1
    (a Wald interval would give zero width at 100/100)."""
    if n == 0:
        return float("nan"), float("nan")
    z = stats.norm.ppf(0.5 + conf / 2)
    phat = k / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    half = z * np.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return center - half, center + half


def describe(accs):
    """Distribution summary of a list of per-document accuracies."""
    a = np.asarray(accs, dtype=float)
    n = len(a)
    q1, med, q3 = np.percentile(a, [25, 50, 75])
    ci_lo, ci_hi = mean_ci(a)
    return {
        "n": n,
        "mean": a.mean(),
        "ci_lo": ci_lo,
        "ci_hi": ci_hi,
        "std": a.std(ddof=1) if n > 1 else 0.0,   # sample std
        "median": med,
        "q1": q1,
        "q3": q3,
        "perfect": int((a == 1.0).sum()),
    }


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def pct(n, d):
    return f"{n / d:.3f}" if d else "-"


def frac(n, d):
    return f"{pct(n, d)} ({n}/{d})" if d else "-"


def classification_labels(confusion):
    """Class labels present in the confusion counts, in canonical order."""
    seen = set()
    for gold, pred in confusion:
        seen.add(gold)
        seen.add(pred)
    ordered = [c for c in FORM_ORDER if c in seen]
    ordered += sorted(c for c in seen if c not in FORM_ORDER)
    return ordered


def classification_report_lines(confusion, labels):
    """Per-class precision / recall / F1 / support, plus macro avg and accuracy.
    ``confusion`` is a {(gold, pred): count} mapping. Uses zero_division=0 semantics
    (no predictions for a class -> precision 0)."""
    support = defaultdict(int)   # # gold == c
    pred_tot = defaultdict(int)  # # pred == c
    tp = defaultdict(int)
    total = correct = 0
    for (gold, pred), n in confusion.items():
        support[gold] += n
        pred_tot[pred] += n
        total += n
        if gold == pred:
            tp[gold] += n
            correct += n

    lines = ["Classification report (per class):"]
    lines.append(f"  {'class':<24}{'precision':>10}{'recall':>9}{'F1':>8}{'support':>9}")
    precs, recs, f1s = [], [], []
    for c in labels:
        s, pt, t = support[c], pred_tot[c], tp[c]
        prec = t / pt if pt else 0.0
        rec = t / s if s else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        lines.append(f"  {c:<24}{prec:>10.3f}{rec:>9.3f}{f1:>8.3f}{s:>9}")
        precs.append(prec)
        recs.append(rec)
        f1s.append(f1)
    lines.append(f"  {'macro avg':<24}{np.mean(precs):>10.3f}{np.mean(recs):>9.3f}"
                 f"{np.mean(f1s):>8.3f}{total:>9}")
    acc = correct / total if total else float("nan")
    lines.append(f"  {'accuracy':<24}{'':>10}{'':>9}{acc:>8.3f}{total:>9}")
    return lines


def confusion_matrix_lines(confusion, labels):
    """Text confusion matrix: rows = gold, columns = predicted."""
    heads = [CLASS_ABBREV.get(c, c) for c in labels]
    w = max(9, max(len(h) for h in heads) + 2)
    rowlabel_w = max(len(c) for c in labels) + 2
    lines = ["Confusion matrix (rows = gold, cols = predicted):"]
    lines.append(" " * rowlabel_w + "".join(f"{h:>{w}}" for h in heads))
    for gold in labels:
        cells = "".join(f"{confusion.get((gold, pred), 0):>{w}}" for pred in labels)
        lines.append(f"{gold:<{rowlabel_w}}{cells}")
    abbreviated = [f"{CLASS_ABBREV[c]}={c}" for c in labels
                   if c in CLASS_ABBREV and CLASS_ABBREV[c] != c]
    if abbreviated:
        lines.append("  cols: " + ", ".join(abbreviated))
    return lines


def build_report(run_name, mode, gold_dir, model_dir, stats, ordered_forms,
                 n_scored, missing, extra, confusion=None):
    lines = []
    lines.append(f"Run:   {run_name}")
    lines.append(f"Mode:  {mode}")
    lines.append(f"Gold:  {gold_dir}")
    lines.append(f"Model: {model_dir}")
    lines.append(f"Scored {n_scored} document(s).")
    if extra:
        lines.append(f"Model outputs with no gold match (ignored): {len(extra)}.")
    lines.append("")

    def emit(name, s):
        lines.append(f"{name:<24} n={s.n_docs}")
        if mode == "e2e":
            lo, hi = wilson_ci(s.class_correct, s.n_docs)
            lines.append(f"  class accuracy:           {frac(s.class_correct, s.n_docs)}"
                         f"   95% CI [{lo:.3f}, {hi:.3f}]")
        if s.field_accs:                        # extractor + e2e both score fields
            d = describe(s.field_accs)
            lines.append(
                f"  per-doc field accuracy:   mean {d['mean']:.3f} "
                f"(95% CI [{d['ci_lo']:.3f}, {d['ci_hi']:.3f}])   std {d['std']:.3f}   "
                f"median {d['median']:.3f}   Q1 {d['q1']:.3f}   Q3 {d['q3']:.3f}   "
                f"(over {d['n']} docs)")
            lines.append(f"  perfect docs (acc=1.0):   {frac(d['perfect'], d['n'])}")
        else:
            lines.append("  per-doc field accuracy:   (no extraction fields)")
        lines.append("")

    tot = FormStats()
    for form in ordered_forms:
        s = stats[form]
        emit(form, s)
        tot.n_docs += s.n_docs
        tot.class_correct += s.class_correct
        tot.field_accs.extend(s.field_accs)

    lines.append("=" * 60)
    emit("OVERALL", tot)

    # Classification quality (e2e only -- extractor is given the class).
    if mode == "e2e" and confusion:
        labels = classification_labels(confusion)
        lines.append("=" * 60)
        lines.extend(classification_report_lines(confusion, labels))
        lines.append("")
        lines.extend(confusion_matrix_lines(confusion, labels))
        lines.append("")

    return "\n".join(lines) + "\n"


def write_error_file(error_dir, stem, cmp):
    """One JSON per errored document, for downstream error analysis."""
    record = {
        "filename": f"{stem}.png",
        "form": cmp["gold_class"],
        "gold_class": cmp["gold_class"],
        "pred_class": cmp["pred_class"],
        "class_correct": cmp["class_correct"],
        "pred_malformed": cmp["pred_malformed"],
        "mismatches": cmp["mismatches"],
    }
    with open(os.path.join(error_dir, f"{stem}.json"), "w") as f:
        json.dump(record, f, indent=2)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gold", required=True, help="Ground-truth directory")
    parser.add_argument("--model", required=True, help="Model output directory "
                        "(its last path component is the run name)")
    parser.add_argument("--mode", required=True, choices=["extractor", "e2e"])
    parser.add_argument("--out-dir", default="./evaluation",
                        help="Where to write <run_name>.txt (default: ./evaluation)")
    parser.add_argument("--output-error", default=None,
                        help="Directory for per-document error JSON files (optional)")
    args = parser.parse_args()

    run_name = os.path.basename(os.path.normpath(args.model))

    gold = load_records(args.gold)
    model = load_records(args.model)
    if not gold:
        raise SystemExit(f"No gold records found under {args.gold}")

    # Which gold docs are in scope for this mode.
    gold_stems = set(gold)
    if args.mode == "extractor":
        # "other" pages have no extractor -> out of scope, not "missing".
        gold_stems = {s for s in gold_stems if gold[s].get("class") != "other"}

    scored = sorted(gold_stems & set(model))
    missing = gold_stems - set(model)          # in scope but not produced
    extra = set(model) - set(gold)             # produced but no gold

    if args.output_error:
        os.makedirs(args.output_error, exist_ok=True)
        # One file per ERRORED document, so a document that stops having errors
        # simply stops being written -- its file from the previous run would linger
        # and be read back as a current error (by build_error_analysis_project.py,
        # among others). Clearing first makes the directory a true snapshot of THIS
        # run. Only *.json is removed, and only at the top level, so the directory
        # can be a plain output target without swallowing anything else.
        for stale in glob.glob(os.path.join(args.output_error, "*.json")):
            os.remove(stale)

    stats = defaultdict(FormStats)
    confusion = defaultdict(int)   # {(gold_class, pred_class): count}
    n_err = 0
    for stem in scored:
        cmp = compare_doc(gold[stem], model[stem])
        aggregate(stats, cmp, args.mode)
        if args.mode == "e2e":
            confusion[(cmp["gold_class"], cmp["pred_class"])] += 1
        if not cmp["class_correct"] or cmp["mismatches"]:
            n_err += 1
            if args.output_error:
                write_error_file(args.output_error, stem, cmp)

    # Report forms in canonical order, then any unexpected classes seen.
    ordered_forms = [f for f in FORM_ORDER if f in stats]
    ordered_forms += [f for f in stats if f not in FORM_ORDER]

    report = build_report(run_name, args.mode, args.gold, args.model, stats,
                          ordered_forms, len(scored), missing, extra, confusion)

    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir, f"{run_name}.txt")
    with open(out_path, "w") as f:
        f.write(report)

    print(report)
    print(f"Wrote report to {out_path}")
    if args.output_error:
        print(f"Wrote {n_err} error file(s) to {args.output_error}")


if __name__ == "__main__":
    main()
