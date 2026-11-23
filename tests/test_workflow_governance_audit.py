from scripts.audit_workflow_governance import audit


def test_audit_flags_write_scope_untrusted_expression_and_destructive_command(tmp_path):
    workflow = tmp_path / "unsafe.yml"
    workflow.write_text(
        r"""name: unsafe
on:
  workflow_dispatch:
permissions:
  contents: write
jobs:
  deploy:
    steps:
      - uses: actions/checkout@v4
      - run: echo "\${{ github.event.issue.title }}"
      - run: docker compose down -v
""",
        encoding="utf-8",
    )

    report = audit(workflow)

    assert "write_permission_requires_justification" in report["findings"]
    assert "untrusted_event_expression_in_workflow_source" in report["findings"]
    assert "destructive_or_high_impact_command_review" in report["findings"]
    assert report["unpinned_actions"] == ["actions/checkout@v4"]


def test_audit_records_secret_and_missing_explicit_permissions(tmp_path):
    workflow = tmp_path / "implicit.yml"
    workflow.write_text(
        r"""name: implicit
on:
  push:
jobs:
  test:
    steps:
      - run: echo "$TOKEN"
        env:
          TOKEN: \${{ secrets.PROJECT_TOKEN }}
""",
        encoding="utf-8",
    )

    report = audit(workflow)

    assert report["secrets"] == ["PROJECT_TOKEN"]
    assert "permissions_not_explicit" in report["findings"]
    assert "secrets_without_detected_environment_protection" in report["findings"]



def test_audit_detects_job_level_permission_overrides_and_inheritance(tmp_path):
    workflow = tmp_path / "job-permissions.yml"
    workflow.write_text(
        """name: scoped
on:
  pull_request:
permissions:
  contents: read
jobs:
  inspect:
    permissions:
      contents: read
    steps:
      - run: echo safe
  publish:
    steps:
      - run: echo publish
""",
        encoding="utf-8",
    )

    report = audit(workflow)

    assert report["job_permissions"]["inspect"] == ["contents: read"]
    assert report["jobs_without_explicit_permissions"] == ["publish"]
    assert "jobs_inherit_workflow_or_repository_permissions" in report["findings"]
