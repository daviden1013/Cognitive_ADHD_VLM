# ==============================================================================
# ADHD (NICHQ Vanderbilt) assessment -- PUBLIC DEMO run book
# ==============================================================================
# Synthetic scans, one annotator, no PHI. Mirrors the real pipeline's layout but
# starts at extraction: the demo has no Stage-1 inclusion pass, because every
# scan is already filed under scans/<split>/<form type>/.
#
# 0. Start Label Studio
# 1. Build the four extraction projects
# 2. Annotate  (manual)
# 3. Export    (manual)
# 4. Ground truth
# 5. TASK: routed e2e runs -- gpt-5.4-mini and gpt-4.1-mini, zero-shot and one-shot
# 6. Evaluation
# ==============================================================================
# Steps 0-4 build the ground truth and need no API access. Steps 5-6 are the
# experiment: they need the vlm4ocr package and an Azure OpenAI deployment.
# Run every command from this directory (AIM_AHEAD_public_page/ADHD_assessment).


# ------------------------------------------------------------------------------
# 0. Start Label Studio
# ------------------------------------------------------------------------------
# Serves the scans straight off disk, with the document root set to the public
# page root -- the parent of this directory. The /data/local-files/?d=... URLs in
# the task files are relative to exactly that root, so start it with this script
# rather than a bare `label-studio start`, or the images will not load.
#
# Leave it running in its own terminal; step 1 talks to it over the REST API.
../label_studio.sh                      # http://localhost:31415


# ------------------------------------------------------------------------------
# 1. Build the four extraction projects
# ------------------------------------------------------------------------------
# One project per form type -- Scale Parent/Teacher, Follow-up Parent/Teacher.
# scans/*/other/ is skipped: "other" is a classifier negative, nothing to extract.
#
# Each project gets its labeling config from labeling_configs/, all pages of that
# form type (dev and test together -- each task carries its own `dataset` field),
# and maximum_annotations=1 for the single annotator.
#
# The token comes from Account & Settings -> Personal Access Token. On Label
# Studio 1.20+ that is a JWT, which the script trades for an access token; the
# older 40-character legacy tokens are disabled by default and come back as
# "401 Invalid token."
# export LABEL_STUDIO_API_KEY=<your personal access token>

python scripts/build_label_studio_projects.py \
        --create \
        --url http://localhost:31415

# Re-running is safe and is also the repair path: a project whose title already
# exists keeps its tasks and annotations, and only a missing Local Files storage
# is added. That storage is what makes the images render -- Label Studio refuses
# to serve a local file unless a storage covering it is attached to the project,
# so "images not showing" almost always means this step has not been run.

# Import files only, no server -- then set the projects up by hand
# (Create Project -> Labeling Setup -> Custom template -> paste the XML ->
# import LS_projects/<form>_tasks.json):
# python scripts/build_label_studio_projects.py


# ------------------------------------------------------------------------------
# 2. Annotate  (manual, in the browser)
# ------------------------------------------------------------------------------
# Sort the Data Manager by shuffle_key ascending -- tasks were shuffled at build
# time (seed 42) so pages are not labeled in filename order, and that key keeps
# the order stable across sessions.
#
# A Vanderbilt form spans two printed pages but a task is a single page, so tick
# `visible_pages` FIRST: only the questions on the page(s) you tick are shown,
# and everything on a page left off is written as "Not Available" in step 4.


# ------------------------------------------------------------------------------
# 3. Export  (manual, from Label Studio)
# ------------------------------------------------------------------------------
# Export each project as JSON into ./annotation/, named per form type:
#
#     Parent.json             <- ADHD demo — Vanderbilt Scale (Parent)
#     Teacher.json            <- ADHD demo — Vanderbilt Scale (Teacher)
#     Parent_followup.json    <- ADHD demo — Vanderbilt Follow-up (Parent)
#     Teacher_followup.json   <- ADHD demo — Vanderbilt Follow-up (Teacher)
#
# The names are what build_ground_truth.py looks for; a file dropped into the
# wrong slot is caught in step 4 by the form_class check, not silently accepted.


# ------------------------------------------------------------------------------
# 4. Ground truth
# ------------------------------------------------------------------------------
# One record per page -> ground_truth/<stem>.json, shaped {"class", "scores"}
# with scores a flat {field: value} object -- the same shape the real pipeline
# emits, so evaluate.py and the rest of the scoring read it unchanged.
#
# Single annotator, so each task's one annotation IS the ground truth: no
# agreement/production split, no modifier file, no completed_by whitelist.
#
# Fails loudly on a task with zero or several annotations, an unknown from_name
# (labeling config and schema out of step), a missing visible_pages, or an export
# in the wrong form's slot. Warns (does not fail) on a choice field never clicked
# on a visible page -- those are written as null.
python scripts/build_ground_truth.py

