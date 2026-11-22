"""The relay self-test (gpu/relay_selftest.py) that gpu/session.py also runs in its preflight."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gpu"))
import relay_selftest  # noqa: E402


def test_relay_rebuilds_streamed_replies():
    assert relay_selftest.run().startswith("relay test OK")
