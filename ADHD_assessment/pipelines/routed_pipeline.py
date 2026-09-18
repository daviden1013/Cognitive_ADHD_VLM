"""
routed_pipeline.py

Experimental routed OCR pipeline for ADHD (NICHQ Vanderbilt) assessment forms, built to
support a systematic evaluation of different VLM models / configs. A single scanned document
may interleave several form types (Vanderbilt scale / follow-up, parent / teacher informant).
Each page is:

  1. classified with `prompt_templates/form_classification_v0.md`, which returns
     {"form": "<Vanderbilt:Scale_Parent | Vanderbilt:Scale_Teacher |
     Vanderbilt:Followup_Parent | Vanderbilt:Followup_Teacher | other>"}, then
  2. routed to the per-form score-extraction prompt for that type.

Each Vanderbilt form spans two printed pages, but a page is the unit of work here: pages are
classified and extracted independently, and both pages of a form carry the same class. The
extractor prompts emit the whole form's fields on every page, marking the ones that are not
on the page in front of them "Not Available" -- so a page-1 and a page-2 record are directly
comparable to their ground truth without any page bookkeeping in this pipeline.

Pages classified as "other" (or any type not present in `routes`) are recorded with the
classification result only -- no extraction is run.

This is an *end-to-end* (e2e) pipeline: it exercises the classifier and every extractor in
one pass. Modular evaluations (classifier-only, single-extractor, etc.) live in sibling
pipelines. All model/prompt/IO choices come from a YAML config in ../configs/, so a run is
fully described by `python routed_pipeline.py -c ../configs/<name>.yaml`.

RoutedFormPipeline is a thin subclass of the shipped vlm4ocr.IndependentPagePipeline: it only
supplies the classify+route logic; loading, concurrency, page ordering, preprocessing and
OCRResult assembly are inherited.
"""
from typing import Callable, Dict, List, Optional
import os
import json
import yaml
import asyncio
import logging
import argparse
from glob import glob

from PIL import Image
from tqdm.asyncio import tqdm

from vlm4ocr import AzureOpenAIVLMEngine, OCREngine, OCRPage, IndependentPagePipeline, FewShotExample
from vlm4ocr.vlm_engines import (MessagesLogger, VLMConfig, BasicVLMConfig,
                                 ReasoningVLMConfig, OpenAIReasoningVLMConfig)

SUPPORTED_EXTS = ['.pdf', '.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp', '.tif', '.tiff']

# The ADHD_assessment project root (parent of pipelines/). All relative paths in a
# config -- prompts, input/output dirs -- resolve against this, so "./prompt_templates/..."
# works no matter where the config file lives or what the current working directory is.
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Names made available to the `vlm_engine.config` mini-expression in the YAML config, e.g.
# `config: BasicVLMConfig(max_new_tokens=4096)`. Kept to config-class constructors only.
VLM_CONFIG_NAMESPACE = {
    "VLMConfig": VLMConfig,
    "BasicVLMConfig": BasicVLMConfig,
    "ReasoningVLMConfig": ReasoningVLMConfig,
    "OpenAIReasoningVLMConfig": OpenAIReasoningVLMConfig,
}


def parse_form(classifier_text: str) -> str:
    """Read the "form" label from a JSON-mode classifier output.

    The classifier prompt asks for a JSON object {"form": "..."}, but OCREngine JSON mode may
    wrap it in an array; handle both. Anything unparseable falls back to "other".
    """
    try:
        data = json.loads(classifier_text)
    except (json.JSONDecodeError, TypeError):
        return "other"
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return "other"
    return data.get("form") or "other"


def build_record(page) -> dict:
    """Shape one OCRPage into the output record: {"class": <form>, "scores": <parsed>}.

    `scores` is the extractor's parsed JSON for a recognized form, or None for pages that
    were classified as "other" (no extraction run). If the extractor text won't parse as
    JSON, the raw string is kept so nothing is silently lost.
    """
    meta = page['metadata']
    form = meta.get('form', 'other')
    if not meta.get('extracted'):
        return {"class": form, "scores": None}
    try:
        scores = json.loads(page['text'])
    except (json.JSONDecodeError, TypeError):
        scores = page['text']
    return {"class": form, "scores": scores}


