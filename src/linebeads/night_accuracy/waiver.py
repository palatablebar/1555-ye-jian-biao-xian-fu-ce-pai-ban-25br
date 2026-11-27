from ..night_waiver.service import *  # noqa: F401,F403
from ..night_waiver.contracts import (  # noqa: F401
    NightAudit,
    ReplayVerdict,
    WaiverRoster,
    WaiverRule,
    WaiverTicket,
)

# Domain-language aliases used by some shift scripts.
night_waiver_audit = audit_nights  # noqa: F405
evaluate_waiver_nights = audit_nights  # noqa: F405
calibrate_exemption_rule = calibrate_waiver_rule  # noqa: F405
replay_exemption_rule = replay_waiver_rule  # noqa: F405
sign_exemption_roster = sign_waiver_roster  # noqa: F405
issue_night_waivers = sign_waiver_roster  # noqa: F405
WaiverPolicy = WaiverRule
NightWaiver = NightAudit
WaiverDecision = WaiverRoster
