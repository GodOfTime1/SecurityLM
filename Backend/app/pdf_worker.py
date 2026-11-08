"""One-shot PDF parser. Process isolation is resource containment, not a sandbox."""
import sys

if __name__ == '__main__':
    # Apply limits before importing the PDF parser. Windows needs an OS/container
    # memory limit; the parent still enforces a wall-clock timeout there.
    if sys.platform.startswith('linux'):
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    import json
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from fastapi import HTTPException
    from app.documents import MAX_BYTES, _extract_pages
    try:
        data = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise HTTPException(413, 'Maximum file size is 10 MiB')
        result = {'pages': _extract_pages('upload.pdf', data)}
    except HTTPException as exc:
        result = {'status': exc.status_code, 'error': exc.detail}
    except Exception:
        result = {'status': 422, 'error': 'Unable to read document'}
    sys.stdout.write(json.dumps(result))
