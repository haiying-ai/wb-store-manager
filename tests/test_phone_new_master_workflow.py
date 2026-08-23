import importlib.util
import argparse
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image


SCRIPT = Path(__file__).parents[1] / ".agents/skills/wb-product-workflow/scripts/phone_new_master_workflow.py"
SPEC = importlib.util.spec_from_file_location("phone_new_master_workflow", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class PhoneNewMasterWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        cohort = {
            "taskId": "task-one",
            "skus": [{"role": "pilot", "vendorCode": "PILOT-1"}, {"role": "stress", "vendorCode": "OTHER"}],
        }
        workflow = {"task_id": "task-one", "flow": "media"}
        (self.root / "cohort.json").write_text(json.dumps(cohort), encoding="utf-8")
        (self.root / "workflow.json").write_text(json.dumps(workflow), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def pointer(self):
        return {
            "schemaVersion": 1,
            "taskId": "task-one",
            "mode": MODULE.MODE,
            "generationMode": MODULE.GENERATION_MODE,
            "status": "WAITING_FOR_USER_MASTER",
            "cohortPath": "cohort.json",
            "workflowStatePath": "workflow.json",
            "pilotVendorCode": "PILOT-1",
            "generationAuthorized": False,
            "liveUploadAuthorized": False,
        }

    def design_pointer(self):
        pointer = self.pointer()
        pointer.update(
            {
                "mode": MODULE.DESIGN_MODE,
                "generationMode": MODULE.DESIGN_GENERATION_MODE,
                "status": "WAITING_DESIGN_BRIEF",
                "designLockPath": None,
                "masterLockPath": None,
            }
        )
        return pointer

    def test_waiting_pointer_is_valid(self):
        self.assertEqual([], MODULE.validate_payload(self.root, self.pointer()))

    def test_random_pilot_is_rejected(self):
        pointer = self.pointer()
        pointer["pilotVendorCode"] = "OTHER"
        self.assertIn("pointer pilotVendorCode does not match cohort pilot", MODULE.validate_payload(self.root, pointer))

    def test_authorization_requires_locked_user_master(self):
        pointer = self.pointer()
        pointer["generationAuthorized"] = True
        pointer["status"] = "MASTER_LOCKED"
        self.assertIn("generationAuthorized requires masterLockPath", MODULE.validate_payload(self.root, pointer))

    def test_master_dimensions_can_be_checked(self):
        master = self.root / "master"
        master.mkdir()
        Image.new("RGB", (900, 1200)).save(master / "01.png")
        Image.new("RGB", (900, 1200)).save(master / "02.png")
        dimensions = set()
        for path in master.iterdir():
            with Image.open(path) as image:
                dimensions.add((image.width, image.height))
        self.assertEqual({(900, 1200)}, dimensions)

    def test_waiting_design_pointer_is_valid(self):
        self.assertEqual([], MODULE.validate_payload(self.root, self.design_pointer()))

    def test_design_synthesis_uses_direct_full_text_generation(self):
        self.assertEqual(MODULE.EXACT_GENERATION_MODE, MODULE.DESIGN_GENERATION_MODE)
        self.assertEqual("DIRECT_FULL_TEXT_IMAGEGEN", self.design_pointer()["generationMode"])

    def test_design_authorization_requires_approved_brief(self):
        pointer = self.design_pointer()
        pointer["generationAuthorized"] = True
        pointer["status"] = "DESIGN_AUTHORIZED"
        self.assertIn("generationAuthorized requires designLockPath", MODULE.validate_payload(self.root, pointer))

    def test_approved_design_brief_authorizes_generation(self):
        brief = self.root / "brief.md"
        contract = self.root / "contract.md"
        brief.write_text("brief", encoding="utf-8")
        contract.write_text("contract", encoding="utf-8")
        lock = {
            "sourceType": "DESIGN_BRIEF",
            "briefPath": "brief.md",
            "contractPath": "contract.md",
            "expectedPages": 8,
            "approvedBy": "user",
        }
        (self.root / "design-lock.json").write_text(json.dumps(lock), encoding="utf-8")
        pointer = self.design_pointer()
        pointer.update(
            {
                "status": "DESIGN_AUTHORIZED",
                "generationAuthorized": True,
                "designLockPath": "design-lock.json",
            }
        )
        self.assertEqual([], MODULE.validate_payload(self.root, pointer))

    def test_completed_exploration_waits_for_direction_selection(self):
        brief = self.root / "brief.md"
        contract = self.root / "contract.md"
        report = self.root / "report.md"
        candidates = self.root / "candidates"
        candidates.mkdir()
        brief.write_text("brief", encoding="utf-8")
        contract.write_text("contract", encoding="utf-8")
        report.write_text("report", encoding="utf-8")
        Image.new("RGB", (1024, 1536)).save(candidates / "B.png")
        Image.new("RGB", (1024, 1536)).save(candidates / "C.png")
        lock = {
            "sourceType": "DESIGN_BRIEF",
            "designStage": MODULE.DIRECTION_EXPLORATION_STAGE,
            "briefPath": "brief.md",
            "contractPath": "contract.md",
            "expectedPages": 2,
            "approvedBy": "user",
            "explorationReportPath": "report.md",
            "candidateDirectory": "candidates",
        }
        (self.root / "design-lock.json").write_text(json.dumps(lock), encoding="utf-8")
        pointer = self.design_pointer()
        pointer.update(
            {
                "status": "WAITING_DIRECTION_SELECTION",
                "generationAuthorized": False,
                "designLockPath": "design-lock.json",
            }
        )
        self.assertEqual([], MODULE.validate_payload(self.root, pointer))

    def test_complete_exploration_disables_generation(self):
        report = self.root / "report.md"
        report.write_text("report", encoding="utf-8")
        candidates = self.root / "candidates"
        candidates.mkdir()
        Image.new("RGB", (1024, 1536)).save(candidates / "B.png")
        Image.new("RGB", (1024, 1536)).save(candidates / "C.png")
        lock = {
            "sourceType": "DESIGN_BRIEF",
            "designStage": MODULE.DIRECTION_EXPLORATION_STAGE,
            "briefPath": "brief.md",
            "contractPath": "contract.md",
            "expectedPages": 2,
            "approvedBy": "user",
        }
        (self.root / "design-lock.json").write_text(json.dumps(lock), encoding="utf-8")
        pointer = self.design_pointer()
        pointer.update(
            {
                "status": "DESIGN_AUTHORIZED",
                "generationAuthorized": True,
                "designLockPath": "design-lock.json",
            }
        )
        (self.root / "pointer.json").write_text(json.dumps(pointer), encoding="utf-8")
        args = argparse.Namespace(
            workspace_root=str(self.root),
            pointer="pointer.json",
            candidate_dir="candidates",
            report_path="report.md",
        )
        self.assertEqual(0, MODULE.cmd_complete_exploration(args))
        updated = json.loads((self.root / "pointer.json").read_text(encoding="utf-8"))
        self.assertEqual("WAITING_DIRECTION_SELECTION", updated["status"])
        self.assertFalse(updated["generationAuthorized"])

    def test_select_direction_returns_to_full_pack_brief_gate(self):
        candidate = self.root / "candidate.png"
        Image.new("RGB", (1024, 1536)).save(candidate)
        report = self.root / "report.md"
        report.write_text("report", encoding="utf-8")
        candidates = self.root / "candidates"
        candidates.mkdir()
        lock = {
            "sourceType": "DESIGN_BRIEF",
            "designStage": MODULE.DIRECTION_EXPLORATION_STAGE,
            "briefPath": "brief.md",
            "contractPath": "contract.md",
            "expectedPages": 2,
            "approvedBy": "user",
            "explorationReportPath": "report.md",
            "candidateDirectory": "candidates",
        }
        (self.root / "design-lock.json").write_text(json.dumps(lock), encoding="utf-8")
        pointer = self.design_pointer()
        pointer.update(
            {
                "status": "WAITING_DIRECTION_SELECTION",
                "generationAuthorized": False,
                "designLockPath": "design-lock.json",
            }
        )
        (self.root / "pointer.json").write_text(json.dumps(pointer), encoding="utf-8")
        args = argparse.Namespace(
            workspace_root=str(self.root),
            pointer="pointer.json",
            direction_id="B",
            candidate_path="candidate.png",
            selected_by="user",
            selection_path="selection.json",
        )
        self.assertEqual(0, MODULE.cmd_select_direction(args))
        updated = json.loads((self.root / "pointer.json").read_text(encoding="utf-8"))
        self.assertEqual("WAITING_DESIGN_BRIEF", updated["status"])
        self.assertFalse(updated["generationAuthorized"])
        self.assertEqual("selection.json", updated["directionSelectionPath"])

    def test_register_design_defaults_to_full_pack_stage(self):
        parsed = MODULE.parser().parse_args(
            [
                "register-design",
                "--brief-path",
                "brief.md",
                "--contract-path",
                "contract.md",
                "--template-id",
                "product-commerce-visual",
            ]
        )
        self.assertEqual(MODULE.FULL_PACK_STAGE, parsed.design_stage)


if __name__ == "__main__":
    unittest.main()
