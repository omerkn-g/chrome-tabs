"""Pure-Python Reader for Chrome's Closed & Open 'Saved Tab Groups' from LevelDB.

Why this exists:
Chrome's `chrome.tabGroups.query({})` extension API only returns Tab Groups that are
currently OPEN in a browser window. Closed "Saved Tab Groups" (which live in Chrome's
bookmarks bar / tab groups menu when closed) are persisted by Chromium's
`SavedTabGroupSyncBridge` inside:
    ~/.config/google-chrome/<Profile>/Sync Data/LevelDB/*.ldb and *.log

Each LevelDB entry with user key `saved_tab_group-dt-<guid>` stores a serialized
`sync_pb.SavedTabGroupSpecifics` protobuf containing either:
- `group` (field 4): `position` (1), `title` (2), `color` (3)
- `tab`   (field 5): `group_guid` (1), `position` (2), `url` (3), `title` (4)

This module implements an exact, read-only LevelDB SSTable (`.ldb`) and Write-Ahead Log
(`.log`) parser with Snappy decompression and prefix-key reconstruction so it can read
100% of closed and open Saved Tab Groups without external C extensions or locking Chrome.
"""

from __future__ import annotations

import glob
import os
import re
import struct
from typing import Any, Iterator, Optional

UUID_RE = re.compile(
    rb"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

PROTO_COLOR_MAP = {
    1: "slate",    # GREY
    2: "blue",     # BLUE
    3: "rose",     # RED
    4: "amber",    # YELLOW
    5: "emerald",  # GREEN
    6: "rose",     # PINK
    7: "violet",   # PURPLE
    8: "indigo",   # CYAN
    9: "amber",    # ORANGE
}

LEVELDB_MAGIC = b"\x57\xfb\x80\x8b\x24\x75\x47\xdb"
WAL_BLOCK_SIZE = 32768


def _read_varint(buf: bytes, offset: int, limit: int) -> tuple[int, int]:
    """Read a base-128 varint from `buf[offset:limit]`, returning (value, new_offset)."""
    result = 0
    shift = 0
    pos = offset
    while pos < limit:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return result, pos
        shift += 7
        if shift > 64:
            raise ValueError("Varint overflow")
    raise ValueError("Truncated varint")


def snappy_decompress(compressed: bytes) -> bytes:
    """Pure-Python Snappy block decompressor for LevelDB SSTable blocks."""
    limit = len(compressed)
    if limit == 0:
        return b""
    uncompressed_len, pos = _read_varint(compressed, 0, limit)
    out = bytearray()

    while pos < limit:
        tag = compressed[pos]
        pos += 1
        tag_type = tag & 0x03

        if tag_type == 0:
            length_code = tag >> 2
            if length_code < 60:
                lit_len = length_code + 1
            else:
                extra_bytes = length_code - 59
                if pos + extra_bytes > limit:
                    break
                lit_len = int.from_bytes(compressed[pos : pos + extra_bytes], "little") + 1
                pos += extra_bytes
            if pos + lit_len > limit:
                break
            out.extend(compressed[pos : pos + lit_len])
            pos += lit_len
        else:
            if tag_type == 1:
                copy_len = ((tag >> 2) & 0x07) + 4
                if pos >= limit:
                    break
                copy_offset = ((tag & 0xE0) << 3) | compressed[pos]
                pos += 1
            elif tag_type == 2:
                copy_len = (tag >> 2) + 1
                if pos + 2 > limit:
                    break
                copy_offset = int.from_bytes(compressed[pos : pos + 2], "little")
                pos += 2
            else:
                copy_len = (tag >> 2) + 1
                if pos + 4 > limit:
                    break
                copy_offset = int.from_bytes(compressed[pos : pos + 4], "little")
                pos += 4

            if copy_offset <= 0 or copy_offset > len(out):
                break
            start = len(out) - copy_offset
            for i in range(copy_len):
                out.append(out[start + i])

    return bytes(out[:uncompressed_len])


def _iter_leveldb_block_entries(block: bytes) -> Iterator[tuple[bytes, bytes]]:
    """Iterate `(key, value)` pairs inside a LevelDB block with prefix decompression."""
    if len(block) < 4:
        return
    num_restarts = struct.unpack_from("<I", block, len(block) - 4)[0]
    restarts_byte_len = 4 * (num_restarts + 1)
    if restarts_byte_len > len(block):
        return
    entries_limit = len(block) - restarts_byte_len

    pos = 0
    prev_key = b""
    while pos < entries_limit:
        try:
            shared, pos = _read_varint(block, pos, entries_limit)
            unshared, pos = _read_varint(block, pos, entries_limit)
            val_len, pos = _read_varint(block, pos, entries_limit)
        except ValueError:
            break
        if pos + unshared + val_len > entries_limit:
            break
        key_delta = block[pos : pos + unshared]
        pos += unshared
        val = block[pos : pos + val_len]
        pos += val_len

        key = prev_key[:shared] + key_delta
        prev_key = key
        yield key, val


def _read_sstable_block(raw: bytes, offset: int, size: int) -> Optional[bytes]:
    """Read and optionally Snappy-decompress a block at `(offset, size)` in an `.ldb` file."""
    if offset < 0 or size < 0 or offset + size + 5 > len(raw):
        return None
    block_payload = raw[offset : offset + size]
    compression_type = raw[offset + size]
    if compression_type == 0:
        return block_payload
    if compression_type == 1:
        try:
            return snappy_decompress(block_payload)
        except Exception:
            return None
    return None


def iter_sstable_records(raw: bytes) -> Iterator[tuple[int, int, bytes, bytes]]:
    """Yield `(seq_num, val_type, user_key, value)` from a LevelDB `.ldb`/`.sst` buffer."""
    if len(raw) < 48 or raw[-8:] != LEVELDB_MAGIC:
        return
    footer = raw[-48:-8]
    try:
        _, pos = _read_varint(footer, 0, len(footer))
        _, pos = _read_varint(footer, pos, len(footer))
        idx_offset, pos = _read_varint(footer, pos, len(footer))
        idx_size, _ = _read_varint(footer, pos, len(footer))
    except ValueError:
        return

    index_block = _read_sstable_block(raw, idx_offset, idx_size)
    if not index_block:
        return

    for _, handle_bytes in _iter_leveldb_block_entries(index_block):
        try:
            b_offset, p = _read_varint(handle_bytes, 0, len(handle_bytes))
            b_size, _ = _read_varint(handle_bytes, p, len(handle_bytes))
        except ValueError:
            continue
        data_block = _read_sstable_block(raw, b_offset, b_size)
        if not data_block:
            continue
        for internal_key, val in _iter_leveldb_block_entries(data_block):
            if len(internal_key) < 8:
                continue
            user_key = internal_key[:-8]
            tag = struct.unpack_from("<Q", internal_key, len(internal_key) - 8)[0]
            val_type = tag & 0xFF
            seq_num = tag >> 8
            yield seq_num, val_type, user_key, val


def iter_wal_log_records(raw: bytes) -> Iterator[tuple[int, int, bytes, bytes]]:
    """Yield `(seq_num, val_type, user_key, value)` from a LevelDB `.log` Write-Ahead Log."""
    batches: list[bytes] = []
    cur_frag = bytearray()
    block_offset = 0
    limit = len(raw)

    while block_offset < limit:
        block_end = min(limit, block_offset + WAL_BLOCK_SIZE)
        pos = block_offset
        while pos + 7 <= block_end:
            rec_len = struct.unpack_from("<H", raw, pos + 4)[0]
            rec_type = raw[pos + 6]
            pos += 7
            if rec_type == 0 or pos + rec_len > block_end:
                break
            fragment = raw[pos : pos + rec_len]
            pos += rec_len
            if rec_type == 1:
                batches.append(fragment)
                cur_frag.clear()
            elif rec_type == 2:
                cur_frag = bytearray(fragment)
            elif rec_type == 3:
                cur_frag.extend(fragment)
            elif rec_type == 4:
                cur_frag.extend(fragment)
                batches.append(bytes(cur_frag))
                cur_frag.clear()
        block_offset += WAL_BLOCK_SIZE

    for batch in batches:
        if len(batch) < 12:
            continue
        base_seq = struct.unpack_from("<Q", batch, 0)[0]
        count = struct.unpack_from("<I", batch, 8)[0]
        pos = 12
        b_limit = len(batch)
        for i in range(count):
            if pos >= b_limit:
                break
            val_type = batch[pos]
            pos += 1
            try:
                k_len, pos = _read_varint(batch, pos, b_limit)
            except ValueError:
                break
            if pos + k_len > b_limit:
                break
            user_key = batch[pos : pos + k_len]
            pos += k_len
            if val_type == 1:
                try:
                    v_len, pos = _read_varint(batch, pos, b_limit)
                except ValueError:
                    break
                if pos + v_len > b_limit:
                    break
                val = batch[pos : pos + v_len]
                pos += v_len
                yield base_seq + i, 1, user_key, val
            elif val_type == 0:
                yield base_seq + i, 0, user_key, b""
            else:
                break


def parse_protobuf_fields(buf: bytes) -> dict[int, list[Any]]:
    """Decode wire-format protobuf fields into `{field_number: [values]}`."""
    fields: dict[int, list[Any]] = {}
    pos = 0
    limit = len(buf)
    while pos < limit:
        try:
            tag, pos = _read_varint(buf, pos, limit)
        except ValueError:
            break
        if tag == 0:
            break
        field_num = tag >> 3
        wire_type = tag & 0x07
        if field_num <= 0 or field_num > 100:
            break

        if wire_type == 0:
            try:
                val, pos = _read_varint(buf, pos, limit)
            except ValueError:
                break
            fields.setdefault(field_num, []).append(val)
        elif wire_type == 1:
            if pos + 8 > limit:
                break
            fields.setdefault(field_num, []).append(buf[pos : pos + 8])
            pos += 8
        elif wire_type == 2:
            try:
                length, pos = _read_varint(buf, pos, limit)
            except ValueError:
                break
            if length < 0 or pos + length > limit:
                break
            fields.setdefault(field_num, []).append(buf[pos : pos + length])
            pos += length
        elif wire_type == 5:
            if pos + 4 > limit:
                break
            fields.setdefault(field_num, []).append(buf[pos : pos + 4])
            pos += 4
        else:
            break
    return fields


def decode_saved_tab_group_specifics(raw_bytes: bytes) -> Optional[dict[str, Any]]:
    """Decode a `SavedTabGroupSpecifics` protobuf (direct or wrapped in DataTypeStore proto)."""
    fields = parse_protobuf_fields(raw_bytes)

    if 1 not in fields or not any(
        isinstance(v, bytes) and len(v) == 36 and UUID_RE.match(v)
        for v in fields.get(1, [])
    ):
        for val_list in fields.values():
            for v in val_list:
                if isinstance(v, bytes) and b"\x0a\x24" in v:
                    nested = decode_saved_tab_group_specifics(v)
                    if nested:
                        return nested
        idx = raw_bytes.find(b"\x0a\x24")
        if idx > 0:
            return decode_saved_tab_group_specifics(raw_bytes[idx:])
        return None

    guid_bytes = next(
        (
            v
            for v in fields.get(1, [])
            if isinstance(v, bytes) and len(v) == 36 and UUID_RE.match(v)
        ),
        None,
    )
    if not guid_bytes:
        return None

    guid = guid_bytes.decode("ascii")
    update_time = int(fields.get(3, fields.get(2, [0]))[0])

    # Field 4: SavedTabGroup { position=1, title=2, color=3 }
    if 4 in fields and isinstance(fields[4][0], bytes):
        grp_fields = parse_protobuf_fields(fields[4][0])
        title_bytes = grp_fields.get(2, [b""])[0]
        title = (
            title_bytes.decode("utf-8", errors="replace").strip()
            if isinstance(title_bytes, bytes)
            else ""
        )
        color_raw = grp_fields.get(3, [2])[0]
        color_id = int(color_raw) if isinstance(color_raw, int) else 2
        pos_raw = grp_fields.get(1, [0])[0]
        position = int(pos_raw) if isinstance(pos_raw, int) else 0
        return {
            "kind": "group",
            "guid": guid,
            "update_time": update_time,
            "title": title,
            "color": PROTO_COLOR_MAP.get(color_id, "blue"),
            "position": position,
        }

    # Field 5: SavedTabGroupTab { group_guid=1, position=2, url=3, title=4 }
    if 5 in fields and isinstance(fields[5][0], bytes):
        tab_fields = parse_protobuf_fields(fields[5][0])
        group_guid_bytes = tab_fields.get(1, [b""])[0]
        if not isinstance(group_guid_bytes, bytes) or not UUID_RE.match(group_guid_bytes):
            return None
        url_bytes = tab_fields.get(3, [b""])[0]
        title_bytes = tab_fields.get(4, [b""])[0]
        url = (
            url_bytes.decode("utf-8", errors="replace").strip()
            if isinstance(url_bytes, bytes)
            else ""
        )
        title = (
            title_bytes.decode("utf-8", errors="replace").strip()
            if isinstance(title_bytes, bytes)
            else url
        )
        pos_raw = tab_fields.get(2, [0])[0]
        position = int(pos_raw) if isinstance(pos_raw, int) else 0
        if not url:
            return None
        return {
            "kind": "tab",
            "guid": guid,
            "group_guid": group_guid_bytes.decode("ascii"),
            "update_time": update_time,
            "url": url,
            "title": title or url,
            "position": position,
        }

    return None


def parse_saved_groups_from_named_buffers(
    named_buffers: list[tuple[str, bytes]],
    profile_label: str = "Default",
) -> list[dict[str, Any]]:
    """Parse `SavedTabGroup` and `SavedTabGroupTab` records from `(filename, bytes)` pairs."""
    kv_state: dict[bytes, tuple[int, int, bytes]] = {}

    for fname, raw in named_buffers:
        iterator = (
            iter_wal_log_records(raw)
            if fname.endswith(".log")
            else iter_sstable_records(raw)
        )
        for seq_num, val_type, user_key, val in iterator:
            if b"saved_tab_group" not in user_key and b"\x0a\x24" not in val:
                continue
            prev = kv_state.get(user_key)
            if prev is None or seq_num >= prev[0]:
                kv_state[user_key] = (seq_num, val_type, val)

    groups_map: dict[str, dict[str, Any]] = {}
    tabs_by_group: dict[str, list[dict[str, Any]]] = {}

    for user_key, (seq_num, val_type, val) in kv_state.items():
        if val_type == 0:
            continue
        if not user_key.startswith(b"saved_tab_group-dt-"):
            continue
        parsed = decode_saved_tab_group_specifics(val)
        if not parsed:
            continue
        if parsed["kind"] == "group":
            groups_map[parsed["guid"]] = parsed
        elif parsed["kind"] == "tab":
            tabs_by_group.setdefault(parsed["group_guid"], []).append(parsed)

    exported: list[dict[str, Any]] = []
    for group_guid, grp in sorted(
        groups_map.items(), key=lambda item: item[1].get("position", 0)
    ):
        group_tabs = sorted(
            tabs_by_group.get(group_guid, []),
            key=lambda t: t.get("position", 0),
        )
        if not group_tabs:
            continue
        title = grp.get("title") or f"Saved Group ({group_guid[:6]})"
        exported.append(
            {
                "group_id": f"saved-grp-{group_guid[:8]}",
                "saved_guid": group_guid,
                "profile": profile_label,
                "session_name": title,
                "color": grp.get("color", "blue"),
                "where_i_left_off_note": f"Saved Tab Group from Chrome ({profile_label}).",
                "tabs": [
                    {
                        "tab_id": f"saved-tab-{t['guid'][:8]}",
                        "title": t["title"],
                        "url": t["url"],
                    }
                    for t in group_tabs
                ],
            }
        )
    return exported


def read_closed_and_saved_tab_groups_from_profile(
    chrome_config_root: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Read all Saved Tab Groups (both closed and open) from Chrome's Sync Data LevelDB."""
    if chrome_config_root is None:
        chrome_config_root = os.path.expanduser("~/.config/google-chrome")

    if not os.path.isdir(chrome_config_root):
        return []

    leveldb_dirs = sorted(
        glob.glob(os.path.join(chrome_config_root, "*", "Sync Data", "LevelDB"))
    )
    if not leveldb_dirs:
        return []

    all_exported_groups: list[dict[str, Any]] = []

    for ldb_dir in leveldb_dirs:
        profile_name = os.path.basename(os.path.dirname(os.path.dirname(ldb_dir)))
        files = sorted(
            glob.glob(os.path.join(ldb_dir, "*.ldb"))
            + glob.glob(os.path.join(ldb_dir, "*.sst"))
            + glob.glob(os.path.join(ldb_dir, "*.log"))
        )
        named_buffers: list[tuple[str, bytes]] = []
        for fpath in files:
            try:
                with open(fpath, "rb") as f:
                    named_buffers.append((os.path.basename(fpath), f.read()))
            except OSError:
                continue

        all_exported_groups.extend(
            parse_saved_groups_from_named_buffers(named_buffers, profile_label=profile_name)
        )

    return all_exported_groups
