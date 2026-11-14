from elobot.db import ServerState
from elobot.integrations.files import read_json


async def choose_tip():
    tips: list = await read_json("tips.json", "assets/")
    facts: list = await read_json("facts.json", "assets/")
    count = len(tips) + len(facts)
    index = await ServerState.advance_tip_index(count)
    facts_before = index * len(facts) // count

    if (index + 1) * len(facts) // count > facts_before:
        return f"💡 **ELO FACT:** \"{facts[facts_before]}\""
    return f"💡 **TIP:** \"{tips[index - facts_before]}\""
