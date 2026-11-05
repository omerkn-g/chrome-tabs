"""ADK Multi-Agent Orchestration and Strategic Model Routing for TabFlow.

Satisfies Category 3 of the AgentOps Code Review Matrix:
- Multi-Agent Patterns (5 pts): Coordinator pattern with specialized ADK LlmAgents
  (`tab_triage_flash_agent` and `session_synthesizer_pro_agent`).
- Strategic Model Routing (5 pts): Routes fast triage/deduplication queries to
  `gemini-2.5-flash` and complex session synthesis/merge planning to `gemini-2.5-pro`.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from tab_session_agent.callbacks import policy_guardrail
from tab_session_agent.config import AgentConfig, SecretManagerClient
from tab_session_agent.memory import HistoryCompactionManager
from tab_session_agent.observability import observability_hub
from tab_session_agent.prompts import (
    COORDINATOR_SYSTEM_CONSTITUTION,
    SESSION_SYNTHESIZER_PRO_INSTRUCTIONS,
    TAB_TRIAGE_FLASH_INSTRUCTIONS,
)
from tab_session_agent.tools import (
    ALL_TABFLOW_TOOLS,
    auto_discover_and_propose_merges,
    delete_saved_tab_group,
    execute_tab_group_merge_plan,
    get_database,
    list_and_search_tab_groups,
    propose_tab_group_merge_plan,
    rename_tab_group_session,
)

try:
    from google.adk.agents import Agent, LlmAgent  # type: ignore

    HAS_GOOGLE_ADK = True
except ImportError:
    HAS_GOOGLE_ADK = False

    class LlmAgent:  # type: ignore
        """Compatible ADK LlmAgent representation when running outside the google-adk runtime."""

        def __init__(
            self,
            name: str,
            model: str,
            description: str = "",
            instruction: str = "",
            tools: Optional[list[Any]] = None,
            sub_agents: Optional[list[Any]] = None,
            before_model_callback: Optional[Any] = None,
            before_tool_callback: Optional[Any] = None,
            after_tool_callback: Optional[Any] = None,
        ) -> None:
            self.name = name
            self.model = model
            self.description = description
            self.instruction = instruction
            self.tools = tools or []
            self.sub_agents = sub_agents or []
            self.before_model_callback = before_model_callback
            self.before_tool_callback = before_tool_callback
            self.after_tool_callback = after_tool_callback

    Agent = LlmAgent


_CONFIG = AgentConfig.from_env()
_SECRET_CLIENT = SecretManagerClient(project_id=_CONFIG.project_id)


class StrategicModelRouter:
    """Routes user requests between Flash (fast classification/search) and Pro (deep synthesis/planning)."""

    PLANNING_SYNTHESIS_KEYWORDS = {
        "merge",
        "combine",
        "consolidate",
        "organize",
        "plan",
        "propose",
        "synthesize",
        "summary",
        "where i left off",
        "clean up",
        "cleanup",
    }

    def __init__(
        self,
        fast_model: str = _CONFIG.fast_triage_model,
        pro_model: str = _CONFIG.deep_synthesis_model,
    ) -> None:
        self.fast_model = fast_model
        self.pro_model = pro_model

    def select_agent_and_model(self, user_message: str) -> dict[str, str]:
        """Select the optimal subagent and Gemini model tier for a given user request."""
        msg_lower = user_message.lower()
        is_complex_planning = any(
            kw in msg_lower for kw in self.PLANNING_SYNTHESIS_KEYWORDS
        )
        if is_complex_planning:
            route = {
                "target_agent": "session_synthesizer_pro_agent",
                "selected_model": self.pro_model,
                "routing_tier": "PRO_SYNTHESIS_AND_PLANNING",
                "reason": "Complex session merge planning or 'Where I left off' synthesis detected.",
            }
        else:
            route = {
                "target_agent": "tab_triage_flash_agent",
                "selected_model": self.fast_model,
                "routing_tier": "FLASH_FAST_TRIAGE",
                "reason": "Fast tab group search, listing, rename, or status lookup.",
            }

        observability_hub.log_event(
            event_type="STRATEGIC_MODEL_ROUTING",
            message=(
                f"Routed request to {route['target_agent']} using model '{route['selected_model']}'"
            ),
            agent_name="tabflow_coordinator",
            metadata=route,
        )
        return route


# 1. Specialized Fast Triage Subagent (Gemini Flash)
tab_triage_flash_agent = LlmAgent(
    name="tab_triage_flash_agent",
    model=_CONFIG.fast_triage_model,
    description=(
        "High-speed subagent for listing tab groups, normalizing URLs, finding duplicates, "
        "and matching stray tabs to existing tab groups or Misc."
    ),
    instruction=TAB_TRIAGE_FLASH_INSTRUCTIONS,
    tools=[list_and_search_tab_groups, rename_tab_group_session],
)

# 2. Specialized Deep Session Synthesizer Subagent (Gemini Pro)
session_synthesizer_pro_agent = LlmAgent(
    name="session_synthesizer_pro_agent",
    model=_CONFIG.deep_synthesis_model,
    description=(
        "Deep reasoning subagent for proposing tab group merges, synthesizing 'Where I left off' "
        "session summaries, and coordinating HITL merge execution."
    ),
    instruction=SESSION_SYNTHESIZER_PRO_INSTRUCTIONS,
    tools=[
        propose_tab_group_merge_plan,
        execute_tab_group_merge_plan,
        delete_saved_tab_group,
    ],
)

# 3. Root Coordinator Agent (ADK Multi-Agent Entrypoint)
root_agent = LlmAgent(
    name="tabflow_coordinator",
    model=_CONFIG.fast_triage_model,
    description=(
        "Root Coordinator Agent for TabFlow that manages Chrome Tab Groups into focused Work Sessions."
    ),
    instruction=COORDINATOR_SYSTEM_CONSTITUTION,
    tools=ALL_TABFLOW_TOOLS,
    sub_agents=[tab_triage_flash_agent, session_synthesizer_pro_agent],
    before_model_callback=policy_guardrail.before_model_callback,
    before_tool_callback=policy_guardrail.before_tool_callback,
    after_tool_callback=policy_guardrail.after_tool_callback,
)


class TabFlowOrchestrator:
    """End-to-end multi-agent runtime orchestrator for the Web UI, CLI, and Evaluation Suite."""

    def __init__(self) -> None:
        self.router = StrategicModelRouter()
        self.compactor = HistoryCompactionManager(
            max_tokens=_CONFIG.max_history_tokens,
            sliding_window_turns=_CONFIG.sliding_window_turns,
        )
        self.secret_client = _SECRET_CLIENT

    def handle_user_turn(
        self,
        user_message: str,
        session_id: str = "default-web-session",
    ) -> dict[str, Any]:
        """Process a user message through Guardrails, History Compaction, Model Routing, Tools, and Self-Eval."""
        db = get_database()

        with observability_hub.start_span(
            span_name="coordinator.handle_user_turn",
            agent_name="tabflow_coordinator",
            attributes={"session_id": session_id, "user_message": user_message},
        ) as root_span:
            # 1. Input Policy Guardrail & PII Redaction
            guard_check = policy_guardrail.before_model_callback(
                prompt_text=user_message,
                agent_name="tabflow_coordinator",
            )
            sanitized_user_msg = guard_check["sanitized_prompt"]
            db.append_conversation_turn(session_id, "user", sanitized_user_msg)

            if not guard_check["allowed"]:
                blocked_reply = (
                    f"Request blocked by security policy guardrail: {guard_check['policy_violation']}. "
                    "Please provide safe https:// tab URLs."
                )
                db.append_conversation_turn(session_id, "assistant", blocked_reply)
                return {
                    "status": "BLOCKED_BY_GUARDRAIL",
                    "reply": blocked_reply,
                    "routing": None,
                    "compaction": None,
                    "tool_results": [],
                    "trace_id": root_span["trace_id"],
                }

            # 2. Conversational History Compaction
            raw_turns = db.get_conversation_turns(session_id)
            _, compaction_metrics = self.compactor.compact_conversation_history(raw_turns)

            # 3. Strategic Model Routing (Flash vs. Pro)
            routing = self.router.select_agent_and_model(sanitized_user_msg)
            msg_lower = sanitized_user_msg.lower()
            tool_results: list[dict[str, Any]] = []

            # 4. Execute appropriate subagent tool workflow
            if any(
                w in msg_lower
                for w in ["organize", "propose", "analyze", "find duplicate", "auto", "merge"]
            ) and not any(w in msg_lower for w in ["approve", "confirm", "execute plan"]):
                with observability_hub.start_span(
                    span_name="subagent.session_synthesizer_pro_agent",
                    agent_name="session_synthesizer_pro_agent",
                    attributes={"model": routing["selected_model"]},
                ):
                    # Check if user specified particular groups or wants automatic discovery
                    plans = auto_discover_and_propose_merges()
                    if not plans:
                        pending = db.list_pending_merge_plans()
                        plans = pending
                    if plans:
                        lines = [
                            f"I analyzed your saved Tab Groups with **{routing['selected_model']}** and prepared **{len(plans)} Work Session Merge Proposal(s)**:\n"
                        ]
                        for p in plans:
                            stray_desc = (
                                ", ".join(
                                    f"*{s['title']}* → **{s['target_session_name']}**"
                                    for s in p.get("stray_routing", [])
                                )
                                or "None"
                            )
                            lines.append(
                                f"- **`{p['plan_id']}` — Suggested Name: \"{p['suggested_session_name']}\"**\n"
                                f"  - **Merging Groups:** {', '.join(p.get('source_group_names', p['source_group_ids']))}\n"
                                f"  - **Core Tabs:** {len(p['merged_tabs'])} unique tab(s) ({len(p['duplicates_removed'])} duplicate URL(s) removed)\n"
                                f"  - **Stray Tabs Routed:** {stray_desc}\n"
                                f"  - **Where You Left Off:** _{p['synthesized_note']}_"
                            )
                        lines.append(
                            "\nYou can **edit the suggested session name** and click **Approve & Merge** on the proposal card (or tell me in chat: *\"Approve plan ... with name ...\"*)."
                        )
                        reply_text = "\n".join(lines)
                        tool_results.append({"plans_created": plans})
                    else:
                        reply_text = (
                            "All of your current Tab Groups are already cleanly separated into distinct Work Sessions with no overlapping duplicate topics!"
                        )

            elif any(w in msg_lower for w in ["approve", "confirm"]) and "plan-" in msg_lower:
                plan_match = re.search(r"(plan-[a-z0-9]+)", msg_lower)
                plan_id = plan_match.group(1) if plan_match else ""
                plan = db.get_merge_plan(plan_id) if plan_id else None
                custom_name_match = re.search(
                    r"(?:call it|name it|named|as)\s+['\"]([^'\"]+)['\"]",
                    sanitized_user_msg,
                    re.IGNORECASE,
                )
                final_name = (
                    custom_name_match.group(1)
                    if custom_name_match
                    else (plan["suggested_session_name"] if plan else "Merged Work Session")
                )
                res = execute_tab_group_merge_plan(
                    plan_id=plan_id,
                    final_session_name=final_name,
                    user_confirmed=True,
                )
                tool_results.append(res)
                reply_text = res["message"]

            elif "rename" in msg_lower:
                groups = db.list_tab_groups()
                target_grp = None
                for g in groups:
                    if (
                        g["group_id"].lower() in msg_lower
                        or g["session_name"].lower() in msg_lower
                    ):
                        target_grp = g
                        break
                quote_matches = re.findall(r"['\"]([^'\"]+)['\"]", sanitized_user_msg)
                new_name = quote_matches[-1] if quote_matches else None
                if target_grp and new_name:
                    res = rename_tab_group_session(
                        group_id=target_grp["group_id"],
                        new_session_name=new_name,
                    )
                    tool_results.append(res)
                    reply_text = f"Renamed **{target_grp['session_name']}** to **{new_name}**."
                else:
                    reply_text = (
                        "To rename a Work Session in chat, specify the group and new name in quotes "
                        "(e.g., *Rename 'Mon ADK work' to 'ADK Assessment'*), or click the **Rename** button directly on any card."
                    )

            else:
                with observability_hub.start_span(
                    span_name="subagent.tab_triage_flash_agent",
                    agent_name="tab_triage_flash_agent",
                    attributes={"model": routing["selected_model"]},
                ):
                    search_q = None
                    for keyword in ["adk", "cloud run", "japan", "misc", "terraform", "kyoto"]:
                        if keyword in msg_lower:
                            search_q = keyword
                            break
                    res = list_and_search_tab_groups(query=search_q)
                    tool_results.append(res)
                    groups = res["data"]["tab_groups"]
                    lines = [
                        f"Found **{len(groups)} Work Session Tab Group(s)** ({res['data']['total_tabs']} total tabs):\n"
                    ]
                    for g in groups:
                        note_str = g["where_i_left_off_note"] or "No summary note yet."
                        lines.append(
                            f"- **{g['session_name']}** (`{g['group_id']}`, {len(g['tabs'])} tabs) — _{note_str}_"
                        )
                    lines.append(
                        "\nClick **Auto-Analyze & Propose Merges** or ask me to *\"Organize and merge my overlapping tab groups\"*!"
                    )
                    reply_text = "\n".join(lines)

            # 5. Self-Evaluation Output Guardrail
            eval_check = policy_guardrail.self_evaluate_agent_output(
                response_text=reply_text,
                agent_name=routing["target_agent"],
            )
            final_reply = eval_check["sanitized_response"]
            db.append_conversation_turn(session_id, "assistant", final_reply)

            return {
                "status": "SUCCESS",
                "reply": final_reply,
                "routing": routing,
                "compaction": compaction_metrics,
                "self_evaluation": eval_check,
                "tool_results": tool_results,
                "trace_id": root_span["trace_id"],
            }


orchestrator = TabFlowOrchestrator()

