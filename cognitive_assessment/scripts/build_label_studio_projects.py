#!/usr/bin/env python3
"""Build (and optionally create) the cognitive extraction projects in Label Studio.

The public demo has no inclusion/classification stage: the synthetic scans are
already filed under ``scans/<split>/<form folder>/``, so the form type of every
page is known from its directory. This script therefore goes straight to the
extraction stage -- one Label Studio project per form type, four in all:

    MMSE                  -> mmse
    MoCA                  -> moca
    Mini-Cog_instruction  -> minicog_instruction
    Mini-Cog_clock        -> minicog_clock

``scans/*/other/`` is skipped: "other" is a negative class for the classifier,
it has nothing to extract.

Each project is set up for a SINGLE annotator (``maximum_annotations = 1``), so
there is no agreement/production split and no per-annotator routing -- every
task is labeled once, by you, and that label is the ground truth.

Images are served off disk with Label Studio's local-files endpoint, so nothing
is uploaded or copied. Every task references its page as
``/data/local-files/?d=<path relative to the document root>``. Two things have
to be in place for that URL to resolve, and missing either one shows up the same
way -- a blank image panel on the labeling screen:

  1. Label Studio must be RUNNING with a document root that contains the scans::

         export LABEL_STUDIO_LOCAL_FILES_SERVING_ENABLED=true
         export LABEL_STUDIO_LOCAL_FILES_DOCUMENT_ROOT=<AIM_AHEAD_public_page>
         label-studio start

     ``../label_studio.sh`` does exactly that, and the default --document-root
     below is the same directory, so the two agree out of the box.

  2. Each project must have a Local Files storage registered on it whose path
     covers the images -- the serving endpoint checks for one before it will
     hand over a file. --create registers ``scans/`` for every project it
     touches; see ensure_local_storage().

Usage
-----
Write the import files only (no server needed)::

    python scripts/build_label_studio_projects.py

Write them AND create the four projects on a running Label Studio::

    export LABEL_STUDIO_API_KEY=<token>    # Account & Settings -> Personal Access Token
    python scripts/build_label_studio_projects.py --create --url http://localhost:31415

Both token schemes work: a personal access token (the JWT Label Studio 1.20+
hands out) is exchanged for an access token automatically, and an older legacy
API token is sent as-is.

Creating is safe to re-run: a project whose title already exists is left alone
(reported as "exists"), so you never end up with duplicate tasks on top of work
you have already done. Pass --title-suffix to make a second, parallel set.
"""
import argparse
import json
import os
import random
import sys
from collections import OrderedDict
from urllib.parse import quote

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC_ROOT = os.path.dirname(PROJECT_DIR)
SCANS_DIR = os.path.join(PROJECT_DIR, "scans")
CONFIG_DIR = os.path.join(PROJECT_DIR, "labeling_configs")
DEFAULT_OUT_DIR = os.path.join(PROJECT_DIR, "LS_projects")

# Splits to pull tasks from. Both go into the same project: which pages are
# few-shot/dev material and which are held-out test material is a modeling
# decision downstream, and it does not change how a page is annotated. Each task
# carries its ``dataset`` so the split survives the export.
SPLITS = ("dev", "test")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}

TITLE_PREFIX = "Cognitive demo"

# --------------------------------------------------------------------------- #
# The four form types
# --------------------------------------------------------------------------- #
# ``folder``     -- the scans/<split>/<folder> directory holding the pages.
# ``name``       -- human-readable form name, shown in the project title.
# ``form_class`` -- canonical class string, the one the pipeline's ground-truth
#                   records use as {"class": ...}; carried on every task so an
#                   export is self-describing.
# ``config``     -- labeling_configs/<config>, the extraction UI for that form.
FORMS = OrderedDict([
    ("mmse", {
        "folder": "MMSE",
        "name": "MMSE",
        "form_class": "MMSE",
        "config": "mmse.xml",
    }),
    ("moca", {
        "folder": "MoCA",
        "name": "MoCA",
        "form_class": "MoCA",
        "config": "moca.xml",
    }),
    ("minicog_instruction", {
        "folder": "Mini-Cog_instruction",
        "name": "Mini-Cog (Instruction page)",
        "form_class": "Mini-Cog:Instruction",
        "config": "minicog_instruction.xml",
    }),
    ("minicog_clock", {
        "folder": "Mini-Cog_clock",
        "name": "Mini-Cog (Clock page)",
        "form_class": "Mini-Cog:Clock_Drawing",
        "config": "minicog_clock.xml",
    }),
])

