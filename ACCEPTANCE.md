# Release acceptance and compatibility

A release promises the documented supported operations, not universal firmware or notebook compatibility. Hermes validation is an admission gate, not an end-to-end test.

## Verified product boundaries

- Profile-scoped credentials, session reuse, cache and runtime outside the replaceable source installation.
- Masked local pairing prompt; model tools do not accept pairing codes. Existing enrollment was used for live qualification; a fresh human pairing was not repeated.
- Paginated cloud library browsing, folder-targeted PDF/EPUB uploads and exact original-file round trips.
- Readback-verified folder creation, rename/move, reversible trash/restore and optimistic concurrency protection. No permanent deletion or automatic replay after uncertain writes.
- Resumable recursive archive backup, incremental reuse, retained local copies and explicit incomplete/error reporting.
- On-demand cloud changes; no background scheduler or two-way synchronization.
- Optional isolated native rendering with synthetic PDF/PNG, typed-text, page-order, geometry and background tests; a supported real notebook page also rendered and was visually inspected.
- Real external renderer-worker integration, malformed-input and process-failure contracts.
- Explicit USB and read-only SSH preview transports tested through loopback/subprocess fixtures. Physical tablet qualification is not claimed.
- Inert import/registration, declared tools matching actual registration, clean installation and repeated setup without replacing credentials.

## Compatibility limits

See the README matrix for exact scene, pen, template and platform support. Native rendering rejects unrecognized content rather than silently omitting it. Standard ruled templates and some newer scene blocks remain unsupported by the pinned engine. Preserve source archives; use official/device-rendered exports for unsupported documents. No handwriting OCR is included.

The native renderer is optional and includes AGPL/commercial PyMuPDF; review `THIRD_PARTY_NOTICES.md` before installation or redistribution. Cloud operations do not require the renderer.

## Reproducible verification

Use Python 3.11+, Node.js 22+, npm and uv. Install the pinned test/native requirements into a virtual environment outside the plugin directory, install the sidecar lockfile, and run:

```text
python -m pytest tests/ -q -rs --import-mode=importlib -W error
npm --prefix sidecar test
hermes plugins validate .
hermes plugins doctor . --ci
```

Cross-platform CI runs without cloud credentials. Live cloud verification is separately opt-in through `tests/smoke_sidecar.py`, read-only by default. Authorized write tests must use a dedicated synthetic QA folder and retain a receipt. Never publish cloud receipts, credentials, account metadata or personal notebook fixtures.
