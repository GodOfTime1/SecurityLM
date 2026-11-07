"""Fail-closed settings and edge restrictions for a small invite-only demo."""
import ipaddress
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
from fastapi import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

log = logging.getLogger('uvicorn.error')


def production():
    mode = os.environ.get('APP_ENV', 'development')
    if mode not in {'development', 'production'}:
        raise RuntimeError('APP_ENV must be development or production')
    return mode == 'production'


def setting(name, default):
    try:
        value = int(os.environ.get(name, default))
        if not 1 <= value <= 1_000_000_000:
            raise ValueError
        return value
    except ValueError:
        raise RuntimeError(f'{name} must be a positive integer') from None


def values(name):
    return tuple(v.strip() for v in os.environ.get(name, '').split(',') if v.strip())


@dataclass(frozen=True)
class Policy:
    hosts: tuple
    origins: tuple
    ip_rpm: int


def policy():
    hosts, origins = values('ALLOWED_HOSTS'), values('CORS_ORIGINS')
    if not hosts or any(not re.fullmatch(r'[a-zA-Z0-9.-]+', h) or '*' in h for h in hosts):
        raise RuntimeError('ALLOWED_HOSTS requires explicit hostnames without ports or wildcards')
    for origin in origins:
        p = urlsplit(origin)
        if p.scheme != 'https' or not p.netloc or p.username or p.password or p.path or p.query or p.fragment:
            raise RuntimeError('CORS_ORIGINS must contain exact HTTPS origins without trailing slashes')
        try:
            _ = p.port
        except ValueError:
            raise RuntimeError('Invalid CORS origin port') from None
        if '*' in origin:
            raise RuntimeError('CORS wildcards are not allowed')
    return Policy(hosts, origins, setting('IP_REQUESTS_PER_MINUTE', 120))


def proxy_networks():
    networks = values('TRUSTED_PROXY_IPS')
    if not networks:
        raise RuntimeError('Set TRUSTED_PROXY_IPS to your TLS proxy IPs or CIDRs')
    try:
        if any(ipaddress.ip_network(n, strict=False).prefixlen == 0 for n in networks):
            raise ValueError
    except ValueError:
        raise RuntimeError('Use specific IPs/CIDRs for TRUSTED_PROXY_IPS; catch-all networks are forbidden') from None
    return networks


def validate_deployment():
    if not production():
        return
    policy()
    proxy_networks()
    database = os.environ.get('DATABASE_PATH', '')
    if not database or not Path(database).is_absolute():
        raise RuntimeError('Production requires an absolute DATABASE_PATH on persistent private storage')
    from .auth import _configured_users
    users = _configured_users()
    if not any(user.role == 'admin' for _, user in users):
        raise RuntimeError('Configure an individual administrator identity')
    for name, default in [('USER_REQUESTS_PER_MINUTE',60),('CHAT_REQUESTS_PER_MINUTE',10),
                          ('MAX_DOCUMENTS',100),('MAX_DOCUMENT_TEXT_BYTES',20*1024*1024),
                          ('MAX_CONVERSATIONS_PER_USER',20),('MAX_MESSAGES_PER_CONVERSATION',200),
                          ('MAX_REQUEST_IDS_PER_CONVERSATION',400),('MAX_ASSISTANTS',30),('MAX_TICKETS',1000)]:
        setting(name, default)
    from .model_client import get_model_client
    get_model_client()  # validate server URL / timeout when an actual model is selected


class DeploymentMiddleware:
    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.window = None
        self.ips = {}

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not production():
            return await self.app(scope, receive, send)
        request_id, started, status = str(uuid4()), time.monotonic(), 500
        async def marked(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
                message = dict(message)
                message['headers'] = [(k,v) for k,v in message.get('headers', [])
                    if k.lower() not in {b'x-request-id',b'cache-control',b'x-content-type-options',b'strict-transport-security'}] + [
                    (b'x-request-id', request_id.encode()), (b'cache-control', b'no-store'),
                    (b'x-content-type-options', b'nosniff'),
                    (b'strict-transport-security', b'max-age=31536000')]
            await send(message)
        async def reject(code, detail, extra=None):
            await JSONResponse({'detail': detail}, status_code=code, headers=extra)(scope, receive, marked)
        try:
            try:
                config = policy()
            except (RuntimeError, ValueError):
                return await reject(503, 'Deployment is not configured')
            async def guarded(inner_scope, inner_receive, inner_send):
                if inner_scope['scheme'] != 'https' and inner_scope['path'] != '/health':
                    return await reject(400, 'HTTPS is required')
                origins = [v.decode('latin-1') for k, v in inner_scope['headers'] if k.lower() == b'origin']
                if len(origins) > 1 or (origins and origins[0] not in config.origins):
                    return await reject(403, 'Origin is not allowed')
                if inner_scope['path'] != '/health':
                    peer = (inner_scope.get('client') or ('unknown', 0))[0]
                    window = int(time.time() // 60)
                    with self.lock:
                        if self.window != window:
                            self.ips.clear()
                            self.window = window
                        if ((peer not in self.ips and len(self.ips) >= 4096)
                                or self.ips.get(peer, 0) >= config.ip_rpm):
                            allowed = False
                        else:
                            self.ips[peer] = self.ips.get(peer, 0) + 1
                            allowed = True
                    if not allowed:
                        return await reject(429, 'Too many requests', {'Retry-After': str(60 - int(time.time()) % 60)})
                if inner_scope['path'] in {'/docs','/docs/','/redoc','/openapi.json','/docs/oauth2-redirect'}:
                    return await reject(404, 'Not found')
                # CORS handles permitted preflights before credential checks.
                cors = CORSMiddleware(self.app, allow_origins=list(config.origins),
                    allow_methods=['GET','POST','PUT','PATCH','DELETE'],
                    allow_headers=['Content-Type','X-API-Key'],
                    expose_headers=['X-Request-ID','Retry-After'], allow_credentials=False)
                await cors(inner_scope, inner_receive, inner_send)
            await TrustedHostMiddleware(guarded, allowed_hosts=list(config.hosts), www_redirect=False)(scope, receive, marked)
        finally:
            route = scope.get('route')
            # Never log query strings, request bodies, API keys, or raw unknown URLs.
            log.info(json.dumps({'request_id': request_id, 'method': scope['method'],
                'route': getattr(route, 'path', '<unmatched>'), 'status': status,
                'duration_ms': round((time.monotonic() - started) * 1000)}))
