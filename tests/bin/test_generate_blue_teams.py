import copy
import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

generator_path = Path(__file__).resolve().parents[2] / "bin" / "generate_blue_teams.py"
generator_spec = importlib.util.spec_from_file_location("generate_blue_teams", generator_path)
generator = importlib.util.module_from_spec(generator_spec)
generator_spec.loader.exec_module(generator)


@pytest.fixture
def source_data():
    return {
        "teams": [
            {"name": "WhiteTeam", "color": "White", "users": [{"username": "instructor", "password": "secret"}]},
            {
                "name": "TemplateBlue",
                "color": "Blue",
                "users": [{"username": "old-blue-user", "password": "old-password"}],
                "services": [
                    {
                        "name": "SSH",
                        "check_name": "SSHCheck",
                        "host": "ssh-{team_number_padded}.exam.local",
                        "port": 22,
                        "points": 100,
                        "environments": [{"matching_content": "^SUCCESS"}],
                    },
                    {
                        "name": "Web",
                        "check_name": "HTTPCheck",
                        "host": "shared-web.exam.local",
                        "port": 443,
                        "points": 50,
                        "environments": [{"matching_content": "OK"}],
                    },
                ],
            },
            {"name": "RedTeam", "color": "Red", "users": [{"username": "red", "password": "redpass"}]},
            {
                "name": "OldBlue",
                "color": "Blue",
                "users": [{"username": "old-blue-two", "password": "old-password"}],
                "services": [],
            },
        ],
        "flags": [{"type": "file", "data": {"content": "preserved"}}],
    }


def test_builds_one_user_per_team_and_clones_template(source_data):
    original = copy.deepcopy(source_data)
    result, credentials, host_count = generator.build_competition(source_data, 3, "TemplateBlue")

    assert [team["name"] for team in result["teams"]] == ["WhiteTeam", "Team1", "Team2", "Team3", "RedTeam"]
    blue_teams = [team for team in result["teams"] if team["color"] == "Blue"]
    assert len(blue_teams) == 3
    assert all(len(team["users"]) == 1 for team in blue_teams)
    assert [team["users"][0]["username"] for team in blue_teams] == ["team1", "team2", "team3"]
    assert [team["services"][0]["host"] for team in blue_teams] == [
        "ssh-001.exam.local",
        "ssh-002.exam.local",
        "ssh-003.exam.local",
    ]
    assert all(team["services"][1]["host"] == "shared-web.exam.local" for team in blue_teams)
    assert all(team["services"][0]["points"] == 100 for team in blue_teams)
    assert [row["team_name"] for row in credentials] == ["Team1", "Team2", "Team3"]
    assert [row["username"] for row in credentials] == ["team1", "team2", "team3"]
    assert len({row["password"] for row in credentials}) == 3
    assert all(len(row["password"].split("-")) == 4 for row in credentials)
    assert all(len(row["password"].encode("utf-8")) <= 72 for row in credentials)
    assert host_count == 1
    assert result["flags"] == source_data["flags"]
    assert source_data == original


def test_defaults_to_first_blue_template_and_preserves_non_blue_teams(source_data):
    result, credentials, host_count = generator.build_competition(source_data, 1)

    blue_team = next(team for team in result["teams"] if team["color"] == "Blue")
    assert blue_team["services"][0]["host"] == "ssh-001.exam.local"
    assert len(credentials) == 1
    assert host_count == 1
    assert {team["name"] for team in result["teams"] if team["color"] != "Blue"} == {"WhiteTeam", "RedTeam"}


def test_host_number_offset_maps_team1_to_tenth_host(source_data):
    source_data["teams"][1]["services"][0]["host"] = "192.168.1.{team_number}"
    result, _, _ = generator.build_competition(source_data, 5, "TemplateBlue", host_number_offset=9)

    blue_teams = [team for team in result["teams"] if team["color"] == "Blue"]
    assert [team["name"] for team in blue_teams] == [f"Team{number}" for number in range(1, 6)]
    assert [team["services"][0]["host"] for team in blue_teams] == [
        f"192.168.1.{number}" for number in range(10, 15)
    ]


def test_start_ip_assigns_consecutive_hosts_and_per_team_exam_flags(source_data):
    result, credentials, host_count = generator.build_competition(source_data, 3, "TemplateBlue", start_ip="192.168.1.10")

    blue_teams = [team for team in result["teams"] if team["color"] == "Blue"]
    assert [team["services"][0]["host"] for team in blue_teams] == [
        "192.168.1.10",
        "192.168.1.11",
        "192.168.1.12",
    ]
    assert [team["services"][1]["host"] for team in blue_teams] == [
        "192.168.1.10",
        "192.168.1.11",
        "192.168.1.12",
    ]
    assert host_count == 2
    assert [row["machine_ip"] for row in credentials] == ["192.168.1.10", "192.168.1.11", "192.168.1.12"]
    assert len({row["exam_flag"] for row in credentials}) == 3
    for team, row in zip(blue_teams, credentials):
        assert team["exam_flag_hash"] == hashlib.sha256(row["exam_flag"].encode("utf-8")).hexdigest()


