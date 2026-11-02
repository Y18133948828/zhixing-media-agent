"""Inspect tracked release files without printing secret values. Run after git add."""

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = {".env.example", ".env.volcengine.example"}
PRIVATE_PARTS = {".runtime", ".tools", ".venv", "node_modules", "__pycache__", "storage", "logs", "示例成果"}
PRIVATE_DOCS = {"共享PostgreSQL迁移说明.md", "原版文档对照与升级方案.md", "难点亮点实现核查.md", "验收记录.md"}
PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{32,}|github_pat_[A-Za-z0-9_]{50,})\b"),
    "personal-path": re.compile(r"[A-Za-z]:[\\/](?:AI|Users)[\\/]", re.IGNORECASE),
}


def local_secrets():
    path = ROOT / "backend/.env"
    if not path.exists():
        return []
    values = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, separator, value = line.partition("=")
        value = value.strip().strip("\"'")
        if not separator or key.lstrip().startswith("#"):
            continue
        if key == "DATABASE_URL":
            value = unquote(urlsplit(value).password or "")
        elif not re.search(r"(?:API_KEY|PASSWORD|SECRET|TOKEN)$", key):
            continue
        if len(value) >= 12 and not any(word in value.lower() for word in ["replace-with", "change-me", "your-", "example"]):
            values.append(value)
    return values


def main():
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    paths = [name for name in paths if name]
    if not paths:
        raise ValueError("没有已暂存/已跟踪文件；请先审阅 git status 并暂存要发布的源码。")
    findings = []
    secrets = local_secrets()
    total = 0
    for name in paths:
        path = Path(name)
        if (set(path.parts) & PRIVATE_PARTS or path.name in PRIVATE_DOCS
                or (path.name.startswith(".env") and path.name not in EXAMPLES)
                or path.suffix.lower() in {".log", ".jsonl", ".dump", ".backup", ".pem", ".key"}):
            findings.append((name, 0, "private-file"))
            continue
        data = (ROOT / path).read_bytes()
        total += len(data)
        if len(data) > 10 * 1024 * 1024:
            findings.append((name, 0, "oversized-file"))
        if b"\0" in data:
            continue
        text = data.decode("utf-8", errors="replace")
        # Vendor bundles keep their original upstream contents, but are still checked for local secrets.
        for value in secrets:
            if value in text:
                findings.append((name, text[:text.index(value)].count("\n") + 1, "local-secret"))
        if "static/docs/" not in name:
            for kind, pattern in PATTERNS.items():
                for match in pattern.finditer(text):
                    # The scanner's own regex declarations are patterns, not live credentials.
                    if name == "scripts/release_check.py":
                        continue
                    findings.append((name, text[:match.start()].count("\n") + 1, kind))
    for name, line, kind in findings:
        print(f"BLOCK {name}:{line} [{kind}]")
    print(f"Checked {len(paths)} files, {total / 1024 / 1024:.2f} MiB; findings={len(findings)}. Secret values are never printed.")
    return bool(findings)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError):
        print("Release check could not complete; verify Git initialization and tracked files.", file=sys.stderr)
        raise SystemExit(1) from None
