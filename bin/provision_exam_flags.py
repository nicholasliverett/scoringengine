#!/usr/bin/env python3
"""Create/reuse per-team exam flag files and register their hashes in
Scoring Engine.

Inventory CSV columns: team_id,machine. Keep the output directory private and
copy each generated text file to the matching student's workstation.
"""

import argparse
import csv
import getpass
import http.cookiejar
import json
import os
import re
import secrets
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path


class CsrfParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.token = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "input" and values.get("name") == "csrf_token":
            self.token = values.get("value")


def request(opener, url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    return opener.open(req, timeout=20)


def http_error_detail(error):
    """Return a concise, useful message from a non-2xx API response."""
    try:
        body = error.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""
    if not body:
        return ""

    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        detail = payload.get("error") or payload.get("message")
        if isinstance(detail, str) and detail.strip():
            return " ".join(detail.split())[:500]

    # Avoid dumping a whole HTML error page (or any large response) to a
    # terminal while preserving the server's short explanation.
    text = re.sub(r"<[^>]*>", " ", body)
    return " ".join(text.split())[:500]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        required=True,
        help="Scoring Engine URL, e.g. https://exam.example.edu",
    )
    parser.add_argument(
        "--inventory",
        required=True,
        type=Path,
        help="CSV with team_id,machine columns",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Private directory for flag files",
    )
    parser.add_argument("--username", required=True, help="White-team account")
    parser.add_argument("--password", help="White-team password (prompted if omitted)")
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="Replace all listed flags with new credentials",
    )
    parser.add_argument(
        "--allow-http",
        action="store_true",
        help="Allow sending admin credentials over plain HTTP",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    if not base_url.startswith(("https://", "http://")):
        parser.error("--base-url must start with https:// or http://")
    if base_url.startswith("http://") and not args.allow_http:
        parser.error("HTTPS is required; use --allow-http only on a trusted isolated network")
    password = args.password or getpass.getpass("White-team password: ")

    rows = []
    with args.inventory.open(newline="", encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            try:
                team_id = int(row["team_id"])
                machine = re.sub(r"[^A-Za-z0-9_.-]+", "-", row["machine"])
                machine = machine.strip(".-_")
            except (KeyError, TypeError, ValueError):
                parser.error("inventory must have integer team_id and machine columns")
            if team_id < 1 or not machine:
                parser.error("inventory contains an invalid team_id or machine")
            rows.append((team_id, machine))
    if not rows or len({team_id for team_id, _ in rows}) != len(rows):
        parser.error("inventory must contain at least one row and each team_id only once")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    if hasattr(os, "chmod"):
        try:
            os.chmod(args.output_dir, 0o700)
        except OSError:
            pass  # Windows ACLs may not map POSIX directory modes.
    secrets_to_register = []
    pending_files = []
    for team_id, machine in rows:
        destination = args.output_dir / f"team-{team_id}-{machine}.txt"
        if destination.exists() and not args.rotate:
            token = destination.read_text(encoding="utf-8").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{30,256}", token):
                parser.error(f"existing flag file is malformed: {destination}")
        else:
            token = secrets.token_urlsafe(32)
        secrets_to_register.append({"team_id": team_id, "token": token})
        pending_files.append((destination, token))

    # Export first: if API registration is interrupted, the same files can be
    # submitted again; --rotate safely retries a credential rotation.
    for destination, token in pending_files:
        fd, temp_name = tempfile.mkstemp(prefix=".exam-flag-", dir=args.output_dir)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            else:
                os.chmod(temp_name, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
                output.write(token + "\n")
            os.replace(temp_name, destination)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    try:
        login_page = request(opener, base_url + "/login").read().decode("utf-8", errors="replace")
        csrf_parser = CsrfParser()
        csrf_parser.feed(login_page)
        if not csrf_parser.token:
            raise RuntimeError("could not find login CSRF token")
        form = urllib.parse.urlencode(
            {
                "csrf_token": csrf_parser.token,
                "username": args.username,
                "password": password,
            }
        ).encode()
        request(
            opener,
            base_url + "/login",
            data=form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                # Flask-WTF requires a same-origin Referer for HTTPS POSTs.
                "Referer": base_url + "/login",
            },
        )
        payload = json.dumps({"credentials": secrets_to_register, "rotate": args.rotate}).encode()
        response = request(
            opener,
            base_url + "/api/admin/exam/credentials",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-CSRFToken": csrf_parser.token,
                "Referer": base_url + "/admin/status",
            },
        )
        result = json.loads(response.read().decode("utf-8"))
        if response.status != 200:
            raise RuntimeError(result.get("error", "registration failed"))
    except urllib.error.HTTPError as exc:
        detail = http_error_detail(exc)
        suffix = f": {detail}" if detail else ""
        print(
            f"Provisioning failed: HTTP {exc.code} {exc.reason}{suffix}",
            file=sys.stderr,
        )
        print(
            "Local flag files were written and may not be registered. Keep them "
            "private; rerun with the same output directory after fixing the error. "
            "Use --rotate only when intentionally replacing existing flags.",
            file=sys.stderr,
        )
        return 1
    except (urllib.error.URLError, RuntimeError, ValueError) as exc:
        print(f"Provisioning failed: {exc}", file=sys.stderr)
        print(
            "Local flag files were written and may not be registered. Keep them "
            "private; rerun with the same output directory after fixing the error. "
            "Use --rotate only when intentionally replacing existing flags.",
            file=sys.stderr,
        )
        return 1

    print(f"Provisioned {len(rows)} exam flag files in {args.output_dir}.")
    print("Copy each team-<id>-<machine>.txt to that student's workstation and " "keep this export directory private.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
