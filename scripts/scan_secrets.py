"""Fail when tracked files or an image contain recognizable credentials."""

import re
import subprocess
import sys
from pathlib import Path

PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
)
SKIP_PARTS = {
    ".git",
    "node_modules",
    ".next",
    ".venv",
    ".tools",
    "playwright-report",
    "test-results",
}


def _scan_text(label: str, text: str, findings: list[str]) -> None:
    for pattern in PATTERNS:
        if pattern.search(text):
            findings.append(f"{label}: matched {pattern.pattern}")


def scan_tree(root: Path) -> list[str]:
    """Scan the git index when available, otherwise the working tree."""
    findings: list[str] = []
    listed = subprocess.run(
        ["git", "ls-files"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if listed.returncode == 0:
        paths = [root / line for line in listed.stdout.splitlines() if line]
    else:
        paths = [
            path
            for path in root.rglob("*")
            if path.is_file() and SKIP_PARTS.isdisjoint(path.parts)
        ]
    for path in paths:
        if not path.is_file() or SKIP_PARTS.intersection(path.parts):
            continue
        if path.suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".woff", ".woff2"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        _scan_text(str(path.relative_to(root)), text, findings)
    return findings


def scan_image(image: str) -> list[str]:
    """Reject an image whose config or history contains a credential pattern."""
    findings: list[str] = []
    for command in (
        ["docker", "image", "inspect", image],
        ["docker", "history", "--no-trunc", image],
    ):
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            findings.append(f"{image}: {' '.join(command)} failed")
            continue
        _scan_text(image, result.stdout, findings)
    present = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh", image, "-c", "test -f /app/.env && echo present || true"],
        check=False,
        capture_output=True,
        text=True,
    )
    if "present" in present.stdout:
        findings.append(f"{image}: contains /app/.env")
    return findings


def main(argv: list[str]) -> int:
    """Scan the repository and any image names passed after --image."""
    root = Path(__file__).resolve().parents[1]
    findings = scan_tree(root)
    images = [item for item in argv if item != "--image"]
    if "--image" in argv:
        for image in images:
            findings.extend(scan_image(image))
    if findings:
        sys.stderr.write("Secret scan failed:\n")
        for finding in findings:
            sys.stderr.write(f"- {finding}\n")
        return 1
    sys.stdout.write("Secret scan passed.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
