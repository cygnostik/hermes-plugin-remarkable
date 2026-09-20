"""One-shot isolated native-renderer worker. JSON stdin/stdout, diagnostics stderr."""
import contextlib
import json
import sys


def run(request):
    if not isinstance(request, dict) or request.get('op') not in ('render', 'extract'):
        return {'error': 'Expected render or extract operation', 'code': 'BAD_OP'}
    args = request.get('args', {})
    try:
        # Native dependency output must never corrupt the response stream.
        with contextlib.redirect_stdout(sys.stderr):
            import renderer
            if request['op'] == 'render':
                return renderer.render_archive(args['source'], args['out_path'], format=args.get('format', 'pdf'),
                                               overwrite=args.get('overwrite', False), pages=args.get('pages'))
            return renderer.extract_archive_text(args['source'])
    except ImportError:
        return {'error': 'Renderer dependencies are unavailable; run hermes remarkable setup-renderer.', 'code': 'RENDERER_MISSING'}
    except Exception as exc:
        return {'error': str(exc), 'code': getattr(exc, 'code', 'RENDER_FAILED')}


if __name__ == '__main__':
    try:
        payload = json.loads(sys.stdin.read())
    except ValueError:
        payload = None
    print(json.dumps(run(payload), ensure_ascii=True))
