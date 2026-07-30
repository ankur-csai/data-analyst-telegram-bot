"""Custom function tools the agent can call: fetch_url and run_python.

Both run in this process (not inside OpenAI's sandbox), so both are careful
about what they touch: fetch_url blocks requests to private/local addresses,
run_python strips secrets from the subprocess environment and bounds runtime.
"""
import ast
import ipaddress
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from urllib.parse import urlparse

import requests

MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 30
RUN_TIMEOUT_SECONDS = 60
MAX_TOOL_OUTPUT_CHARS = 8000

FETCH_URL_SCHEMA = {
    "type": "function",
    "name": "fetch_url",
    "description": (
        "Download a public web resource (HTML page, CSV, XLSX, JSON, ...) and "
        "save it to the conversation's working directory. Returns a short "
        "preview (for tabular data: columns/shape/head) so you can decide what "
        "run_python code to write next, plus the local file path to read it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The http(s) URL to download."},
        },
        "required": ["url"],
        "additionalProperties": False,
    },
}

RUN_PYTHON_SCHEMA = {
    "type": "function",
    "name": "run_python",
    "description": (
        "Execute Python code in a sandboxed subprocess (60s timeout) with "
        "pandas/numpy/openpyxl available. The working directory contains any "
        "files saved by fetch_url, referenced by the paths it returned. Print "
        "whatever you need to see - stdout/stderr are returned to you."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python source to execute."},
        },
        "required": ["code"],
        "additionalProperties": False,
    },
}


def _host_is_blocked(hostname):
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return True
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False


def _guess_ext(url, content_type):
    path = urlparse(url).path
    if "." in path.rsplit("/", 1)[-1]:
        return "." + path.rsplit(".", 1)[-1].split("?")[0][:10]
    if "csv" in content_type:
        return ".csv"
    if "json" in content_type:
        return ".json"
    if "excel" in content_type or "spreadsheet" in content_type:
        return ".xlsx"
    if "html" in content_type:
        return ".html"
    return ".bin"


def _tabular_preview(path, ext):
    import pandas as pd

    try:
        if ext == ".csv":
            df = pd.read_csv(path)
        elif ext in (".xlsx", ".xls"):
            df = pd.read_excel(path)
        elif ext in (".html", ".htm"):
            tables = pd.read_html(path)
            if not tables:
                return None
            df = tables[0]
        else:
            return None
    except Exception as e:
        return f"(could not preview as tabular data: {e})"

    with pd.option_context("display.max_columns", 20, "display.width", 120):
        return (
            f"shape={df.shape}\ncolumns={list(df.columns)}\n\nhead:\n{df.head(5)}"
        )


def fetch_url(url, workdir):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return {"error": f"unsupported scheme: {parsed.scheme!r}"}
    if not parsed.hostname or _host_is_blocked(parsed.hostname):
        return {"error": "refused: target host resolves to a private/local address"}

    try:
        resp = requests.get(
            url,
            timeout=FETCH_TIMEOUT_SECONDS,
            headers={"User-Agent": "Mozilla/5.0 (data-analyst-bot)"},
            stream=True,
        )
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "").lower()
        ext = _guess_ext(url, content_type)

        Path(workdir).mkdir(parents=True, exist_ok=True)
        name = f"fetched_{abs(hash(url)) % 10**8}{ext}"
        dest = Path(workdir) / name

        size = 0
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    return {"error": f"file exceeded {MAX_DOWNLOAD_BYTES} byte cap, aborted"}
                f.write(chunk)
    except requests.RequestException as e:
        return {"error": f"request failed: {e}"}

    result = {"path": str(dest), "content_type": content_type, "size_bytes": size}
    preview = _tabular_preview(dest, ext)
    if preview:
        result["preview"] = preview[:MAX_TOOL_OUTPUT_CHARS]
    return result


def _repl_style(code):
    """Like a REPL/notebook cell: if the last top-level statement is a bare
    expression, print its repr too - models often forget to print(), and a
    silent stdout has previously led to answers coming from the model's
    memory instead of the actual computed result."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last = tree.body.pop()
        tree.body.append(
            ast.Expr(value=ast.Call(
                func=ast.Name(id="print", ctx=ast.Load()),
                args=[ast.Call(
                    func=ast.Name(id="repr", ctx=ast.Load()),
                    args=[last.value], keywords=[])],
                keywords=[],
            ))
        )
        ast.fix_missing_locations(tree)
        return ast.unparse(tree)
    return code


def run_python(code, workdir):
    Path(workdir).mkdir(parents=True, exist_ok=True)
    code = _repl_style(code)
    safe_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": workdir,
        "LANG": "en_US.UTF-8",
    }
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=workdir,
            env=safe_env,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"timed out after {RUN_TIMEOUT_SECONDS}s"}

    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout[-MAX_TOOL_OUTPUT_CHARS:],
        "stderr": proc.stderr[-MAX_TOOL_OUTPUT_CHARS:],
    }


def dispatch(name, arguments_json, workdir):
    args = json.loads(arguments_json) if arguments_json else {}
    if name == "fetch_url":
        return fetch_url(args["url"], workdir)
    if name == "run_python":
        return run_python(args["code"], workdir)
    return {"error": f"unknown tool {name!r}"}
