#!/usr/bin/env python3
"""evaluate.py -- score a model OCR run against the ground truth.

Compares model output records ``{"class": ..., "scores": ...}`` (produced by the
routed / extractor pipelines) against the ground-truth records built by
``build_ground_truth.py``, and reports per-form accuracy.

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
                accuracy = 0.0, every text field = 0.0); if the class is right,
                each field is compared. (Classifier quality is read from an e2e
                run -- the pipeline's step-1 classification is written into each
                output's ``class`` -- so there is no separate classifier mode.)

Two kinds of field, scored two ways
-----------------------------------
A Vanderbilt record mixes two genuinely different tasks, and blending them into one
number would hide both:

``SCORE_FIELDS``   ``medication_status``, ``symptom_N``, ``performance_N``,
                   ``side_effect_N`` -- reading a mark out of a printed grid. Scored
                   by **exact string match**, and reported exactly as the cognitive
                   project reports it: per-document accuracy (matched fields / its
                   fields), summarized per form as a distribution -- mean, std,
                   median, Q1, Q3 -- plus the share of "perfect" docs.

``TEXT_FIELDS``    ``class_name``, ``comments`` -- transcribing handwriting. Exact
                   match is the wrong instrument (one wrong character fails a
                   500-character comment), so these are scored by **string
                   similarity** and reported in their own section, per field. They
                   are excluded from the per-document accuracy above, so a
                   transcription miss can never move the grid number.

Scoring the text fields -- two questions, asked separately
----------------------------------------------------------
A text field holds either a transcription or nothing at all: ``null`` (the box is on
the page and empty) or, for ``class_name``, ``"Not Available"`` (the page it lives on
is not in this scan). Averaging those two situations together produces a number that
mostly measures how often the box was empty, so they are reported as two questions:

**1. Detection -- did the model find a transcription where there was one?**
A plain 2x2, "positive" = a transcription is present:

  * **TP** gold has text, model has text (regardless of whether the text is right)
  * **FP** gold has none, model wrote one
  * **TN** gold has none, model has none
  * **FN** gold has text, model wrote none

reported as **sensitivity, specificity, PPV and NPV**.

**2. Transcription quality -- given that both sides have text, how close is it?**
Over the true positives only: normalized Levenshtein similarity, ``1 - dist/max_len``
(equivalently 1 - character error rate), summarized as mean / median / min plus the
share that match exactly.

Strings are normalized before comparison -- stripped, internal whitespace (including
the annotators' ``\\n``) collapsed to single spaces, and casefolded -- because the
question is transcription accuracy, not whitespace or capitalization agreement.

Keeping the two apart is what makes the numbers readable: most gold ``comments`` are
empty, so a model that never transcribes anything scores a high *specificity* and an
undefined *sensitivity* -- which is obvious -- rather than a flattering single average.

Outputs
-------
``--out-dir/<run_name>.txt``   Human-readable per-form report. ``<run_name>`` is
                               the last path component of ``--model``.
``--output-error/<stem>.json`` One file per document that had any error
                               (class mismatch, grid mismatches, and/or an
                               imperfect text field), holding gold vs pred for
                               later error analysis.

Usage
-----
    python scripts/evaluate.py \\
        --gold  ./ground_truth \\
        --model ./ocr/gpt-5.4-mini_zeroshot_e2e_v0 \\
        --mode  e2e \\
        --out-dir ./evaluation \\
        --output-error ./evaluation/errors/gpt-5.4-mini_zeroshot_e2e_v0

This script only reads the JSON records; it never opens the scanned images.
"""
import argparse
import glob
import json
import os
import re
from collections import OrderedDict, defaultdict

import numpy as np
from scipy import stats

# Canonical form order for the report (gold "class" values). Any other class seen
# in the gold set is appended after these.
FORM_ORDER = ["Vanderbilt:Scale_Parent", "Vanderbilt:Scale_Teacher",
              "Vanderbilt:Followup_Parent", "Vanderbilt:Followup_Teacher", "other"]

# Short column headers for the confusion matrix (full names stay on the rows).
CLASS_ABBREV = {
    "Vanderbilt:Scale_Parent": "S-Par",
    "Vanderbilt:Scale_Teacher": "S-Tch",
    "Vanderbilt:Followup_Parent": "F-Par",
    "Vanderbilt:Followup_Teacher": "F-Tch",
    "other": "other",
}

# The two handwriting-transcription fields, scored by similarity in their own section.
# Everything else in a record is a grid field scored by exact match.
TEXT_FIELDS = ("class_name", "comments")

_MISSING = object()   # a field absent from the prediction (distinct from a null value)

