from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any, Mapping


def which(command: str) -> str | None:
    return shutil.which(command)


def run(
    argv: list[str],
    timeout: float = 3.0,
    input_text: str | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[int, str, str]:
    """执行外部命令，永远不经过 shell，返回 (rc, stdout, stderr)。"""
    if not argv:
        return 1, "", "empty command"
    if shutil.which(argv[0]) is None:
        return 127, "", f"{argv[0]}: not found"
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
            input=input_text,
            env=dict(env) if env is not None else None,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except OSError as exc:
        return 1, "", str(exc)


def run_text(argv: list[str], timeout: float = 3.0) -> str:
    rc, out, _err = run(argv, timeout=timeout)
    return out if rc == 0 else ""


def run_json(argv: list[str], timeout: float = 3.0) -> Any:
    text = run_text(argv, timeout=timeout).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
