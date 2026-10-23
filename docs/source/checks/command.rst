Command
=======
Runs a configured shell command locally on the scoring worker. The configured
service host and port are passed as ``$1`` and ``$2`` to ``/bin/sh -c``. Use
only trusted competition YAML because the command executes with the worker's
account.

Custom properties:

.. list-table::
   :widths: 25 50

   * - command
     - Shell command to run on the worker. Use ``$1`` for the service host and
       ``$2`` for its port.