# No NA-equivalence table is needed here (the cognitive project has one for the
# Mini-Cog clock times). The extractor prompts and the labeling configs share one
# vocabulary by construction -- "Blank", "Not Available", the bare digits, the
# medication phrases, None/Mild/Moderate/Severe -- so every grid field is compared
# verbatim. If a run shows the model spelling a value differently ("N/A" for
# "Not Available", say), fold it here rather than touching the frozen gold.


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_records(dir_path):
    """stem -> record for every ``*.json`` under dir_path (searched recursively,
    so the pipeline's ``<run>/<TYPE>/<stem>.json`` layout works too)."""
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

    * ``null``            -> {} (no scorable fields, e.g. the "other" class)
    * dict                -> the dict itself (the shape every extractor emits)
    * one-element list    -> unwrapped, for a model whose JSON mode wraps the object
    * anything else (a raw unparsed string) -> None, meaning "malformed": the
      extractor output could not be parsed, so no field can match.
    """
    if scores is None:
        return OrderedDict()
    if isinstance(scores, list):
        if len(scores) == 1 and isinstance(scores[0], dict):
            return OrderedDict(scores[0])
        return None
    if isinstance(scores, dict):
        return OrderedDict(scores)
    return None


# --------------------------------------------------------------------------- #
# Grid fields -- exact match
# --------------------------------------------------------------------------- #
def values_equal(gold_v, pred_v):
    """Exact-match one grid field. null matches only null; everything else is
    compared as whitespace-stripped strings (values are already strings or null)."""
    if pred_v is _MISSING:
        return False
    if gold_v is None or pred_v is None:
        return gold_v is None and pred_v is None
    return str(gold_v).strip() == str(pred_v).strip()


# --------------------------------------------------------------------------- #
# Text fields -- string similarity
# --------------------------------------------------------------------------- #
_WS = re.compile(r"\s+")


def normalize_text(s):
    """Strip, collapse all whitespace runs (including newlines) to one space, casefold."""
    return _WS.sub(" ", str(s)).strip().casefold()


def levenshtein(a, b):
    """Edit distance between two strings (stdlib two-row DP -- no extra dependency)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1,          # deletion
                           cur[j - 1] + 1,       # insertion
                           prev[j - 1] + (ca != cb)))   # substitution
        prev = cur
    return prev[-1]


def similarity(a, b):
    """Normalized Levenshtein similarity in [0, 1]: 1 - dist / max(len). Equivalent
    to 1 - character error rate, so 0.95 means ~5% of characters are wrong."""
    if not a and not b:
        return 1.0
    return 1.0 - levenshtein(a, b) / max(len(a), len(b))


def sentinel_kind(v):
    """Which sentinel a text value is -- ``"empty"`` (null or an all-whitespace string)
    or ``"na"`` ("Not Available") -- or None if it is a real transcription."""
    if v is None:
        return "empty"
    if isinstance(v, str):
        t = v.strip()
        if not t:
            return "empty"
        if t == "Not Available":
            return "na"
    return None


def score_text_field(gold_v, pred_v):
    """Score one text field. Returns (score, category).

    Categories: ``empty_match`` / ``na_match`` (both sides the same sentinel -- an
    empty box, or a box that is not on this page), ``sentinel_mismatch`` (both
    sentinels but different -- empty vs "Not Available", i.e. the box was blank vs the
    box was not on the page), ``missed`` (gold has text, prediction does not),
    ``spurious`` (prediction has text, gold does not), ``missing_gold_text`` /
    ``missing_gold_empty`` (the field is absent from the prediction entirely --
    counted as "the model produced no transcription", split by what gold held),
    ``text_pair`` (both sides transcribed -- the only case that earns partial credit).
    """
    if pred_v is _MISSING:
        return 0.0, ("missing_gold_empty" if sentinel_kind(gold_v) else "missing_gold_text")
    gk, pk = sentinel_kind(gold_v), sentinel_kind(pred_v)
    if gk and pk:
        return (1.0, f"{gk}_match") if gk == pk else (0.0, "sentinel_mismatch")
    if gk:
        return 0.0, "spurious"
    if pk:
        return 0.0, "missed"
    return similarity(normalize_text(gold_v), normalize_text(pred_v)), "text_pair"


