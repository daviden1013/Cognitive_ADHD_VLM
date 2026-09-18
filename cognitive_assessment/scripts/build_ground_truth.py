#!/usr/bin/env python3
"""Generate ground-truth score files from the demo's Label Studio exports.

Output is identical in shape to the real pipeline's
``cognitive_assessment/scripts/build_ground_truth.py`` -- one JSON file per page,
named ``<stem>.json``, shaped as::

    {"class": <form>, "scores": <parsed>}

so a model run under ``ocr/<run>/`` can be scored file-for-file against
``ground_truth/``, and the pipeline's ``evaluate.py`` reads it unchanged.

``scores`` is an array of ``{question, points}`` rows for MMSE, MoCA and the
Mini-Cog Instruction page, and a flat object for the Mini-Cog Clock page --
matching the extractor prompts key-for-key in both cases.

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
One JSON export per form type -- ``MMSE.json``, ``MoCA.json``,
``Mini-cog_instruction.json``, ``Mini-cog_clock.json``.

Faithfulness note
-----------------
Annotator vocabulary is preserved verbatim: choice values such as ``"blank"`` and
``"illegible"`` are written straight through, NOT remapped to ``null`` or to the
model prompt's ``"unclear"``/``"ambiguous"`` wording. The only ``null``s are the
MoCA MEMORY row (which has no annotation field at all) and the Mini-Cog clock's
``written_time``, the one genuinely optional field.

Usage:
    python scripts/build_ground_truth.py
"""
import argparse
import json
import os
import re
from collections import OrderedDict, defaultdict

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ANNOTATION_DIR = os.path.join(PROJECT_DIR, "annotation")
DEFAULT_OUT_DIR = os.path.join(PROJECT_DIR, "ground_truth")

# --------------------------------------------------------------------------- #
# Form schemas
# --------------------------------------------------------------------------- #
# For the "array of {question, points}" forms each schema is an ordered list of
# (question_label, from_name). A from_name of None means the row has no
# annotation field and is always emitted with null points (the MoCA MEMORY
# section). question_label strings are the exact labels the extractor prompts and
# model outputs use.
MMSE_SCHEMA = [
    ("ORIENTATION - TIME", "orientation_time"),
    ("ORIENTATION - PLACE", "orientation_place"),
    ("REGISTRATION", "registration"),
    ("ATTENTION AND CALCULATION", "attention_calculation"),
    ("RECALL", "recall"),
    ("LANGUAGE - NAMING", "language_naming"),
    ("LANGUAGE - REPETITION", "language_repetition"),
    ("LANGUAGE - 3-STAGE COMMAND", "language_command"),
    ("LANGUAGE - READING", "language_read_obey"),
    ("LANGUAGE - WRITING", "language_write"),
    ("LANGUAGE - COPYING", "language_copy"),
    ("TOTAL", "total_score"),
]

MOCA_SCHEMA = [
    ("VISUOSPATIAL / EXECUTIVE", "visuospatial_executive"),
    ("NAMING", "naming"),
    ("MEMORY", None),  # not scored on the form -> always null
    ("ATTENTION - DIGIT SPAN", "attention_digitspan"),
    ("ATTENTION - LETTER TAPPING", "attention_vigilance"),
    ("ATTENTION - SERIAL 7", "attention_serial7"),
    ("LANGUAGE - SENTENCE REPETITION", "language_repetition"),
    ("LANGUAGE - FLUENCY", "language_fluency"),
    ("ABSTRACTION", "abstraction"),
    ("DELAYED RECALL", "delayed_recall"),
    ("ORIENTATION", "orientation"),
    ("TOTAL", "total_score"),
]

MINICOG_INSTRUCTION_SCHEMA = [
    ("WORD RECALL", "word_recall"),
    ("CLOCK DRAW", "clock_draw"),
    ("TOTAL SCORE", "total_score"),
]

# The clock page is a flat object, not a question/points array. Ordered keys --
# the order the clock extractor prompt emits them in.
MINICOG_CLOCK_KEYS = ["long_arm", "short_arm", "clock_time", "written_time",
                      "word_recall", "clock_draw", "total_score"]

