from scoring_engine.engine.basic_check import BasicCheck


class CommandCheck(BasicCheck):
    """Run trusted competition shell commands on the scoring worker."""

    required_properties = ["command"]
    CMD = "/bin/sh -c {0} command-check {1} {2}"

    def command_format(self, properties):
        return (properties["command"], self.host, self.port)