# Non-default locations:
# python scripts/build_ground_truth.py \
#         --annotation-dir ./annotation \
#         --out-dir        ./ground_truth


# ------------------------------------------------------------------------------
# 5. TASK: routed e2e runs
# ------------------------------------------------------------------------------
# In: scans/test/<TYPE>/*.JPG      Out: ocr/<run_name>/<TYPE>/<stem>.json
#                                       messages_logs/<run_name>/<TYPE>/
#
# "Routed" == end-to-end: every page is first classified by
# prompt_templates/form_classification_v0.md, then routed to that form's v1
# extractor prompt. A page classified "other" is recorded with the class only and
# no extraction, which is how the demo covers the negative class without ever
# having had an inclusion stage.
#
# Output records are {"class", "scores"} -- the same shape build_ground_truth.py
# writes, which is what lets step 6 compare them file-for-file.
#
# Needs the vlm4ocr package (conda env `ocr` here, NOT the `anno` env used for
# steps 0-4) and an Azure OpenAI deployment for each model named in the configs:
# export AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
# export AZURE_OPENAI_API_KEY=<your key>

# --- gpt-5.4-mini (reasoning arm) ---
python pipelines/routed_pipeline.py -c configs/gpt-5.4-mini_zeroshot_e2e_v1.yaml
python pipelines/routed_pipeline.py -c configs/gpt-5.4-mini_oneshot_e2e_v1.yaml

# --- gpt-4.1-mini (non-reasoning arm; same prompts, same routing, greedy decode) ---
python pipelines/routed_pipeline.py -c configs/gpt-4.1-mini_zeroshot_e2e_v1.yaml
python pipelines/routed_pipeline.py -c configs/gpt-4.1-mini_oneshot_e2e_v1.yaml

# The one-shot arms read their in-context example's expected answer out of
# ground_truth/, so step 4 has to have run first. Their examples are the
# scans/dev/ pages -- held out of scans/test/, so the test set stays clean.
# The classifier is zero-shot in every arm, which isolates "does an example help
# the extractors?".
#
# Runs are RESUMABLE: a page that already has its <TYPE>/<stem>.json output is
# skipped, so an interrupted run picks up where it stopped. Pass --overwrite to
# force every page to be reprocessed -- needed after editing a prompt or an engine
# setting, otherwise the stale outputs are kept and the arm is silently a mix.


# ------------------------------------------------------------------------------
# 6. Evaluation
# ------------------------------------------------------------------------------
# In: ground_truth/ + ocr/<run_name>/   Out: evaluation/<run_name>.txt
#                                            evaluation/errors/<run_name>/<stem>.json
#
# --mode e2e scores classification AND extraction, with extraction *gated* on the
# class: a page whose predicted class is wrong scores 0 on its fields. Use it for
# every run below -- these are all e2e runs. (--mode extractor exists for runs
# where the form type was given rather than predicted; the demo has none.)
#
# Scoring is exact string match per field, so annotator vocabulary and prompt
# vocabulary have to agree -- that is why build_ground_truth.py passes values
# through verbatim instead of normalizing them here.
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

# One run on its own:
# python scripts/evaluate.py \
#         --gold          ./ground_truth \
#         --model         ./ocr/gpt-5.4-mini_zeroshot_e2e_v1 \
#         --mode          e2e \
#         --out-dir       ./evaluation \
#         --output-error  ./evaluation/errors/gpt-5.4-mini_zeroshot_e2e_v1


# ------------------------------------------------------------------------------
# Not ported to the demo
# ------------------------------------------------------------------------------
# Only the routed (end-to-end) pipeline is here. In AIM_AHEAD_pipeline there are
# also extractor_pipeline.py (extraction on a known form type) and the
# text_*_pipeline.py pair (the Tesseract OCR + text-only LLM baseline, which is
# why the *_text_* prompt templates and their configs are not copied either), plus
# scripts/build_error_analysis_project.py, which turns evaluation/errors/ into a
# Label Studio project for reviewing what the model got wrong.
#
# Upstream steps that will never apply here, whatever else gets ported:
#   * inter-annotator agreement + the agreement modifier -- one annotator
#   * the Stage-1 inclusion project and its "other" pages -- no classification stage
#   * sample_dev_test.py -- the demo's scans/ ships already split into dev/ and test/
