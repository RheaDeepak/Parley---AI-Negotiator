import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live_llm: calls the real Anthropic API; skipped by default, run with --run-live-llm"
    )


def pytest_addoption(parser):
    parser.addoption(
        "--run-live-llm", action="store_true", default=False,
        help="Run tests marked live_llm (makes real Anthropic API calls; needs ANTHROPIC_API_KEY or an ant auth profile)",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live-llm"):
        return
    skip_live = pytest.mark.skip(reason="need --run-live-llm to run (calls the real Anthropic API)")
    for item in items:
        if "live_llm" in item.keywords:
            item.add_marker(skip_live)
