"""Production entry point: place behind a TLS proxy on a private network."""
import uvicorn
from .auth import validate_config
from .deployment import production, proxy_networks, validate_deployment


def main():
    validate_config()
    validate_deployment()
    secure = production()
    uvicorn.run('app.main:app', host='0.0.0.0', port=8000,
                proxy_headers=secure,
                forwarded_allow_ips=','.join(proxy_networks()) if secure else '',
                reload=False, workers=1, access_log=False, server_header=False,
                limit_concurrency=32, timeout_keep_alive=5, timeout_graceful_shutdown=130)


if __name__ == '__main__':
    main()
