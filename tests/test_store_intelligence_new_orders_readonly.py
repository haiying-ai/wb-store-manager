import pytest

from wb_store_intelligence.client import ReadOnlyWbClient
from wb_store_intelligence.config import StoreConfig
from wb_store_intelligence.policy import ReadOnlyPolicy, SafetyViolation


def test_new_fbs_orders_is_exact_read():
    rule = ReadOnlyPolicy().authorize(
        "GET", "https://marketplace-api.wildberries.ru/api/v3/orders/new"
    )
    assert rule.name == "marketplace-new-orders"


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/v3/orders/new"),
    ("PATCH", "/api/v3/orders/5706297681/cancel"),
    ("PUT", "/api/v3/orders/new"),
    ("GET", "/api/v3/orders/new/extra"),
])
def test_new_orders_change_does_not_allow_mutation_or_other_paths(method, path):
    def no_network(*args, **kwargs):
        raise AssertionError("must reject before network")
    client = ReadOnlyWbClient(
        StoreConfig("benchmark-b", "benchmark_readonly", "WB_INTEL_BENCHMARK_B_TOKEN"),
        opener=no_network,
    )
    with pytest.raises(SafetyViolation):
        client.request(method, "https://marketplace-api.wildberries.ru" + path)
