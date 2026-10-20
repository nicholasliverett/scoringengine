#!/usr/bin/env python3
"""Generate practical-exam teams, logins, exam flags, and Salt deployment data."""

import argparse
import copy
import csv
import hashlib
import ipaddress
import os
import secrets
import sys
import tempfile
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = ROOT / "configs" / "competition-ssh-template.yaml"
DEFAULT_OUTPUT = ROOT / "generated-exam"
MAX_TEAMS = 1000
PASSWORD_WORD_COUNT = 4
DEFAULT_FLAG_PATH = "/tmp/exam-flag.txt"

# Short, plain ASCII words keep passphrases easy to read and below bcrypt's
# 72-byte input limit even when all four words are at their maximum length.
WORDS = tuple(
    """
    amber anchor apple apron arch arrow ash atlas aurora autumn badger bamboo
    basil beacon beaver berry birch bird blossom blue breeze brook brown bucket
    cactus candle canyon cedar chime citrus cloud cobalt comet coral cosmos
    cotton crater cream daisy delta denim desert diver donkey dragon dream echo
    ember falcon fern field finch flame forest fossil fox galaxy garden garlic
    gentle ginger glass glider golden granite grape grass green harbor hazel
    heron hollow honey horizon island ivory jasmine jazz kernel kitten kiwi
    ladder lantern lemon lilac linden lizard lotus maple marble meadow melon
    memory mint mirror mist monkey moss mountain mouse muffin music navy nectar
    nickel night north ocean olive orbit orchid otter paper pebble pepper petal
    phoenix piano pickle pine planet plum pocket pollen poppy prairie prism
    pumpkin quartz quiet rabbit radar raven red river robin rocket saffron sage
    salmon sandal scarlet season shadow shell silver sky sleep small smile
    smooth snow solar sparrow spice spirit spring star steel stone storm summer
    sunny sunset swan swift temple tiger timber toast tomato tulip turtle valley
    velvet violet walnut water whale willow window winter wizard yellow yogurt
    zebra zero
    """.split()
)

HOST_TOKENS = ("{team_number}", "{team_number_padded}", "{team_name}", "{team_ip}")


def generate_username(team_number, used_usernames):
    username = f"team{team_number}"
    if username in used_usernames:
        raise ValueError(f"generated username '{username}' collides with a non-Blue team user")
    used_usernames.add(username)
    return username


def generate_password(used_passwords):
    while True:
        password = "-".join(secrets.choice(WORDS) for _ in range(PASSWORD_WORD_COUNT))
        if password not in used_passwords:
            if len(password.encode("utf-8")) > 72:
                raise ValueError("Generated password exceeds the application's 72-byte bcrypt limit")
            used_passwords.add(password)
            return password


def render_host(value, team_number, team_name, host_number_offset=0, team_ip=None):
    if not isinstance(value, str):
        return value
    host_number = team_number + host_number_offset
    replacements = {
        "{team_number}": str(host_number),
        "{team_number_padded}": f"{host_number:03d}",
        "{team_name}": team_name,
        "{team_ip}": team_ip if team_ip is not None else "{team_ip}",
    }
    for token, replacement in replacements.items():
        value = value.replace(token, replacement)
    return value


