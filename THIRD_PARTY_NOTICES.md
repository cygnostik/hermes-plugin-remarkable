# Third-party notices

The plugin's original source is MIT. Dependencies retain their own licenses;
the MIT license does not relicense the combined optional rendering stack.

## Cloud runtime

Installed separately with `hermes remarkable setup-cloud` using
`sidecar/package-lock.json`, not bundled in the source release.

| Component | Pin | License / source |
|---|---|---|
| rmapi-js | 14.3.0 | MIT, https://github.com/erikbrinkman/rmapi-js |
| JSZip | 3.10.2 | MIT (also offered under GPL-3.0), https://github.com/Stuk/jszip |

Transitive versions are locked in `sidecar/package-lock.json`. Retain each
installed package's LICENSE/COPYING notices when redistributing dependencies.
No rmapi or rmfakecloud AGPL source was copied into the cloud adapter.

## Optional isolated renderer

**LIB rMLines**  
https://github.com/RedTTGMoss/librm_lines

Copyright 2025–2026 RedTTG. The official `rm-lines-sys==1.4.7` native binding
uses the tagged project's Apache-2.0 terms **with an additional attribution
requirement**. Exact upstream terms and third-party notices are preserved in
`licenses/librm_lines-LICENSE.md` and `licenses/librm_lines-THIRD-PARTY-NOTICES.md`.
This software uses FreeType; see the linked upstream notices. Native binaries
are downloaded by the user-run setup, not vendored in this plugin.

| Component | Pin | License / source |
|---|---|---|
| rmscene | 0.8.0 | MIT, https://github.com/ricklupton/rmscene |
| PyMuPDF / MuPDF | 1.27.2.3 | AGPL-3.0 or commercial Artifex license, https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright |
| Pillow | 12.1.1 | MIT-CMU, https://github.com/python-pillow/Pillow |
| packaging | 23.2 | Apache-2.0 / BSD-2-Clause, https://github.com/pypa/packaging |

**PyMuPDF is not permissively licensed.** Its AGPL obligations or an appropriate
Artifex commercial license apply to covered uses/distributions. Installing it
in a subprocess/venv is dependency isolation, not a license exemption. This
source release does not ship PyMuPDF/MuPDF binaries. Before redistributing a
bundled runtime or using the rendering stack as part of a proprietary/network
service, review the applicable terms and corresponding-source obligations.
Cloud transfers and backups do not require this optional stack.

All original renderer/worker source is included under the plugin's MIT license,
which permits its use under compatible copyleft terms. No paid license is
purchased by setup. Users who cannot accept the dependency terms should not run
`setup-renderer`; official tablet PDF export remains available separately.

## Provenance and trademarks

Original adapter, worker, tests, docs and synthetic rendering fixtures were
written for ProDyn.ai under Chris M.'s direction with AI assistance. Synthetic
fixtures are not copies of user notebooks. Private account data, credentials,
cloud receipts and notebook exports are excluded from releases.

reMarkable is a trademark of reMarkable AS. This project is unofficial and is
not affiliated with or endorsed by reMarkable AS or the listed projects.