class RoutedFormPipeline(IndependentPagePipeline):
    """Classify each page, then route it to the matching extractor OCREngine.

    `extractors` maps a classifier form label to an extractor engine. A page whose label is
    not in `extractors` (including "other") is returned with the classification recorded and
    no extraction run.

    Optional one-shot support: `classifier_examples` and `extractor_examples[form]` hold the
    few-shot example list bound to each engine. `concurrent_ocr` does not thread few-shot
    through the pipeline, so the examples are injected here at each `ocr_image_async` call.
    When None, that engine runs zero-shot (unchanged behaviour).
    """
    def __init__(self, classifier: OCREngine, extractors: Dict[str, OCREngine], *,
                 form_key: Callable[[str], str] = parse_form,
                 classifier_examples: Optional[List[FewShotExample]] = None,
                 extractor_examples: Optional[Dict[str, List[FewShotExample]]] = None,
                 **kwargs):
        self.classifier = classifier
        self.extractors = extractors
        self.form_key = form_key
        self.classifier_examples = classifier_examples
        self.extractor_examples = extractor_examples or {}
        super().__init__(self._classify_and_extract, output_mode="JSON", **kwargs)

    async def _classify_and_extract(self, image: Image.Image, *,
                                    messages_logger: MessagesLogger = None) -> OCRPage:
        # Sees only its own image -> independent. Both calls share the logger for one audit trail.
        classification = await self.classifier.ocr_image_async(
            image, few_shot_examples=self.classifier_examples, messages_logger=messages_logger)
        form = self.form_key(classification.text)
        extractor = self.extractors.get(form)

        if extractor is None:
            # "other" / unrecognized -> keep the classification page, skip extraction.
            classification.metadata["form"] = form
            classification.metadata["extracted"] = False
            classification.metadata["classification"] = classification.text
            return classification

        page = await extractor.ocr_image_async(
            image, few_shot_examples=self.extractor_examples.get(form),
            messages_logger=messages_logger)
        page.metadata["form"] = form
        page.metadata["extracted"] = True
        page.metadata["classification"] = classification.text
        return page


async def run_pipeline(pipeline: RoutedFormPipeline, file_paths, subdir_by_stem, output_dir,
                       messages_log_dir, run_name, concurrent_batch_size, max_file_load):
    # Output mirrors the input layout so predictions can be joined back to ground truth:
    #   <output_dir>/<run_name>/<TYPE>/<stem>.json  (TYPE is the input subfolder, if any).
    out_run = os.path.join(output_dir, run_name)
    log_run = os.path.join(messages_log_dir, run_name)
    pbar = tqdm(total=len(file_paths), desc="Processing files", unit="file")
    async for result in pipeline.concurrent_ocr(file_paths, concurrent_batch_size=concurrent_batch_size,
                                                max_file_load=max_file_load):
        stem = os.path.splitext(os.path.basename(result.filename))[0]
        subdir = subdir_by_stem.get(stem, "")
        out_folder = os.path.join(out_run, subdir)
        log_folder = os.path.join(log_run, subdir)
        os.makedirs(out_folder, exist_ok=True)
        os.makedirs(log_folder, exist_ok=True)
        if result.status == "success":
            logging.info(f"Successfully processed {result.filename} ({len(result)} pages)")
            if len(result.pages) > 1:
                logging.warning(f"{result.filename} has {len(result.pages)} pages; "
                                f"writing only page 0 to {stem}.json")
            record = build_record(result.pages[0])
            with open(os.path.join(out_folder, f"{stem}.json"), 'w') as f:
                json.dump(record, f, indent=2)
        else:
            logging.error(f"Failed to process {result.filename}")
        with open(os.path.join(log_folder, f"{stem}.json"), 'w') as f:
            json.dump(result.get_messages_log(), f, indent=4)
        pbar.update(1)
    pbar.close()


def build_vlm_config(spec: Optional[str]):
    """Evaluate a `config:` mini-expression (e.g. 'BasicVLMConfig(max_new_tokens=4096)')."""
    if spec is None:
        return None
    return eval(spec, {"__builtins__": {}}, VLM_CONFIG_NAMESPACE)


def build_vlm_engine(config: dict):
    """Build the VLM engine for a run. Only Azure OpenAI is supported for now.

    Endpoint and API key are read from the environment (AZURE_OPENAI_ENDPOINT,
    AZURE_OPENAI_API_KEY) by the underlying openai client -- never from the config.
    """
    provider = config.get('provider', 'azure').lower()
    vlm = config['vlm_engine']
    vlm_config = build_vlm_config(vlm.get('config'))

    if provider == 'azure':
        return AzureOpenAIVLMEngine(
            model=vlm['model'],                      # Azure deployment name
            api_version=vlm['api_version'],
            config=vlm_config,
            max_concurrent_requests=vlm.get('max_concurrent_requests'),
            max_requests_per_minute=vlm.get('max_requests_per_minute'),
        )
    raise ValueError(f"Unsupported provider {provider!r}. Only 'azure' is supported for now.")


