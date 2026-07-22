import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / ".agents" / "skills" / "wb-product-workflow" / "scripts" / "workflow_state.py"


def load_module():
    spec = importlib.util.spec_from_file_location("workflow_state", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_all_flows_start_valid():
    module = load_module()
    for flow, names in module.FLOWS.items():
        state = {"flow": flow, "stages": {name: {"status": "pending"} for name in names}}
        assert module.validate_state(state) == []


def test_later_stage_cannot_pass_early():
    module = load_module()
    names = module.FLOWS["daily"]
    state = {"flow": "daily", "stages": {name: {"status": "pending"} for name in names}}
    state["stages"]["report"]["status"] = "passed"
    assert any("report" in error for error in module.validate_state(state))


def test_approval_requires_actor():
    module = load_module()
    names = module.FLOWS["media"]
    state = {"flow": "media", "stages": {name: {"status": "pending"} for name in names}}
    for name in names[:5]:
        state["stages"][name]["status"] = "passed"
    assert any("approved_by" in error for error in module.validate_state(state))
    state["stages"]["approve"]["approved_by"] = "user"
    assert module.validate_state(state) == []
