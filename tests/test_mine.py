from scope_envelope.mine import added_lines, footprint_from_pr, ticket_text
from scope_envelope.normalize import extract_subject, normalize

PR = {
    "number": 50373,
    "title": "cloudwatch: add evaluation_window argument to aws_cloudwatch_metric_alarm",
    "body": "<!-- template comment -->\n- [x] I ran the tests\nPR body",
    "url": "https://github.com/hashicorp/terraform-provider-aws/pull/50373",
    "mergedAt": "2026-10-30T18:04:11Z",
    "additions": 176,
    "closingIssuesReferences": {"nodes": [
        {"number": 50001, "title": "Support EvaluationWindow on metric alarms", "body": "AWS added it.\n<!-- hidden -->"}
    ]},
}
FILES = [
    {"filename": "internal/service/cloudwatch/metric_alarm.go", "patch": "@@\n+\t\t\"evaluation_window\": {\n+\t\t},\n context"},
    {"filename": "website/docs/r/cloudwatch_metric_alarm.html.markdown", "patch": "+* `evaluation_window` - (Optional) Window for aws_cloudwatch_metric_alarm."},
    {"filename": ".changelog/50373.txt", "patch": "+```release-note:enhancement\n+resource/aws_cloudwatch_metric_alarm: Add `evaluation_window` argument\n+```"},
]


def test_terraform_subject():
    assert extract_subject("r/aws_neptune_cluster: Add network_type argument") == "neptune cluster"


def test_camel_case_subject_still_works():
    assert extract_subject("Implement Kafka consumer for OrderPlacedEvent") == "order placed"


def test_changelog_number_becomes_placeholder():
    assert normalize(".changelog/50373.txt", None) == ".changelog/{n}.txt"


def test_ticket_text_uses_closing_issue_and_strips_template():
    text = ticket_text(PR)
    assert text.startswith(PR["title"])
    assert "Support EvaluationWindow on metric alarms" in text
    assert "<!--" not in text and "PR body" not in text


def test_ticket_text_falls_back_to_pr_body():
    text = ticket_text({**PR, "closingIssuesReferences": {"nodes": []}})
    assert "PR body" in text and "[x]" not in text


def test_added_lines_skip_trivial_and_context():
    assert added_lines(FILES[0]["patch"]) == ['"evaluation_window": {']


def test_footprint_is_normalized_for_reuse():
    fp = footprint_from_pr("hashicorp/terraform-provider-aws", PR, FILES)
    assert fp["ticket"] == "hashicorp/terraform-provider-aws#50373"
    assert fp["subject"] == "cloudwatch metric alarm"
    assert fp["verb"] == "add" and fp["success"] is True and fp["calls"] is None
    assert fp["files_changed"] == [
        ".changelog/{n}.txt",
        "internal/service/cloudwatch/metric_alarm.go",
        "website/docs/r/{snake}.html.markdown",
    ]
    assert "resource/aws_{snake}: Add `evaluation_window` argument" in fp["added_lines"]
    assert fp["merged_at"].year == 2026
    assert fp["issue"] == 50001
    assert fp["issue_url"] == "https://github.com/hashicorp/terraform-provider-aws/issues/50001"
