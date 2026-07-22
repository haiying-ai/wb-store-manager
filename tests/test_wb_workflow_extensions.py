import json
from pathlib import Path
from types import SimpleNamespace

from wb_manager.cli import main as cli_main
from wb_manager.live_price import set_prices
from wb_manager.paths import DEFAULT_BUSINESS_ROOT


class FakePriceClient:
    def __init__(self):
        self.calls = []

    def request(self, method, url, body=None):
        self.calls.append((method, url, body))
        if method == "POST":
            return SimpleNamespace(status=200, ok=True, data={"uploadID": 42})
        return SimpleNamespace(
            status=200,
            ok=True,
            data={"data": {"listGoods": [{"nmID": 123, "discount": 20, "sizes": [{"price": 486}]}]}},
        )


def test_default_business_root_uses_shared_platform_layout():
    assert DEFAULT_BUSINESS_ROOT.as_posix().endswith("电商运营/01平台资料/01WB")


def test_price_set_is_dry_run_without_confirmation(capsys):
    result = cli_main(
        ["pricing", "set", "--nm-id", "123", "--price", "486", "--discount", "20"]
    )
    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["status"] == "dry_run"
    assert output["live"] is False


def test_live_price_write_keeps_request_response_and_readback(tmp_path):
    client = FakePriceClient()
    report = set_prices(
        client,
        [{"nmID": 123, "price": 486, "discount": 20}],
        operation_name="test_price",
        output_dir=tmp_path,
        readback_delay=0,
    )
    saved = json.loads(Path(report["path"]).read_text(encoding="utf-8"))
    assert saved["request"]["data"][0]["nmID"] == 123
    assert saved["response"]["ok"] is True
    assert saved["readback"]["price"][0]["nmID"] == 123
    assert [call[0] for call in client.calls] == ["POST", "GET"]
