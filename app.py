"""Modern Light Web Application Server and REST API for TabFlow Work Session Agent.

Serves the responsive Option 2 Web UI (`web/index.html`, `web/styles.css`, `web/app.js`)
and exposes JSON API endpoints wired directly to the ADK Multi-Agent Orchestrator and
Pydantic-validated tools.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tab_session_agent.agent import orchestrator
from tab_session_agent.chrome_profile_reader import (
    parse_saved_groups_from_named_buffers,
    read_closed_and_saved_tab_groups_from_profile,
)
from tab_session_agent.observability import observability_hub
from tab_session_agent.tools import (
    auto_discover_and_propose_merges,
    delete_saved_tab_group,
    execute_tab_group_merge_plan,
    get_database,
    list_and_search_tab_groups,
    propose_tab_group_merge_plan,
    rename_tab_group_session,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")


def build_board_snapshot(query: str = "") -> dict[str, Any]:
    """Build complete UI state snapshot including tab groups, pending HITL plans, and telemetry."""
    db = get_database()
    res = list_and_search_tab_groups(query=query or None)
    data = res.get("data", {})
    return {
        "tab_groups": data.get("tab_groups", []),
        "total_groups": data.get("total_groups", 0),
        "total_tabs": data.get("total_tabs", 0),
        "pending_merge_plans": db.list_pending_merge_plans(),
        "memory_profile": db.get_user_memory_profile(),
        "telemetry": {
            "pii_redactions_count": observability_hub.scrubber.redaction_count,
            "async_memory_tasks_completed": db.async_memory_tasks_completed,
            "recent_logs": observability_hub.recent_logs[-20:][::-1],
            "recent_spans": observability_hub.recent_spans[-15:][::-1],
        },
    }


class TabFlowRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler serving static Web UI assets and JSON Agent API routes."""

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress default noisy stderr access logs in favor of structured JSON logging
        return

    def _send_json(self, payload: dict[str, Any], status_code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        if path == "/api/state":
            params = dict(urllib.parse.parse_qsl(parsed.query))
            query = params.get("q", "")
            self._send_json({"status": "SUCCESS", **build_board_snapshot(query)})
            return

        # Serve static files from web/
        if path == "/" or path == "":
            file_path = os.path.join(WEB_DIR, "index.html")
        else:
            safe_rel = os.path.normpath(path.lstrip("/"))
            file_path = os.path.join(WEB_DIR, safe_rel)

        if not os.path.abspath(file_path).startswith(os.path.abspath(WEB_DIR)) or not os.path.isfile(file_path):
            self._send_json({"status": "ERROR", "message": "Not found"}, status_code=404)
            return

        mime_type, _ = mimetypes.guess_type(file_path)
        with open(file_path, "rb") as f:
            content = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mime_type or "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        body = self._read_json_body()

        if path == "/api/chat":
            message = str(body.get("message", "")).strip()
            session_id = str(body.get("session_id", "web-ui-session"))
            turn_result = orchestrator.handle_user_turn(
                user_message=message,
                session_id=session_id,
            )
            self._send_json(
                {
                    "status": turn_result["status"],
                    "turn": turn_result,
                    "board": build_board_snapshot(),
                }
            )
            return

        if path == "/api/propose-auto-merges":
            turn_result = orchestrator.handle_user_turn(
                user_message="Organize and propose merge plans for my overlapping saved tab groups",
                session_id="web-ui-session",
            )
            self._send_json(
                {
                    "status": "SUCCESS",
                    "turn": turn_result,
                    "board": build_board_snapshot(),
                }
            )
            return

        if path == "/api/propose-custom-merge":
            source_ids = body.get("source_group_ids", [])
            suggested_name = body.get("suggested_session_name")
            res = propose_tab_group_merge_plan(
                source_group_ids=source_ids,
                suggested_session_name=suggested_name,
            )
            self._send_json({"result": res, "board": build_board_snapshot()})
            return

        if path == "/api/execute-merge":
            plan_id = str(body.get("plan_id", ""))
            final_name = str(body.get("final_session_name", ""))
            note = body.get("where_i_left_off_note")
            confirmed = bool(body.get("user_confirmed", False))
            res = execute_tab_group_merge_plan(
                plan_id=plan_id,
                final_session_name=final_name,
                where_i_left_off_note=note,
                user_confirmed=confirmed,
            )
            self._send_json({"result": res, "board": build_board_snapshot()})
            return

        if path == "/api/rename-group":
            group_id = str(body.get("group_id", ""))
            new_name = str(body.get("new_session_name", ""))
            note = body.get("where_i_left_off_note")
            res = rename_tab_group_session(
                group_id=group_id,
                new_session_name=new_name,
                where_i_left_off_note=note,
            )
            self._send_json({"result": res, "board": build_board_snapshot()})
            return

        if path == "/api/delete-group":
            group_id = str(body.get("group_id", ""))
            reason = str(body.get("reason", "User requested deletion from Web UI"))
            confirmed = bool(body.get("user_confirmed", False))
            res = delete_saved_tab_group(
                group_id=group_id,
                reason=reason,
                user_confirmed=confirmed,
            )
            self._send_json({"result": res, "board": build_board_snapshot()})
            return

        if path == "/api/sync-chrome":
            ext_groups = list(body.get("groups", []))
            # Also read closed Saved Tab Groups from Chrome's local Sync Data LevelDB if accessible
            disk_saved_groups = read_closed_and_saved_tab_groups_from_profile()
            open_names = {
                str(g.get("session_name", "")).strip().lower() for g in ext_groups
            }
            closed_from_disk = [
                g
                for g in disk_saved_groups
                if str(g.get("session_name", "")).strip().lower() not in open_names
            ]
            combined = ext_groups + closed_from_disk

            db = get_database()
            synced = db.replace_with_live_chrome_groups(combined)
            observability_hub.log_event(
                event_type="LIVE_CHROME_SYNC",
                message=(
                    f"Synced {len(synced)} group(s) ({len(ext_groups)} from extension, "
                    f"{len(closed_from_disk)} closed Saved Tab Group(s) from Chrome LevelDB)"
                ),
                metadata={
                    "synced_group_count": len(synced),
                    "extension_groups": len(ext_groups),
                    "closed_disk_groups": len(closed_from_disk),
                },
            )
            self._send_json(
                {
                    "status": "SUCCESS",
                    "synced_count": len(synced),
                    "closed_disk_count": len(closed_from_disk),
                    "board": build_board_snapshot(),
                }
            )
            return

        if path == "/api/import-leveldb":
            # Accepts base64-encoded .ldb / .log files selected from ~/.config/google-chrome/<Profile>/Sync Data/LevelDB
            raw_files = body.get("files", [])
            named_buffers: list[tuple[str, bytes]] = []
            for item in raw_files:
                fname = str(item.get("name", ""))
                b64 = str(item.get("content_b64", ""))
                if fname and b64:
                    try:
                        named_buffers.append((fname, base64.b64decode(b64)))
                    except Exception:
                        pass

            parsed_groups = parse_saved_groups_from_named_buffers(
                named_buffers, profile_label="Chrome Sync LevelDB"
            )
            db = get_database()
            all_groups = db.upsert_multiple_tab_groups(parsed_groups)
            observability_hub.log_event(
                event_type="LEVELDB_SAVED_GROUPS_IMPORT",
                message=f"Decoded {len(parsed_groups)} Saved Tab Group(s) (open & closed) from Chrome LevelDB",
                metadata={"imported_saved_groups": len(parsed_groups)},
            )
            self._send_json(
                {
                    "status": "SUCCESS",
                    "imported_count": len(parsed_groups),
                    "board": build_board_snapshot(),
                }
            )
            return

        if path == "/api/reset-demo":
            db = get_database()
            db.reset_to_seed_data()
            observability_hub.log_event(
                event_type="DEMO_DATA_RESET",
                message="Reset Tab Groups database back to initial seed_tab_groups.json",
            )
            self._send_json({"status": "SUCCESS", "board": build_board_snapshot()})
            return

        self._send_json({"status": "ERROR", "message": "Unknown endpoint"}, status_code=404)


def run_server(port: int = 8080) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", port), TabFlowRequestHandler)
    print(f"TabFlow Work Session Manager UI running at http://localhost:{port}")
    server.serve_forever()


if __name__ == "__main__":
    port = int(os.environ.get("TABFLOW_PORT", "8080"))
    run_server(port=port)