PROJECT_DESCRIPTION = (
    "Synthetic {name} pages. Record the score written in each item box, using "
    "'blank'/'illegible' where the form gives you nothing to read. One "
    "annotator: your submitted annotation is the ground truth."
)


# --------------------------------------------------------------------------- #
# Task building
# --------------------------------------------------------------------------- #
def local_files_url(image_path, document_root):
    """The ``/data/local-files/?d=...`` URL for one image.

    ``d`` is the image path relative to the Label Studio document root,
    percent-encoded with the path separators left intact.
    """
    rel = os.path.relpath(image_path, document_root)
    if rel.startswith(".."):
        raise ValueError(f"{image_path} is not inside document root {document_root}")
    return "/data/local-files/?d=" + quote(rel.replace(os.sep, "/"), safe="/")


def find_images(folder):
    """Every page of one form type, as (split, path), in split then name order."""
    found = []
    for split in SPLITS:
        d = os.path.join(SCANS_DIR, split, folder)
        if not os.path.isdir(d):
            continue
        for fname in sorted(os.listdir(d)):
            path = os.path.join(d, fname)
            if (os.path.splitext(fname)[1].lower() in IMAGE_EXTS
                    and os.path.isfile(path)):
                found.append((split, path))
    return found


def build_tasks(slug, form, document_root, seed):
    """The Label Studio import list for one form type."""
    tasks = []
    for split, path in find_images(form["folder"]):
        fname = os.path.basename(path)
        tasks.append({"data": {
            "image": local_files_url(path, document_root),
            # Identifiers carried through so an export is traceable without
            # re-reading the scans directory. Not shown to the annotator except
            # for filename/doc_id, which the labeling configs display.
            "filename": fname,
            # Each synthetic scan is a standalone one-page document -- the
            # trailing number in the filename is a sample index, not a page
            # number -- so the whole stem is the document id.
            "doc_id": os.path.splitext(fname)[0],
            "page": 1,
            "form": slug,
            "form_class": form["form_class"],
            "dataset": split,
        }})

    # Shuffle so the pages are not annotated in filename order, then stamp each
    # task with its position: sort the Data Manager ascending by shuffle_key and
    # the order is explicit and stable (a filename sort would undo it).
    random.Random(seed).shuffle(tasks)
    for i, task in enumerate(tasks):
        task["data"]["shuffle_key"] = i
    return tasks


def read_config(form):
    path = os.path.join(CONFIG_DIR, form["config"])
    if not os.path.isfile(path):
        sys.exit(f"Labeling config not found: {path}")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# Label Studio REST API
