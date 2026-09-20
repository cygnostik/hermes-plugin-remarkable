"""Explicit, bounded contracts for the reMarkable agent tools."""


def field(kind, description, **kwargs):
    return dict(type=kind, description=description, **kwargs)


def schema(name, description, properties=None, required=(), transport=True):
    props = dict(properties or {})
    if transport:
        props['transport'] = field('string', 'Explicit transport; defaults to configured cloud. Never silently switch a write.', enum=['cloud', 'usb', 'ssh'])
    return {'name': 'remarkable_' + name, 'description': description,
            'parameters': {'type': 'object', 'properties': props, 'required': list(required), 'additionalProperties': False}}


ID = field('string', 'Document/folder ID returned by remarkable_list.')
PARENT = field('string', "Folder ID; empty string means library root.")
OVERWRITE = field('boolean', 'Explicitly replace an existing local output file. Default false.')
SOURCE = field('string', 'Local full document archive (.zip/.rmdoc); otherwise supply id to download it.')
ENROLL = schema('enroll', 'Get secure local pairing instructions. Never request or pass an enrollment code in chat. The human runs hermes remarkable enroll in their own terminal.', transport=False)
STATUS = schema('status', 'Check enrollment and selected transport reachability. Does not change the tablet or enroll automatically.')
LIST = schema('list', 'Browse/search library names and cloud tags, including folders, notebooks, PDFs and EPUBs. Local search is names-only; tags/trash options require cloud. Omit parent for the full library; follow next_offset/has_more for complete coverage. Read partial errors; never treat an incomplete listing as exhaustive.', {
    'parent': PARENT,
    'query': field('string', 'Case-insensitive name/tag query; not handwriting OCR.'),
    'offset': field('integer', 'Pagination offset.', minimum=0),
    'limit': field('integer', 'Maximum returned entries.', minimum=1, maximum=200),
    'refresh': field('boolean', 'Bypass listing cache and request fresh metadata.'),
    'include_tags': field('boolean', 'Fetch per-document tags.'),
    'include_trash': field('boolean', 'Include trashed entries explicitly.')})
DOWNLOAD = schema('download', 'Download cloud originals or complete document archives. Defaults to original for cloud, archive for USB/SSH. USB also supports device-generated PDF, not original PDF. SSH supports archives only. Original PDFs exclude handwritten overlays; use remarkable_export for rendering. Local transports never overwrite existing outputs.', {
    'id': ID, 'out_path': field('string', 'Absolute local destination path.'),
    'format': field('string', 'original: cloud only; archive: all transports; pdf: USB device export only.', enum=['original', 'archive', 'pdf']), 'overwrite': OVERWRITE}, ['id', 'out_path'])
UPLOAD = schema('upload', 'Add a PDF/EPUB to an explicit folder, then verify the cloud entry. Never replaces an existing document. A timeout may mean the remote write happened: inspect before retrying.', {
    'path': field('string', 'Absolute local PDF/EPUB path.'), 'name': field('string', 'Visible document name.'), 'parent': PARENT,
    'kind': field('string', 'Inferred from extension if omitted.', enum=['pdf', 'epub'])}, ['path', 'name', 'parent'])
MKDIR = schema('mkdir', 'Create a folder beneath an explicit parent and verify readback.', {'name': field('string', 'Folder name.'), 'parent': PARENT}, ['name', 'parent'])
MANAGE = schema('manage', 'Rename, move, trash or restore an explicitly authorized document/folder. Get user authorization before confirm=true. No permanent delete. expected_version prevents changing an item that changed since listing.', {
    'id': ID, 'action': field('string', 'Requested mutation.', enum=['rename', 'move', 'trash', 'restore']),
    'name': field('string', 'New name for rename.'), 'parent': PARENT,
    'confirm': field('boolean', 'True only after user authorized this specific change.'),
    'expected_version': field('string', 'Opaque version from the listing, when supplied.')}, ['id', 'action', 'confirm'])
EXPORT = schema('export', 'Render notebook/annotated document archive to PDF or PNG using librm_lines. Requires isolated renderer setup. Returns unsupported-content warnings/errors rather than claiming fidelity silently.', {
    'id': ID, 'source': SOURCE, 'out_path': field('string', 'Absolute output PDF path or PNG output target.'),
    'format': field('string', 'Rendered output type.', enum=['pdf', 'png']), 'overwrite': OVERWRITE,
    'pages': field('array', 'Optional 1-based page selection.', items={'type':'integer','minimum':1})}, ['out_path'])
EXTRACT_TEXT = schema('extract_text', 'Extract typed notebook text from a complete document archive. This is not handwriting recognition and does not use an OCR service.', {'id': ID, 'source': SOURCE})
BACKUP = schema('backup', 'Create/resume a read-only recursive archive backup with a folder metadata manifest. Reports partial failures and pending items; rerun to resume. Never deletes local archives when cloud documents disappear.', {
    'destination': field('string', 'Absolute local backup directory.'), 'parent': PARENT,
    'max_items': field('integer', 'Maximum changed documents to download this run.', minimum=1, maximum=5000)}, ['destination'])
CHANGES = schema('changes', 'Report cloud library changes since the previous completed snapshot. First call establishes a baseline. On-demand only; does not create scheduled jobs or two-way sync.', {'reset': field('boolean', 'Establish a fresh baseline instead of comparing.')})
SEARCH = schema('search', 'Search typed notebook text across explicitly selected document IDs. Use remarkable_list to find candidates; this is not handwriting OCR or a claim of whole-library coverage.', {
    'query': field('string', 'Case-insensitive text to find.'),
    'ids': field('array', 'Explicit document IDs to search; bounded to 50.', items={'type':'string'}, minItems=1, maxItems=50)}, ['query', 'ids'])
ALL = [ENROLL, STATUS, LIST, DOWNLOAD, UPLOAD, MKDIR, MANAGE, EXPORT, EXTRACT_TEXT, SEARCH, BACKUP, CHANGES]
