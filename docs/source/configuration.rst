Configuration
*************

Location to config file
-----------------------
Docker
^^^^^^
.. note:: This file needs to be edited before running the make commands.

::

  <path to source root>/docker/engine.conf.inc

Manual
^^^^^^
.. note:: Need to restart each scoring engine service once the config is modified.

::

  /home/engine/scoring_engine/src/engine.conf


Configuration Keys
------------------
.. note:: Each of these config keys can be expressed via environment variables (and take precendence over the values defined in the file). IE: To define target_round_time, I'd set SCORINGENGINE_TARGET_ROUND_TIME=3.

.. note:: An environment variable that exists but is **empty** (or whitespace only) is treated as *unset* and does **not** override the config file. A leftover ``SCORINGENGINE_FOO=`` line in a copied ``.env`` therefore cannot silently blank out a value you configured in ``engine.conf``. If you genuinely want an option to be empty, leave it empty in the config file instead (this is how ``redis_password`` ships).

.. list-table::
   :widths: 25 50
   :header-rows: 1

   * - Key Name
     - Description
   * - checks_location
     - Local path to directory of checks
   * - target_round_time
     - Length of time (seconds) the engine should target per round
   * - agent_psk
     - The pre-shared key used for encryption between BTA and the Scoring Engine
   * - agent_show_flag_early_mins
     - The length of time in minutes before a flag becomes active that BTA can grab the flag details
   * - worker_refresh_time
     - Amount of time (seconds) the engine will sleep for in-between polls of worker status
   * - max_consecutive_round_failures
     - How many rounds may fail back to back before the engine gives up and exits non-zero. A failed round is rolled back and retried (transient database blips should not stop a competition), but an error that repeats every round is not transient, so the engine exits and lets the container restart make the problem visible. Default is 3. Set to 0 to retry forever (not recommended)
   * - worker_num_concurrent_tasks
     - The number of concurrent tasks the worker will run. Set to -1 to default to number of processors.
   * - worker_queue
     - The queue name for a worker to pull tasks from. This can be used to control which workers get which service checks. Default is 'main'
   * - blue_team_update_hostname
     - A boolean indicating if blue teams should be allowed to update the hostnames associated for scored checks
   * - blue_team_update_port
     - A boolean indicating if blue teams should be allowed to update the port associated for scored checks
   * - blue_team_update_account_usernames
     - A boolean indicating if blue teams should be allowed to change usernames associated with scored checks
   * - blue_team_update_account_passwords
     - A boolean indicating if blue teams should be allowed to change passwords of scored users
   * - blue_team_view_check_output
     - A boolean indicating if blue teams should be allowed to view verbose output from checks
   * - timezone
     - Local timezone of the competition
   * - debug
     - Determines wether or not the engine should be run in debug mode (useful for development). The worker will also display output from all checks.
   * - secret_key
     - The key Flask uses to sign session cookies. Must be a long random value and must be identical across restarts and across every web process/container. See `Session Secret Key`_.
   * - db_uri
     - Database connection URI
   * - cache_type
     - The type of storage for the cache. Set to null to disable caching
   * - redis_host
     - The hostname/ip of the redis server
   * - redis_port
     - The port of the redis server
   * - redis_password
     - The password used to connect to redis (if no password, leave empty)
   * - session_cookie_secure
     - A boolean marking the session and remember-me cookies "Secure" so browsers only send them over HTTPS. The bundled docker compose stack terminates TLS at nginx and sets this to True. Leave it False for a plain-HTTP dev run, otherwise the browser drops the session cookie and logins silently fail. (default: False)
   * - sla_enabled
     - A boolean to enable/disable SLA penalties for consecutive service failures
   * - sla_penalty_threshold
     - Number of consecutive failures before penalties begin (default: 5)
   * - sla_penalty_percent
     - Penalty percentage per failure after threshold (default: 10)
   * - sla_penalty_max_percent
     - Maximum total penalty percentage cap (default: 50)
   * - sla_penalty_mode
     - Penalty calculation mode: additive, flat, exponential, or next_check_reduction
   * - sla_allow_negative
     - A boolean to allow scores to go negative from penalties
   * - dynamic_scoring_enabled
     - A boolean to enable/disable time-based scoring multipliers
   * - dynamic_scoring_early_rounds
     - Number of rounds in the early phase (default: 10)
   * - dynamic_scoring_early_multiplier
     - Points multiplier for early phase (default: 2.0)
   * - dynamic_scoring_late_start_round
     - Round number when late phase begins (default: 50)
   * - dynamic_scoring_late_multiplier
     - Points multiplier for late phase (default: 0.5)
   * - inject_scores_visible
     - A boolean to show inject scores on the public scoreboard (default: False, private grading)