def build_competition(source_data, count, template_team_name=None, host_number_offset=0, start_ip=None):
    """Return (generated config, credential rows, templated host count)."""
    if not isinstance(count, int) or isinstance(count, bool) or count < 1 or count > MAX_TEAMS:
        raise ValueError(f"team count must be between 1 and {MAX_TEAMS}")
    if not isinstance(host_number_offset, int) or isinstance(host_number_offset, bool):
        raise ValueError("host number offset must be an integer")
    team_ips = None
    if start_ip is not None:
        try:
            first_ip = ipaddress.IPv4Address(start_ip)
        except ipaddress.AddressValueError as error:
            raise ValueError("starting IP must be a valid IPv4 address") from error
        last_ip = int(first_ip) + count - 1
        if last_ip > int(ipaddress.IPv4Address.max):
            raise ValueError(f"{count} consecutive team IPs from {first_ip} exceed the IPv4 range")
        team_ips = [str(ipaddress.IPv4Address(int(first_ip) + offset)) for offset in range(count)]
    if not isinstance(source_data, dict) or not isinstance(source_data.get("teams"), list):
        raise ValueError("source YAML must have a top-level teams list")
    for index, team in enumerate(source_data["teams"], start=1):
        if not isinstance(team, dict):
            raise ValueError(f"source team {index} must be a YAML mapping")
        if not isinstance(team.get("name"), str) or not team["name"]:
            raise ValueError(f"source team {index} must have a non-empty name")
        if team.get("color") not in ("Blue", "White", "Red"):
            raise ValueError(f"source team '{team['name']}' must use color Blue, White, or Red")
        if not isinstance(team.get("users"), list):
            raise ValueError(f"source team '{team['name']}' must have a users list")
        if any(not isinstance(user, dict) or not isinstance(user.get("username"), str) for user in team["users"]):
            raise ValueError(f"source team '{team['name']}' has a user without a username")

    blue_teams = [team for team in source_data["teams"] if isinstance(team, dict) and team.get("color") == "Blue"]
    if not blue_teams:
        raise ValueError("source YAML must contain at least one Blue team to use as a template")
    if template_team_name is None:
        template_team = blue_teams[0]
    else:
        template_team = next((team for team in blue_teams if team.get("name") == template_team_name), None)
        if template_team is None:
            raise ValueError(f"Blue template team not found: {template_team_name}")

    template_services = template_team.get("services")
    if not isinstance(template_services, list) or not template_services:
        raise ValueError(f"template team '{template_team.get('name', '?')}' must have at least one service")
    for index, service in enumerate(template_services, start=1):
        if not isinstance(service, dict) or not isinstance(service.get("host"), str):
            raise ValueError(f"template team '{template_team['name']}' service {index} must have a host string")

    preserved_teams = [
        team for team in source_data["teams"] if not isinstance(team, dict) or team.get("color") != "Blue"
    ]
    existing_names = {team.get("name") for team in preserved_teams if isinstance(team, dict)}
    generated_names = [f"Team{number}" for number in range(1, count + 1)]
    collisions = sorted(existing_names.intersection(generated_names))
    if collisions:
        raise ValueError("generated team names collide with non-Blue teams: " + ", ".join(collisions))

    used_usernames = {
        user.get("username")
        for team in preserved_teams
        if isinstance(team, dict)
        for user in team.get("users", [])
        if isinstance(user, dict) and isinstance(user.get("username"), str)
    }
    used_passwords = set()
    generated_teams = []
    credentials = []
    templated_host_count = 0

    for number, team_name in enumerate(generated_names, start=1):
        username = generate_username(number, used_usernames)
        password = generate_password(used_passwords)
        exam_flag = secrets.token_urlsafe(32) if team_ips is not None else None
        team = copy.deepcopy(template_team)
        team["name"] = team_name
        team["color"] = "Blue"
        team["users"] = [{"username": username, "password": password}]
        team.pop("exam_flag_hash", None)
        if exam_flag is not None:
            team["exam_flag_hash"] = hashlib.sha256(exam_flag.encode("utf-8")).hexdigest()
        for service in team["services"]:
            original_host = service.get("host")
            if team_ips is not None:
                service["host"] = team_ips[number - 1]
                templated_host_count += 1
            elif isinstance(original_host, str) and any(token in original_host for token in HOST_TOKENS):
                templated_host_count += 1
                service["host"] = render_host(original_host, number, team_name, host_number_offset)
            else:
                service["host"] = render_host(original_host, number, team_name, host_number_offset)
        generated_teams.append(team)
        credential = {"team_name": team_name, "username": username, "password": password}
        if exam_flag is not None:
            credential["machine_ip"] = team_ips[number - 1]
            credential["exam_flag"] = exam_flag
        credentials.append(credential)

    result = copy.deepcopy(source_data)
    result["teams"] = []
    inserted_generated_teams = False
    for team in source_data["teams"]:
        if isinstance(team, dict) and team.get("color") == "Blue":
            if not inserted_generated_teams:
                result["teams"].extend(generated_teams)
                inserted_generated_teams = True
        else:
            result["teams"].append(copy.deepcopy(team))
    if not inserted_generated_teams:
        result["teams"].extend(generated_teams)

    return result, credentials, templated_host_count


def _write_private_file(destination, content):
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, destination)
        if os.name == "posix":
            destination.chmod(0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _dump_yaml(path, data):
    _write_private_file(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120))


