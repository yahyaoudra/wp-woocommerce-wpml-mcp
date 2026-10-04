from pathlib import Path
from app.config import Settings
from app.policy import Policy
from app.errors import PolicyError


def mk(**kwargs):
    base = dict(
        wp_url="https://example.com",
        wc_consumer_key="ck_12345678",
        wc_consumer_secret="cs_12345678",
        audit_log=Path("/tmp/test-audit.jsonl"),
    )
    base.update(kwargs)
    return Settings(**base)


def test_writes_off_by_default():
    p = Policy(mk())
    try:
        p.require_write()
        assert False
    except PolicyError:
        pass


def test_price_has_separate_gate():
    p = Policy(mk(allow_writes=True, allow_price_writes=False))
    try:
        p.require_price_write()
        assert False
    except PolicyError:
        pass


def test_content_patch_blocks_price():
    p = Policy(mk())
    try:
        p.validate_content_patch({"name": "A", "regular_price": "10"})
        assert False
    except PolicyError:
        pass