# --------------------------------------------------------------------------- #
# Per-document comparison
# --------------------------------------------------------------------------- #
def compare_doc(gold_rec, pred_rec):
    """Compare one document. Returns the raw comparison (no gating); the aggregator
    applies e2e's "class wrong -> 0"."""
    gold_class = gold_rec.get("class")
    pred_class = pred_rec.get("class")

    gold_fields = scores_to_fields(gold_rec.get("scores"))
    pred_fields = scores_to_fields(pred_rec.get("scores"))
    pred_malformed = pred_fields is None
    lookup = OrderedDict() if pred_malformed else pred_fields

    grid_matches = OrderedDict()
    mismatches = []
    text_results = OrderedDict()
    for field, gold_v in gold_fields.items():
        pred_v = lookup.get(field, _MISSING)
        if field in TEXT_FIELDS:
            score, category = score_text_field(gold_v, pred_v)
            text_results[field] = {
                "field": field, "gold": gold_v,
                "pred": None if pred_v is _MISSING else pred_v,
                "score": score, "category": category,
            }
            continue
        ok = values_equal(gold_v, pred_v)
        grid_matches[field] = ok
        if not ok:
            mismatches.append({"field": field, "gold": gold_v,
                               "pred": None if pred_v is _MISSING else pred_v})
    return {
        "gold_class": gold_class,
        "pred_class": pred_class,
        "class_correct": gold_class == pred_class,
        "n_grid_fields": len(grid_matches),
        "grid_matches": grid_matches,
        "mismatches": mismatches,
        "text_results": text_results,
        "pred_malformed": pred_malformed,
    }


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
class TextStats:
    """One text field, one form type: a detection 2x2 plus transcription quality.

    "Positive" means a transcription is present -- gold is positive when the annotator
    typed something in the box, the prediction is positive when the model emitted text
    rather than null / "Not Available".
    """
    def __init__(self):
        self.tp = self.fp = self.tn = self.fn = 0
        self.sims = []               # similarity, one per true positive
        self.n_exact = 0             # true positives that matched exactly
        self.sentinel_mismatch = 0   # agreed "no text" but disagreed null vs "Not Available"
        self.field_absent = 0        # field missing from the prediction altogether
        self.gated = 0               # e2e: excluded because the class was wrong

    def add(self, category, score):
        if category == "text_pair":
            self.tp += 1
            self.sims.append(score)
            self.n_exact += (score == 1.0)
        elif category == "spurious":
            self.fp += 1
        elif category == "missed":
            self.fn += 1
        elif category == "missing_gold_text":
            self.fn += 1
            self.field_absent += 1
        elif category == "missing_gold_empty":
            self.tn += 1
            self.field_absent += 1
        elif category == "sentinel_mismatch":
            # Both sides said "no text", so it is a true negative for detection; the
            # null-vs-"Not Available" disagreement is footnoted instead.
            self.tn += 1
            self.sentinel_mismatch += 1
        else:                        # empty_match / na_match
            self.tn += 1

    def merge(self, other):
        self.tp += other.tp
        self.fp += other.fp
        self.tn += other.tn
        self.fn += other.fn
        self.sims.extend(other.sims)
        self.n_exact += other.n_exact
        self.sentinel_mismatch += other.sentinel_mismatch
        self.field_absent += other.field_absent
        self.gated += other.gated


class FormStats:
    def __init__(self):
        self.n_docs = 0
        self.class_correct = 0
        self.field_accs = []                        # per-doc grid accuracy
        self.text = defaultdict(TextStats)          # field -> TextStats


def aggregate(stats, cmp, mode):
    """Fold one document's comparison into its form's stats, applying mode gating."""
    s = stats[cmp["gold_class"]]
    s.n_docs += 1
    if cmp["class_correct"]:
        s.class_correct += 1

    # e2e: a wrong class zeroes the whole document -- the grid and the text fields alike,
    # since the extractor that ran was the wrong form's.
    gated = (mode == "e2e" and not cmp["class_correct"])

    if cmp["n_grid_fields"]:
        acc = 0.0 if gated else sum(cmp["grid_matches"].values()) / cmp["n_grid_fields"]
        s.field_accs.append(acc)

    # A wrong class means the wrong form's extractor ran, so "did it find the comment"
    # is not a meaningful question for that document: it is excluded from the detection
    # 2x2 and counted separately. (The grid metric zeroes such documents instead; the
    # class error itself is already reported by class accuracy and the confusion matrix.)
    for field, res in cmp["text_results"].items():
        if gated:
            s.text[field].gated += 1
        else:
            s.text[field].add(res["category"], res["score"])


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


