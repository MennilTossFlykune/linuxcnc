"""qtcnc-gen-keys: generate a CURVE keypair for a daemon or client.

Wraps `zmq.auth.create_certificates(target_dir, name)`. Two files are
written: `<name>.key` (public only, safe to share) and `<name>.key_secret`
(both keys, treat like an SSH private key).

Usage:

    qtcnc-gen-keys --dir ~/.qtcnc/keys --name server
    qtcnc-gen-keys --dir ~/.qtcnc/keys --name workstation-1

After generating, copy the public `.key` of each authorized client into
the daemon's `--authorized-clients-dir`. The daemon's own public `.key`
must be distributed to every client that wants to talk to it (clients
pass it via `--curve-server-key`).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="qtcnc-gen-keys",
        description="Generate a CURVE keypair for qtcnc daemon or client",
    )
    p.add_argument(
        "--dir", "-d", required=True,
        help="target directory (created if absent)",
    )
    p.add_argument(
        "--name", "-n", required=True,
        help="cert basename, e.g. 'server' or 'workstation-1'",
    )
    p.add_argument(
        "--force", "-f", action="store_true",
        help="overwrite existing key files",
    )
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    try:
        from zmq.auth.certs import create_certificates
    except ImportError as e:
        print(f"qtcnc-gen-keys: pyzmq missing or built without auth: {e}",
              file=sys.stderr)
        return 1

    args = _parse_args(argv if argv is not None else sys.argv[1:])
    target = Path(args.dir).expanduser()
    target.mkdir(parents=True, exist_ok=True)

    public = target / f"{args.name}.key"
    secret = target / f"{args.name}.key_secret"
    if (public.exists() or secret.exists()) and not args.force:
        print(
            f"qtcnc-gen-keys: refusing to overwrite existing keys in {target}\n"
            f"  {public}\n  {secret}\n"
            f"pass --force to replace",
            file=sys.stderr,
        )
        return 2

    pub_path, sec_path = create_certificates(str(target), args.name)
    # Lock down secret key file permissions — pyzmq writes 0644 by default
    # which is the wrong choice for a private key.
    try:
        os.chmod(sec_path, 0o600)
    except OSError:
        pass
    print(f"public key: {pub_path}")
    print(f"secret key: {sec_path}")
    print()
    print("To use as a daemon: pass --curve-secret-key", sec_path)
    print("To use as a client: pass --curve-client-key", sec_path)
    print("                    and --curve-server-key <daemon-public-key>")
    print()
    print(
        "Authorize a client on the daemon: copy the client's .key file into "
        "the daemon's --authorized-clients-dir.",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
