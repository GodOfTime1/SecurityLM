"""Create individual demo keys. Outputs a secret JSON file; sends no messages."""
import argparse
import json
import os
import re
import secrets
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--admin', required=True, help='Stable administrator ID')
    parser.add_argument('--employee', action='append', default=[], help='Stable employee ID; repeat per invitee')
    parser.add_argument('--output', required=True, type=Path, help='New private JSON file; never overwritten')
    args = parser.parse_args()
    ids = [args.admin, *args.employee]
    if (len(ids) > 100 or len(set(ids)) != len(ids)
            or any(not re.fullmatch(r'[A-Za-z0-9._-]{1,100}', i) or i.startswith('demo-') for i in ids)):
        parser.error('Use unique stable IDs (letters/digits/dot/underscore/hyphen), excluding demo- prefixes; max 100')
    entries = [{'id': identity, 'role': 'admin' if index == 0 else 'employee',
                'key': secrets.token_urlsafe(32)} for index, identity in enumerate(ids)]
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(entries, stream, indent=2)
        stream.write('\n')
    print(f'Created {len(entries)} identities. Treat the output file as secret; configure USER_API_KEYS from its JSON.')


if __name__ == '__main__':
    main()
