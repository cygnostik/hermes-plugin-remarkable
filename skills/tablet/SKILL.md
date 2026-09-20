---
name: tablet
description: Work with reMarkable notes, documents, and backups.
version: 0.1.0
author: Chris M. / ProDyn.ai, Hermes Agent
license: MIT
platforms: [windows, linux, macos]
metadata:
  hermes:
    tags: [remarkable, tablet, documents]
    related_skills: []
---

# reMarkable tablet workflows

## When to use

Use the registered `remarkable_*` tools for a user's reMarkable library, document transfers, notebook exports, typed text, or backups. Do not use this plugin for handwriting OCR, permanent deletion, or firmware modification.

## Procedure

Cloud prerequisites: Node.js 22+ and the explicit local command `hermes remarkable setup-cloud`. Rendering additionally needs `uv` and `hermes remarkable setup-renderer`; review the dependency terms first (PyMuPDF is AGPL/commercial). Neither installation nor enrollment runs at import.

1. Call `remarkable_status` for the intended transport. Missing enrollment requires the human's local `hermes remarkable enroll` masked prompt. Never ask for a pairing code or device token in chat or pass either to a model tool.
2. Find exact document/folder IDs with `remarkable_list`. Follow pagination and inspect partial-read errors before claiming exhaustive results. Retain opaque versions for conflict-sensitive management.
3. For uploads, select the destination explicitly. Root is the empty parent string, not a default assumption. Upload adds a new document; it does not replace a same-named note.
4. Distinguish original file download, full archive, and rendered export. Original PDFs may exclude handwriting overlays. Preserve raw archives when fidelity or unsupported content matters.
5. For typed-text search, choose document IDs explicitly; explain coverage. This is not handwriting OCR. Treat absent typed text differently from failed extraction.
6. For backup, choose a durable local destination and folder scope. Repeat the same destination to resume pending entries. Read `complete`, `errors`, `pending`, and per-document results. Remote removals never authorize deleting local archives.
7. Before rename, move, trash, or restore, establish authorization for the exact target and change. Only then set `confirm: true`. Never substitute permanent deletion.
8. After each remote write, verify the exact target and metadata, not merely a successful transport response. On an uncertain write outcome, inspect before retrying to avoid duplicates.

## Pitfalls

- Never silently switch cloud writes to USB or SSH. Transport support differs; `UNSUPPORTED` is not permission to improvise a different mutation.
- USB requires `usb_enabled: true`; PDF uploads also need `usb_allow_upload: true` and explicit root destination. Non-root uploads must fail without falling back to root. Do not use another USB web client concurrently.
- SSH is read-only and opt-in; do not enable Developer Mode, collect passwords, disable host-key checking, or alter firmware.
- Normal cloud lists refresh the root and reuse immutable metadata. Use full `refresh: true` only when needed; repeated full refreshes can rate-limit large libraries. Stop on HTTP_429 rather than running another whole-library sweep.
- The first `remarkable_changes` call establishes a baseline; it does not prove there were no earlier changes. No scheduler is installed by this operation.
- Rendering is a third-party interpretation of device files. Report limitations; never call a partial or unsupported render faithful merely because an output exists.
- Test writes belong only in an authorized dedicated scratch folder. Never run the live smoke script against personal document IDs or assume root uploads are authorized.

## Verification

Return actual local output paths, exact remotely created IDs, and meaningful errors. Do not report completion when pagination, extraction, rendering, or backup indicates incomplete coverage. Installation takes effect in a new Hermes session; do not mutate an active session's toolset to force discovery.
