#!/usr/bin/env python3
"""Generate a fresh Scoring Engine competition config and Blue-team credentials.

The selected Blue team is used as the service-check template. Set placeholders
in service ``host`` values to create a per-team target, for example
``ssh-team{team_number}.exam.local``. Plain host values are copied unchanged.
Use ``--host-number-offset`` when host numbering starts above Team1.
"""

import argparse
import copy
import csv
import os
import secrets
import sys
import tempfile
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = ROOT / "bin" / "competition.yaml"
DEFAULT_OUTPUT = ROOT / "generated-exam"
MAX_TEAMS = 1000
PASSWORD_WORD_COUNT = 4

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

HOST_TOKENS = ("{team_number}", "{team_number_padded}", "{team_name}")


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


def render_host(value, team_number, team_name, host_number_offset=0):
    if not isinstance(value, str):
        return value
    host_number = team_number + host_number_offset
    replacements = {
        "{team_number}": str(host_number),
        "{team_number_padded}": f"{host_number:03d}",
        "{team_name}": team_name,
    }
    for token, replacement in replacements.items():
        value = value.replace(token, replacement)
    return value


def build_competition(source_data, count, template_team_name=None, host_number_offset=0):
    """Return (generated config, credential rows, templated host count)."""
    if not isinstance(count, int) or isinstance(count, bool) or count < 1 or count > MAX_TEAMS:
        raise ValueError(f"team count must be between 1 and {MAX_TEAMS}")
    if not isinstance(host_number_offset, int) or isinstance(host_number_offset, bool):
        raise ValueError("host number offset must be an integer")
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
        team = copy.deepcopy(template_team)
        team["name"] = team_name
        team["color"] = "Blue"
        team["users"] = [{"username": username, "password": password}]
        for service in team["services"]:
            original_host = service.get("host")
            if isinstance(original_host, str) and any(token in original_host for token in HOST_TOKENS):
                templated_host_count += 1
            service["host"] = render_host(original_host, number, team_name, host_number_offset)
        generated_teams.append(team)
        credentials.append({"team_name": team_name, "username": username, "password": password})

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


def write_outputs(data, credentials, output_dir, force=False):
    output_dir = Path(output_dir)
    create_output_dir = not output_dir.exists()
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if create_output_dir and os.name == "posix":
        output_dir.chmod(0o700)

    yaml_path = output_dir / "competition.yaml"
    credentials_path = output_dir / "blue-team-credentials.csv"
    existing = [path for path in (yaml_path, credentials_path) if path.exists()]
    if existing and not force:
        raise FileExistsError(
            "output already exists: " + ", ".join(str(path) for path in existing) + "; use --force to replace it"
        )

    yaml_content = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120)
    from io import StringIO

    csv_buffer = StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=("team_name", "username", "password"))
    writer.writeheader()
    writer.writerows(credentials)
    _write_private_file(yaml_path, yaml_content)
    _write_private_file(credentials_path, csv_buffer.getvalue())
    return yaml_path, credentials_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("count", type=int, help=f"number of Blue teams to generate (1-{MAX_TEAMS})")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="source competition YAML")
    parser.add_argument("--template-team", help="Blue team whose services are copied (default: first Blue team)")
    parser.add_argument(
        "--host-number-offset",
        type=int,
        default=0,
        help="add this number to {team_number} host placeholders without changing generated team names",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT, help=f"private output directory (default: {DEFAULT_OUTPUT})"
    )
    parser.add_argument("--force", action="store_true", help="replace existing generated output files")
    args = parser.parse_args(argv)

    source_path = args.source if args.source.is_absolute() else Path.cwd() / args.source
    output_dir = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    try:
        with source_path.open(encoding="utf-8") as source_file:
            source_data = yaml.safe_load(source_file)
        data, credentials, templated_host_count = build_competition(
            source_data, args.count, args.template_team, args.host_number_offset
        )
        yaml_path, credentials_path = write_outputs(data, credentials, output_dir, args.force)
    except (OSError, yaml.YAMLError, ValueError) as error:
        parser.error(str(error))

    print(f"Generated {args.count} Blue teams in {yaml_path}")
    print(f"Generated private logins in {credentials_path}")
    if args.host_number_offset:
        print(
            f"Applied host number offset {args.host_number_offset}; generated names remain "
            f"Team1 through Team{args.count}."
        )
    if templated_host_count:
        print(
            f"Rendered per-team placeholders in {templated_host_count} template host value(s); "
            "any remaining plain host values are still shared."
        )
    else:
        print(
            "WARNING: no service host placeholders were found; all teams will check the same template hosts. "
            "Use {team_number}, {team_number_padded}, or {team_name} in a service host for per-team targets."
        )
    if os.name != "posix":
        print("Note: Windows file permissions follow the output directory's inherited ACLs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
