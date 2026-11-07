"""Authenticate and bound requests before FastAPI parses their bodies."""
import asyncio
import time
from threading import BoundedSemaphore
from fastapi import HTTPException
from starlette.responses import JSONResponse
from .auth import authenticate_key

MAX_REQUEST_BYTES = 11 * 1024 * 1024  # file plus multipart envelope
MAX_JSON_BYTES = 64 * 1024
BODY_TIMEOUT = 15
PUBLIC_PATHS = frozenset({'/health', '/docs', '/docs/oauth2-redirect', '/redoc', '/openapi.json'})


class SecurityMiddleware:
    def __init__(self, app):
        self.app = app
        # Per-process bound on bodies buffered and requests handled concurrently.
        self.slots = BoundedSemaphore(8)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)

        async def secure_send(message):
            if message['type'] == 'http.response.start':
                message = dict(message)
                headers = list(message.get('headers', []))
                headers.extend([(b'cache-control', b'no-store'),
                                (b'x-content-type-options', b'nosniff'),
                                (b'referrer-policy', b'no-referrer')])
                message['headers'] = headers
            await send(message)

        async def reject(status, detail):
            await JSONResponse({'detail': detail}, status_code=status)(scope, receive, secure_send)

        headers = scope.get('headers', [])
        path = scope['path']
        parts = path.rstrip('/').split('/')
        upload = ((path.rstrip('/') == '/documents' and scope['method'] == 'POST')
                  or (len(parts) == 3 and parts[1] == 'documents' and scope['method'] == 'PUT'))
        if path not in PUBLIC_PATHS:
            keys = [v for k, v in headers if k.lower() == b'x-api-key']
            if len(keys) != 1:
                return await reject(401, 'Invalid or missing API key')
            try:
                user = authenticate_key(keys[0].decode('latin-1'))
            except HTTPException as exc:
                return await reject(exc.status_code, exc.detail)
            if upload and user.role != 'admin':
                return await reject(403, 'Administrator access required')

        limit = MAX_REQUEST_BYTES if upload else MAX_JSON_BYTES
        lengths = [v for k, v in headers if k.lower() == b'content-length']
        if lengths:
            if len(lengths) != 1 or len(lengths[0]) > 20 or not lengths[0].isdigit():
                return await reject(400, 'Invalid Content-Length')
            if int(lengths[0]) > limit:
                return await reject(413, 'Request body too large')
        if not self.slots.acquire(blocking=False):
            return await reject(503, 'Server busy; retry later')
        try:
            body = bytearray()
            deadline = time.monotonic() + BODY_TIMEOUT
            while True:
                try:
                    message = await asyncio.wait_for(receive(), max(0, deadline - time.monotonic()))
                except TimeoutError:
                    return await reject(408, 'Request body timed out')
                if message['type'] == 'http.disconnect':
                    return
                chunk = message.get('body', b'')
                if len(body) + len(chunk) > limit:
                    return await reject(413, 'Request body too large')
                body.extend(chunk)
                if not message.get('more_body', False):
                    break
            if lengths and int(lengths[0]) != len(body):
                return await reject(400, 'Content-Length mismatch')
            delivered = False

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                return await receive()

            await self.app(scope, replay, secure_send)
        finally:
            self.slots.release()