def text_field_lines(field, t):
    """Report block for one text field of one form: detection 2x2, then similarity."""
    n = t.tp + t.fp + t.tn + t.fn
    if not n and not t.gated:
        return []
    lines = [f"  {field}:"]
    lines.append(f"      detection (is a transcription present?)   "
                 f"TP {t.tp}   FP {t.fp}   TN {t.tn}   FN {t.fn}   (n={n})")
    lines.append(f"        sensitivity {frac(t.tp, t.tp + t.fn):<16} "
                 f"specificity {frac(t.tn, t.tn + t.fp)}")
    lines.append(f"        PPV         {frac(t.tp, t.tp + t.fp):<16} "
                 f"NPV         {frac(t.tn, t.tn + t.fn)}")
    if t.sims:
        a = np.asarray(t.sims, dtype=float)
        lines.append(f"      similarity (the {t.tp} true positive(s)) "
                     f"mean {a.mean():.3f}   median {np.median(a):.3f}   "
                     f"min {a.min():.3f}   exact {frac(t.n_exact, t.tp)}")
    else:
        lines.append("      similarity: (no document had a transcription on both sides)")
    notes = []
    if t.sentinel_mismatch:
        notes.append(f'{t.sentinel_mismatch} TN(s) agreed "no text" but disagreed '
                     f'empty vs "Not Available"')
    if t.field_absent:
        notes.append(f"{t.field_absent} absent from the prediction entirely")
    if t.gated:
        notes.append(f"{t.gated} excluded (predicted class was wrong)")
    if notes:
        lines.append("      note: " + "; ".join(notes))
    return lines


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
    lines.append(f"  {'class':<30}{'precision':>10}{'recall':>9}{'F1':>8}{'support':>9}")
    precs, recs, f1s = [], [], []
    for c in labels:
        s, pt, t = support[c], pred_tot[c], tp[c]
        prec = t / pt if pt else 0.0
        rec = t / s if s else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        lines.append(f"  {c:<30}{prec:>10.3f}{rec:>9.3f}{f1:>8.3f}{s:>9}")
        precs.append(prec)
        recs.append(rec)
        f1s.append(f1)
    lines.append(f"  {'macro avg':<30}{np.mean(precs):>10.3f}{np.mean(recs):>9.3f}"
                 f"{np.mean(f1s):>8.3f}{total:>9}")
    acc = correct / total if total else float("nan")
    lines.append(f"  {'accuracy':<30}{'':>10}{'':>9}{acc:>8.3f}{total:>9}")
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
    if missing:
        lines.append(f"Gold documents with no model output (not scored): {len(missing)}.")
    if extra:
        lines.append(f"Model outputs with no gold match (ignored): {len(extra)}.")
    lines.append("")
    lines.append("Grid fields (medication_status / symptom / performance / side_effect) are scored")
    lines.append("by exact match and drive the per-doc field accuracy below.")
    lines.append("")
    lines.append("class_name and comments are handwriting transcriptions and are reported")
    lines.append("separately, in two parts: whether the model FOUND a transcription where there")
    lines.append("was one (a detection 2x2 -- positive = text is present), and, for the true")
    lines.append("positives only, how CLOSE the text is (normalized edit-distance similarity,")
    lines.append("1.000 = identical). They are excluded from the per-doc field accuracy.")
    lines.append("")

    def emit(name, s):
        lines.append(f"{name:<30} n={s.n_docs}")
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
        for field in TEXT_FIELDS:
            if field in s.text:
                lines.extend(text_field_lines(field, s.text[field]))
        lines.append("")

    tot = FormStats()
    for form in ordered_forms:
        s = stats[form]
        emit(form, s)
        tot.n_docs += s.n_docs
        tot.class_correct += s.class_correct
        tot.field_accs.extend(s.field_accs)
        for field, t in s.text.items():
            tot.text[field].merge(t)

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
        # Text fields are listed whenever they are not a perfect match, with the
        # similarity that was awarded, so error analysis can see near-misses.
        "text_fields": [r for r in cmp["text_results"].values() if r["score"] < 1.0],
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

    stats_by_form = defaultdict(FormStats)
    confusion = defaultdict(int)   # {(gold_class, pred_class): count}
    n_err = 0
    for stem in scored:
        cmp = compare_doc(gold[stem], model[stem])
        aggregate(stats_by_form, cmp, args.mode)
        if args.mode == "e2e":
            confusion[(cmp["gold_class"], cmp["pred_class"])] += 1
        imperfect_text = any(r["score"] < 1.0 for r in cmp["text_results"].values())
        if not cmp["class_correct"] or cmp["mismatches"] or imperfect_text:
            n_err += 1
            if args.output_error:
                write_error_file(args.output_error, stem, cmp)

    # Report forms in canonical order, then any unexpected classes seen.
    ordered_forms = [f for f in FORM_ORDER if f in stats_by_form]
    ordered_forms += [f for f in stats_by_form if f not in FORM_ORDER]

    report = build_report(run_name, args.mode, args.gold, args.model, stats_by_form,
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