Practical exam check appearance
-------------------------------

For practical exam mode, each Blue-team service in ``bin/competition.yaml``
can set ``exam_display`` to control its label on the Blue Overview and the
score category for its points. The sample competition marks SSH and HTTP as
named availability services:

.. code-block:: yaml

   - name: SSH
     exam_display: name
     check_name: SSHCheck
     port: 22
     points: 150
   - name: HTTP
     exam_display: name
     check_name: HTTPCheck
     port: 80
     points: 100

Use these values:

* ``name`` displays the configured service name and counts points as Service
  check points.
* ``finding`` displays an anonymous ``Finding N`` label and counts points as
  Threat check points. This is the default when the field is omitted.
* ``threat`` displays an anonymous ``Threat N`` label and also counts points
  as Threat check points.

The field belongs on each service entry. Updating ``bin/competition.yaml``
affects a newly initialized database; an existing database must have its
service ``exam_display`` values updated separately.


Session Secret Key
------------------

The web application signs session cookies with ``secret_key``. It is the only
configuration key that is security critical *and* has no default, so it is worth
calling out separately.

**Why it must be set**

* If the key changes, every existing session cookie becomes invalid and all
  users -- including white team -- are logged out. A key that is generated at
  startup therefore logs everyone out on every restart, redeploy, or crash loop.
* If two web processes or containers hold different keys, a session issued by
  one is rejected by the other. That makes it impossible to run more than one
  web replica behind a load balancer, i.e. it blocks horizontal scaling
  entirely.

**Why there is no default**

No key is shipped in ``engine.conf.inc``. A fixed default that operators forget
to change would let anyone who has read the source forge a session cookie for
any team, including white team. An unset key is a warning; a shared default key
would be a vulnerability.

**Generating a key**

Generate it yourself, on a machine you trust:

::

  python -c "import secrets; print(secrets.token_hex(64))"

The engine never generates a key *for* you and prints it. Anything written to
stdout ends up in the container log stream, which is readable by anyone who can
run ``docker logs`` and is frequently shipped to a log aggregator -- a signing
key that appears there should be considered compromised.

**Setting it**

Docker (recommended -- ``docker/engine.conf.inc`` is baked into the image at
build time, so use the environment instead):

::

  # in .env, next to the other credentials
  SCORINGENGINE_SECRET_KEY=<paste the generated value>

The ``web``, ``engine``, and ``bootstrap`` services in ``docker-compose.yml``
already pass this variable through. ``.env.example`` ships this line commented
out on purpose: an empty ``SCORINGENGINE_SECRET_KEY=`` is still an assignment,
and compose would forward it into the containers as an empty string. (Since an
empty environment variable is treated as unset, it no longer overrides the
config file -- but a commented-out line makes the intent unambiguous.)

Manual install, in ``engine.conf``:

::

  secret_key = <paste the generated value>

**If it is not set**

The application still starts. It generates a random key for that process and
logs a warning explaining that sessions will not survive a restart and cannot
scale beyond one process. ``bin/setup`` performs the same check and warns with
the command to generate a key -- it does not print a key. This is fine for local
development and never fine for a competition.

.. warning:: Treat the key like a password. Do not commit it, and rotate it if it leaks -- rotating logs everyone out, which is the intended effect.
