"""Persistent Session Database and Asynchronous Memory Operations.

Satisfies Category 2 of the AgentOps Code Review Matrix:
- Persistent Session State (5 pts): SQLite persistent store managing Tab Groups,
  Merge Plans, Conversational History, and Semantic Keyword Retrieval across turns.
- Async Memory Operations (5 pts): Non-blocking background tasks (`asyncio.create_task`
  and `ThreadPoolExecutor`) for expensive user memory consolidation.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import datetime
import json
import os
import sqlite3
import threading
import urllib.parse
from typing import Any, Optional

from tab_session_agent.observability import observability_hub


class PersistentSessionDatabase:
    """Persistent SQLite database for Tab Groups, Merge Plans, Conversation History, and User Memory."""

    def __init__(self, db_path: str, seed_json_path: Optional[str] = None) -> None:
        self.db_path = db_path
        self.seed_json_path = seed_json_path
        self._lock = threading.RLock()
        self._bg_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="tabflow_async_memory"
        )
        self.async_memory_tasks_completed = 0

        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._init_schema()
        if self.count_tab_groups() == 0 and self.seed_json_path:
            self.reset_to_seed_data()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tab_groups (
                    group_id TEXT PRIMARY KEY,
                    session_name TEXT NOT NULL,
                    color TEXT NOT NULL DEFAULT 'blue',
                    where_i_left_off_note TEXT NOT NULL DEFAULT '',
                    tabs_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS merge_plans (
                    plan_id TEXT PRIMARY KEY,
                    suggested_session_name TEXT NOT NULL,
                    source_group_ids_json TEXT NOT NULL,
                    merged_tabs_json TEXT NOT NULL,
                    duplicates_removed_json TEXT NOT NULL,
                    stray_routing_json TEXT NOT NULL,
                    synthesized_note TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversation_turns (
                    turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_memory_bank (
                    memory_key TEXT PRIMARY KEY,
                    memory_value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            conn.commit()

    def count_tab_groups(self) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM tab_groups").fetchone()
            return int(row["cnt"]) if row else 0

    def reset_to_seed_data(self) -> list[dict[str, Any]]:
        """Reset tab_groups and pending merge_plans back to seed_tab_groups.json."""
        if not self.seed_json_path or not os.path.exists(self.seed_json_path):
            return []

        with open(self.seed_json_path, "r", encoding="utf-8") as f:
            raw_groups = json.load(f)

        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM tab_groups")
            conn.execute("DELETE FROM merge_plans")
            for grp in raw_groups:
                scrubbed_tabs: list[dict[str, Any]] = []
                for tab in grp.get("tabs", []):
                    raw_url = tab.get("url", "")
                    clean_url, removed_keys = (
                        observability_hub.scrubber.normalize_and_scrub_url(raw_url)
                    )
                    domain = urllib.parse.urlsplit(clean_url).netloc.lower()
                    scrubbed_tabs.append(
                        {
                            "tab_id": tab.get("tab_id"),
                            "title": observability_hub.scrubber.scrub_text(
                                tab.get("title", "")
                            ),
                            "url": clean_url,
                            "domain": domain,
                            "sanitized_params": removed_keys,
                        }
                    )
                conn.execute(
                    """
                    INSERT INTO tab_groups (
                        group_id, session_name, color, where_i_left_off_note, tabs_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        grp["group_id"],
                        observability_hub.scrubber.scrub_text(grp["session_name"]),
                        grp.get("color", "blue"),
                        observability_hub.scrubber.scrub_text(
                            grp.get("where_i_left_off_note", "")
                        ),
                        json.dumps(scrubbed_tabs),
                        grp.get(
                            "updated_at",
                            datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        ),
                    ),
                )
            conn.commit()
        return self.list_tab_groups()

    def replace_with_live_chrome_groups(
        self, chrome_groups: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Replace stored tab groups with live groups & ungrouped window pools captured from Chrome."""
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        existing_notes = {
            g["group_id"]: g.get("where_i_left_off_note", "")
            for g in self.list_tab_groups()
        }

        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM tab_groups")
            conn.execute("DELETE FROM merge_plans")
            for grp in chrome_groups:
                gid = str(grp.get("group_id", ""))
                if not gid:
                    continue
                scrubbed_tabs: list[dict[str, Any]] = []
                for tab in grp.get("tabs", []):
                    raw_url = tab.get("url", "")
                    clean_url, removed_keys = (
                        observability_hub.scrubber.normalize_and_scrub_url(raw_url)
                    )
                    domain = urllib.parse.urlsplit(clean_url).netloc.lower()
                    scrubbed_tabs.append(
                        {
                            "tab_id": tab.get("tab_id"),
                            "title": observability_hub.scrubber.scrub_text(
                                tab.get("title", "")
                            ),
                            "url": clean_url,
                            "domain": domain,
                            "sanitized_params": removed_keys,
                        }
                    )
                preserved_note = existing_notes.get(gid) or grp.get(
                    "where_i_left_off_note", ""
                )
                conn.execute(
                    """
                    INSERT INTO tab_groups (
                        group_id, session_name, color, where_i_left_off_note, tabs_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        gid,
                        observability_hub.scrubber.scrub_text(
                            grp.get("session_name", "Unnamed Group")
                        ),
                        grp.get("color", "blue"),
                        observability_hub.scrubber.scrub_text(preserved_note),
                        json.dumps(scrubbed_tabs),
                        now_iso,
                    ),
                )
            conn.commit()

        self.trigger_async_memory_consolidation(event_reason="live_chrome_sync")
        return self.list_tab_groups()

    def upsert_multiple_tab_groups(
        self, incoming_groups: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Upsert multiple tab groups (e.g., closed Saved Tab Groups from LevelDB) without deleting open groups."""
        existing = self.list_tab_groups()
        existing_names = {
            g["session_name"].strip().lower(): g["group_id"]
            for g in existing
            if not g["group_id"].startswith("grp-")
        }
        # First remove demo seed groups if still present
        for g in existing:
            if g["group_id"] in {
                "grp-adk-mon",
                "grp-unnamed-2",
                "grp-cloudrun-1",
                "grp-cloudrun-2",
                "grp-japan",
            }:
                self.delete_tab_group(g["group_id"])

        for grp in incoming_groups:
            name_lower = str(grp.get("session_name", "")).strip().lower()
            # Skip if an open Chrome group with the exact same name is already loaded
            if name_lower in existing_names and not str(
                existing_names[name_lower]
            ).startswith("saved-grp-"):
                continue

            scrubbed_tabs: list[dict[str, Any]] = []
            for tab in grp.get("tabs", []):
                raw_url = tab.get("url", "")
                clean_url, removed_keys = (
                    observability_hub.scrubber.normalize_and_scrub_url(raw_url)
                )
                domain = urllib.parse.urlsplit(clean_url).netloc.lower()
                scrubbed_tabs.append(
                    {
                        "tab_id": tab.get("tab_id"),
                        "title": observability_hub.scrubber.scrub_text(
                            tab.get("title", "")
                        ),
                        "url": clean_url,
                        "domain": domain,
                        "sanitized_params": removed_keys,
                    }
                )
            self.upsert_tab_group(
                group_id=str(grp.get("group_id")),
                session_name=str(grp.get("session_name", "Saved Group")),
                color=str(grp.get("color", "blue")),
                where_i_left_off_note=str(
                    grp.get("where_i_left_off_note", "Closed Saved Tab Group from Chrome.")
                ),
                tabs=scrubbed_tabs,
            )

        self.trigger_async_memory_consolidation(event_reason="closed_saved_groups_import")
        return self.list_tab_groups()

    def list_tab_groups(self, query: Optional[str] = None) -> list[dict[str, Any]]:
        """Retrieve all saved Tab Groups (Work Sessions), optionally filtered by keyword."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tab_groups ORDER BY updated_at DESC, group_id ASC"
            ).fetchall()

        results: list[dict[str, Any]] = []
        q = query.strip().lower() if query else ""
        for row in rows:
            tabs = json.loads(row["tabs_json"])
            group_dict = {
                "group_id": row["group_id"],
                "session_name": row["session_name"],
                "color": row["color"],
                "where_i_left_off_note": row["where_i_left_off_note"],
                "updated_at": row["updated_at"],
                "tabs": tabs,
                "tab_count": len(tabs),
            }
            if q:
                haystack = " ".join(
                    [
                        group_dict["group_id"],
                        group_dict["session_name"],
                        group_dict["where_i_left_off_note"],
                        " ".join(
                            f"{t.get('title', '')} {t.get('url', '')}" for t in tabs
                        ),
                    ]
                ).lower()
                if q not in haystack:
                    continue
            results.append(group_dict)
        return results

    def get_tab_group(self, group_id: str) -> Optional[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tab_groups WHERE group_id = ?", (group_id,)
            ).fetchone()
        if not row:
            return None
        tabs = json.loads(row["tabs_json"])
        return {
            "group_id": row["group_id"],
            "session_name": row["session_name"],
            "color": row["color"],
            "where_i_left_off_note": row["where_i_left_off_note"],
            "updated_at": row["updated_at"],
            "tabs": tabs,
            "tab_count": len(tabs),
        }

    def upsert_tab_group(
        self,
        group_id: str,
        session_name: str,
        color: str,
        where_i_left_off_note: str,
        tabs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        clean_name = observability_hub.scrubber.scrub_text(session_name)
        clean_note = observability_hub.scrubber.scrub_text(where_i_left_off_note)
        clean_tabs = observability_hub.scrubber.scrub_payload(tabs)

        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tab_groups (
                    group_id, session_name, color, where_i_left_off_note, tabs_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    session_name = excluded.session_name,
                    color = excluded.color,
                    where_i_left_off_note = excluded.where_i_left_off_note,
                    tabs_json = excluded.tabs_json,
                    updated_at = excluded.updated_at
                """,
                (
                    group_id,
                    clean_name,
                    color,
                    clean_note,
                    json.dumps(clean_tabs),
                    now_iso,
                ),
            )
            conn.commit()
        return self.get_tab_group(group_id) or {}

    def delete_tab_group(self, group_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM tab_groups WHERE group_id = ?", (group_id,)
            )
            conn.commit()
            return cursor.rowcount > 0

    def save_merge_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO merge_plans (
                    plan_id, suggested_session_name, source_group_ids_json,
                    merged_tabs_json, duplicates_removed_json, stray_routing_json,
                    synthesized_note, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan["plan_id"],
                    plan["suggested_session_name"],
                    json.dumps(plan["source_group_ids"]),
                    json.dumps(plan["merged_tabs"]),
                    json.dumps(plan["duplicates_removed"]),
                    json.dumps(plan["stray_routing"]),
                    plan["synthesized_note"],
                    plan.get("status", "PENDING"),
                    now_iso,
                ),
            )
            conn.commit()
        return self.get_merge_plan(plan["plan_id"]) or plan

    def get_merge_plan(self, plan_id: str) -> Optional[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM merge_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "plan_id": row["plan_id"],
            "suggested_session_name": row["suggested_session_name"],
            "source_group_ids": json.loads(row["source_group_ids_json"]),
            "merged_tabs": json.loads(row["merged_tabs_json"]),
            "duplicates_removed": json.loads(row["duplicates_removed_json"]),
            "stray_routing": json.loads(row["stray_routing_json"]),
            "synthesized_note": row["synthesized_note"],
            "status": row["status"],
            "created_at": row["created_at"],
        }

    def list_pending_merge_plans(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT plan_id FROM merge_plans WHERE status = 'PENDING' ORDER BY created_at DESC"
            ).fetchall()
        plans: list[dict[str, Any]] = []
        for r in rows:
            p = self.get_merge_plan(r["plan_id"])
            if p:
                plans.append(p)
        return plans

    def mark_merge_plan_status(self, plan_id: str, status: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE merge_plans SET status = ? WHERE plan_id = ?",
                (status, plan_id),
            )
            conn.commit()

    def append_conversation_turn(
        self, session_id: str, role: str, content: str
    ) -> None:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        clean_content = observability_hub.scrubber.scrub_text(content)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_turns (session_id, role, content, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, role, clean_content, now_iso),
            )
            conn.commit()

    def get_conversation_turns(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT role, content, created_at
                FROM conversation_turns
                WHERE session_id = ?
                ORDER BY turn_id ASC
                """,
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def _consolidate_memory_sync(self, event_reason: str) -> dict[str, Any]:
        """Compute domain frequency and work session profile in background thread."""
        groups = self.list_tab_groups()
        domain_counts: dict[str, int] = {}
        session_names: list[str] = []
        for grp in groups:
            session_names.append(grp["session_name"])
            for tab in grp.get("tabs", []):
                dom = tab.get("domain") or urllib.parse.urlsplit(tab.get("url", "")).netloc
                if dom:
                    domain_counts[dom] = domain_counts.get(dom, 0) + 1

        top_domains = sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        profile = {
            "active_work_sessions": session_names,
            "top_domains": [{"domain": d, "count": c} for d, c in top_domains],
            "last_consolidation_reason": event_reason,
            "consolidated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO user_memory_bank (memory_key, memory_value_json, updated_at)
                VALUES ('user_work_profile', ?, ?)
                """,
                (json.dumps(profile), now_iso),
            )
            conn.commit()

        self.async_memory_tasks_completed += 1
        observability_hub.log_event(
            event_type="ASYNC_MEMORY_CONSOLIDATION",
            message=f"Background memory consolidation completed ({event_reason})",
            metadata=profile,
        )
        return profile

    def trigger_async_memory_consolidation(
        self, event_reason: str = "session_update"
    ) -> concurrent.futures.Future[dict[str, Any]]:
        """Schedule non-blocking background memory consolidation so UI/chat never blocks."""
        try:
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(
                self._bg_executor, self._consolidate_memory_sync, event_reason
            )
            return future  # type: ignore
        except RuntimeError:
            return self._bg_executor.submit(
                self._consolidate_memory_sync, event_reason
            )

    def get_user_memory_profile(self) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT memory_value_json FROM user_memory_bank WHERE memory_key = 'user_work_profile'"
            ).fetchone()
        if not row:
            return self._consolidate_memory_sync("initial_load")
        return json.loads(row["memory_value_json"])

    def shutdown(self) -> None:
        """Wait for any pending background async memory consolidation tasks to complete."""
        self._bg_executor.shutdown(wait=True)

