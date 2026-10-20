# Individual practical exam mode

This fork adds private, per-team participation flags. A Blue team pastes the
contents of the text file installed on its assigned workstation into the Exam
login page. The flag can be used for repeated logins; the first successful
submission records one participation event and awards the configured attendance
credit. A database unique constraint and one transaction protect the award from
concurrent or repeated submissions.

Blue users see only Overview and Stats. These pages show their own points and
anonymous Finding 1, Finding 2, etc. statuses from each check's latest result.
They do not show service names or round-by-round check history. The public
scoreboard, service/check pages, injects, flags, team data, and their APIs are
unavailable to Blue users. White accounts retain the normal password login and
instructor pages. Service checks, scoring, and the configured check ports
continue to operate on the server.

## Service scoring for the practical exam

Each service contributes its configured point value once while its latest
completed check passes. A failed check immediately changes that service to zero;
a later pass restores its configured points. Repeated passing rounds do not add
points. Dynamic scoring multipliers and SLA failure penalties remain available
as status information, but do not change service score totals. Inject and manual
adjustment points continue to be added separately.

Threat-hunting checks should pass when the machine is in the expected clean
state. For example, a check for an unauthorized SSH key should pass when that
key is absent and fail while it exists. Availability checks for SSH or a website
should pass while the service is reachable. Both use the same up/down scoring
rule, with the configured service points controlling the value at stake.

## Configure attendance credit

The default is 100 points. Set `exam_participation_points` under `[OPTIONS]` in
`engine.conf` (or set `SCORINGENGINE_EXAM_PARTICIPATION_POINTS`) to change it.
Non-positive values are treated as zero. When deploying this fork to an existing
installation, rebuild and start the Compose stack so bootstrap applies pending
database migrations before the web and engine containers start:

```sh
docker compose up -d --build
```

Whenever bootstrap runs, it applies pending migrations. For a fresh database,
it creates all model tables and stamps the current Alembic head.

## Generate a practical exam

Create the team config, VM addresses, team logins, and Salt flag data in one
command. The only required inputs are the first VM IPv4 address and the number
of teams:

```sh
python bin/generate_blue_teams.py 24 --start-ip 192.168.1.10
```

The default template is `configs/competition-ssh-template.yaml`. It keeps the
White and Red teams and clones the selected Blue team as `Team1` through
`Team24`. Each clone gets the next IP (`192.168.1.10`, `.11`, and so on), its
own generated Scoring Engine login, and a unique exam flag. Every service in a
team's template points at that team's VM. Change the template when the checks,
service accounts, or team roles differ for an exam. Use `--source`
to select another YAML template and `--output-dir` to change the output
location.

The output is private under `generated-exam/`:

- `competition.yaml` is the generated Scoring Engine config. It contains only
  SHA-256 hashes of the exam flags; loading it into a new database registers
  those flags for the generated Blue users.
- `blue-team-credentials.csv` contains the generated Blue usernames and
  passwords.
- `salt/pillar/top.sls` targets minions by their `ipv4` grain. Each
  `salt/pillar/practical_exam/teamN.sls` contains only that team's flag.
- `salt/states/top.sls` and
  `salt/states/practical_exam/install_exam_flag.sls` install the target team's
  flag at `/tmp/exam-flag.txt` with read-only mode `0444`, so the assigned team
  can submit it from its VM.

The private output directory and secret files receive owner-only POSIX
permissions where supported. Generated output is ignored by Git, and the
generator refuses to overwrite existing output unless `--force` is supplied.
Use `--flag-path` if the VM's Salt-managed token file should be somewhere other
than `/tmp/exam-flag.txt`.

To apply the generated Salt data, add the generated `salt/pillar` directory to
Salt's `pillar_roots` and `salt/states` directory to `file_roots`, then run:

```sh
salt '*' state.apply
```

The generated top files match minions whose `ipv4` grain contains the assigned
address, so each VM receives only its own flag. The template's SSH check logs in
as `scoringcheck` and runs `id`; configure that account and password on every
VM, or edit the service account in the template. Generated exam flags are
registered when the generated `competition.yaml` is used to initialize the
database. To use this config with a new database, copy it to
`bin/competition.yaml` before the initial Compose setup. Config changes do not
modify teams or services in an already initialized database.

For a live database, the legacy `bin/provision_exam_flags.py` flow remains
available for deliberate flag registration or rotation. Its API flow requires
team IDs and an authenticated White-team account; it is not needed for a new
exam generated from this template.

## Operational notes

- Use one Blue user per team; that account is the identity attached to its flag.
- Keep white-team password authentication enabled for instructors.
- The database revisions include `011_add_exam_participation` and
  `012_current_state_service_scoring`.
- The provisioning API is white-team-only, requires the normal authenticated
  session and CSRF protection, and validates every team before saving.
- Keep the generated `generated-exam/` directory and Salt pillar data private;
  exam flags are bearer login credentials.
