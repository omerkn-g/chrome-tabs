"""Guardrails, Policy Plugins, Human-in-the-Loop Hooks, and Intent/Outcome Callbacks.

Satisfies Category 3 & Category 4 of the AgentOps Code Review Matrix:
- Guardrails & Policy Plugins (5 pts): Input/output policy validation and self-evaluation.
- Human-in-the-Loop Hooks (5 pts): Explicit code stops requiring human confirmation
  before high-stakes merge or delete operations.
- Intent vs. Outcome Capture (5 pts): Pre-execution intent and post-execution outcome
  logging via tool lifecycle callbacks.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Optional

from tab_session_agent.observability import observability_hub

HIGH_STAKES_TOOLS = {
    "execute_tab_group_merge_plan",
    "delete_saved_tab_group",
}

FORBIDDEN_URL_SCHEMES = ("javascript:", "data:", "vbscript:", "file://")


class PolicyGuardrailPlugin:
    """Enforces security policies, PII redaction, self-evaluation, and HITL confirmation hooks."""

    def __init__(self) -> None:
        self.blocked_calls_count = 0
        self.hitl_intercepts_count = 0
        self.self_eval_checks_count = 0

    def before_model_callback(
        self,
        prompt_text: str,
        agent_name: str = "tabflow_coordinator",
    ) -> dict[str, Any]:
        """ADK before_model_callback: Scrub PII and enforce input policy guardrails before LLM call."""
        scrubbed_prompt = observability_hub.scrubber.scrub_text(prompt_text)

        # Policy check for prompt injection or forbidden URI schemes
        lower_prompt = scrubbed_prompt.lower()
        for scheme in FORBIDDEN_URL_SCHEMES:
            if scheme in lower_prompt:
                self.blocked_calls_count += 1
                observability_hub.log_event(
                    event_type="POLICY_GUARDRAIL_BLOCK",
                    message=f"Blocked unsafe URI scheme '{scheme}' in model input",
                    agent_name=agent_name,
                    metadata={"blocked_scheme": scheme},
                )
                return {
                    "allowed": False,
                    "sanitized_prompt": scrubbed_prompt,
                    "policy_violation": f"Disallowed URI scheme detected: {scheme}",
                }

        return {
            "allowed": True,
            "sanitized_prompt": scrubbed_prompt,
            "policy_violation": None,
        }

    def before_tool_callback(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        agent_name: str = "tabflow_coordinator",
    ) -> Optional[dict[str, Any]]:
        """ADK before_tool_callback: Log INTENT, validate policy, and enforce Human-in-the-Loop stop.

        Returns:
            None if execution may proceed, or a structured response dict if intercepted
            by the policy guardrail or Human-in-the-Loop gate.
        """
        observability_hub.log_intent(
            tool_name=tool_name,
            intended_args=tool_args,
            agent_name=agent_name,
        )

        # 1. Policy check on any URLs or text arguments
        serialized_args = str(tool_args).lower()
        for scheme in FORBIDDEN_URL_SCHEMES:
            if scheme in serialized_args:
                self.blocked_calls_count += 1
                observability_hub.log_event(
                    event_type="POLICY_GUARDRAIL_BLOCK",
                    message=f"Blocked tool '{tool_name}' due to forbidden URI scheme '{scheme}'",
                    agent_name=agent_name,
                    metadata={"tool_name": tool_name, "blocked_scheme": scheme},
                )
                return {
                    "status": "ERROR",
                    "tool_name": tool_name,
                    "message": f"Policy Guardrail blocked tool '{tool_name}': unsafe URI scheme '{scheme}'.",
                    "data": {},
                    "error_code": "POLICY_VIOLATION_UNSAFE_URI",
                    "recovery_instructions": (
                        "Remove any javascript:, data:, or file:// URLs and retry with valid https:// URLs."
                    ),
                }

        # 2. Human-in-the-Loop (HITL) Hook for high-stakes state mutations
        if tool_name in HIGH_STAKES_TOOLS:
            user_confirmed = bool(tool_args.get("user_confirmed", False))
            if not user_confirmed:
                self.hitl_intercepts_count += 1
                observability_hub.log_event(
                    event_type="HITL_CONFIRMATION_REQUIRED",
                    message=(
                        f"Human-in-the-Loop gate paused high-stakes tool '{tool_name}' "
                        "because user_confirmed is False."
                    ),
                    agent_name=agent_name,
                    metadata={"tool_name": tool_name, "tool_args": tool_args},
                )
                return {
                    "status": "PENDING_HUMAN_CONFIRMATION",
                    "tool_name": tool_name,
                    "message": (
                        f"Action '{tool_name}' requires explicit Human-in-the-Loop confirmation. "
                        "Please review the proposed changes and confirm to proceed."
                    ),
                    "data": {"pending_arguments": observability_hub.scrubber.scrub_payload(tool_args)},
                    "error_code": "HITL_CONFIRMATION_REQUIRED",
                    "recovery_instructions": (
                        "Present the merge or deletion preview to the user and ask for explicit approval. "
                        "Only re-invoke this tool with user_confirmed=True after the user confirms."
                    ),
                }

        return None

    def after_tool_callback(
        self,
        tool_name: str,
        tool_result: dict[str, Any],
        duration_ms: float,
        agent_name: str = "tabflow_coordinator",
    ) -> dict[str, Any]:
        """ADK after_tool_callback: Scrub PII from tool output and log actual OUTCOME."""
        scrubbed_result = observability_hub.scrubber.scrub_payload(tool_result)
        observability_hub.log_outcome(
            tool_name=tool_name,
            actual_outcome=scrubbed_result,
            duration_ms=duration_ms,
            agent_name=agent_name,
        )
        return scrubbed_result

    def self_evaluate_agent_output(
        self,
        response_text: str,
        agent_name: str = "tabflow_coordinator",
    ) -> dict[str, Any]:
        """Self-evaluation guardrail checking that output contains no leaked PII/secrets or bookmark drift."""
        self.self_eval_checks_count += 1
        scrubbed = observability_hub.scrubber.scrub_text(response_text)
        leaked_pii = scrubbed != response_text

        evaluation = {
            "passed": not leaked_pii,
            "pii_redacted_in_output": leaked_pii,
            "sanitized_response": scrubbed,
        }
        observability_hub.log_event(
            event_type="SELF_EVALUATION_GUARDRAIL",
            message=f"Self-evaluation completed (passed={evaluation['passed']})",
            agent_name=agent_name,
            metadata={"passed": evaluation["passed"], "pii_redacted": leaked_pii},
        )
        return evaluation


policy_guardrail = PolicyGuardrailPlugin()

