# <!-- TODO: project title -->

## 1. Introduction

This repo presents a modular design for visual information extraction systems to process cognitive assessments (i.e., MMSE, MoCA, Mini-Cog) and the NICHQ Vanderbilt Assessment using multimodal LLMs. The systems begin with form-type classification, which assigns a scanned page to a form type (e.g., MMSE, MoCA, Mini-Cog, or other). The page is then assigned to the corresponding score extraction module for question, field, and score extraction (e.g., Mini-Cog word recall, clock drawing, and total scores). 


Methodology flowchart. The Cognitive Assessment Score Extraction System and the NICHQ Vanderbilt Assessment Score Extraction System comprise two layers: form classifier and score extractors. The form classifier assigns a scanned page to a downstream score extractor for question, field, and score extraction. 
<div align="center"><img src="readme_images/method_flowchart.png" width=800 ></div>

Scanned cognitive assessments and NICHQ Vanderbilt Assessment. (A)The Mini-Mental State Examination (MMSE). (B) The Montreal Cognitive Assessment (MoCA). (C) The Mini-Cog instruction and scoring page. (D) The Mini-Cog clock-drawing page. (E) The NICHQ Vanderbilt Assessment Scale – Parent Information. (F) The NICHQ Vanderbilt Assessment Scale – Teacher Information. (G) The NICHQ Vanderbilt Assessment Follow-up – Parent Information. (H) The NICHQ Vanderbilt Assessment Follow-up – Teacher Information.

> **Note on the data.** All scans in this repository are **synthetic**. 
> There is no protected health information (PHI) in this repo. The synthetic forms
> follow the same layouts and the same annotation schema, so the pipeline,
> prompts, configs and scoring code run here exactly as they do on the real
> corpus — only the numbers differ.

<div align="center"><img src="readme_images/demo_figure_synthesized.png" width=800 ></div>

---

## 2. Prerequisites and installation

### vlm4ocr

