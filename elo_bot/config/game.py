import interactions as ipy

# Elo
K = 100  # Maximum elo that can be gained
C = 400  # Magic number
LOSS_FACTOR = 0.8
WIN_FACTOR = 1

# Leagues and base tiers
LEAGUES = {
    "Miner Spammer": (0, 999),
    "Amateur": (1000, 1199),
    "Battle-Tested": (1200, 1399),
    "Unstoppable": (1400, 1599),
    "Touch Grass Please": (1600, 9999)
}
BASE_TIERS = {
    "Average": (0, 999),
    "Good": (1000, 1099),
    "Great": (1100, 1199),
    "Excellent": (1200, 9999)
}
BASE_SUBMISSION_THRESHOLD = 1100

# Districts
DISTRICT_ORDER = {
    "Capital Peak": 0,
    "Wizard Valley": 1,
    "Barbarian Camp": 1,
    "Skeleton Park": 7,
    "Balloon Lagoon": 3,
    "Builder's Workshop": 4,
    "Golem Quarry": 6,
    "Dragon Cliffs": 5,
    "Goblin Mines": 8
}
DISTRICT_INDEX = {
    "Barbarian Camp": 0,
    "Skeleton Park": 0,
    "Wizard Valley": 0,
    "Balloon Lagoon": 1,
    "Builder's Workshop": 1,
    "Golem Quarry": 1,
    "Dragon Cliffs": 2,
    "Goblin Mines": 2,
    "Capital Peak": 2
}
DISTRICT_CODES = {
    "Barbarian Camp": "3A1",
    "Skeleton Park": "3A7",
    "Balloon Lagoon": "3A3",
    "Builder's Workshop": "3A4",
    "Golem Quarry": "3A6",
    "Dragon Cliffs": "3A5",
    "Goblin Mines": "3A8",
    "Wizard Valley": "3A2",
    "Capital Peak": "3A0"
}
DISTRICT_CODES_REVERSED = {
    "3A1": "Barbarian Camp",
    "3A7": "Skeleton Park",
    "3A3": "Balloon Lagoon",
    "3A4": "Builder's Workshop",
    "3A6": "Golem Quarry",
    "3A5": "Dragon Cliffs",
    "3A8": "Goblin Mines",
    "3A2": "Wizard Valley",
    "3A0": "Capital Peak"
}
DISTRICT_CHOICES = [
    ipy.SlashCommandChoice(name="Capital Peak", value="Capital Peak"),
    ipy.SlashCommandChoice(name="Wizard Valley", value="Wizard Valley"),
    ipy.SlashCommandChoice(name="Barbarian Camp", value="Barbarian Camp"),
    ipy.SlashCommandChoice(name="Balloon Lagoon", value="Balloon Lagoon"),
    ipy.SlashCommandChoice(name="Builder's Workshop", value="Builder's Workshop"),
    ipy.SlashCommandChoice(name="Dragon Cliffs", value="Dragon Cliffs"),
    ipy.SlashCommandChoice(name="Golem Quarry", value="Golem Quarry"),
    ipy.SlashCommandChoice(name="Skeleton Park", value="Skeleton Park"),
    ipy.SlashCommandChoice(name="Goblin Mines", value="Goblin Mines")
]

# Host clans
HOST_CLAN_CHOICES = [
    ipy.SlashCommandChoice(name="Balloon Moon", value="#2RY0QP8GY"),
    ipy.SlashCommandChoice(name="Sen's Fortress", value="#2JGGGR2QL"),
    ipy.SlashCommandChoice(name="BAMBOO DONATER", value="#2YVGQ2YQL"),
    ipy.SlashCommandChoice(name="2 duals in 1", value="#2J9GV29RV"),
    ipy.SlashCommandChoice(name="Helper's Forge", value="#2J9CYJG88"),
    ipy.SlashCommandChoice(name="The Senate", value="#2RGULPGUY"),
    ipy.SlashCommandChoice(name="Silent Stock", value="#2GYQQGPUP"),
    ipy.SlashCommandChoice(name="✨✨✨✨✨✨✨✨✨✨", value="#2R0VLCRPV"),
    ipy.SlashCommandChoice(name="Star ⭐️", value="#2RRPQYCQY"),
    ipy.SlashCommandChoice(name="Asterism", value="#2JYGQG9G0")
]
HOST_CLAN_OPTIONS = [ipy.StringSelectOption(label=str(c.name), value=str(c.value)) for c in HOST_CLAN_CHOICES]

# Stored bases
BASE_STORE_SLOTS = ("Slot 1", "Slot 2")
