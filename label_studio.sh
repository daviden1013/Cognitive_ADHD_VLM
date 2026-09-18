#!/usr/bin/env bash
# Start Label Studio for the public demo.
#
# The scans are served straight off disk, so the document root has to be this
# directory -- the parent of both ADHD_assessment/scans and
# cognitive_assessment/scans. It is the same path the project builders use as
# their default --document-root, so the /data/local-files/?d=... URLs in the
# import files resolve without any further configuration.
#
# Unlike the real pipeline, these scans are synthetic: no PHI, nothing to
# restrict. Serving them locally is only about not having to upload 40 images.
set -euo pipefail

PUBLIC_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export LABEL_STUDIO_LOCAL_FILES_SERVING_ENABLED=true
export LABEL_STUDIO_LOCAL_FILES_DOCUMENT_ROOT="$PUBLIC_ROOT"

echo "Serving local files from: $LABEL_STUDIO_LOCAL_FILES_DOCUMENT_ROOT"
label-studio start --port "${LABEL_STUDIO_PORT:-31415}"