# --------------------------------------------------------------------------- #
class LabelStudio:
    """The few endpoints this script needs, over the REST API."""

    def __init__(self, url, api_key):
        try:
            import requests
        except ImportError:
            sys.exit("--create needs the 'requests' package (pip install requests)")
        self.session = requests.Session()
        self.url = url.rstrip("/")
        self._authenticate(api_key)

    # -- authentication ---------------------------------------------------- #
    # Label Studio has two token schemes and they are used completely
    # differently:
    #
    #   * personal access token -- a JWT, and what Account & Settings hands out
    #     on 1.20+. It is a *refresh* token: trade it at /api/token/refresh/ for
    #     a short-lived access token, then send `Authorization: Bearer <access>`.
    #   * legacy token -- the older 40-character key, sent directly as
    #     `Authorization: Token <key>`. Since 1.20 an organization has this
    #     DISABLED by default (JWTSettings.legacy_api_tokens_enabled = False),
    #     so on a current server a legacy token usually just 401s.
    #
    # A JWT is three dot-separated segments, which is what we key off. Either
    # way we probe the API once up front, so a bad token fails here with an
    # explanation instead of halfway through creating projects.
    def _authenticate(self, api_key):
        api_key = api_key.strip()
        if api_key.count(".") == 2:
            self.scheme = "Bearer (personal access token)"
            self.session.headers["Authorization"] = f"Bearer {self._access_token(api_key)}"
        else:
            self.scheme = "Token (legacy token)"
            self.session.headers["Authorization"] = f"Token {api_key}"

        probe = self._request("GET", "/api/projects/?page=1&page_size=1")
        if probe.status_code == 401:
            sys.exit(self._auth_help(probe))
        if probe.status_code >= 400:
            sys.exit(f"Label Studio at {self.url} answered HTTP "
                     f"{probe.status_code}\n{probe.text[:1000]}")

    def _access_token(self, refresh_token):
        """Trade a personal access token for a short-lived access token.

        The access token lives ~5 minutes, which is far longer than this script
        runs, so it is fetched once and never refreshed.
        """
        resp = self._request("POST", "/api/token/refresh/",
                             json={"refresh": refresh_token})
        if resp.status_code >= 400:
            sys.exit(
                f"Could not exchange the personal access token for an access "
                f"token.\n"
                f"  POST {self.url}/api/token/refresh/ -> HTTP {resp.status_code}\n"
                f"  {resp.text[:500]}\n\n"
                f"Copy the token again from Account & Settings -> Personal "
                f"Access Token.\nIt expires (24h by default), so a token from an "
                f"earlier session may simply have aged out."
            )
        access = resp.json().get("access")
        if not access:
            sys.exit(f"/api/token/refresh/ returned no access token: {resp.text[:500]}")
        return access

    def _auth_help(self, resp):
        return (
            f"Label Studio rejected the token (HTTP 401).\n"
            f"  server: {self.url}\n"
            f"  sent as: {self.scheme}\n"
            f"  detail: {resp.text[:300]}\n\n"
            f"On Label Studio 1.20+ the token under Account & Settings is a\n"
            f"personal access token (a long JWT with two dots in it), and the\n"
            f"older 40-character legacy tokens are disabled for new organizations.\n"
            f"Copy the personal access token and pass that instead.\n\n"
            f"To use a legacy token instead, turn it back on for the organization\n"
            f"(Organization -> Settings -> API tokens -> legacy tokens), or start\n"
            f"the server with LABEL_STUDIO_ENABLE_LEGACY_API_TOKEN=true."
        )

    # -- requests ---------------------------------------------------------- #
    def _request(self, method, path, **kwargs):
        return self.session.request(method, self.url + path, timeout=60, **kwargs)

    def _json(self, method, path, **kwargs):
        resp = self._request(method, path, **kwargs)
        if resp.status_code >= 400:
            sys.exit(f"{method} {path} -> HTTP {resp.status_code}\n{resp.text[:2000]}")
        return resp.json() if resp.content else {}

    def project_titles(self):
        """title -> id for every project the token can see."""
        titles, page = {}, 1
        while True:
            body = self._json("GET", f"/api/projects/?page={page}&page_size=100")
            results = body.get("results", body if isinstance(body, list) else [])
            for p in results:
                titles[p["title"]] = p["id"]
            if not body.get("next"):
                return titles
            page += 1

    def create_project(self, title, description, label_config):
        return self._json("POST", "/api/projects/", json={
            "title": title,
            "description": description,
            "label_config": label_config,
            # One annotator: a task is done as soon as it has been submitted once.
            "maximum_annotations": 1,
            "show_skip_button": True,
            # Keep the annotator on the Data Manager grid between tasks, so the
            # dev/test columns and shuffle_key ordering stay visible.
            "show_instruction": True,
            "expert_instruction": description,
        })

    def import_tasks(self, project_id, tasks):
        return self._json("POST", f"/api/projects/{project_id}/import", json=tasks)

    # -- local files storage ------------------------------------------------ #
    def local_storages(self, project_id):
        # This endpoint answers with a bare JSON array, unlike /api/projects/
        # which is paginated into {"results": [...]}. Handle both.
        body = self._json("GET", f"/api/storages/localfiles/?project={project_id}")
        if isinstance(body, list):
            return body
        return body.get("results", [])

    def create_local_storage(self, project_id, path, title):
        return self._json("POST", "/api/storages/localfiles/", json={
            "project": project_id,
            "path": path,
            "title": title,
            # The tasks are imported directly by this script, so this storage is
            # only ever here to authorize serving. It must NEVER be synced: a
            # sync scans the directory and adds a second copy of every page as a
            # new task.
            "use_blob_urls": False,
        })


