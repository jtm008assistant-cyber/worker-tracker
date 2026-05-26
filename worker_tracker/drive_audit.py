"""Drive audit — classifies every file in the shared drive as gold/decent/
stale/garbage based on (1) how many workers reference it in Sam's Processes
& Tools tab and (2) when it was last modified on Drive.

Gives the admin a cleanup list: which sheets to keep, which to archive,
which probably nobody is using.

Classification rules:
- gold:    referenced 3+ times by workers OR modified within last 14 days AND referenced ≥1x
- decent:  referenced 1-2 times by workers OR modified within last 30 days
- stale:   not referenced, not modified in 30-180 days
- garbage: not referenced, not modified in 180+ days (cleanup candidate)
- orphan:  in the drive but never mentioned by anyone, ever
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build as gbuild

from . import config, sheets

log = logging.getLogger(__name__)

URL_FILE_ID_RE = re.compile(r"/d/([a-zA-Z0-9_-]+)")


def _drive_service():
    creds = Credentials.from_service_account_file(config.SERVICE_ACCOUNT_JSON, scopes=config.SCOPES)
    return gbuild("drive", "v3", credentials=creds, cache_discovery=False)


def _extract_file_id(url: str) -> str | None:
    m = URL_FILE_ID_RE.search(url or "")
    return m.group(1) if m else None


def _list_drive_files(drive_id: str | None = None) -> list[dict]:
    """List every file in the shared drive (paginated). Returns simplified records."""
    drive_id = drive_id or config.SHARED_DRIVE_ID
    if not drive_id:
        return []
    service = _drive_service()
    files: list[dict] = []
    page_token = None
    while True:
        resp = service.files().list(
            corpora="drive",
            driveId=drive_id,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            q="trashed=false",
            fields="nextPageToken, files(id,name,mimeType,modifiedTime,createdTime,owners(displayName,emailAddress),parents,size)",
            pageSize=200,
            pageToken=page_token,
        ).execute()
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


def _knowledge_references_by_file_id() -> dict[str, list[dict]]:
    """Read Processes & Tools, map file_id → list of worker references."""
    try:
        ws = sheets.open_tracker().worksheet(config.KNOWLEDGE_TAB)
        rows = ws.get_all_records()
    except Exception:
        return {}
    out: dict[str, list[dict]] = {}
    for r in rows:
        url = r.get("URL") or ""
        fid = _extract_file_id(url)
        if not fid:
            continue
        out.setdefault(fid, []).append({
            "worker": r.get("Worker", ""),
            "name": r.get("Name", ""),
            "kind": r.get("Kind", ""),
            "description": r.get("Description", ""),
            "first_mentioned": r.get("First Mentioned", ""),
            "last_updated": r.get("Last Updated", ""),
            "times_referenced": r.get("Times Referenced", 0),
        })
    return out


def _classify(file_record: dict, refs: list[dict] | None) -> tuple[str, str]:
    """Returns (label, reason)."""
    refs = refs or []
    ref_count = sum(int(r.get("times_referenced") or 0) for r in refs)
    worker_count = len({r.get("worker") for r in refs if r.get("worker")})

    # Parse modified time
    mod_str = file_record.get("modifiedTime", "")
    try:
        mod = datetime.fromisoformat(mod_str.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - mod).days
    except Exception:
        age_days = 9999

    if ref_count >= 3 or worker_count >= 2:
        return "gold", f"{worker_count} worker(s) reference it, {ref_count} total mentions"
    if ref_count >= 1 and age_days <= 14:
        return "gold", f"referenced + modified {age_days}d ago"
    if ref_count >= 1:
        return "decent", f"{ref_count} mention(s), last modified {age_days}d ago"
    if age_days <= 30:
        return "decent", f"no worker mentions but modified {age_days}d ago"
    if age_days <= 180:
        return "stale", f"no mentions, untouched {age_days}d"
    return "garbage", f"no mentions, last touched {age_days}d ago — likely abandoned"


def audit(drive_id: str | None = None) -> dict:
    """Run a full audit. Returns dict with classified file lists + counts."""
    files = _list_drive_files(drive_id)
    refs_by_id = _knowledge_references_by_file_id()

    buckets: dict[str, list[dict]] = {"gold": [], "decent": [], "stale": [], "garbage": [], "orphan": []}
    for f in files:
        # Skip folders
        if f.get("mimeType") == "application/vnd.google-apps.folder":
            continue
        fid = f.get("id")
        refs = refs_by_id.get(fid, [])
        # Files in drive but never mentioned anywhere
        label, reason = _classify(f, refs)
        if not refs and label == "garbage":
            label = "orphan"  # rename for clarity in output
        entry = {
            "id": fid,
            "name": f.get("name"),
            "mime": f.get("mimeType"),
            "modified": f.get("modifiedTime"),
            "created": f.get("createdTime"),
            "owners": [o.get("emailAddress") for o in (f.get("owners") or [])],
            "refs": refs,
            "reason": reason,
        }
        buckets[label].append(entry)

    # Sort each bucket by recency (newest first for gold/decent, oldest first for cleanup)
    for label in ("gold", "decent"):
        buckets[label].sort(key=lambda x: x.get("modified") or "", reverse=True)
    for label in ("stale", "garbage", "orphan"):
        buckets[label].sort(key=lambda x: x.get("modified") or "")

    return {
        "total_files": sum(len(v) for v in buckets.values()),
        "buckets": buckets,
    }


def format_audit_summary(audit_result: dict, max_per_bucket: int = 12) -> str:
    """Format the audit dict into a human-readable Slack message."""
    buckets = audit_result["buckets"]
    total = audit_result["total_files"]

    lines = [
        f"📊 *Drive Audit — {total} files total*",
        "",
        f"🟢 Gold: {len(buckets['gold'])}   "
        f"🟡 Decent: {len(buckets['decent'])}   "
        f"🟠 Stale: {len(buckets['stale'])}   "
        f"🔴 Garbage: {len(buckets['garbage'])}   "
        f"⚪ Orphan: {len(buckets['orphan'])}",
        "",
    ]

    emoji = {"gold": "🟢", "decent": "🟡", "stale": "🟠", "garbage": "🔴", "orphan": "⚪"}
    labels = {
        "gold": "GOLD (actively used by team)",
        "decent": "DECENT (some use)",
        "stale": "STALE (not touched recently)",
        "garbage": "GARBAGE / CLEANUP CANDIDATES",
        "orphan": "ORPHAN (never mentioned by any worker)",
    }
    for label in ("gold", "decent", "stale", "garbage", "orphan"):
        items = buckets[label]
        if not items:
            continue
        lines.append(f"{emoji[label]} *{labels[label]}* ({len(items)})")
        for f in items[:max_per_bucket]:
            ref_summary = ""
            if f["refs"]:
                workers = ", ".join({r["worker"] for r in f["refs"]})
                ref_summary = f" — used by {workers}"
            lines.append(f"  • {f['name']}{ref_summary} _({f['reason']})_")
        if len(items) > max_per_bucket:
            lines.append(f"  …{len(items) - max_per_bucket} more")
        lines.append("")
    lines.append("_run again anytime with `audit drive`. cleanup the garbage list in Drive directly when you've reviewed._")
    return "\n".join(lines)
