#!/usr/bin/env python3
"""Generate ground-truth score files from the demo's Label Studio exports.

Output is identical in shape to the real pipeline's
``ADHD_assessment/scripts/build_ground_truth.py`` -- one JSON file per page,
named ``<stem>.json``, shaped as::

    {"class": <form>, "scores": <flat {field: value} object>}

so a model run under ``ocr/<run>/`` can be scored file-for-file against
``ground_truth/``, and the pipeline's ``evaluate.py`` reads it unchanged.

What the demo drops
-------------------
The real project has three annotators, so it splits each form into an
*agreement* set (everyone labels it, reconciled afterwards through a modifier
file) and a *production* set (one annotator each). The demo has ONE annotator,
so there is exactly one annotation per task and it is taken as-is: no splits, no
modifier, no ``completed_by`` whitelist. It also has no Stage-1 inclusion pass,
so there are no ``"other"`` pages with ``scores: null``.

Inputs (Label Studio exports in ``annotation/``)
------------------------------------------------
One JSON export per form type -- ``Parent.json``, ``Teacher.json``,
``Parent_followup.json``, ``Teacher_followup.json``.

The one/two page problem
------------------------
A Vanderbilt form spans two printed pages but a task is a single page image, so
each labeling config lists ALL of the form's questions and the annotator first
ticks ``visible_pages`` to say which page(s) the scan actually shows. Questions
belonging to a page that was not ticked are hidden from the annotator and,
per the labeling-config note, "recorded as Not Available in post-processing" --
that post-processing is here.

Faithfulness note
-----------------
Annotator vocabulary is preserved verbatim: ``"Blank"``, ``"Not Available"``,
``"None"``/``"Mild"``/``"Moderate"``/``"Severe"``, ``"was on medication"`` and the
bare digits are written straight through, with no case folding and no remapping
to ``null``. Any normalization for scoring belongs in the eval script, not here.

``null`` is emitted in exactly two situations, matching the pipeline:

  * a free-text box (``class_name``, ``comments``) left empty on a page that
    *was* visible -- an empty TextArea is simply absent from the export;
  * a choice field on a visible page that was never clicked. Per-question fields
    are optional in the labeling configs (only ``visible_pages`` is required), so
    this is a real gap: it cannot be told apart from "Blank" after the fact, so
    it is left null and reported as a WARNING rather than guessed at.

Usage:
    python scripts/build_ground_truth.py
"""
import argparse
import json
import os
from collections import OrderedDict, defaultdict

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ANNOTATION_DIR = os.path.join(PROJECT_DIR, "annotation")
DEFAULT_OUT_DIR = os.path.join(PROJECT_DIR, "ground_truth")

# The value written for every field of a page the scan does not show.
NOT_AVAILABLE = "Not Available"

# The two free-text boxes. Absent == the box was left empty, which is a real
# null rather than a gap, so these are never warned about.
FREETEXT_FIELDS = ("class_name", "comments")

# ``comments`` sits outside the page toggles in every config -- it is always
# shown, so it is never "Not Available".
ALWAYS_VISIBLE = ("comments",)


# --------------------------------------------------------------------------- #
# Form schemas
# --------------------------------------------------------------------------- #
# Field names and their page assignment are the ones the labeling configs emit.
# Keep the two in step if a config is ever rebuilt.
def items(prefix, start, end):
    """['<prefix>_<start>', ..., '<prefix>_<end>'] (inclusive)."""
    return [f"{prefix}_{i}" for i in range(start, end + 1)]


SIDE_EFFECTS = items("side_effect", 1, 12)

# class -> {"export": <file in annotation/>, "pages": {page toggle -> fields}}
FORMS = OrderedDict([
    ("Vanderbilt:Scale_Parent", {
        "export": "Parent.json",
        "pages": OrderedDict([
            ("Page 1", ["medication_status"] + items("symptom", 1, 32)),
            ("Page 2", items("symptom", 33, 47) + items("performance", 48, 55)),
        ]),
    }),
    ("Vanderbilt:Scale_Teacher", {
        "export": "Teacher.json",
        "pages": OrderedDict([
            ("Page 1", ["class_name", "medication_status"] + items("symptom", 1, 31)),
            ("Page 2", items("symptom", 32, 35) + items("performance", 36, 43)),
        ]),
    }),
    ("Vanderbilt:Followup_Parent", {
        "export": "Parent_followup.json",
        "pages": OrderedDict([
            ("Page 1", ["medication_status"] + items("symptom", 1, 18)
                       + items("performance", 19, 26)),
            ("Page 2", list(SIDE_EFFECTS)),
        ]),
    }),
    ("Vanderbilt:Followup_Teacher", {
        "export": "Teacher_followup.json",
        "pages": OrderedDict([
            ("Page 1", ["class_name", "medication_status"] + items("symptom", 1, 18)
                       + items("performance", 19, 26)),
            ("Page 2", list(SIDE_EFFECTS)),
        ]),
    }),
])


def form_fields(form):
    """Every field of a form, in labeling-config order, ``comments`` last."""
    ordered = [f for fields in form["pages"].values() for f in fields]
    return ordered + list(ALWAYS_VISIBLE)


def build_scores(form, values, visible):
    """Flat {field: value} for one task.

    ``visible`` is the set of ticked ``visible_pages`` choices. A field on a page
    that is not visible is ``"Not Available"``; a field on a visible page is the
    annotator's value, or null if they never touched it.
    """
    scores = OrderedDict()
    for page, fields in form["pages"].items():
        shown = page in visible
        for field in fields:
            scores[field] = values.get(field) if shown else NOT_AVAILABLE
    for field in ALWAYS_VISIBLE:
        scores[field] = values.get(field)
    return scores


