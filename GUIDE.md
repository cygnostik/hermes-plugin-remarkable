# reMarkable for Hermes · setup and reference

Browse your tablet library, send documents to folders, bring notes back as PDF/PNG, extract typed text, and keep a resumable local backup.

**Unofficial. Not affiliated with or endorsed by reMarkable AS.** Cloud access uses [rmapi-js](https://github.com/erikbrinkman/rmapi-js); vendor API and firmware changes can affect compatibility.

> **Cloud-first toolkit.** Native rendering has an explicit compatibility matrix below; USB/SSH are opt-in preview transports. Unsupported content produces an error instead of an incomplete export. See `ACCEPTANCE.md` for the release qualification.

## Start here

1. Install from the standalone repository: `hermes plugins install https://github.com/cygnostik/hermes-plugin-remarkable`, then `hermes plugins enable remarkable`. The plugin is also listed in the Hermes catalog as `remarkable`.
2. Have **Node.js 22+ and npm** available on PATH, or configure `node_path`. Have `uv` available if you want notebook rendering. Windows AMD64 with Python 3.11 is the tested native-rendering platform; Linux wheels require glibc 2.39+, macOS wheels require macOS 15+.
3. In **your own terminal**, run:

   ```text
   hermes remarkable setup-cloud
   hermes remarkable enroll
   hermes remarkable status
   hermes remarkable setup-renderer
   ```

   Cloud setup installs the exact lockfile with npm lifecycle scripts disabled; it is explicit, never import-time installation. Enrollment opens the account-pairing page and asks for the one-time code in a **masked local prompt**. Never send that code to chat. Configure `token_file` instead if reusing an existing compatible device-token file. Rendering is optional: read its dependency license terms in `THIRD_PARTY_NOTICES.md` before running `setup-renderer` (PyMuPDF is AGPL/commercial, not MIT).
4. Start a new Hermes conversation for tool discovery. Try:

   > Show my reMarkable folders.
   > Send this PDF to my Reading folder.
   > Export this notebook as a PDF without replacing an existing file.
   > Back up my Work folder, including its subfolders.

The plugin does not purchase subscriptions, enable Developer Mode, change firmware, start scheduled jobs, or silently switch transports. Cloud availability depends on the account and service; an authentication error does not automatically prove a subscription problem.

## Tools

| Tool | Purpose |
|---|---|
| `remarkable_status` | Check the selected transport and enrollment |
| `remarkable_enroll` | Secure local setup instructions; never accepts a code in chat |
| `remarkable_list` | Paginated browse and name/tag filtering |
| `remarkable_upload` | Add PDF/EPUB to an explicit folder |
| `remarkable_download` | Original PDF/EPUB or full document archive |
| `remarkable_export` | Notebook/annotated-document PDF or PNG via `librm_lines` |
| `remarkable_extract_text` | Typed text, not handwriting OCR |
| `remarkable_search` | Typed-text search across selected documents |
| `remarkable_mkdir` | Create a folder |
| `remarkable_manage` | Confirmed rename, move, trash, restore; no permanent delete |
| `remarkable_backup` | Resumable, non-destructive archive backup |
| `remarkable_changes` | On-demand changes since a saved cloud snapshot |

### Originals, archives, and exports

An original PDF is the uploaded file, without a guarantee that tablet handwriting overlays are included. A full archive preserves the device document files. Use export for rendering and inspect any unsupported-content warnings. Third-party rendering is not guaranteed pixel-identical to the official app for every firmware feature.

### Native-rendering compatibility

This release uses **LIB rMLines 1.4.7**, not the tablet's official renderer. Actual native execution, synthetic fixtures, and one supported page from a real notebook have been verified on Windows AMD64. That is not a claim that every notebook is supported.

| Content | Qualified behavior |
|---|---|
| v6 ink and typed text in recognized scene blocks | PDF; PNG for one selected page |
| Blank / five supported grid templates | Background preserved by native engine |
| Standard ruled templates (`P Lines small/medium/large`) | **Not supported upstream**; fails rather than replacing with blank |
| Legacy v3/v5 | Bounded single-layer portrait fineliner conversion; other legacy ink remains unsupported |
| Legacy typed text | Explicit empty result after structural validation; those formats have no native typed text |
| Original PDF backgrounds | Vector/searchable background retained where annotation geometry is qualified |
| Unknown/newer blocks, images, hidden layers, custom templates/transforms, clipped/infinite pages | Explicit unsupported error; no silent omission |

`pages` is **1-based**. Selecting supported pages can export those pages even when an unselected page uses an unsupported template. Whole-notebook typed-text extraction fails if any selected document page cannot be interpreted safely; search reports per-document errors rather than claiming exhaustive coverage.

For unsupported material, retain the raw archive and use an official tablet/app PDF export. Explicit USB PDF download is another device-rendered path when that tablet firmware supports the web endpoint; physical-device compatibility has not yet been qualified here. No handwriting OCR is enabled.

### Backups

Backups use stable document-ID filenames and `manifest.json` to preserve names and folder hierarchy. Rerun the same destination and scope to resume. Remote removals are reported but **never delete local copies**. Errors and pending work prevent `complete: true`. This is a best-effort snapshot, not a transactional capture of all simultaneous tablet edits.

### Writes

Folder targeting is explicit, including `parent: ""` when root is deliberately intended. Management requires `confirm: true` after authorization for the specific change. Keep the listing's opaque version for conflict-sensitive requests. A timed-out write may have completed: inspect the destination before retrying. No blind retry through a different transport.

## Configuration

Use Hermes settings, never a token pasted into configuration:

```text
hermes config set plugins.entries.remarkable.settings.node_path "C:/Program Files/nodejs/node.exe"
hermes config set plugins.entries.remarkable.settings.token_file "C:/path/to/existing/device-token"
```

| Setting | Default / use |
|---|---|
| `token_file` | Profile `credentials/remarkable/device-token` |
| `state_dir` | Profile `plugin-data/remarkable` |
| `node_path` | `node` |
| `timeout_ms` | Cloud-operation deadline in milliseconds |
| `transport` | `cloud`; explicit alternatives `usb`, `ssh` |
| `renderer_python` | Private rendering environment created by setup |
| `render_timeout` | Native-rendering deadline in seconds |
| `usb_enabled` | `false`; must be explicitly enabled to use USB |
| `usb_allow_upload` | `false`; additional permission for USB PDF uploads |
| `usb_url` | `http://10.11.99.1`, after enabling tablet USB web access |
| `ssh_enabled` | `false`; must be explicitly enabled to use SSH |
| `ssh_host`, `ssh_user`, `ssh_port`, `ssh_identity` | Existing key-based SSH connection; trusted host key required |

Settings bind to each plugin registration instead of sharing a global configuration across profiles. Credentials and state stay outside the installation and survive replacement. Temporary sessions are cached beside the device token as `<token_file>.session.json`, bound to the exact device credential and renewed before expiry. Both files contain credentials and are **not encrypted by this plugin**; protect your OS account/directory ACL and revoke pairings from your reMarkable account if necessary. Never include tokens, downloaded notes, caches, or backup manifests in public bug reports.

`setup-renderer` uses `uv` and pinned requirements in a separate Python environment. It does not replace Hermes packages. Cloud transfers do not require native rendering dependencies. Native renderer availability is platform-dependent; consult the acceptance evidence for actually tested platforms.

## Local tablet access

Cloud is default. USB/SSH are opt-in—not silent workarounds for failed writes.

- **USB:** enable the tablet web interface and set `usb_enabled: true`. PDF/archive reads are supported. PDF writes also require `usb_allow_upload: true` and explicit root `parent: ""`; a requested non-root folder fails without uploading anywhere. Do not concurrently use another USB web client: the tablet uses shared folder-selection state. Firmware endpoints differ; live-device compatibility has not been established by the loopback tests.
- **SSH:** read-only document access with your existing key and known host. No password collection, automatic Developer Mode, firmware changes, or write-back. Unknown hosts fail rather than disabling host-key checking.

Local transports do not promise every cloud operation. `UNSUPPORTED` distinguishes absent capability from network failure.

## Updating

After `hermes plugins update remarkable` (or replacing the source directory), run `hermes remarkable setup-cloud` again. Updates replace install-local Node dependencies. Credentials, sessions, backups and the isolated renderer runtime stay outside that directory and survive. If the release changes native requirements, rerun `hermes remarkable setup-renderer` too; it updates the existing isolated environment. Start a new Hermes conversation after updating. `CLOUD_SETUP_REQUIRED` includes the repair command and does not mean you need to enroll again.

## Troubleshooting

- **NO_TOKEN:** enroll locally or configure an existing token-file path; never paste its contents.
- **AUTH_FAILED with existing enrollment:** run `hermes remarkable clear-session`, then retry a read such as `status`. This removes only the cached session, not device enrollment. If the device credential was revoked, use the masked enrollment flow. Never automatically replay a failed write.
- **NO_NODE:** install a supported Node release or configure its executable.
- **RENDERER_MISSING:** run `hermes remarkable setup-renderer`. Keep platform/install errors for diagnosis; no account credential is needed.
- **Partial listing:** inspect errors, then retry with `refresh: true`. Missing results are not proof of deleted notes.
- **HTTP_429:** stop and let the service's rate limit clear. Normal browsing always refreshes the root and reuses immutable document metadata; `refresh: true` deliberately bypasses that cache and can be expensive on a large library. Do not repeatedly request a whole-library full refresh.
- **WRITE_OUTCOME_UNKNOWN / uncertain write:** read back the destination before retrying.
- **EXISTS:** choose another local output or explicitly authorize overwrite.
- **SSH authentication/host-key failure:** establish trust and key access yourself in your terminal; the plugin does not weaken SSH.
- **Unsupported rendering content:** preserve the archive and use official export for that document rather than discarding the source.

## Development and live verification

Run offline tests, renderer tests in its isolated environment, Node tests, and both Hermes gates. Keep virtual environments outside the plugin package.

```text
python -m pytest tests/ --import-mode=importlib
python cli.py setup-cloud
npm --prefix sidecar test
hermes plugins validate .
hermes plugins doctor . --ci
```

`tests/smoke_sidecar.py` is inert on import and **read-only by default**. It requires an explicit token-file path and a fresh output directory. Writes require `--allow-writes` plus `--create-scratch` or a `--scratch-folder` named `Hermes Plugin QA ...`. Its receipt records attempts before dispatch and returned IDs afterwards. It uses generated fixtures only, compares downloaded originals byte-for-byte, and leaves the test folder for inspection. Never blindly repeat a run after an ambiguous outcome.

## License

Plugin source: MIT. Dependencies retain their own licenses; see `THIRD_PARTY_NOTICES.md` and the pinned distributions. The reMarkable name belongs to reMarkable AS.
