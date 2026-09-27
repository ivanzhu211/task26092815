import os
import pytest


def pytest_collection_modifyitems(config, items):
    if os.environ.get("IP_PERF") == "1":
        return
    skip = pytest.mark.skip(reason="规模测试，设置 IP_PERF=1 后运行")
    for item in items:
        if "perf" in item.keywords:
            item.add_marker(skip)
