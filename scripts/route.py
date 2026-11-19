"""Point a SIP dispatch rule at an agent name, so one phone number can switch between running workers.

Usage: route.py <rule name or id> [agent name]. Without an agent name it prints where the rule points now.
Reads LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET from the environment.
"""
from __future__ import annotations

import asyncio
import sys

from livekit import api


async def main(rule_ref: str, agent: str | None) -> int:
    lk = api.LiveKitAPI()
    try:
        rules = (await lk.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())).items
        rule = next((r for r in rules if rule_ref in (r.sip_dispatch_rule_id, r.name)), None)
        if rule is None:
            print(f"no dispatch rule named {rule_ref}", file=sys.stderr)
            return 1
        current = ", ".join(a.agent_name for a in rule.room_config.agents) or "none"
        if not agent:
            print(f"{rule.name}: {current}")
            return 0
        del rule.room_config.agents[:]
        rule.room_config.agents.append(api.RoomAgentDispatch(agent_name=agent))
        await lk.sip.update_dispatch_rule(rule.sip_dispatch_rule_id, rule)
        rules = (await lk.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())).items
        after = next(r for r in rules if r.sip_dispatch_rule_id == rule.sip_dispatch_rule_id)
        now = ", ".join(a.agent_name for a in after.room_config.agents) or "none"
        print(f"{after.name}: {current} -> {now}")
        return 0 if now == agent else 1
    finally:
        await lk.aclose()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)))
