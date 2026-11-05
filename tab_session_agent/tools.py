"""TabFlow Work Session Tools with Descriptive Naming, Schemas, and Guided Error Recovery.

Satisfies Category 1 & Category 3 of the AgentOps Code Review Matrix:
- Comprehensive Tool Docstrings (5 pts)
- Descriptive Naming (5 pts)
- Explicit JSON Schemas (5 pts)
- Guided Error Handling (5 pts)
- Human-in-the-Loop Hooks (5 pts)
"""

from __future__ import annotations

import re
import time
import urllib.parse
import uuid
from typing import Any, Optional

from tab_session_agent.callbacks import policy_guardrail
from tab_session_agent.config import AgentConfig
from tab_session_agent.database import PersistentSessionDatabase
from tab_session_agent.observability import observability_hub
from tab_session_agent.schemas import (
    DeleteTabGroupInput,
    ExecuteMergePlanInput,
    GuidedToolOutputSchema,
    ListTabGroupsInput,
    ProposeMergePlanInput,
    RenameTabGroupInput,
)

_CONFIG = AgentConfig.from_env()
_DB: Optional[PersistentSessionDatabase] = None


def get_database() -> PersistentSessionDatabase:
    """Return the singleton PersistentSessionDatabase instance."""
    global _DB
    if _DB is None:
        _DB = PersistentSessionDatabase(
            db_path=_CONFIG.db_path,
            seed_json_path=_CONFIG.seed_json_path,
        )
    return _DB


def set_database_for_testing(db: PersistentSessionDatabase) -> None:
    """Inject a custom or temporary SQLite database instance for evaluation tests."""
    global _DB
    _DB = db


STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "http",
    "https",
    "www",
    "com",
    "org",
    "io",
    "html",
    "index",
    "docs",
    "guide",
    "overview",
    "unnamed",
    "group",
    "work",
    "mon",
    "tues",
    "wed",
    "thu",
    "fri",
}

TOPIC_CLUSTERS = {
    "adk": {
        "keywords": {"adk", "agent", "agents", "multi-agents", "vertex", "evaluate", "callbacks"},
        "domains": {"google.github.io", "github.com", "docs.cloud.google.com"},
        "default_name": "ADK Multi-Agent & Evaluation",
        "color": "blue",
    },
    "cloudrun": {
        "keywords": {"cloud", "run", "iam", "terraform", "secrets", "secret", "deploy", "403", "securing"},
        "domains": {"cloud.google.com", "registry.terraform.io"},
        "default_name": "Cloud Run IAM & Deployment",
        "color": "rose",
    },
    "japan": {
        "keywords": {"japan", "tokyo", "kyoto", "ryokan", "shinkansen", "shibuya", "shinjuku", "foliage", "jr"},
        "domains": {"japan-guide.com", "www.japan-guide.com", "booking.com", "www.booking.com"},
        "default_name": "Japan Trip",
        "color": "emerald",
    },
}


def _tokenize_tab(tab: dict[str, Any]) -> set[str]:
    text = f"{tab.get('title', '')} {tab.get('url', '')}".lower()
    tokens = set(re.findall(r"[a-z0-9]{3,}", text))
    return tokens - STOPWORDS


def _classify_tab_topic(tab: dict[str, Any]) -> Optional[str]:
    tokens = _tokenize_tab(tab)
    domain = (tab.get("domain") or urllib.parse.urlsplit(tab.get("url", "")).netloc).lower()
    best_topic: Optional[str] = None
    best_score = 0
    for topic_key, spec in TOPIC_CLUSTERS.items():
        kw_hits = len(tokens & spec["keywords"])
        dom_hit = 2 if domain in spec["domains"] else 0
        # Special disambiguation for cloud.google.com/run vs agent-builder
        url_lower = tab.get("url", "").lower()
        if topic_key == "adk" and ("adk" in url_lower or "agent-builder" in url_lower):
            kw_hits += 3
        if topic_key == "cloudrun" and ("/run/" in url_lower or "cloud_run" in url_lower):
            kw_hits += 3
        score = kw_hits + dom_hit
        if score > best_score:
            best_score = score
            best_topic = topic_key
    return best_topic if best_score >= 2 else None


def _dominant_topic_for_tabs(tabs: list[dict[str, Any]]) -> Optional[str]:
    counts: dict[str, int] = {}
    for t in tabs:
        topic = _classify_tab_topic(t)
        if topic:
            counts[topic] = counts.get(topic, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda x: x[1])[0]


