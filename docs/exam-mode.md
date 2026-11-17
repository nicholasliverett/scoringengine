# Individual practical exam mode

This fork adds private, per-team participation flags. A Blue team pastes the
contents of the text file installed on its assigned workstation into the Exam
login page. The flag can be used for repeated logins; the first successful
submission records one participation event and awards the configured attendance
credit. A database unique constraint and one transaction protect the award from
concurrent or repeated submissions.

Blue users see the Overview page with their total points, service and threat
check points, and each check's latest status. Check names can be shown or
obscured in the competition configuration. The public
scoreboard, service/check pages, injects, flags, team data, and their APIs are
unavailable to Blue users. White accounts retain the normal password login and
instructor pages. Service checks, scoring, and the configured check ports
continue to operate on the server.

Set `exam_display` on each service in `competition.yaml` to control its overview
label and score category:

- `name` shows the configured service name and counts its points under Services.
- `finding` shows an anonymous `Finding N` label and counts its points under
  Threats.
- `threat` shows an anonymous `Threat N` label and counts its points under
  Threats.

The default is `finding`, which keeps existing configurations anonymous. The
overview shows earned/max points for the total, each category, and every check;
the total includes the configured attendance credit.

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
sudo python3 bin/generate_blue_teams.py 24 --start-ip 192.168.1.10 --deploy-salt
```

Run this on the Salt master. `--deploy-salt` installs the state under
`/srv/salt/practical_exam/`, the generated team pillars under
`/srv/pillar/practical_exam/`, and merges the generated target entries into
`/srv/pillar/top.sls`. It sets the managed directories to `salt:salt` mode
`0750` and the managed files to `salt:salt` mode `0640`, including the two
configured Salt roots. The `salt` user and group must exist, and the command
needs permission to write to both Salt roots.
The roots can be changed with `--salt-state-root` and `--salt-pillar-root`.
Without `--deploy-salt`, the command only writes the private files under
`generated-exam/` as before.

To deploy files already generated in `generated-exam/` without rotating team
passwords or flags, run:

```sh
sudo python3 bin/generate_blue_teams.py --deploy-existing-salt
```

Use `--output-dir` if the existing output is stored elsewhere. Add `--force`
only when you intend to replace files already installed in the Salt roots.
When generating new teams, `--force` also replaces the generated credentials
and flags.

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
- `salt/states/practical_exam/install_exam_flag.sls` installs the target
  team's flag at `/tmp/exam-flag.txt` with read-only mode `0444`, so the
  assigned team can submit it from its VM.

The private output directory and secret files receive owner-only POSIX
permissions where supported. Generated output is ignored by Git, and the
generator refuses to overwrite existing output unless `--force` is supplied.
Use `--flag-path` if the VM's Salt-managed token file should be somewhere other
than `/tmp/exam-flag.txt`.

If you did not use `--deploy-salt`, put
`salt/states/practical_exam/install_exam_flag.sls` under the master's
configured state root, put `salt/pillar/practical_exam/` under a configured
pillar root, and merge the entries from `salt/pillar/top.sls` into that root's
pillar top file. The pillar top assigns each team's private flag to its VM.
The state itself is applied directly by name, so no state top file is needed.
After deploying the files, refresh pillar data and apply the state:

```sh
sudo salt-run fileserver.update
sudo salt '*' saltutil.refresh_pillar
sudo salt -C 'G@ipv4:192.168.1.10' state.apply practical_exam.install_exam_flag
```

Replace the sample IP with the VM's address. To apply to all exam VMs in one
command, target their minion IDs or use a compound expression matching the
assigned IPs. The generator deploys only the flag state and team pillars. To
deploy the separate threat state from the repository on the Salt master:

```sh
sudo install -d -o salt -g salt -m 0750 /srv/salt/practical_exam
sudo install -o salt -g salt -m 0640 salt/practical_exam/deploy_threats.sls /srv/salt/practical_exam/deploy_threats.sls
sudo salt-run fileserver.update
sudo salt -C 'G@ipv4:192.168.1.10' state.apply practical_exam.deploy_threats
```

It creates the visible `threatuser` account and starts the Ncat Bash listener
on port 4444. The listener runs as `threatuser` and provides Bash to clients
that can reach the port, so use it only on the isolated competition network.
The threat template's SSH check logs
in as `scoringcheck` and checks whether `threatuser` exists; configure that
account and password on every VM, or edit the service account in the template.
Generated exam flags are registered when the generated
`competition.yaml` is used to initialize the database. To use this config with
a new database, copy it to `bin/competition.yaml` before the initial Compose
setup. Config changes do not modify teams or services in an already initialized
database.

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