def write_salt_outputs(credentials, output_dir, flag_path=DEFAULT_FLAG_PATH, force=False):
    """Write targeted Salt pillar and state files for generated exam flags."""
    flagged_teams = [row for row in credentials if "exam_flag" in row]
    if not flagged_teams:
        return None

    salt_root = Path(output_dir) / "salt"
    pillar_root = salt_root / "pillar"
    team_pillar_root = pillar_root / "practical_exam"
    state_root = salt_root / "states"
    state_module_root = state_root / "practical_exam"
    for directory in (team_pillar_root, state_module_root):
        created = not directory.exists()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if created and os.name == "posix":
            directory.chmod(0o700)

    pillar_top = {"base": {}}
    state_top = {"base": {}}
    for row in flagged_teams:
        minion_match = f"G@ipv4:{row['machine_ip']}"
        pillar_top["base"][minion_match] = [
            {"match": "compound"},
            f"practical_exam.{row['team_name'].lower()}",
        ]
        state_top["base"][minion_match] = [
            {"match": "compound"},
            "practical_exam.install_exam_flag",
        ]
        _dump_yaml(
            team_pillar_root / f"{row['team_name'].lower()}.sls",
            {
                "practical_exam": {
                    "team": row["team_name"],
                    "ip": row["machine_ip"],
                    "flag_path": flag_path,
                    "flag": row["exam_flag"],
                }
            },
        )

    _dump_yaml(pillar_root / "top.sls", pillar_top)
    _dump_yaml(state_root / "top.sls", state_top)
    state_template = """{% set flag_path = salt['pillar.get']('practical_exam:flag_path', '/tmp/exam-flag.txt') %}
install_exam_flag:
  file.managed:
    - name: {{ flag_path | tojson }}
    - contents_pillar: practical_exam:flag
    - mode: '0444'
    - makedirs: True
"""
    _write_private_file(state_module_root / "install_exam_flag.sls", state_template)

    # Remove stale generated team pillars when --force regenerates fewer teams.
    if force:
        for path in team_pillar_root.glob("team*.sls"):
            if path.stem not in {row["team_name"].lower() for row in flagged_teams}:
                path.unlink()
    return salt_root


def write_outputs(data, credentials, output_dir, force=False, flag_path=DEFAULT_FLAG_PATH):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        output_dir.chmod(0o700)

    yaml_path = output_dir / "competition.yaml"
    credentials_path = output_dir / "blue-team-credentials.csv"
    salt_path = output_dir / "salt"
    existing = [path for path in (yaml_path, credentials_path) if path.exists()]
    if any("exam_flag" in row for row in credentials) and salt_path.exists():
        existing.append(salt_path)
    if existing and not force:
        raise FileExistsError(
            "output already exists: " + ", ".join(str(path) for path in existing) + "; use --force to replace it"
        )

    yaml_content = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120)
    from io import StringIO

    csv_buffer = StringIO(newline="")
    writer = csv.DictWriter(
        csv_buffer,
        fieldnames=("team_name", "username", "password"),
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerows(credentials)
    _write_private_file(yaml_path, yaml_content)
    _write_private_file(credentials_path, csv_buffer.getvalue())
    write_salt_outputs(credentials, output_dir, flag_path=flag_path, force=force)
    return yaml_path, credentials_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("count", type=int, help=f"number of Blue teams to generate (1-{MAX_TEAMS})")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="source competition YAML")
    parser.add_argument("--start-ip", required=True, help="starting IPv4 address; each team receives the next address")
    parser.add_argument("--template-team", help="Blue team whose services are copied (default: first Blue team)")
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT, help=f"private output directory (default: {DEFAULT_OUTPUT})"
    )
    parser.add_argument("--force", action="store_true", help="replace existing generated output files")
    parser.add_argument(
        "--flag-path",
        default=DEFAULT_FLAG_PATH,
        help=f"path for the generated exam token on each VM (default: {DEFAULT_FLAG_PATH})",
    )
    args = parser.parse_args(argv)

    source_path = args.source if args.source.is_absolute() else Path.cwd() / args.source
    output_dir = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    try:
        with source_path.open(encoding="utf-8") as source_file:
            source_data = yaml.safe_load(source_file)
        data, credentials, templated_host_count = build_competition(
            source_data, args.count, args.template_team, start_ip=args.start_ip
        )
        yaml_path, credentials_path = write_outputs(data, credentials, output_dir, args.force, flag_path=args.flag_path)
        salt_path = output_dir / "salt"
    except (OSError, yaml.YAMLError, ValueError) as error:
        parser.error(str(error))

    print(f"Generated {args.count} Blue teams in {yaml_path}")
    print(f"Generated private logins in {credentials_path}")
    print(f"Generated team-targeted Salt pillar and state files in {salt_path}")
    print(
        f"Assigned {args.count} consecutive VM addresses from {args.start_ip} "
        f"through {credentials[-1]['machine_ip']}."
    )
    if templated_host_count:
        print(f"Assigned per-team VM addresses to {templated_host_count} service host value(s).")
    if os.name != "posix":
        print("Note: Windows file permissions follow the output directory's inherited ACLs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
