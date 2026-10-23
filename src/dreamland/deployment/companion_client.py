"""Backend-owned private companion probe; it grants no domain capability."""

from __future__ import annotations

import argparse
import http.client
import json
import multiprocessing
import os
import re
import signal
import stat
import sys
import time
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from dreamland.deployment import companion

_MAX_RESPONSE = 32 * 1024
_MAX_WORKER_RESULT = 2048
PROBE_TIMEOUT_SECONDS = 15.0
HEALTHCHECK_TIMEOUT_SECONDS = 3.0
WORKER_STOP_GRACE_SECONDS = 0.25


class ProbeError(ValueError):
    """The private connection did not satisfy the supported metadata contract."""


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProbeError("Invalid companion response.")
        result[key] = value
    return result


def _constant(_value: str) -> None:
    raise ProbeError("Invalid companion response.")


def _json(content: bytes) -> object:
    try:
        return json.loads(content, object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ProbeError("Invalid companion document or response.") from None


def _read_file(path: Path, *, secret: bool) -> bytes:
    limit = 256 if secret else _MAX_RESPONSE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size > limit
                or (
                    secret
                    and (
                        metadata.st_uid != os.geteuid()
                        or stat.S_IMODE(metadata.st_mode) != 0o600
                    )
                )
            ):
                raise ProbeError("Companion input file is unsafe.")
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise ProbeError("Companion input file is too large.")
        return content
    except OSError:
        raise ProbeError("Companion input file is unavailable.") from None


def _token(path: Path, role: str) -> str:
    content = _read_file(path, secret=True)
    pattern = rb"dreamland-" + role.encode("ascii") + rb"-transport:[A-Za-z0-9_-]{64}\n"
    if not re.fullmatch(pattern, content):
        raise ProbeError("Companion credential file is invalid.")
    return content[:-1].decode("ascii")


