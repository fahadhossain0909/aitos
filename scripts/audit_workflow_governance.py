#!/usr/bin/env python3
"""Static, non-mutating GitHub Actions governance inventory.

Flags are review targets, not a YAML semantic analyzer or a safety certification.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
SECRET_RE = re.compile(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)")
USES_RE = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)", re.MULTILINE)
UNTRUSTED_RE = re.compile(
    r"\$\{\{\s*github\.event\.(?:head_commit\.message|issue\.title|"
    r"issue\.body|pull_request\.(?:title|body|head\.ref)|comment\.body)"
)
RISK_RE = re.compile(
    r"\b(?:rm\s+-rf|docker\s+compose\s+down\s+-v|"
    r"DROP\s+(?:DATABASE|TABLE)|TRUNCATE\s+TABLE|terraform\s+destroy|"
    r"git\s+push\s+origin\s+main)\b",
    re.IGNORECASE,
)
WRITE_RE = re.compile(
    r"^\s*(contents|actions|packages|deployments):\s*write\s*$", re.MULTILINE
)


def audit(path: Path) -> dict:
    source = path.read_text(encoding="utf-8")
    permission_match = re.search(
        r"^permissions:\s*\n((?:^[ \t]+[^\n]+\n)+)", source, re.MULTILINE
    )
    permissions = (
        [
            line.strip()
            for line in permission_match.group(1).splitlines()
            if line.strip()
        ]
        if permission_match
        else ["implicit/default — verify repository/org policy"]
    )
    uses = sorted(set(USES_RE.findall(source)))
    secrets = sorted(set(SECRET_RE.findall(source)))
    trigger_match = re.search(
        r"^on:\s*\n((?:^[ \t].*\n|^[ \t]*\n)+)", source, re.MULTILINE
    )
    triggers = (
        [
            line.strip()
            for line in trigger_match.group(1).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ][:20]
        if trigger_match
        else []
    )
    findings = []
    if "implicit/default — verify repository/org policy" in permissions:
        findings.append("permissions_not_explicit")
    if WRITE_RE.search(source):
        findings.append("write_permission_requires_justification")
    if UNTRUSTED_RE.search(source):
        findings.append("untrusted_event_expression_in_workflow_source")
    if RISK_RE.search(source):
        findings.append("destructive_or_high_impact_command_review")
    if secrets and not re.search(r"^\s*environment:\s*", source, re.MULTILINE):
        findings.append("secrets_without_detected_environment_protection")
    unpinned = [ref for ref in uses if not re.search(r"@[0-9a-f]{40}$", ref)]
    if unpinned:
        findings.append("action_refs_not_full_sha_pinned")
    if re.search(r"workflow_dispatch", source) and RISK_RE.search(source):
        findings.append("manual_trigger_for_high_impact_workflow")
    return {
        "workflow": path.name,
        "triggers": triggers,
        "permissions": permissions,
        "secrets": secrets,
        "actions": uses,
        "unpinned_actions": unpinned,
        "findings": findings,
    }


def render_markdown(reports: list[dict]) -> str:
    lines = [
        "# GitHub Actions Static Governance Audit",
        "",
        "Static flags require human adjudication; absence of a flag is not proof of safety.",
        "",
        f"Workflows scanned: **{len(reports)}**",
        "",
        "| Workflow | Findings | Permissions | Secrets |",
        "|---|---|---|---|",
    ]
    for item in reports:
        findings = ", ".join(item["findings"]) or "No static flags"
        lines.append(
            f"| {item['workflow']} | {findings} | "
            f"{'; '.join(item['permissions'])} | {', '.join(item['secrets']) or '—'} |"
        )
    lines.extend(
        [
            "",
            "## Required human review",
            "",
            "- Confirm event triggers and workflow-call/fork trust boundaries.",
            "- Verify least-privilege workflow and job permissions.",
            "- Review every secret use, protected environment and approval gate.",
            "- Review action pins, shell interpolation, artifact provenance and deployment targets.",
            "- Confirm destructive workflows are manually gated, scoped and backed up.",
            "- Check required-check consumers and dispatch references before archival.",
            "",
            "This report does not change, disable, merge, or delete any workflow.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()
    paths = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    reports = [audit(path) for path in paths]
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(reports, indent=2) + "\n", encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(reports), encoding="utf-8")
    print(f"Scanned {len(reports)} GitHub Actions workflow files.")
    for item in reports:
        if item["findings"]:
            print(f"{item['workflow']}: {', '.join(item['findings'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