def resolve_path(p: Optional[str]) -> Optional[str]:
    """Resolve a config path relative to the project dir (absolute paths kept as-is)."""
    if p is None:
        return None
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(PROJECT_DIR, p))


def load_prompt(path: str) -> str:
    with open(path, 'r') as f:
        return f.read()


def build_example_text(gt_record: dict, kind: str) -> str:
    """Derive the exact raw string a model should emit for a one-shot example from its
    ground-truth record. The GT file wraps things differently than the model's raw output:

      - classifier: model emits {"form": "<label>"}; GT stores the label under "class".
        Compact one-line JSON, matching the classifier prompt's shown format.
      - extractor:  model emits the bare `scores` payload (array/object); GT wraps it as
        {"class": ..., "scores": ...}. Pretty-printed, matching the extractor prompts.

    Raises ValueError if the record lacks the field needed for `kind`, so a misconfigured
    example fails loudly rather than teaching the model a malformed answer.
    """
    if kind == "classifier":
        cls = gt_record.get("class")
        if not cls:
            raise ValueError("ground-truth record has no 'class' for a classifier example")
        return json.dumps({"form": cls})
    if kind == "extractor":
        scores = gt_record.get("scores")
        if scores is None:
            raise ValueError("ground-truth record has no 'scores' for an extractor example")
        return json.dumps(scores, indent=2)
    raise ValueError(f"unknown few-shot kind {kind!r}")


def load_few_shot_example(image_path: str, ground_truth_dir: str, kind: str, *,
                          rotate_correction=False,
                          max_dimension_pixels: Optional[int] = None) -> List[FewShotExample]:
    """Build the one-shot example list for one engine from a dev image + its ground truth.

    `image_path` is resolved against the project dir; its stem selects the answer file
    <ground_truth_dir>/<stem>.json. Missing image or ground truth is a hard error (the run
    is meant to be one-shot, so a silently-dropped example must not pass as success). Returns
    a single-element list so multi-shot is a trivial future extension.
    """
    img_path = resolve_path(image_path)
    if not os.path.isfile(img_path):
        raise FileNotFoundError(f"few-shot image not found: {img_path}")
    stem = os.path.splitext(os.path.basename(img_path))[0]
    gt_path = os.path.join(resolve_path(ground_truth_dir), f"{stem}.json")
    if not os.path.isfile(gt_path):
        raise FileNotFoundError(
            f"few-shot ground truth not found for image {img_path}: expected {gt_path}")
    with open(gt_path) as f:
        gt_record = json.load(f)
    text = build_example_text(gt_record, kind)
    image = Image.open(img_path)
    image.load()  # decode now so the file handle can close
    return [FewShotExample(image=image, text=text, rotate_correction=rotate_correction,
                           max_dimension_pixels=max_dimension_pixels)]


def rel_subdir(file_path: str, input_dir: str) -> str:
    """The file's subfolder relative to input_dir (e.g. 'Vanderbilt_scale_parent'), or '' if directly
    under input_dir. This is the <TYPE> segment mirrored into the output layout."""
    return os.path.dirname(os.path.relpath(file_path, input_dir))


def is_done(output_run_dir: str, input_dir: str, file_path: str) -> bool:
    """A file is done if its mirrored <TYPE>/<stem>.json output already exists."""
    stem = os.path.splitext(os.path.basename(file_path))[0]
    return os.path.isfile(os.path.join(output_run_dir, rel_subdir(file_path, input_dir),
                                       f"{stem}.json"))