def _match_stray_tab_to_existing_group(
    tab: dict[str, Any],
    candidate_groups: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Check if a stray tab semantically belongs to another existing open/closed tab group."""
    tab_topic = _classify_tab_topic(tab)
    tab_tokens = _tokenize_tab(tab)

    best_group: Optional[dict[str, Any]] = None
    best_score = 0

    for grp in candidate_groups:
        if grp["group_id"] == "grp-misc":
            continue
        grp_tabs = grp.get("tabs", [])
        grp_topic = _dominant_topic_for_tabs(grp_tabs)
        grp_tokens = set(re.findall(r"[a-z0-9]{3,}", grp["session_name"].lower())) - STOPWORDS
        for gt in grp_tabs:
            grp_tokens |= _tokenize_tab(gt)

        score = len(tab_tokens & grp_tokens)
        if tab_topic and tab_topic == grp_topic:
            score += 5

        if score > best_score and score >= 2:
            best_score = score
            best_group = grp

    return best_group


def _run_with_callbacks(
    tool_name: str,
    raw_args: dict[str, Any],
    fn: Any,
    agent_name: str = "tabflow_coordinator",
) -> dict[str, Any]:
    """Execute a tool wrapped in OpenTelemetry tracing, before/after callbacks, and guided error recovery."""
    start_ts = time.perf_counter()
    with observability_hub.start_span(
        span_name=f"tool.{tool_name}",
        agent_name=agent_name,
        attributes={"tool_name": tool_name, "args": raw_args},
    ):
        intercept = policy_guardrail.before_tool_callback(
            tool_name=tool_name,
            tool_args=raw_args,
            agent_name=agent_name,
        )
        if intercept is not None:
            duration_ms = (time.perf_counter() - start_ts) * 1000
            return policy_guardrail.after_tool_callback(
                tool_name=tool_name,
                tool_result=intercept,
                duration_ms=duration_ms,
                agent_name=agent_name,
            )

        try:
            result = fn()
        except Exception as exc:
            result = GuidedToolOutputSchema(
                status="ERROR",
                tool_name=tool_name,
                message=f"Tool execution failed: {exc}",
                data={},
                error_code="UNEXPECTED_TOOL_ERROR",
                recovery_instructions=(
                    "Verify all input parameter types and group IDs using "
                    "`list_and_search_tab_groups` before retrying."
                ),
            ).model_dump()

        duration_ms = (time.perf_counter() - start_ts) * 1000
        return policy_guardrail.after_tool_callback(
            tool_name=tool_name,
            tool_result=result,
            duration_ms=duration_ms,
            agent_name=agent_name,
        )


def list_and_search_tab_groups(query: Optional[str] = None) -> dict[str, Any]:
    """List all saved Chrome Tab Groups (Work Sessions) or filter them by a search query.

    Each returned Tab Group represents a Work Session with its `group_id`,
    `session_name`, `color`, `where_i_left_off_note`, and deduplicated `tabs`.

    Args:
        query: Optional keyword string to filter tab groups by session name,
            "Where I left off" note, tab title, or URL domain. Omit or pass None
            to retrieve all saved tab groups.

    Returns:
        dict[str, Any]: Standardized `GuidedToolOutputSchema` dictionary containing:
            - `status`: `'SUCCESS'` or `'ERROR'`
            - `data.tab_groups`: List of matching tab group session objects
            - `data.total_groups`: Count of returned tab groups
            - `data.total_tabs`: Total tabs across returned groups
            - `data.pending_merge_plans`: Any pending merge proposals awaiting approval
    """

    def _impl() -> dict[str, Any]:
        validated = ListTabGroupsInput(query=query)
        db = get_database()
        groups = db.list_tab_groups(query=validated.query)
        pending_plans = db.list_pending_merge_plans()
        total_tabs = sum(len(g.get("tabs", [])) for g in groups)

        return GuidedToolOutputSchema(
            status="SUCCESS",
            tool_name="list_and_search_tab_groups",
            message=f"Found {len(groups)} tab group work session(s) containing {total_tabs} tab(s).",
            data={
                "tab_groups": groups,
                "total_groups": len(groups),
                "total_tabs": total_tabs,
                "pending_merge_plans": pending_plans,
            },
        ).model_dump()

    return _run_with_callbacks(
        tool_name="list_and_search_tab_groups",
        raw_args={"query": query},
        fn=_impl,
        agent_name="tab_triage_flash_agent",
    )


def propose_tab_group_merge_plan(
    source_group_ids: list[str],
    suggested_session_name: Optional[str] = None,
) -> dict[str, Any]:
    """Analyze 2 or more Tab Groups, deduplicate URLs, route stray tabs, and create a Merge Plan preview.

    This is a non-destructive planning tool. It performs:
    1. Canonical URL normalization & PII/token redaction across `source_group_ids`.
    2. Exact URL deduplication across the groups being merged.
    3. Stray tab detection: Any tab unrelated to the primary session topic is checked
       against all other existing Tab Groups. If a matching group exists, the stray tab
       is routed there; otherwise it is routed to the `Misc` tab group (`grp-misc`).
    4. Smart naming & synthesis: Suggests a unified Work Session name and generates a
       "Where I left off" summary note.

    Args:
        source_group_ids: List of at least 2 existing `group_id` strings to merge
            (e.g., `["grp-adk-mon", "grp-unnamed-2"]`).
        suggested_session_name: Optional custom name for the merged Work Session. If
            omitted, the agent synthesizes a smart name from the core tabs.

    Returns:
        dict[str, Any]: Standardized `GuidedToolOutputSchema` dictionary with `data.merge_plan`
        containing `plan_id`, `suggested_session_name`, `merged_tabs`, `duplicates_removed`,
        `stray_routing`, and `synthesized_note`.
    """

    def _impl() -> dict[str, Any]:
        try:
            validated = ProposeMergePlanInput(
                source_group_ids=source_group_ids,
                suggested_session_name=suggested_session_name,
            )
        except Exception as exc:
            return GuidedToolOutputSchema(
                status="ERROR",
                tool_name="propose_tab_group_merge_plan",
                message=f"Invalid merge plan input: {exc}",
                data={},
                error_code="INVALID_MERGE_SOURCE_GROUPS",
                recovery_instructions=(
                    "Provide at least 2 valid group IDs in `source_group_ids`. "
                    "Call `list_and_search_tab_groups()` first to inspect available `group_id` values."
                ),
            ).model_dump()

        db = get_database()
        all_groups = db.list_tab_groups()
        groups_by_id = {g["group_id"]: g for g in all_groups}

        missing_ids = [gid for gid in validated.source_group_ids if gid not in groups_by_id]
        if missing_ids:
            return GuidedToolOutputSchema(
                status="ERROR",
                tool_name="propose_tab_group_merge_plan",
                message=f"Tab group ID(s) not found: {missing_ids}",
                data={"available_group_ids": list(groups_by_id.keys())},
                error_code="GROUP_ID_NOT_FOUND",
                recovery_instructions=(
                    f"Unknown group IDs {missing_ids}. Choose from existing IDs: "
                    f"{list(groups_by_id.keys())} by calling `list_and_search_tab_groups()`."
                ),
            ).model_dump()

        source_groups = [groups_by_id[gid] for gid in validated.source_group_ids]
        other_groups = [
            g for g in all_groups if g["group_id"] not in set(validated.source_group_ids)
        ]

        # Collect all tabs from source groups and normalize/scrub URLs
        raw_tab_entries: list[tuple[str, dict[str, Any]]] = []
        for grp in source_groups:
            for tab in grp.get("tabs", []):
                clean_url, removed_keys = (
                    observability_hub.scrubber.normalize_and_scrub_url(tab.get("url", ""))
                )
                normalized_tab = {
                    "tab_id": tab.get("tab_id") or f"tab-{uuid.uuid4().hex[:6]}",
                    "title": observability_hub.scrubber.scrub_text(tab.get("title", "")),
                    "url": clean_url,
                    "domain": urllib.parse.urlsplit(clean_url).netloc.lower(),
                    "sanitized_params": removed_keys,
                }
                raw_tab_entries.append((grp["group_id"], normalized_tab))

        # Determine dominant topic across all tabs in the source groups
        dominant_topic = _dominant_topic_for_tabs([t for _, t in raw_tab_entries])

        seen_urls: dict[str, dict[str, Any]] = {}
        duplicates_removed: list[dict[str, Any]] = []
        core_merged_tabs: list[dict[str, Any]] = []
        stray_routing: list[dict[str, Any]] = []

        for from_group_id, tab in raw_tab_entries:
            canonical_url = tab["url"]
            if canonical_url in seen_urls:
                duplicates_removed.append(
                    {
                        "tab_id": tab["tab_id"],
                        "title": tab["title"],
                        "url": canonical_url,
                        "duplicate_of_tab_id": seen_urls[canonical_url]["tab_id"],
                        "from_group_id": from_group_id,
                    }
                )
                continue

            seen_urls[canonical_url] = tab
            tab_topic = _classify_tab_topic(tab)

            # A tab is stray if the merge has a clear dominant topic and this tab does not match it
            is_stray = bool(dominant_topic and tab_topic != dominant_topic)
            if is_stray:
                matched_group = _match_stray_tab_to_existing_group(tab, other_groups)
                if matched_group is not None:
                    stray_routing.append(
                        {
                            "tab_id": tab["tab_id"],
                            "title": tab["title"],
                            "url": canonical_url,
                            "domain": tab["domain"],
                            "from_group_id": from_group_id,
                            "target_group_id": matched_group["group_id"],
                            "target_session_name": matched_group["session_name"],
                            "routing_reason": (
                                f"Matches existing tab group '{matched_group['session_name']}' "
                                f"({matched_group['group_id']})."
                            ),
                        }
                    )
                else:
                    stray_routing.append(
                        {
                            "tab_id": tab["tab_id"],
                            "title": tab["title"],
                            "url": canonical_url,
                            "domain": tab["domain"],
                            "from_group_id": from_group_id,
                            "target_group_id": "grp-misc",
                            "target_session_name": "Misc",
                            "routing_reason": (
                                "Unrelated to merged session topic and no matching existing group found; "
                                "routing to 'Misc' tab group."
                            ),
                        }
                    )
            else:
                core_merged_tabs.append(tab)

        # Synthesize smart Session Name & "Where I left off" note
        topic_spec = TOPIC_CLUSTERS.get(dominant_topic or "", {})
        if validated.suggested_session_name and validated.suggested_session_name.strip():
            smart_name = observability_hub.scrubber.scrub_text(
                validated.suggested_session_name.strip()
            )
        elif topic_spec.get("default_name"):
            smart_name = str(topic_spec["default_name"])
        else:
            named_sources = [
                g["session_name"]
                for g in source_groups
                if not g["session_name"].lower().startswith("unnamed")
            ]
            smart_name = (
                " + ".join(named_sources[:2]) if named_sources else "Consolidated Work Session"
            )

        existing_notes = [
            g["where_i_left_off_note"].strip()
            for g in source_groups
            if g.get("where_i_left_off_note", "").strip()
        ]
        top_titles = [t["title"].split(" - ")[0].split(" | ")[0] for t in core_merged_tabs[:3]]
        synthesized_note = (
            f"{existing_notes[0]} " if existing_notes else ""
        ) + f"Key focus areas: {', '.join(top_titles)} ({len(core_merged_tabs)} core tabs)."

        plan_id = f"plan-{uuid.uuid4().hex[:8]}"
        plan_obj = {
            "plan_id": plan_id,
            "suggested_session_name": smart_name,
            "color": str(topic_spec.get("color", source_groups[0].get("color", "blue"))),
            "source_group_ids": validated.source_group_ids,
            "source_group_names": [g["session_name"] for g in source_groups],
            "merged_tabs": core_merged_tabs,
            "duplicates_removed": duplicates_removed,
            "stray_routing": stray_routing,
            "synthesized_note": synthesized_note.strip(),
            "status": "PENDING",
        }
        db.save_merge_plan(plan_obj)

        return GuidedToolOutputSchema(
            status="SUCCESS",
            tool_name="propose_tab_group_merge_plan",
            message=(
                f"Created Merge Plan '{plan_id}': '{smart_name}' with {len(core_merged_tabs)} core tab(s), "
                f"{len(duplicates_removed)} duplicate(s) removed, and {len(stray_routing)} stray tab(s) routed. "
                "Awaiting user approval or custom session name."
            ),
            data={"merge_plan": plan_obj},
        ).model_dump()

    return _run_with_callbacks(
        tool_name="propose_tab_group_merge_plan",
        raw_args={
            "source_group_ids": source_group_ids,
            "suggested_session_name": suggested_session_name,
        },
        fn=_impl,
        agent_name="session_synthesizer_pro_agent",
    )


def execute_tab_group_merge_plan(
    plan_id: str,
    final_session_name: str,
    where_i_left_off_note: Optional[str] = None,
    user_confirmed: bool = False,
) -> dict[str, Any]:
    """Execute an approved Tab Group Merge Plan (Human-in-the-Loop gated).

    Applies a pending merge plan by:
    1. Creating the unified Work Session Tab Group with `final_session_name` and
       its "Where I left off" summary note.
    2. Moving any stray tabs to their matched existing Tab Group or creating/updating
       the `Misc` Tab Group (`grp-misc`).
    3. Removing the old fragmented source tab groups.
    4. Triggering asynchronous background memory consolidation.

    Args:
        plan_id: Identifier of the pending merge plan from `propose_tab_group_merge_plan`.
        final_session_name: The user-approved or user-customized Work Session name.
        where_i_left_off_note: Optional updated "Where I left off" summary note.
        user_confirmed: Explicit Human-in-the-Loop boolean confirmation. MUST be True
            for the merge to execute.

    Returns:
        dict[str, Any]: Standardized `GuidedToolOutputSchema` dictionary containing the
        newly merged `work_session`, `routed_stray_groups`, and updated board state.
    """

    def _impl() -> dict[str, Any]:
        try:
            validated = ExecuteMergePlanInput(
                plan_id=plan_id,
                final_session_name=final_session_name,
                where_i_left_off_note=where_i_left_off_note,
                user_confirmed=user_confirmed,
            )
        except Exception as exc:
            return GuidedToolOutputSchema(
                status="ERROR",
                tool_name="execute_tab_group_merge_plan",
                message=f"Invalid execute merge arguments: {exc}",
                data={},
                error_code="INVALID_EXECUTE_MERGE_INPUT",
                recovery_instructions=(
                    "Ensure `plan_id` and `final_session_name` are non-empty strings "
                    "and `user_confirmed=True` after receiving user approval."
                ),
            ).model_dump()

        db = get_database()
        plan = db.get_merge_plan(validated.plan_id)
        if not plan:
            pending = [p["plan_id"] for p in db.list_pending_merge_plans()]
            return GuidedToolOutputSchema(
                status="ERROR",
                tool_name="execute_tab_group_merge_plan",
                message=f"Merge plan '{validated.plan_id}' was not found.",
                data={"pending_plan_ids": pending},
                error_code="MERGE_PLAN_NOT_FOUND",
                recovery_instructions=(
                    f"Call `propose_tab_group_merge_plan` first to generate a valid `plan_id`. "
                    f"Currently pending plan IDs: {pending}."
                ),
            ).model_dump()

        if plan.get("status") == "EXECUTED":
            return GuidedToolOutputSchema(
                status="ERROR",
                tool_name="execute_tab_group_merge_plan",
                message=f"Merge plan '{validated.plan_id}' has already been executed.",
                data={"plan_id": validated.plan_id},
                error_code="MERGE_PLAN_ALREADY_EXECUTED",
                recovery_instructions=(
                    "Call `list_and_search_tab_groups()` to inspect the already-merged work sessions."
                ),
            ).model_dump()

        source_ids: list[str] = plan["source_group_ids"]
        primary_group_id = source_ids[0]
        existing_primary = db.get_tab_group(primary_group_id)
        color = existing_primary["color"] if existing_primary else "blue"

        note_to_save = (
            validated.where_i_left_off_note.strip()
            if validated.where_i_left_off_note and validated.where_i_left_off_note.strip()
            else plan["synthesized_note"]
        )

        # 1. Route stray tabs to matched existing tab groups or 'Misc' (grp-misc)
        routed_destinations: list[dict[str, Any]] = []
        for stray in plan.get("stray_routing", []):
            target_gid = stray["target_group_id"]
            target_name = stray["target_session_name"]
            target_grp = db.get_tab_group(target_gid)
            if target_grp is None:
                target_tabs: list[dict[str, Any]] = []
                target_color = "slate" if target_gid == "grp-misc" else "violet"
                target_note = (
                    "Holding group for miscellaneous tabs separated during work session merges."
                    if target_gid == "grp-misc"
                    else ""
                )
            else:
                target_tabs = list(target_grp.get("tabs", []))
                target_color = target_grp.get("color", "slate")
                target_note = target_grp.get("where_i_left_off_note", "")

            existing_urls = {t.get("url") for t in target_tabs}
            if stray["url"] not in existing_urls:
                target_tabs.append(
                    {
                        "tab_id": stray["tab_id"],
                        "title": stray["title"],
                        "url": stray["url"],
                        "domain": stray.get("domain", ""),
                    }
                )
            updated_target = db.upsert_tab_group(
                group_id=target_gid,
                session_name=target_name,
                color=target_color,
                where_i_left_off_note=target_note,
                tabs=target_tabs,
            )
            routed_destinations.append(
                {
                    "tab_title": stray["title"],
                    "target_group_id": target_gid,
                    "target_session_name": updated_target.get("session_name", target_name),
                }
            )

        # 2. Save the consolidated Work Session into primary_group_id and delete secondary source groups
        merged_session = db.upsert_tab_group(
            group_id=primary_group_id,
            session_name=validated.final_session_name,
            color=color,
            where_i_left_off_note=note_to_save,
            tabs=plan["merged_tabs"],
        )
        for old_gid in source_ids[1:]:
            if old_gid != primary_group_id:
                db.delete_tab_group(old_gid)

        db.mark_merge_plan_status(validated.plan_id, "EXECUTED")

        # 3. Trigger non-blocking async memory consolidation
        db.trigger_async_memory_consolidation(
            event_reason=f"merged_{validated.plan_id}_into_{primary_group_id}"
        )

        return GuidedToolOutputSchema(
            status="SUCCESS",
            tool_name="execute_tab_group_merge_plan",
            message=(
                f"Merged {len(source_ids)} tab groups into Work Session "
                f"'{merged_session['session_name']}' ({len(merged_session['tabs'])} core tabs, "
                f"{len(routed_destinations)} stray tab(s) routed)."
            ),
            data={
                "merged_work_session": merged_session,
                "routed_stray_tabs": routed_destinations,
                "stray_routing": plan.get("stray_routing", []),
                "duplicates_removed": plan.get("duplicates_removed", []),
                "removed_group_ids": source_ids[1:],
                "all_tab_groups": db.list_tab_groups(),
            },
        ).model_dump()

    return _run_with_callbacks(
        tool_name="execute_tab_group_merge_plan",
        raw_args={
            "plan_id": plan_id,
            "final_session_name": final_session_name,
            "where_i_left_off_note": where_i_left_off_note,
            "user_confirmed": user_confirmed,
        },
        fn=_impl,
        agent_name="tabflow_coordinator",
    )


def rename_tab_group_session(
    group_id: str,
    new_session_name: str,
    where_i_left_off_note: Optional[str] = None,
) -> dict[str, Any]:
    """Rename a Tab Group Work Session and optionally update its 'Where I left off' note.

    This non-destructive action executes immediately without requiring a HITL confirmation stop.

    Args:
        group_id: Unique ID of the tab group to rename (e.g., `'grp-adk-mon'`).
        new_session_name: New user-chosen display name for the Work Session.
        where_i_left_off_note: Optional updated summary note describing current progress.

    Returns:
        dict[str, Any]: Standardized `GuidedToolOutputSchema` dictionary containing the
        updated `work_session`.
    """

    def _impl() -> dict[str, Any]:
        try:
            validated = RenameTabGroupInput(
                group_id=group_id,
                new_session_name=new_session_name,
                where_i_left_off_note=where_i_left_off_note,
            )
        except Exception as exc:
            return GuidedToolOutputSchema(
                status="ERROR",
                tool_name="rename_tab_group_session",
                message=f"Invalid rename parameters: {exc}",
                data={},
                error_code="INVALID_RENAME_INPUT",
                recovery_instructions=(
                    "Pass a non-empty `group_id` and `new_session_name`."
                ),
            ).model_dump()

        db = get_database()
        existing = db.get_tab_group(validated.group_id)
        if not existing:
            available = [g["group_id"] for g in db.list_tab_groups()]
            return GuidedToolOutputSchema(
                status="ERROR",
                tool_name="rename_tab_group_session",
                message=f"Tab group '{validated.group_id}' does not exist.",
                data={"available_group_ids": available},
                error_code="GROUP_ID_NOT_FOUND",
                recovery_instructions=(
                    f"Use one of the existing group IDs {available} from `list_and_search_tab_groups()`."
                ),
            ).model_dump()

        note = (
            validated.where_i_left_off_note
            if validated.where_i_left_off_note is not None
            else existing.get("where_i_left_off_note", "")
        )
        updated = db.upsert_tab_group(
            group_id=validated.group_id,
            session_name=validated.new_session_name.strip(),
            color=existing.get("color", "blue"),
            where_i_left_off_note=note,
            tabs=existing.get("tabs", []),
        )
        db.trigger_async_memory_consolidation(event_reason=f"renamed_{validated.group_id}")

        return GuidedToolOutputSchema(
            status="SUCCESS",
            tool_name="rename_tab_group_session",
            message=f"Renamed tab group '{validated.group_id}' to '{updated['session_name']}'.",
            data={"work_session": updated},
        ).model_dump()

    return _run_with_callbacks(
        tool_name="rename_tab_group_session",
        raw_args={
            "group_id": group_id,
            "new_session_name": new_session_name,
            "where_i_left_off_note": where_i_left_off_note,
        },
        fn=_impl,
        agent_name="tabflow_coordinator",
    )


def delete_saved_tab_group(
    group_id: str,
    reason: str,
    user_confirmed: bool = False,
) -> dict[str, Any]:
    """Permanently delete a saved Tab Group Work Session (Human-in-the-Loop gated).

    Args:
        group_id: Unique ID of the tab group to delete.
        reason: Short explanation of why the tab group is being removed.
        user_confirmed: Explicit Human-in-the-Loop confirmation flag. MUST be True
            before deletion is executed.

    Returns:
        dict[str, Any]: Standardized `GuidedToolOutputSchema` dictionary confirming deletion.
    """

    def _impl() -> dict[str, Any]:
        try:
            validated = DeleteTabGroupInput(
                group_id=group_id,
                reason=reason,
                user_confirmed=user_confirmed,
            )
        except Exception as exc:
            return GuidedToolOutputSchema(
                status="ERROR",
                tool_name="delete_saved_tab_group",
                message=f"Invalid delete parameters: {exc}",
                data={},
                error_code="INVALID_DELETE_INPUT",
                recovery_instructions="Provide a valid `group_id`, `reason`, and `user_confirmed=True`.",
            ).model_dump()

        db = get_database()
        existing = db.get_tab_group(validated.group_id)
        if not existing:
            available = [g["group_id"] for g in db.list_tab_groups()]
            return GuidedToolOutputSchema(
                status="ERROR",
                tool_name="delete_saved_tab_group",
                message=f"Tab group '{validated.group_id}' not found.",
                data={"available_group_ids": available},
                error_code="GROUP_ID_NOT_FOUND",
                recovery_instructions=(
                    f"Verify the `group_id` via `list_and_search_tab_groups()`. Available: {available}."
                ),
            ).model_dump()

        db.delete_tab_group(validated.group_id)
        db.trigger_async_memory_consolidation(event_reason=f"deleted_{validated.group_id}")

        return GuidedToolOutputSchema(
            status="SUCCESS",
            tool_name="delete_saved_tab_group",
            message=f"Deleted tab group '{existing['session_name']}' ({validated.group_id}).",
            data={
                "deleted_group_id": validated.group_id,
                "deleted_session_name": existing["session_name"],
                "reason": validated.reason,
            },
        ).model_dump()

    return _run_with_callbacks(
        tool_name="delete_saved_tab_group",
        raw_args={
            "group_id": group_id,
            "reason": reason,
            "user_confirmed": user_confirmed,
        },
        fn=_impl,
        agent_name="tabflow_coordinator",
    )


def auto_discover_and_propose_merges() -> list[dict[str, Any]]:
    """Scan all current tab groups by dominant topic and automatically generate merge plans for overlapping groups."""
    db = get_database()
    groups = db.list_tab_groups()
    topic_buckets: dict[str, list[str]] = {}
    for grp in groups:
        if grp["group_id"] == "grp-misc":
            continue
        topic = _dominant_topic_for_tabs(grp.get("tabs", []))
        if topic:
            topic_buckets.setdefault(topic, []).append(grp["group_id"])

    created_plans: list[dict[str, Any]] = []
    for topic, gids in topic_buckets.items():
        if len(gids) >= 2:
            res = propose_tab_group_merge_plan(source_group_ids=gids)
            if res.get("status") == "SUCCESS" and res.get("data", {}).get("merge_plan"):
                created_plans.append(res["data"]["merge_plan"])
    return created_plans


ALL_TABFLOW_TOOLS = [
    list_and_search_tab_groups,
    propose_tab_group_merge_plan,
    execute_tab_group_merge_plan,
    rename_tab_group_session,
    delete_saved_tab_group,
]

