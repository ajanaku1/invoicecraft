import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCAL_ONLY_REFERENCES = (
    "brand/",
    "proposals/",
    "DEMO_SCRIPT.md",
)


def test_public_verifier_does_not_require_local_only_artifacts():
    verifier = (ROOT / "verify.sh").read_text(encoding="utf-8")

    for local_reference in LOCAL_ONLY_REFERENCES:
        assert local_reference not in verifier


def test_acceptance_demo_uses_the_published_readme():
    record = json.loads(
        (ROOT / "evidence" / "acceptance-record.json").read_text(encoding="utf-8")
    )

    assert record["demo"]["script"] == "README.md"
    assert (ROOT / record["demo"]["script"]).is_file()


def test_full_verifier_skips_the_ignored_proposal_test():
    verifier = (ROOT / "verify.sh").read_text(encoding="utf-8")

    assert "--ignore=tests/test_verify_proposals.py" in verifier


def test_public_verifier_scans_published_reports():
    verifier = (ROOT / "verify.sh").read_text(encoding="utf-8")

    assert "evidence docs reports README.md" in verifier
