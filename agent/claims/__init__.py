"""Additional text checks shared by present cards and the guide.

Each check takes (card, card_input, output, rules) and returns a list of issues:
{code, severity, message, data}, as in agent.guards. Checks return [] when they
do not apply. They must not mutate the inputs or call models or the network.

Register new checks here, after TEXT_CHECKS is defined:
    from .personal import check_history
    TEXT_CHECKS.append(check_history)

Registration order is execution order. Existing guards stay in agent.guards.
"""

TEXT_CHECKS = []

from .cheaper import check_cheaper
TEXT_CHECKS.append(check_cheaper)