def ensure_local_storage(ls, project_id, scans_dir):
    """Register scans/ with the project as a Local Files storage, if need be.

    Starting Label Studio with LOCAL_FILES_DOCUMENT_ROOT is necessary but NOT
    sufficient to make the images appear. The serving endpoint
    (io_storages/localfiles/views.py::localfiles_data) resolves
    ``/data/local-files/?d=<path>`` only when some LocalFilesImportStorage whose
    ``path`` is a prefix of that file's directory is attached to a project the
    viewer can open; with no such storage it returns 404 and the labeling screen
    shows an empty image panel.

    One storage per project, pointed at scans/, covers that project's dev and
    test pages at once. Label Studio requires the path to exist and to be a
    strict subdirectory of the document root, which scans/ is.
    """
    target = os.path.normpath(scans_dir)
    for storage in ls.local_storages(project_id):
        existing = os.path.normpath(storage.get("path") or "")
        if existing and target.startswith(existing):
            return "storage present"
    ls.create_local_storage(project_id, target, "Demo scans (serving only)")
    return "storage added"


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                    help=f"Where the import files are written (default: {DEFAULT_OUT_DIR})")
    ap.add_argument("--document-root", default=PUBLIC_ROOT,
                    help="Label Studio document root; must match "
                         "LABEL_STUDIO_LOCAL_FILES_DOCUMENT_ROOT and be a parent "
                         f"of scans/ (default: {PUBLIC_ROOT})")
    ap.add_argument("--seed", type=int, default=42,
                    help="Seed for the task shuffle (default: 42, i.e. reproducible)")
    ap.add_argument("--create", action="store_true",
                    help="Also create the projects on a running Label Studio and "
                         "import the tasks.")
    ap.add_argument("--url", default=os.environ.get("LABEL_STUDIO_URL",
                                                    "http://localhost:31415"),
                    help="Label Studio base URL (env: LABEL_STUDIO_URL)")
    ap.add_argument("--api-key", default=os.environ.get("LABEL_STUDIO_API_KEY"),
                    help="Personal access token from Account & Settings, or a "
                         "legacy API token (env: LABEL_STUDIO_API_KEY)")
    ap.add_argument("--title-suffix", default="",
                    help="Appended to every project title, e.g. 'round 2', to "
                         "create a second parallel set instead of reusing the first.")
    args = ap.parse_args()

    # Joined with a space, so --title-suffix "round 2" reads as a suffix rather
    # than running into the form name.
    title_suffix = f" {args.title_suffix.strip()}" if args.title_suffix.strip() else ""

    document_root = os.path.abspath(args.document_root)
    if not os.path.isdir(SCANS_DIR):
        sys.exit(f"Scans directory not found: {SCANS_DIR}")
    if os.path.commonpath([document_root, SCANS_DIR]) != document_root:
        sys.exit(f"--document-root must be a parent of {SCANS_DIR}\n"
                 f"  got: {document_root}")

    ls = None
    if args.create:
        if not args.api_key:
            sys.exit("--create needs --api-key (or LABEL_STUDIO_API_KEY). Find it in "
                     "Label Studio under Account & Settings -> Personal Access Token.")
        ls = LabelStudio(args.url, args.api_key)
        existing = ls.project_titles()

    os.makedirs(args.out_dir, exist_ok=True)
    rows, total = [], 0

    for slug, form in FORMS.items():
        tasks = build_tasks(slug, form, document_root, args.seed)
        if not tasks:
            print(f"WARNING: no images under scans/*/{form['folder']} -- skipped")
            continue

        out_path = os.path.join(args.out_dir, f"{slug}_tasks.json")
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(tasks, fh, ensure_ascii=False, indent=2)
        total += len(tasks)

        title = f"{TITLE_PREFIX} — {form['name']}{title_suffix}"
        status = "file only"
        if ls is not None:
            if title in existing:
                project_id = existing[title]
                status = f"exists (project {project_id}, tasks left alone)"
            else:
                description = PROJECT_DESCRIPTION.format(name=form["name"])
                project = ls.create_project(title, description, read_config(form))
                project_id = project["id"]
                result = ls.import_tasks(project_id, tasks)
                imported = result.get("task_count", len(tasks))
                status = f"created (project {project_id}, {imported} tasks imported)"
            # Checked on every run, not just on creation, so re-running repairs a
            # project whose images do not load because it has no storage yet.
            status += f"; {ensure_local_storage(ls, project_id, SCANS_DIR)}"
        rows.append((slug, len(tasks), title, status))

    print(f"\nWrote {len(rows)} import file(s), {total} task(s) total, to {args.out_dir}")
    for slug, n, title, status in rows:
        print(f"  {slug:28} {n:3} tasks  {title}\n  {'':28} {status}")

    if ls is None:
        print("\nNothing was created on a server (no --create). To import by hand:")
        print("  Label Studio -> Create Project -> Labeling Setup -> Custom template,")
        print(f"  paste labeling_configs/<form>.xml, then import "
              f"{os.path.relpath(args.out_dir, PROJECT_DIR)}/<form>_tasks.json")
    print("\nLabel Studio must be serving local files from:")
    print(f"  {document_root}")
    print("  (see ../label_studio.sh)")


if __name__ == "__main__":
    main()