def main():
    parser = argparse.ArgumentParser(description="Run the routed ADHD-assessment OCR pipeline.")
    parser.add_argument("-c", "--config", type=str, required=True,
                        help="Path to a run config YAML (e.g. ../configs/gpt-5.4-mini_e2e_v0.yaml)")
    parser.add_argument("--overwrite", action="store_true", default=False,
                        help="Reprocess every input file. By default, files that already have "
                             "output in <output_directory>/<run_name>/ are skipped.")
    args = parser.parse_args()

    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
    with open(args.config, 'r') as f:
        config = yaml.safe_load(f)

    vlm_engine = build_vlm_engine(config)

    # Each OCREngine is an atom: one prompt, one task. The pipeline composes them.
    classifier = OCREngine(
        vlm_engine, output_mode="JSON",
        user_prompt=load_prompt(resolve_path(config['classifier']['user_prompt_path'])),
    )
    extractors = {
        form: OCREngine(vlm_engine, output_mode="JSON",
                        user_prompt=load_prompt(resolve_path(route['user_prompt_path'])))
        for form, route in config['routes'].items()
    }

    pipe = config['pipeline']

    # Optional one-shot examples (absent -> zero-shot). Each `few_shot_image` names a dev
    # image; its answer is derived from <ground_truth_directory>/<stem>.json. The example is
    # preprocessed like real inputs (same rotate/resize as the pipeline).
    gt_dir = config.get('ground_truth_directory')
    fs_kwargs = dict(rotate_correction=pipe.get('rotate_correction', False),
                     max_dimension_pixels=pipe.get('max_dimension_pixels'))

    classifier_examples = None
    if config['classifier'].get('few_shot_image'):
        if not gt_dir:
            raise ValueError("classifier.few_shot_image set but 'ground_truth_directory' is missing")
        classifier_examples = load_few_shot_example(
            config['classifier']['few_shot_image'], gt_dir, "classifier", **fs_kwargs)

    extractor_examples = {}
    for form, route in config['routes'].items():
        if route.get('few_shot_image'):
            if not gt_dir:
                raise ValueError(f"routes.{form}.few_shot_image set but "
                                 f"'ground_truth_directory' is missing")
            extractor_examples[form] = load_few_shot_example(
                route['few_shot_image'], gt_dir, "extractor", **fs_kwargs)
    if classifier_examples or extractor_examples:
        logging.info("One-shot enabled: classifier=%s, extractors=%s",
                     bool(classifier_examples), sorted(extractor_examples))

    pipeline = RoutedFormPipeline(
        classifier=classifier,
        extractors=extractors,
        classifier_examples=classifier_examples,
        extractor_examples=extractor_examples,
        rotate_correction=pipe.get('rotate_correction', False),
        max_dimension_pixels=pipe.get('max_dimension_pixels'),
    )

    input_dir = resolve_path(config['input_directory'])
    output_dir = resolve_path(config['output_directory'])
    messages_log_dir = resolve_path(config['messages_log_directory'])
    run_name = config['run_name']
    os.makedirs(os.path.join(output_dir, run_name), exist_ok=True)
    os.makedirs(os.path.join(messages_log_dir, run_name), exist_ok=True)

    # Match the extension case-insensitively: one recursive glob, then filter on the
    # lower-cased suffix. Globbing f"**/*{ext}" per extension instead would be
    # case-SENSITIVE on Linux, which silently skips the demo's ".JPG" scans while
    # still picking up the lower-case ".jpg" ones -- a partial run that looks like
    # a successful one.
    file_paths = sorted(
        fp for fp in glob(os.path.join(input_dir, "**/*"), recursive=True)
        if os.path.isfile(fp) and os.path.splitext(fp)[1].lower() in SUPPORTED_EXTS
    )
    if not file_paths:
        logging.warning(f"No supported files found in {input_dir}")
        return
    logging.info(f"Found {len(file_paths)} files in {input_dir}.")

    # stem -> input subfolder (<TYPE>), used to mirror the layout into the output.
    subdir_by_stem = {os.path.splitext(os.path.basename(fp))[0]: rel_subdir(fp, input_dir)
                      for fp in file_paths}

    if not args.overwrite:
        output_run_dir = os.path.join(output_dir, run_name)
        pending = [fp for fp in file_paths if not is_done(output_run_dir, input_dir, fp)]
        skipped = len(file_paths) - len(pending)
        if skipped:
            logging.info(f"Skipping {skipped} already-completed files (use --overwrite to redo).")
        file_paths = pending
        if not file_paths:
            logging.info("Nothing to do -- all files already completed.")
            return
    logging.info(f"Processing {len(file_paths)} files.")

    asyncio.run(run_pipeline(
        pipeline=pipeline, file_paths=file_paths, subdir_by_stem=subdir_by_stem,
        output_dir=output_dir, messages_log_dir=messages_log_dir, run_name=run_name,
        concurrent_batch_size=pipe.get('concurrent_batch_size', 8),
        max_file_load=pipe.get('max_file_load'),
    ))


if __name__ == "__main__":
    main()
