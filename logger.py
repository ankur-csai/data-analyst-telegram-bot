"""Append-only JSONL run log, pushed to GitHub via the Contents API so the
raw.githubusercontent.com URL stays public and wget-able without needing a
git binary or local credential setup inside the deployed container.
"""
import asyncio
import base64
import json
import os

import requests

GITHUB_API = "https://api.github.com"
LOG_PATH = "logs/run.jsonl"

_lock = asyncio.Lock()


def _repo():
    """Accepts either 'owner/repo' or a full github.com URL, in case the env
    var gets set to a pasted URL instead of the short form."""
    repo = os.environ["GITHUB_REPO"].strip().rstrip("/")
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if repo.startswith(prefix):
            repo = repo[len(prefix):]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return repo


def log_url():
    return f"https://raw.githubusercontent.com/{_repo()}/main/{LOG_PATH}"


def _headers():
    return {
        "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
        "Accept": "application/vnd.github+json",
    }


def _get_current():
    repo = _repo()
    resp = requests.get(
        f"{GITHUB_API}/repos/{repo}/contents/{LOG_PATH}",
        headers=_headers(),
        params={"ref": "main"},
        timeout=15,
    )
    if resp.status_code == 200:
        data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, data["sha"]
    if resp.status_code == 404:
        return "", None
    resp.raise_for_status()


def _put(new_content, sha, message):
    repo = _repo()
    body = {
        "message": message,
        "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"),
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    resp = requests.put(
        f"{GITHUB_API}/repos/{repo}/contents/{LOG_PATH}",
        headers=_headers(),
        json=body,
        timeout=15,
    )
    resp.raise_for_status()


def append_log_line_sync(record):
    line = json.dumps(record, ensure_ascii=False)
    current, sha = _get_current()
    if current and not current.endswith("\n"):
        current += "\n"
    updated = current + line + "\n"
    _put(updated, sha, f"log: chat {record.get('chat_id')} @ {record.get('timestamp')}")


async def append_log_line(record):
    async with _lock:
        await asyncio.to_thread(append_log_line_sync, record)
