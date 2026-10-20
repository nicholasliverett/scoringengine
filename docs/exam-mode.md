# Individual practical exam mode

This fork adds private, per-team participation flags. A Blue team pastes the
contents of the text file installed on its assigned workstation into the Exam
login page. The flag can be used for repeated logins; the first successful submission records one
participation event and awards the configured attendance credit. A database
unique constraint and one transaction protect the award from concurrent or
repeated submissions.

Blue users see only Overview and Stats. These pages show their own points and
round history. The public scoreboard, service/check pages, injects, flags,
team data, and their APIs are unavailable to Blue users. White accounts retain
the normal password login and instructor pages. Service checks, scoring, and
the configured check ports continue to operate on the server.

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
Non-positive values are treated as zero. For an existing installation, apply
the new database migration before the exam:

```sh
docker compose build bootstrap
docker compose run --rm --no-deps --entrypoint python bootstrap /app/bin/migrate
```

For a fresh database, the normal bootstrap creates all model tables and stamps
the current Alembic head.

## Provision flags

Create a private inventory CSV:

```csv
team_id,machine
12,student-01
13,student-02
```

Use a white-team account to create the exports and register only their hashes
in the application:

```sh
python bin/provision_exam_flags.py \
  --base-url https://exam.example.edu \
  --inventory exam-machines.csv \
  --output-dir /secure/exam-flags \
  --username instructor
```

The script prompts for the account password when omitted by the shell wrapper;
for automated use provide it through a protected secret source. (Avoid putting
it in shell history.) The output directory is created if needed. Each
`team-<id>-<machine>.txt` contains a high-entropy random flag and is written
with owner-only permissions where the operating system supports them. Copy
each file to the corresponding student's workstation using your existing
configuration management. Protect both the export directory and transport;
the flag is a bearer login credential. HTTPS is required unless `--allow-http`
is explicitly supplied on an isolated trusted network.

Rerunning without `--rotate` reuses existing files and is idempotent. The
application never returns plaintext flags and stores only SHA-256 hashes. To
replace compromised or lost credentials, rerun with `--rotate`; deploy the new
files to the workstations promptly because the previous flags stop working
when registration commits. If an interrupted rotation leaves local files and
database state uncertain, rerun with `--rotate` and distribute the newly
generated export.

## Operational notes

- Use one Blue user per team; that account is the identity attached to its flag.
- Keep white-team password authentication enabled for instructors.
- The database revisions include `011_add_exam_participation` and
  `012_current_state_service_scoring`.
- The provisioning API is white-team-only, requires the normal authenticated
  session and CSRF protection, and validates every team before saving.
- Export files are deliberately not emitted by `docker compose up`: flags are
  secrets, and provisioning must happen after the machine-to-team inventory is
  known. The script is suitable for an IaC job after the application is live.
