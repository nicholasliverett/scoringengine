import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path

script_path = Path(__file__).resolve().parents[2] / "bin" / "provision_exam_flags.py"
script_spec = importlib.util.spec_from_file_location("provision_exam_flags", script_path)
provisioner = importlib.util.module_from_spec(script_spec)
script_spec.loader.exec_module(provisioner)


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status

    def read(self):
        return self.body


def run_provisioner(tmp_path, monkeypatch, fake_request, capsys):
    inventory = tmp_path / "machines.csv"
    inventory.write_text("team_id,machine\n3,192.168.1.10\n", encoding="utf-8")
    output_dir = tmp_path / "flags"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(script_path),
            "--base-url",
            "https://scoring_engine",
            "--inventory",
            str(inventory),
            "--output-dir",
            str(output_dir),
            "--username",
            "whiteteamuser",
            "--password",
            "test-password",
        ],
    )
    monkeypatch.setattr(provisioner, "request", fake_request)
    result = provisioner.main()
    return result, output_dir, capsys.readouterr()


def test_https_login_and_registration_send_referers(tmp_path, monkeypatch, capsys):
    calls = []

    def fake_request(opener, url, data=None, headers=None):
        calls.append((url, data, headers or {}))
        if url.endswith("/login") and data is None:
            return FakeResponse('<input type="hidden" name="csrf_token" value="test-csrf">')
        if url.endswith("/api/admin/exam/credentials"):
            return FakeResponse(json.dumps({"registered": 1, "rotated": False}))
        return FakeResponse("OK")

    result, _, _ = run_provisioner(tmp_path, monkeypatch, fake_request, capsys)

    assert result == 0
    login_post = next(call for call in calls if call[0].endswith("/login") and call[1] is not None)
    api_post = next(call for call in calls if call[0].endswith("/api/admin/exam/credentials"))
    assert login_post[2]["Referer"] == "https://scoring_engine/login"
    assert api_post[2]["Referer"] == "https://scoring_engine/admin/status"
    assert api_post[2]["X-CSRFToken"] == "test-csrf"


def test_http_error_body_is_shown_without_rotating_flags(tmp_path, monkeypatch, capsys):
    def fake_request(opener, url, data=None, headers=None):
        if url.endswith("/login") and data is None:
            return FakeResponse('<input type="hidden" name="csrf_token" value="test-csrf">')
        raise urllib.error.HTTPError(
            url,
            400,
            "BAD REQUEST",
            {},
            io.BytesIO(b'{"error":"CSRF validation failed"}'),
        )

    result, output_dir, captured = run_provisioner(tmp_path, monkeypatch, fake_request, capsys)

    assert result == 1
    assert "CSRF validation failed" in captured.err
    assert "Use --rotate only when intentionally replacing existing flags" in (captured.err)
    assert len(list(output_dir.glob("team-*.txt"))) == 1