def missing_choices(form, values, visible):
    """Choice fields on a visible page that were never clicked."""
    out = []
    for page, fields in form["pages"].items():
        if page not in visible:
            continue
        out += [f for f in fields if f not in FREETEXT_FIELDS and f not in values]
    return out


# --------------------------------------------------------------------------- #
# Label Studio export parsing
# --------------------------------------------------------------------------- #
def result_value(res):
    """Scalar value out of one Label Studio result entry, verbatim.

    Returns None for an entry carrying no value. An empty TextArea is simply
    absent from the export, so absence == null here.
    """
    val = res.get("value", {})
    if res.get("type") == "choices":
        choices = val.get("choices", [])
        return choices[0] if choices else None
    if res.get("type") == "textarea":
        text = val.get("text", [])
        v = text[0].strip() if text else ""
        return v or None
    return None


def annotation_values(annotation):
    """from_name -> scalar value for one annotation's result list.

    ``visible_pages`` is the one multi-select in the configs and is returned
    separately, as a set of page names.
    """
    values, visible = {}, set()
    for res in annotation.get("result", []):
        from_name = res.get("from_name")
        if from_name is None:
            continue
        if from_name == "visible_pages":
            visible.update(res.get("value", {}).get("choices", []))
        else:
            values[from_name] = result_value(res)
    return values, visible


def task_stem(task):
    """Output file stem for a task, matching the scan's filename."""
    return os.path.splitext(task["data"]["filename"])[0]


def sole_annotation(task, source, stem):
    """The single annotation on a task.

    The demo is single-annotator, so anything other than exactly one annotation
    means the export is not what this script assumes -- fail rather than pick.
    """
    anns = [a for a in task.get("annotations", []) if not a.get("was_cancelled")]
    if len(anns) != 1:
        raise SystemExit(
            f"{source}: task {stem} has {len(anns)} annotations, expected exactly 1. "
            f"The demo is single-annotator; re-export after removing the extras.")
    return anns[0]


# --------------------------------------------------------------------------- #
# Record assembly
# --------------------------------------------------------------------------- #
def check_schema(values, form, source, stem):
    """Fail loudly on a from_name the schema does not know about.

    Every field the labeling config defines is listed in FORMS; an unexpected one
    means the config was changed (renumbered items, a new section) without this
    script being updated, which would silently drop annotated values.
    """
    unknown = sorted(f for f in values if f not in set(form_fields(form)))
    if unknown:
        raise SystemExit(
            f"{source}: task {stem} has unknown field(s) {', '.join(unknown)} -- "
            f"labeling config and FORMS schema are out of step")


def check_form_class(task, class_label, source, stem):
    """Tasks carry their form type in ``data.form_class`` (stamped by
    build_label_studio_projects.py); make sure the export really is the form this
    entry of FORMS claims, i.e. that the files were not mixed up."""
    seen = task["data"].get("form_class")
    if seen is not None and seen != class_label:
        raise SystemExit(
            f"{source}: task {stem} is form_class {seen!r}, expected {class_label!r}")


def write_record(out_dir, stem, class_label, scores):
    record = {"class": class_label, "scores": scores}
    with open(os.path.join(out_dir, f"{stem}.json"), "w") as f:
        json.dump(record, f, indent=2)


def process_form(class_label, annotation_dir, out_dir, stats, gaps, datasets):
    form = FORMS[class_label]
    source = form["export"]
    path = os.path.join(annotation_dir, source)
    if not os.path.isfile(path):
        raise SystemExit(f"Annotation export not found: {path}")
    with open(path) as f:
        tasks = json.load(f)

    for task in tasks:
        stem = task_stem(task)
        check_form_class(task, class_label, source, stem)
        annotation = sole_annotation(task, source, stem)
        values, visible = annotation_values(annotation)
        check_schema(values, form, source, stem)
        if not visible:
            raise SystemExit(
                f"{source}: task {stem} has no visible_pages selection -- the field "
                f"is required in the labeling config, so re-export from Label Studio")
        unknown_pages = sorted(visible - set(form["pages"]))
        if unknown_pages:
            raise SystemExit(
                f"{source}: task {stem} selects unknown page(s) {', '.join(unknown_pages)}")
        for field in missing_choices(form, values, visible):
            gaps.append((source, stem, field))

        write_record(out_dir, stem, class_label, build_scores(form, values, visible))
        stats[class_label] += 1
        datasets[task["data"].get("dataset", "?")] += 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-a", "--annotation-dir", default=DEFAULT_ANNOTATION_DIR,
                    help=f"Directory of Label Studio exports (default: {DEFAULT_ANNOTATION_DIR})")
    ap.add_argument("-o", "--out-dir", default=DEFAULT_OUT_DIR,
                    help=f"Output directory (default: {DEFAULT_OUT_DIR})")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    stats, datasets, gaps = defaultdict(int), defaultdict(int), []
    for class_label in FORMS:
        process_form(class_label, args.annotation_dir, args.out_dir, stats, gaps, datasets)

    # Choice fields left untouched on a page that was visible. Written as null;
    # listed here so they can be checked against the scans if needed.
    if gaps:
        print(f"WARNING: {len(gaps)} choice field(s) on a visible page were never "
              f"clicked -- written as null:")
        for source, stem, field in gaps:
            print(f"    {source:24} {stem:34} {field}")

    total = sum(stats.values())
    print(f"\nWrote ground truth to {args.out_dir}")
    for class_label in FORMS:
        print(f"  {class_label:30} {stats[class_label]:3} files")
    print(f"  {'TOTAL':30} {total:3} files "
          f"({', '.join(f'{k}={v}' for k, v in sorted(datasets.items()))})")


if __name__ == "__main__":
    main()
