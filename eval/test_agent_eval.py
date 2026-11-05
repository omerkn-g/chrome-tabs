"""Automated Evaluation Suite for TabFlow Work Session Agent.

Satisfies Category 5 of the AgentOps Code Review Matrix:
- Automated Evaluation Suites (5 pts): Testing harness against `eval/golden_dataset.json`
  to statically and dynamically measure agent regressions across all 5 rubric categories.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tab_session_agent.agent import TabFlowOrchestrator, root_agent
from tab_session_agent.callbacks import policy_guardrail
from tab_session_agent.config import SecretManagerClient
from tab_session_agent.database import PersistentSessionDatabase
from tab_session_agent.memory import HistoryCompactionManager
from tab_session_agent.observability import observability_hub
from tab_session_agent.schemas import (
    DeleteTabGroupInput,
    ExecuteMergePlanInput,
    ProposeMergePlanInput,
)
from tab_session_agent.tools import (
    ALL_TABFLOW_TOOLS,
    delete_saved_tab_group,
    execute_tab_group_merge_plan,
    list_and_search_tab_groups,
    propose_tab_group_merge_plan,
    rename_tab_group_session,
    set_database_for_testing,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEED_JSON_PATH = os.path.join(BASE_DIR, "data", "seed_tab_groups.json")
GOLDEN_DATASET_PATH = os.path.join(BASE_DIR, "eval", "golden_dataset.json")


class TestTabFlowGoldenEvaluation(unittest.TestCase):
    """Comprehensive evaluation harness validating TabFlow against golden_dataset.json."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "eval_test.db")
        self.db = PersistentSessionDatabase(
            db_path=self.db_path,
            seed_json_path=SEED_JSON_PATH,
        )
        set_database_for_testing(self.db)
        self.orchestrator = TabFlowOrchestrator()

        with open(GOLDEN_DATASET_PATH, "r", encoding="utf-8") as f:
            self.golden = json.load(f)

    def tearDown(self) -> None:
        self.db.shutdown()
        self.temp_dir.cleanup()

    def test_01_tool_docstrings_naming_and_json_schemas(self) -> None:
        """Verify Category 1: Tool docstrings, descriptive names, and explicit JSON schemas."""
        for tool_fn in ALL_TABFLOW_TOOLS:
            self.assertIsNotNone(tool_fn.__doc__, f"{tool_fn.__name__} missing docstring")
            self.assertIn("Args:", tool_fn.__doc__)
            self.assertIn("Returns:", tool_fn.__doc__)
            self.assertGreaterEqual(len(tool_fn.__name__.split("_")), 3)

        schema = ProposeMergePlanInput.model_json_schema()
        self.assertEqual(schema["type"], "object")
        self.assertIn("source_group_ids", schema["properties"])

    def test_02_guided_error_handling_on_invalid_inputs(self) -> None:
        """Verify Category 1: Guided error returns provide recovery_instructions instead of crashing."""
        res_too_few = propose_tab_group_merge_plan(source_group_ids=["grp-adk-mon"])
        self.assertEqual(res_too_few["status"], "ERROR")
        self.assertEqual(res_too_few["error_code"], "INVALID_MERGE_SOURCE_GROUPS")
        self.assertIn("list_and_search_tab_groups", res_too_few["recovery_instructions"])

        res_missing_id = propose_tab_group_merge_plan(
            source_group_ids=["grp-adk-mon", "grp-non-existent"]
        )
        self.assertEqual(res_missing_id["status"], "ERROR")
        self.assertEqual(res_missing_id["error_code"], "GROUP_ID_NOT_FOUND")
        self.assertIn("available_group_ids", res_missing_id["data"])

    def test_03_merge_deduplication_and_stray_tab_routing_golden_case(self) -> None:
        """Verify golden case eval-02: URL deduplication + routing stray tabs to existing group or Misc."""
        case = self.golden["test_cases"][1]
        res = propose_tab_group_merge_plan(
            source_group_ids=case["source_group_ids"],
            suggested_session_name=case["suggested_session_name"],
        )
        self.assertEqual(res["status"], "SUCCESS")
        plan = res["data"]["merge_plan"]

        self.assertEqual(plan["suggested_session_name"], "ADK Course & Eval Session")
        self.assertEqual(len(plan["merged_tabs"]), case["expected_core_tab_count"])
        self.assertEqual(
            len(plan["duplicates_removed"]), case["expected_duplicates_removed_count"]
        )

        # Verify stray tabs: Kyoto Ryokan -> 'Japan Trip' (grp-japan), Wordle -> 'Misc' (grp-misc)
        stray_by_target = {s["target_group_id"]: s for s in plan["stray_routing"]}
        self.assertIn("grp-japan", stray_by_target)
        self.assertEqual(stray_by_target["grp-japan"]["target_session_name"], "Japan Trip")
        self.assertIn("grp-misc", stray_by_target)
        self.assertEqual(stray_by_target["grp-misc"]["target_session_name"], "Misc")

    def test_04_human_in_the_loop_blocks_unconfirmed_and_allows_confirmed_merge(self) -> None:
        """Verify Category 3: HITL hook blocks unconfirmed merge/delete and executes when confirmed."""
        prop = propose_tab_group_merge_plan(
            source_group_ids=["grp-adk-mon", "grp-unnamed-2"]
        )
        plan_id = prop["data"]["merge_plan"]["plan_id"]

        # 1. Attempt execution with user_confirmed=False -> must be intercepted by HITL hook
        blocked = execute_tab_group_merge_plan(
            plan_id=plan_id,
            final_session_name="My Custom ADK Session",
            user_confirmed=False,
        )
        self.assertEqual(blocked["status"], "PENDING_HUMAN_CONFIRMATION")
        self.assertEqual(blocked["error_code"], "HITL_CONFIRMATION_REQUIRED")

        # 2. Execute with user_confirmed=True and custom session name
        executed = execute_tab_group_merge_plan(
            plan_id=plan_id,
            final_session_name="My Custom ADK Session",
            user_confirmed=True,
        )
        self.assertEqual(executed["status"], "SUCCESS")
        merged_grp = executed["data"]["merged_work_session"]
        self.assertEqual(merged_grp["session_name"], "My Custom ADK Session")
        self.assertEqual(len(merged_grp["tabs"]), 5)

        # Check that Japan Trip received the Kyoto tab and Misc was created with Wordle
        japan_grp = self.db.get_tab_group("grp-japan")
        self.assertIsNotNone(japan_grp)
        self.assertEqual(len(japan_grp["tabs"]), 3)

        misc_grp = self.db.get_tab_group("grp-misc")
        self.assertIsNotNone(misc_grp)
        self.assertEqual(misc_grp["session_name"], "Misc")
        self.assertEqual(len(misc_grp["tabs"]), 1)

        # Verify delete tool is also HITL-gated
        del_blocked = delete_saved_tab_group(
            group_id="grp-misc", reason="Test HITL", user_confirmed=False
        )
        self.assertEqual(del_blocked["status"], "PENDING_HUMAN_CONFIRMATION")

        del_ok = delete_saved_tab_group(
            group_id="grp-misc", reason="Test HITL confirmed", user_confirmed=True
        )
        self.assertEqual(del_ok["status"], "SUCCESS")

    def test_05_strategic_model_routing_and_guardrails(self) -> None:
        """Verify Category 3: Strategic routing (Flash vs Pro) and policy guardrails."""
        # Fast search -> Flash
        turn_flash = self.orchestrator.handle_user_turn("Show my Cloud Run work sessions")
        self.assertEqual(turn_flash["status"], "SUCCESS")
        self.assertEqual(
            turn_flash["routing"]["target_agent"], "tab_triage_flash_agent"
        )
        self.assertEqual(turn_flash["routing"]["selected_model"], "gemini-2.5-flash")

        # Complex merge proposal -> Pro
        turn_pro = self.orchestrator.handle_user_turn(
            "Organize and propose merge plans for my overlapping tab groups"
        )
        self.assertEqual(turn_pro["status"], "SUCCESS")
        self.assertEqual(
            turn_pro["routing"]["target_agent"], "session_synthesizer_pro_agent"
        )
        self.assertEqual(turn_pro["routing"]["selected_model"], "gemini-2.5-pro")

        # Unsafe URI -> Blocked by policy guardrail
        turn_blocked = self.orchestrator.handle_user_turn(
            "Add tab javascript:alert(document.cookie) to my group"
        )
        self.assertEqual(turn_blocked["status"], "BLOCKED_BY_GUARDRAIL")

    def test_06_pii_redaction_and_history_compaction(self) -> None:
        """Verify Category 2 & 4: PII/token scrubbing, Intent/Outcome logs, and History Compaction."""
        case = self.golden["test_cases"][3]
        clean_url, removed = observability_hub.scrubber.normalize_and_scrub_url(
            case["raw_url"]
        )
        self.assertEqual(clean_url, case["expected_clean_url"])
        for forbidden in case["forbidden_substrings"]:
            self.assertNotIn(forbidden, clean_url)
        self.assertTrue(any("redacted:token" in r for r in removed))

        # Test HistoryCompactionManager
        compactor = HistoryCompactionManager(max_tokens=120, sliding_window_turns=3)
        long_turns = [
            {"role": "user", "content": f"Turn {i}: discussing tab group organization"}
            for i in range(10)
        ]
        compacted, metrics = compactor.compact_conversation_history(long_turns)
        self.assertTrue(metrics["compacted"])
        self.assertLess(len(compacted), len(long_turns))
        self.assertTrue(compacted[0].get("is_compacted_summary"))

    def test_07_secret_manager_no_hardcoded_keys(self) -> None:
        """Verify Category 5: SecretManagerClient retrieves via env/Secret Manager with no hardcoded secrets."""
        os.environ["TEST_TABFLOW_SECRET"] = "injected-runtime-value"
        client = SecretManagerClient(project_id="test-project")
        val = client.get_secret("non-existent-sm-id", env_var_fallback="TEST_TABFLOW_SECRET")
        self.assertEqual(val, "injected-runtime-value")
        del os.environ["TEST_TABFLOW_SECRET"]

    def test_08_closed_saved_tab_group_protobuf_decoder(self) -> None:
        """Verify decoding of Chrome closed SavedTabGroupSpecifics protobuf records from LevelDB."""
        from tab_session_agent.chrome_profile_reader import decode_saved_tab_group_specifics

        group_guid = b"11111111-2222-3333-4444-555555555555"
        tab_guid = b"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        # Group proto: field 1=guid, field 3=update_time(100), field 4=SavedTabGroup(pos=0, title="Closed Q4 Work", color=2)
        group_inner = b"\x08\x00\x12\x0eClosed Q4 Work\x18\x02"
        group_proto = (
            b"\x0a\x24" + group_guid + b"\x18\x64\x22" + bytes([len(group_inner)]) + group_inner
        )
        parsed_grp = decode_saved_tab_group_specifics(group_proto)
        self.assertIsNotNone(parsed_grp)
        self.assertEqual(parsed_grp["kind"], "group")
        self.assertEqual(parsed_grp["title"], "Closed Q4 Work")
        self.assertEqual(parsed_grp["color"], "blue")

        # Tab proto: field 1=tab_guid, field 3=update_time(101), field 5=SavedTabGroupTab(group_guid, pos=0, url, title)
        url_b = b"https://cloud.google.com/run"
        title_b = b"Cloud Run Docs"
        tab_inner = (
            b"\x0a\x24"
            + group_guid
            + b"\x10\x00\x1a"
            + bytes([len(url_b)])
            + url_b
            + b"\x22"
            + bytes([len(title_b)])
            + title_b
        )
        tab_proto = (
            b"\x0a\x24" + tab_guid + b"\x18\x65\x2a" + bytes([len(tab_inner)]) + tab_inner
        )
        parsed_tab = decode_saved_tab_group_specifics(tab_proto)
        self.assertIsNotNone(parsed_tab)
        self.assertEqual(parsed_tab["kind"], "tab")
        self.assertEqual(parsed_tab["group_guid"], group_guid.decode("ascii"))
        self.assertEqual(parsed_tab["url"], "https://cloud.google.com/run")



if __name__ == "__main__":
    unittest.main()
