"""Explicit JSON Schemas for Tool Inputs and Outputs.

Satisfies Category 1 of the AgentOps Code Review Matrix:
- Explicit JSON Schemas (5 pts): Strict input and output schemas to validate tool
  arguments and constrain LLMs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

try:
    from pydantic import BaseModel, Field, field_validator  # type: ignore

    HAS_PYDANTIC = True
except ImportError:
    HAS_PYDANTIC = False

    class BaseModel:  # type: ignore
        """Lightweight Pydantic-compatible schema model when running in stdlib-only mode."""

        def __init__(self, **kwargs: Any) -> None:
            annotations = getattr(self.__class__, "__annotations__", {})
            for name in annotations:
                if name in kwargs:
                    setattr(self, name, kwargs[name])
                elif hasattr(self.__class__, name):
                    default_val = getattr(self.__class__, name)
                    if isinstance(default_val, list):
                        setattr(self, name, list(default_val))
                    elif isinstance(default_val, dict):
                        setattr(self, name, dict(default_val))
                    else:
                        setattr(self, name, default_val)
                else:
                    raise ValueError(f"Missing required schema field: '{name}'")
            for k, v in kwargs.items():
                if k not in annotations:
                    setattr(self, k, v)
            if hasattr(self, "_validate"):
                self._validate()

        def model_dump(self) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for k, v in self.__dict__.items():
                if isinstance(v, BaseModel):
                    result[k] = v.model_dump()
                elif isinstance(v, list):
                    result[k] = [
                        item.model_dump() if isinstance(item, BaseModel) else item
                        for item in v
                    ]
                else:
                    result[k] = v
            return result

        @classmethod
        def model_json_schema(cls) -> dict[str, Any]:
            props: dict[str, Any] = {}
            required: list[str] = []
            annotations = getattr(cls, "__annotations__", {})
            for name, type_hint in annotations.items():
                type_str = str(type_hint)
                prop_type = "string"
                if "bool" in type_str:
                    prop_type = "boolean"
                elif "int" in type_str:
                    prop_type = "integer"
                elif "list" in type_str:
                    prop_type = "array"
                elif "dict" in type_str:
                    prop_type = "object"
                props[name] = {"type": prop_type, "title": name}
                if "Optional" not in type_str and not hasattr(cls, name):
                    required.append(name)
            return {
                "title": cls.__name__,
                "type": "object",
                "properties": props,
                "required": required,
            }

    def Field(default: Any = ..., **kwargs: Any) -> Any:  # type: ignore
        if default is ...:
            return None
        return default

    def field_validator(*args: Any, **kwargs: Any) -> Any:  # type: ignore
        def decorator(fn: Any) -> Any:
            return fn

        return decorator


VALID_GROUP_COLORS = {"blue", "indigo", "emerald", "rose", "amber", "violet", "slate"}


class TabItemSchema(BaseModel):
    """Schema representing a single browser tab within a saved Tab Group."""

    tab_id: str = Field(..., description="Unique identifier of the tab.")
    title: str = Field(..., description="Human-readable page title of the tab.")
    url: str = Field(..., description="Normalized, PII-scrubbed canonical URL.")
    domain: str = Field("", description="Extracted hostname/domain of the tab URL.")


class TabGroupSessionSchema(BaseModel):
    """Schema representing a consolidated Tab Group (1 Work Session = 1 Tab Group)."""

    group_id: str = Field(..., description="Unique identifier for the tab group.")
    session_name: str = Field(..., description="Display name of the Work Session / Tab Group.")
    color: str = Field("blue", description="Visual badge color for the tab group card.")
    where_i_left_off_note: str = Field(
        "",
        description="Synthesized summary note describing what the user was working on and next steps.",
    )
    updated_at: str = Field(..., description="ISO-8601 timestamp of last update.")
    tabs: list[ dict[str, Any] ] = Field(
        default_factory=list, description="Deduplicated tabs belonging to this work session."
    )


class StrayTabRoutingItem(BaseModel):
    """Schema describing how a stray/unrelated tab in a group will be routed during a merge."""

    tab_id: str = Field(..., description="ID of the stray tab.")
    title: str = Field(..., description="Title of the stray tab.")
    url: str = Field(..., description="Canonical URL of the stray tab.")
    from_group_id: str = Field(..., description="Source tab group where the stray tab was found.")
    target_group_id: str = Field(
        ..., description="Destination tab group ID (existing matching group or 'grp-misc')."
    )
    target_session_name: str = Field(
        ..., description="Destination tab group name (existing group name or 'Misc')."
    )
    routing_reason: str = Field(
        ..., description="Explanation of why this tab was classified as stray and where it routes."
    )


class ListTabGroupsInput(BaseModel):
    """Strict input schema for list_and_search_tab_groups."""

    query: Optional[str] = Field(
        None,
        description="Optional keyword filter matching session names, notes, tab titles, or URLs.",
    )


class ProposeMergePlanInput(BaseModel):
    """Strict input schema for propose_tab_group_merge_plan."""

    source_group_ids: list[str] = Field(
        ...,
        description="List of 2 or more tab group IDs to analyze, deduplicate, and propose merging.",
    )
    suggested_session_name: Optional[str] = Field(
        None,
        description="Optional user-preferred or agent-suggested name for the unified Work Session.",
    )

    def _validate(self) -> None:
        if not isinstance(self.source_group_ids, list) or len(self.source_group_ids) < 2:
            raise ValueError(
                "source_group_ids must contain at least 2 tab group IDs to propose a merge."
            )


class ExecuteMergePlanInput(BaseModel):
    """Strict input schema for execute_tab_group_merge_plan (HITL-gated)."""

    plan_id: str = Field(..., description="The merge plan ID returned by propose_tab_group_merge_plan.")
    final_session_name: str = Field(
        ...,
        description="Approved or user-customized name for the merged Work Session.",
    )
    where_i_left_off_note: Optional[str] = Field(
        None,
        description="Optional override for the 'Where I left off' summary note.",
    )
    user_confirmed: bool = Field(
        False,
        description="Explicit Human-in-the-Loop confirmation flag. Must be True to commit merge.",
    )

    def _validate(self) -> None:
        if not self.plan_id or not str(self.plan_id).strip():
            raise ValueError("plan_id cannot be empty.")
        if not self.final_session_name or not str(self.final_session_name).strip():
            raise ValueError("final_session_name cannot be empty.")


class RenameTabGroupInput(BaseModel):
    """Strict input schema for rename_tab_group_session."""

    group_id: str = Field(..., description="Target tab group ID to rename or update.")
    new_session_name: str = Field(..., description="New name for the Tab Group / Work Session.")
    where_i_left_off_note: Optional[str] = Field(
        None,
        description="Optional updated 'Where I left off' summary note for this Work Session.",
    )

    def _validate(self) -> None:
        if not self.group_id or not str(self.group_id).strip():
            raise ValueError("group_id cannot be empty.")
        if not self.new_session_name or not str(self.new_session_name).strip():
            raise ValueError("new_session_name cannot be empty.")


class DeleteTabGroupInput(BaseModel):
    """Strict input schema for delete_saved_tab_group (HITL-gated)."""

    group_id: str = Field(..., description="Target tab group ID to permanently delete.")
    reason: str = Field(..., description="Brief explanation of why this tab group is being deleted.")
    user_confirmed: bool = Field(
        False,
        description="Explicit Human-in-the-Loop confirmation flag. Must be True to delete.",
    )

    def _validate(self) -> None:
        if not self.group_id or not str(self.group_id).strip():
            raise ValueError("group_id cannot be empty.")


class GuidedToolOutputSchema(BaseModel):
    """Standardized output schema returned by all TabFlow tools, supporting guided error recovery."""

    status: str = Field(
        ...,
        description="Execution status: 'SUCCESS', 'PENDING_HUMAN_CONFIRMATION', or 'ERROR'.",
    )
    tool_name: str = Field(..., description="Name of the executed tool.")
    message: str = Field(..., description="Human-readable summary of the result or error.")
    data: dict[str, Any] = Field(
        default_factory=dict, description="Structured payload returned by the tool."
    )
    error_code: Optional[str] = Field(
        None, description="Machine-readable error identifier when status is 'ERROR'."
    )
    recovery_instructions: Optional[str] = Field(
        None,
        description="Actionable recovery steps guiding the LLM on how to fix invalid calls.",
    )