# Fields the labeling config marks required, so a missing one means the
# annotation predates the current schema -- never a real null. ``written_time``
# is the one genuinely optional field and is deliberately absent.
REQUIRED_FIELDS = {
    "MMSE": tuple(f for _, f in MMSE_SCHEMA if f),
    "MoCA": tuple(f for _, f in MOCA_SCHEMA if f),
    "Mini-Cog:Instruction": tuple(f for _, f in MINICOG_INSTRUCTION_SCHEMA if f),
    "Mini-Cog:Clock_Drawing": tuple(k for k in MINICOG_CLOCK_KEYS if k != "written_time"),
}


def build_array_scores(schema, values):
    """[{question, points}, ...] from an ordered schema and a from_name->value map."""
    return [{"question": label,
             "points": None if from_name is None else values.get(from_name)}
            for label, from_name in schema]


def build_clock_scores(values):
    """Flat clock object; any field the annotator didn't fill is null."""
    return {k: values.get(k) for k in MINICOG_CLOCK_KEYS}


# --------------------------------------------------------------------------- #
# Free-text canonicalization
# --------------------------------------------------------------------------- #
# The ground truth is normalized to the canonical form the extractor prompts are
# written to emit, so evaluation stays pure exact-match (no hidden normalization
# in the scorer). Only free-text score fields are touched:
#
#   * lowercase         -- so the not-determinable sentinel is "na", never "NA".
#   * zero-padded HH:MM -- for time fields, so "5:30" -> "05:30".
#
# The discrete choice fields need no normalization (they are already canonical
# single tokens: digits, "blank", "illegible").
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def normalize_freetext(value, is_time):
    if value is None:
        return None
    v = value.strip().lower()
    if is_time:
        m = _TIME_RE.match(v)
        if m:
            return f"{int(m.group(1)):02d}:{m.group(2)}"
    return v


def normalize_values(values, spec):
    """Canonicalize the free-text fields of a from_name->value map, in place.
    ``spec`` is {from_name: is_time}."""
    for from_name, is_time in spec.items():
        if from_name in values:
            values[from_name] = normalize_freetext(values[from_name], is_time)
    return values


# class -> {"export", "build" (scores builder), "normalize" ({from_name: is_time})}
FORMS = OrderedDict([
    ("MMSE", {
        "export": "MMSE.json",
        "build": lambda v: build_array_scores(MMSE_SCHEMA, v),
        "normalize": {"total_score": False},
    }),
    ("MoCA", {
        "export": "MoCA.json",
        "build": lambda v: build_array_scores(MOCA_SCHEMA, v),
        "normalize": {"total_score": False},
    }),
    ("Mini-Cog:Instruction", {
        "export": "Mini-cog_instruction.json",
        "build": lambda v: build_array_scores(MINICOG_INSTRUCTION_SCHEMA, v),
        "normalize": {},   # every field here is a discrete choice
    }),
    ("Mini-Cog:Clock_Drawing", {
        "export": "Mini-cog_clock.json",
        "build": build_clock_scores,
        "normalize": {"clock_time": True, "written_time": True},
    }),
])


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
    """from_name -> scalar value for one annotation's result list."""
    values = {}
    for res in annotation.get("result", []):
        from_name = res.get("from_name")
        if from_name is not None:
            values[from_name] = result_value(res)
    return values


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
def check_required(values, required, source, stem):
    """Fail loudly if a required field is absent from an annotation.

    Every field in ``required`` is required="true" in the labeling config, so a
    missing one means this annotation was submitted under an older schema.
    Writing it through would silently put a null in the ground truth.
    """
    missing = [f for f in required if f not in values]
    if missing:
        raise SystemExit(
            f"{source}: task {stem} is missing required field(s) "
            f"{', '.join(missing)} -- annotation predates the current schema")


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


def process_form(class_label, annotation_dir, out_dir, stats, datasets):
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
        values = annotation_values(sole_annotation(task, source, stem))
        check_required(values, REQUIRED_FIELDS[class_label], source, stem)
        normalize_values(values, form["normalize"])

        write_record(out_dir, stem, class_label, form["build"](values))
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
    stats, datasets = defaultdict(int), defaultdict(int)
    for class_label in FORMS:
        process_form(class_label, args.annotation_dir, args.out_dir, stats, datasets)

    total = sum(stats.values())
    print(f"\nWrote ground truth to {args.out_dir}")
    for class_label in FORMS:
        print(f"  {class_label:30} {stats[class_label]:3} files")
    print(f"  {'TOTAL':30} {total:3} files "
          f"({', '.join(f'{k}={v}' for k, v in sorted(datasets.items()))})")


if __name__ == "__main__":
    main()
