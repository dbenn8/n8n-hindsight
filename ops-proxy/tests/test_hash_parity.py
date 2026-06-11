"""Cross-repo hash-parity guard for _nodes_content_sha256.

ops-proxy/workflow_validator.py:_nodes_content_sha256 (n8n-hindsight) and
hooks/lib/validator_metadata.py:_nodes_content_sha256 (n8n-knowledge) MUST stay
byte-identical. This test pins the expected hash of a shared edge-case fixture
(unicode + emoji, NULLs, integers, floats, blobs, mixed types). The identical
literal is pinned in n8n-knowledge tests/test-hash-parity.sh. If either
implementation drifts, that repo's suite fails here.
"""

from pathlib import Path

from workflow_validator import _nodes_content_sha256

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "nodes-parity-fixture.db"

# Pinned parity hash — identical literal pinned in the n8n-knowledge suite.
EXPECTED_PARITY_HASH = (
    "a9a698eb493b3f3b6dc1c1818ae14540c303f9ce769dca7c57a099dffcec5fb7"
)


def test_fixture_exists():
    assert FIXTURE.is_file(), f"parity fixture missing: {FIXTURE}"


def test_nodes_content_sha256_matches_pinned_parity_hash():
    actual = _nodes_content_sha256(FIXTURE)
    assert actual == EXPECTED_PARITY_HASH, (
        "_nodes_content_sha256 drifted from the cross-repo pinned hash. "
        "If this implementation changed intentionally, the matching change and "
        "pinned hash must be updated in BOTH repos "
        "(n8n-hindsight ops-proxy/workflow_validator.py and "
        "n8n-knowledge hooks/lib/validator_metadata.py)."
    )