The pipeline is built on **[vlm4ocr](https://github.com/daviden1013/vlm4ocr)**,
which handles the VLM engines, page rasterization, concurrency and OCR result
assembly. 

```bash
pip install vlm4ocr
```

For the pipeline, install `PyYaml` and `tqdm`:

```bash
pip install pyyaml tqdm
```

For LLM access, install `openai` Python package:

```bash
pip install openai
```

Tested with Python 3.12, `vlm4ocr` 0.6.0, `openai` 2.36, `pillow` 12.2.

### Model access

The configs target **Azure OpenAI**. You need a deployment for each model you
intend to run, and the deployment name must match the `vlm_engine.model` field in
the config (`gpt-5.4-mini`, `gpt-4.1-mini`). Credentials are read from the
environment:

```bash
export AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
export AZURE_OPENAI_API_KEY=<your key>
```

---

## 3. Repository structure

Two assessments, each self-contained and laid out identically:

```
.
├── ADHD_assessment/         NICHQ Vanderbilt scale + follow-up, parent + teacher
├── cognitive_assessment/    MMSE, MoCA, Mini-Cog instruction + clock
├── readme_images/
└── label_studio.sh          annotation server launcher (internal use)
```

Inside each assessment:

| Path | What it is |
| --- | --- |
| `scans/test/<TYPE>/` | The evaluation set — 4 pages per form type, plus 4 `other` pages |
| `scans/dev/<TYPE>/` | Held out of the test set; 1 page per type, used as the one-shot example |
| `ground_truth/<stem>.json` | Gold record per page, `{"class": ..., "scores": ...}` |
| `prompt_templates/` | The classifier prompt (`form_classification_v0.md`) and one v1 score-extraction prompt per form type |
| `configs/*.yaml` | One file per experimental arm — model, prompts, routing and I/O |
| `pipelines/routed_pipeline.py` | The end-to-end pipeline: classify each page, then route it to that form's extractor |
| `scripts/evaluate.py` | Scores a run against `ground_truth/` |
| `run.sh` | Run book — every command below, with the reasoning behind each |
| `ocr/<run>/<TYPE>/` | Model output, one JSON per page *(created by a run)* |
| `evaluation/` | Reports and per-document error files *(created by evaluation)* |
| `messages_logs/<run>/` | Full prompt/response logs per page *(created by a run)* |
| `labeling_configs/`, `LS_projects/`, `annotation/` | Annotation assets |

`<TYPE>` is the form-type folder name, e.g. `MMSE` or `Vanderbilt_scale_parent`.
Output mirrors the input layout, so predictions join back to the ground truth by
filename stem.

### Ground-truth record shape

`scores` is a flat `{field: value}` object for the Vanderbilt forms and the
Mini-Cog clock page, and an array of `{question, points}` rows for MMSE, MoCA and
the Mini-Cog instruction page — matching what the extraction prompts emit, so
scoring is a field-by-field exact string comparison.

Annotator vocabulary is preserved verbatim (`"Blank"`, `"illegible"`, bare
digits). A Vanderbilt form spans two printed pages while a task is a single page,
so fields belonging to a page the scan does not show are recorded as
`"Not Available"`.

---

## 4. Running the experiments

All commands run **from an assessment directory**, not the repository root:

```bash
cd cognitive_assessment       # or: cd ADHD_assessment
```

The two assessments are independent — run either or both, in any order.

### 4.1 The experimental arms

Four configs per assessment, a 2 × 2 over model and shot count. All are
**end-to-end**: the classifier predicts the form type and the page is routed to
that form's extractor, so a classification error propagates into extraction.

| Config | Model | Decoding | Extractor examples |
| --- | --- | --- | --- |
| `gpt-5.4-mini_zeroshot_e2e_v1.yaml` | gpt-5.4-mini | reasoning | none |
| `gpt-5.4-mini_oneshot_e2e_v1.yaml` | gpt-5.4-mini | reasoning | 1 per form type |
| `gpt-4.1-mini_zeroshot_e2e_v1.yaml` | gpt-4.1-mini | greedy (temp 0) | none |
| `gpt-4.1-mini_oneshot_e2e_v1.yaml` | gpt-4.1-mini | greedy (temp 0) | 1 per form type |

The **classifier is zero-shot in every arm**, so the one-shot comparison isolates
the effect of an example on the *extractors*.

One-shot examples come from `scans/dev/` — held out of the test set — and their
expected answers are read from `ground_truth/`, so no test page is ever shown to
the model as an example.

### 4.2 Run the pipeline

```bash
python pipelines/routed_pipeline.py -c configs/gpt-5.4-mini_zeroshot_e2e_v1.yaml
python pipelines/routed_pipeline.py -c configs/gpt-5.4-mini_oneshot_e2e_v1.yaml
python pipelines/routed_pipeline.py -c configs/gpt-4.1-mini_zeroshot_e2e_v1.yaml
python pipelines/routed_pipeline.py -c configs/gpt-4.1-mini_oneshot_e2e_v1.yaml
```

Each run reads the 20 pages in `scans/test/` and writes:

- `ocr/<run_name>/<TYPE>/<stem>.json` — the prediction, `{"class", "scores"}`
- `messages_logs/<run_name>/<TYPE>/` — the full prompt and response for each page

Runs are **resumable**: a page that already has an output file is skipped, so an
interrupted run continues where it stopped. After editing a prompt or an engine
setting, pass `--overwrite` to force every page to be reprocessed — otherwise the
stale outputs are kept and the arm becomes a silent mix of old and new.

### 4.3 Evaluate

```bash
for run in gpt-5.4-mini_zeroshot_e2e_v1 \
           gpt-5.4-mini_oneshot_e2e_v1 \
           gpt-4.1-mini_zeroshot_e2e_v1 \
           gpt-4.1-mini_oneshot_e2e_v1; do
    python scripts/evaluate.py \
            --gold          ./ground_truth \
            --model         ./ocr/$run \
            --mode          e2e \
            --out-dir       ./evaluation \
            --output-error  ./evaluation/errors/$run
done
```

This writes:

- `evaluation/<run_name>.txt` — the report
- `evaluation/errors/<run_name>/<stem>.json` — gold vs predicted for every
  document with any error, for error analysis

`--mode e2e` scores classification **and** extraction, with extraction *gated* on
the class: a page whose predicted class is wrong scores 0 on its fields. (The
`extractor` mode exists for runs where the form type is given rather than
predicted; it does not apply to these configs.)

### 4.4 Reading the report

Each report gives, per form type and overall:

- **class accuracy** — did the classifier identify the form correctly
- **per-doc field accuracy** — each page gets its own accuracy (matched fields ÷
  its fields), reported as a distribution (mean, 95% CI, std, median, Q1, Q3)
  rather than one pooled number, so a single long form cannot dominate
- **perfect docs** — the share of pages extracted with no field error at all
- a per-class **precision / recall / F1** table and a confusion matrix

Scoring is exact string match, field by field; `null` matches only `null`.

> **Expected line:** `Model outputs with no gold match (ignored): 4`. The four
> `other` pages in `scans/test/other/` are decoys for the classifier and have no
> ground-truth record, so they are classified but not scored.

---


## 5. Citation

<!-- TODO: add the citation once the paper is available. -->

```bibtex

```

<!-- TODO: acknowledgements / funding (AIM AHEAD), license. -->