def _request(handoff: dict, operation: str, token: str | None) -> tuple[int, object]:
    connection = http.client.HTTPConnection(
        handoff["transport"]["alias"], handoff["transport"]["port"], timeout=5
    )
    headers = {"Accept": "application/json", "Connection": "close"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    try:
        connection.request("GET", handoff["paths"][operation], headers=headers)
        response = connection.getresponse()
        content = response.read(_MAX_RESPONSE + 1)
        if (
            len(content) > _MAX_RESPONSE
            or response.getheader("Content-Type", "").split(";", 1)[0]
            != "application/json"
        ):
            raise ProbeError("Invalid companion response.")
        return response.status, _json(content)
    except (OSError, http.client.HTTPException):
        raise ProbeError("Private companion connection failed.") from None
    finally:
        connection.close()


def _require_auth_denial(status: int, value: object) -> None:
    validated = companion.validate_response("error", value)
    if status != 401 or validated["error"]["code"] != "unauthorized":
        raise ProbeError("Companion did not provide a valid authentication denial.")


def _probe(
    handoff: dict, token_file: Path, *, deny_handoff: dict | None = None
) -> dict:
    """Own response/authority validation inside the supervised worker."""
    handoff = companion.validate_descriptor(handoff)
    if deny_handoff is not None:
        deny_handoff = companion.validate_descriptor(deny_handoff)
        if deny_handoff["role"] == handoff["role"]:
            raise ProbeError("Cross-role check requires a different role.")
    token = _token(token_file, handoff["role"])
    responses = {}
    for operation, expected_status in (
        ("identity", 200),
        ("health", 200),
        ("readiness", 503),
    ):
        status, value = _request(handoff, operation, token)
        if status != expected_status:
            raise ProbeError("Unexpected companion authentication or readiness status.")
        responses[operation] = companion.validate_response(operation, value)
    identity, health, readiness = (
        responses[name] for name in ("identity", "health", "readiness")
    )
    if (
        identity["handoff"] != handoff
        or any(
            value["service_epoch"] != identity["service_epoch"]
            for value in (health, readiness)
        )
        or any(value["role"] != handoff["role"] for value in (health, readiness))
    ):
        raise ProbeError("Companion identity or epoch mismatch.")
    _require_auth_denial(*_request(handoff, "identity", None))
    if deny_handoff is not None:
        _require_auth_denial(*_request(deny_handoff, "identity", token))
    return {
        "kind": "dreamland.companion-probe",
        "project": handoff["project"],
        "role": handoff["role"],
        "service_epoch": identity["service_epoch"],
        "transport_verified": True,
        "domain_ready": False,
        "missing_auth_denied": True,
        "wrong_role_denied": deny_handoff is not None,
    }


def _healthcheck(root: Path) -> int:
    handoff, token = companion.load_service(root, role=companion.describe(root)["role"])
    # Docker health is process/metadata health, never domain readiness.
    local = {**handoff, "transport": {**handoff["transport"], "alias": "127.0.0.1"}}
    status, value = _request(local, "health", token)
    if status != 200:
        return 1
    health = companion.validate_response("health", value)
    return 0 if health["role"] == handoff["role"] else 1


def _worker(sender: Connection, operation: str, arguments: dict[str, Any]) -> None:
    """Read credentials only here; return no network body, token, or exception."""
    # This dedicated process is terminated by its parent. Do not print a child
    # traceback when an interactive interrupt reaches the entire process group.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        if operation == "probe":
            result: object = _probe(**arguments)
        elif operation == "healthcheck":
            result = _healthcheck(**arguments)
        else:
            raise ProbeError("Unknown companion check.")
        message = {"ok": True, "result": result}
    except Exception:
        message = {"ok": False}
    try:
        content = json.dumps(message, allow_nan=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(content) > _MAX_WORKER_RESULT:
            content = b'{"ok":false}'
        sender.send_bytes(content)
    except (OSError, ValueError):
        pass
    finally:
        sender.close()


def _supervise(operation: str, arguments: dict[str, Any], *, timeout: float) -> object:
    """Bound DNS, socket I/O and cleanup without an uncancellable worker thread."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_worker,
        args=(sender, operation, arguments),
        name="dreamland-companion-check",
    )
    started = time.monotonic()
    did_start = False
    try:
        process.start()
        did_start = True
        sender.close()
        process.join(timeout=max(0, timeout - (time.monotonic() - started)))
        if process.is_alive():
            raise ProbeError("Private companion check exceeded its deadline.")
        if process.exitcode != 0 or not receiver.poll():
            raise ProbeError("Private companion check failed.")
        # Wait for worker exit before reading. The only writer is now closed,
        # so a truncated frame cannot leave the parent waiting for more bytes.
        message = _json(receiver.recv_bytes(_MAX_WORKER_RESULT))
        if (
            not isinstance(message, dict)
            or set(message) != {"ok", "result"}
            or message["ok"] is not True
        ):
            raise ProbeError("Private companion check failed.")
        return message["result"]
    except (OSError, EOFError, ValueError):
        raise ProbeError(
            "Private companion check failed or exceeded its deadline."
        ) from None
    finally:
        sender.close()
        receiver.close()
        if did_start:
            if process.is_alive():
                process.terminate()
                process.join(timeout=WORKER_STOP_GRACE_SECONDS)
            if process.is_alive():
                process.kill()
                process.join(timeout=WORKER_STOP_GRACE_SECONDS)
            if process.is_alive():
                raise ProbeError("Private companion worker did not stop.")
        process.close()


def probe(handoff: dict, token_file: Path, *, deny_handoff: dict | None = None) -> dict:
    """Check identity, readiness and authority within one total process deadline."""
    result = _supervise(
        "probe",
        {"handoff": handoff, "token_file": token_file, "deny_handoff": deny_handoff},
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    if not isinstance(result, dict):
        raise ProbeError("Invalid private companion result.")
    return result


def healthcheck(root: Path) -> int:
    """Bound the local healthcheck below Docker's six-second health timeout."""
    result = _supervise(
        "healthcheck", {"root": root}, timeout=HEALTHCHECK_TIMEOUT_SECONDS
    )
    if type(result) is not int or result not in {0, 1}:
        raise ProbeError("Invalid private companion health result.")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handoff", type=Path)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--deny-handoff", type=Path)
    parser.add_argument("--require-domain-ready", action="store_true")
    parser.add_argument("--healthcheck-root", type=Path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.healthcheck_root is not None:
            if (
                arguments.handoff
                or arguments.token_file
                or arguments.deny_handoff
                or arguments.require_domain_ready
            ):
                parser.error("healthcheck cannot be combined with probe options")
            return healthcheck(arguments.healthcheck_root)
        if arguments.handoff is None or arguments.token_file is None:
            parser.error("--handoff and --token-file are required")
        result = probe(
            companion.validate_descriptor(
                _json(_read_file(arguments.handoff, secret=False))
            ),
            arguments.token_file,
            deny_handoff=(
                companion.validate_descriptor(
                    _json(_read_file(arguments.deny_handoff, secret=False))
                )
                if arguments.deny_handoff is not None
                else None
            ),
        )
        print(json.dumps(result, sort_keys=True))
        if arguments.require_domain_ready:
            print(
                "Domain services are unavailable in this checkpoint.", file=sys.stderr
            )
            return 1
        return 0
    except (OSError, ValueError, RecursionError):
        print(
            "Companion probe failed; check the handoff, credential, and private connection.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("Companion probe cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
