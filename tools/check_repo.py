"""Repo sanity checks that catch mistakes which are otherwise silent.

Two failure modes have actually bitten this project:

1. **LF line endings in a .bat file.** ``cmd.exe`` mis-parses them and silently
   skips the rest of the script, so a launcher "just flashes and closes" with no
   error at all. Easy to reintroduce when a batch file is rewritten by a tool
   that emits Unix line endings.
2. **Target interface drift.** ``wechat.py`` and ``qq.py`` must expose the same
   names for ``targets.py`` to dispatch on; a rename in one would otherwise only
   show up when a user selects that target.

Also compiles every Python file, since a syntax error in a rarely used module is
easy to miss.

Run: python tools/check_repo.py
"""

from __future__ import annotations

import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: Files that cmd.exe / wscript require to use CRLF.
CRLF_SUFFIXES = {".bat", ".cmd", ".vbs"}

#: The interface targets.py dispatches on.
TARGET_INTERFACE = (
    "KEY",
    "LABEL",
    "PROCESS_NAMES",
    "OFFSETS_FILE",
    "DEFAULTS",
    "load_offsets",
    "save_offsets",
    "find_main_windows",
    "all_candidate_windows",
    "activate_main_window",
    "force_canonical_size",
    "voice_control_points",
    "preflight",
    "enter_voice_mode",
    "finish_voice_mode",
    "leave_recording_mode",
)


def iter_project_files(suffixes: set[str] | None = None):
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if any(part in {"_reference", "node_modules", "__pycache__", ".git"} for part in relative.parts):
            continue
        if suffixes is not None and path.suffix.casefold() not in suffixes:
            continue
        yield path


def check_line_endings() -> list[str]:
    problems = []
    for path in iter_project_files(CRLF_SUFFIXES):
        raw = path.read_bytes()
        crlf = raw.count(b"\r\n")
        bare = raw.count(b"\n") - crlf
        if bare:
            problems.append(
                f"{path.relative_to(ROOT)} has {bare} bare LF line ending(s); "
                "cmd.exe will mis-parse it. Convert the file to CRLF."
            )
        if raw.startswith(b"\xef\xbb\xbf"):
            problems.append(
                f"{path.relative_to(ROOT)} starts with a UTF-8 BOM, which cmd.exe "
                "treats as part of the first command."
            )
    return problems


def check_compiles() -> list[str]:
    problems = []
    for path in iter_project_files({".py", ".pyw"}):
        try:
            py_compile.compile(str(path), doraise=True, quiet=2)
        except py_compile.PyCompileError as error:
            problems.append(f"{path.relative_to(ROOT)} does not compile: {error.msg}")
    return problems


def check_targets() -> list[str]:
    problems = []
    try:
        import targets
    except Exception as error:  # pragma: no cover - import-time failure
        return [f"could not import targets: {error}"]

    for key, module in targets.TARGETS.items():
        missing = [name for name in TARGET_INTERFACE if not hasattr(module, name)]
        if missing:
            problems.append(f"target {key!r} ({module.__name__}) is missing: {', '.join(missing)}")

    shared = {getattr(module, "OFFSETS_FILE", None) for module in targets.TARGETS.values()}
    if len(shared) != len(targets.TARGETS):
        problems.append("targets share an offsets file; each must keep its own coordinates")
    return problems


def main() -> int:
    checks = (
        ("batch file line endings", check_line_endings),
        ("python compiles", check_compiles),
        ("target interface", check_targets),
    )

    failures = 0
    for title, check in checks:
        try:
            problems = check()
        except Exception as error:  # noqa: BLE001 - report and continue
            problems = [f"check crashed: {error}"]
        if problems:
            failures += len(problems)
            print(f"[FAIL] {title}")
            for problem in problems:
                print(f"       {problem}")
        else:
            print(f"[ OK ] {title}")

    print()
    if failures:
        print(f"{failures} problem(s) found.")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
