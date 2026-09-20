# reMarkable 0.2.0

Cloud-first reMarkable integration for Hermes, with 12 tools and an optional isolated native notebook renderer. Unofficial; not affiliated with reMarkable AS.

## Features

- Paginated cloud browsing, name/tag filtering, explicit folder-targeted PDF/EPUB uploads, original downloads and raw archives.
- Readback-verified folder creation, rename/move, reversible trash/restore and immutable-root concurrency protection. No permanent deletion or blind write retry.
- Resumable, non-destructive folder backups and on-demand cloud change reports.
- Durable profile-scoped enrollment, cross-process session reuse, masked local pairing and a session-reset command that preserves enrollment.
- PDF/PNG rendering through LIB rMLines and typed-text extraction/search for supported scenes. No handwriting OCR.
- Explicit USB and read-only SSH preview transports; no silent fallback, firmware changes or automatic Developer Mode.

## Release verification

The final public source passed **215 Python tests and 40 subtests**, with one Windows symlink-privilege skip. The Node suite passed **56 tests**, with one POSIX-only permission skip on Windows. Hermes validation passed without warnings and Doctor discovered all 12 tools. Cross-platform CI independently runs native/worker and cloud protocol tests on Windows, Ubuntu 24.04 and macOS 15.

A clean isolated installation exercised cloud setup, optional native setup, repeated setup, real worker PDF output, discovery and registration. Update-survival tests cover durable state and credentials; missing cloud dependencies now return an actionable `setup-cloud` repair command. Native setup uses binary wheels and returns structured launch/install errors.

Live cloud qualification used an authorized account with synthetic QA-folder writes: folder-targeted PDF/EPUB round trips, exact mutation readback, recursive/incremental backup, reversible trash/restore and change tracking passed. Existing enrollment was reused rather than repeating human pairing. Private documents and receipts are not included in this repository.

Renderer review added source-grounded support for redundant geometry fields and native timestamped text width, with synthetic pixel-equivalence and worker tests. Real notebook pages were also exercised without publishing their content.

## Explicit compatibility boundaries

This is not a universal notebook converter. The pinned native engine lacks standard ruled templates and does not safely interpret every newer scene layout, image, brush or transform. Unsupported content fails rather than silently disappearing. The README lists supported formats; preserve archives and use official/device exports for unsupported documents. A supported real-page render does not prove whole-notebook fidelity.

USB/SSH have protocol-boundary tests, not physical-tablet qualification. Backups are best-effort snapshots, not atomic captures of simultaneous tablet edits. See `ACCEPTANCE.md` and `THIRD_PARTY_NOTICES.md`; optional PyMuPDF has AGPL/commercial licensing obligations.

## Install and update

Follow the README. After updating plugin source, rerun `hermes remarkable setup-cloud`; rerun `setup-renderer` when native requirements change. Credentials and isolated runtime remain outside the replaceable plugin directory. Start a new Hermes conversation for changed tool code.