def test_outputs_register_flag_hashes_and_create_team_targeted_salt_data(tmp_path, source_data):
    data, credentials, _ = generator.build_competition(source_data, 2, "TemplateBlue", start_ip="192.168.1.10")
    yaml_path, credentials_path = generator.write_outputs(
        data, credentials, tmp_path / "out", flag_path="/opt/exam/flag.txt"
    )
    salt_root = tmp_path / "out" / "salt"
    pillar_top = generator.yaml.safe_load((salt_root / "pillar" / "top.sls").read_text(encoding="utf-8"))
    state_top = generator.yaml.safe_load((salt_root / "states" / "top.sls").read_text(encoding="utf-8"))
    generated_config = generator.yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    blue_teams = [team for team in generated_config["teams"] if team["color"] == "Blue"]

    assert "G@ipv4:192.168.1.10" in pillar_top["base"]
    assert "G@ipv4:192.168.1.11" in pillar_top["base"]
    assert "G@ipv4:192.168.1.10" in state_top["base"]
    assert "G@ipv4:192.168.1.11" in state_top["base"]
    for team, row in zip(blue_teams, credentials):
        pillar = generator.yaml.safe_load(
            (salt_root / "pillar" / "practical_exam" / f"{team['name'].lower()}.sls").read_text(encoding="utf-8")
        )
        assert pillar["practical_exam"]["flag"] == row["exam_flag"]
        assert pillar["practical_exam"]["ip"] == row["machine_ip"]
        assert pillar["practical_exam"]["flag_path"] == "/opt/exam/flag.txt"
        assert team["exam_flag_hash"] == hashlib.sha256(row["exam_flag"].encode("utf-8")).hexdigest()
    assert "contents_pillar: practical_exam:flag" in (
        salt_root / "states" / "practical_exam" / "install_exam_flag.sls"
    ).read_text(encoding="utf-8")
    assert credentials_path.read_text(encoding="utf-8").splitlines()[0] == "team_name,username,password"
    assert all(row["exam_flag"] not in credentials_path.read_text(encoding="utf-8") for row in credentials)


@pytest.mark.parametrize("start_ip", ["not-an-ip", "::1", "255.255.255.255"])
def test_rejects_invalid_or_overflowing_ip_range(source_data, start_ip):
    with pytest.raises(ValueError, match="starting IP|exceed the IPv4 range"):
        generator.build_competition(source_data, 2, start_ip=start_ip)


def test_included_ssh_template_generates_requested_five_hosts():
    template_path = Path(__file__).resolve().parents[2] / "configs" / "competition-ssh-template.yaml"
    with template_path.open(encoding="utf-8") as source_file:
        source = generator.yaml.safe_load(source_file)

    result, _, _ = generator.build_competition(source, 5, "Team1", host_number_offset=9)
    blue_teams = [team for team in result["teams"] if team["color"] == "Blue"]

    assert [team["services"][0]["host"] for team in blue_teams] == [f"192.168.1.{ip}" for ip in range(10, 15)]
    assert all(team["services"][0]["check_name"] == "SSHCheck" for team in blue_teams)
    assert all(team["services"][0]["points"] == 100 for team in blue_teams)


@pytest.mark.parametrize("count", [0, -1, generator.MAX_TEAMS + 1, True])
def test_rejects_invalid_team_count(source_data, count):
    with pytest.raises(ValueError, match="team count"):
        generator.build_competition(source_data, count)


def test_rejects_missing_template(source_data):
    with pytest.raises(ValueError, match="Blue template team not found"):
        generator.build_competition(source_data, 2, "missing")


def test_rejects_malformed_source_team(source_data):
    source_data["teams"].append({"name": "Malformed", "color": "Green", "users": []})
    with pytest.raises(ValueError, match="must use color"):
        generator.build_competition(source_data, 2)


def test_rejects_generated_team_name_collision(source_data):
    source_data["teams"][0]["name"] = "Team1"
    with pytest.raises(ValueError, match="collide"):
        generator.build_competition(source_data, 2)


def test_rejects_generated_username_collision(source_data):
    source_data["teams"][0]["users"][0]["username"] = "team1"
    with pytest.raises(ValueError, match="generated username 'team1' collides"):
        generator.build_competition(source_data, 2)


def test_outputs_credentials_and_refuses_accidental_overwrite(tmp_path, source_data):
    data, credentials, _ = generator.build_competition(source_data, 2, "TemplateBlue")
    yaml_path, credentials_path = generator.write_outputs(data, credentials, tmp_path / "out")

    assert yaml_path.exists()
    assert credentials_path.read_text(encoding="utf-8").splitlines()[0] == "team_name,username,password"
    with pytest.raises(FileExistsError, match="--force"):
        generator.write_outputs(data, credentials, tmp_path / "out")
    generator.write_outputs(data, credentials, tmp_path / "out", force=True)

    if sys.platform != "win32":
        assert yaml_path.stat().st_mode & 0o777 == 0o600
        assert credentials_path.stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "out").stat().st_mode & 0o777 == 0o700
