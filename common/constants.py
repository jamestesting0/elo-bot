import os

import interactions as ipy

# Bot configurations
TOKENS = {
    "main": os.getenv("BOT_MAIN_TOKEN"),
    "beta": os.getenv("BOT_BETA_TOKEN")
}
COC_API_KEYS = {
    "main": "Elo-Bot",
    "beta": "Elo-Beta"
}
COC_API_EMAIL = os.getenv("COC_API_EMAIL")
COC_API_PASSWORD = os.getenv("COC_API_PASSWORD")
MAIN_GUILD = 1033924790513311756  # SenFGR's Capital
TEST_GUILD = 888075522536505394   # James Bot Demo
GUILD_IDS = [
    MAIN_GUILD,
    TEST_GUILD,
]
BOT_ID = 1212767017917218876
BASE_STORE_SLOTS = ("Slot 1", "Slot 2")
MONGO_DB_URL = os.getenv("MONGO_DB_URL")
HELPER_API_BASE_URL = os.getenv("HELPER_API_BASE_URL")
HELPER_API_KEY = os.getenv("HELPER_API_KEY")
DATA_BASES = {
    "main": "elo_bot",
    "beta": "elo_beta"
}

# Emojis
SUCCESS_EMOJI = ipy.PartialEmoji.from_str("<:success:827078558220288030>")
ERROR_EMOJI = ipy.PartialEmoji.from_str("<:error:827078558140334100>")
TOKEN_EMOJI = ipy.PartialEmoji(name="token", id=1555865080711876660)
STAR_EMOJI = ipy.PartialEmoji.from_str("<:cocstar:826130043251982416>")
ALEX_EMOJI = ipy.PartialEmoji(name="alex", id=1194625714281009232)
WIN_EMOJI = ipy.PartialEmoji(name="upkey", id=1213689170342715443)
LOSS_EMOJI = ipy.PartialEmoji(name="downkey", id=1213689168350150696)
TICK_EMOJI = ipy.PartialEmoji(name="whitetick", id=892818096291733616)
CROSS_EMOJI = ipy.PartialEmoji(name="whitecross", id=892818096665006141)
STAFF_EMOJI = ipy.PartialEmoji(name="stafficon", id=822135593059876895)
DISTRICT_EMOJI = ipy.PartialEmoji(name="dh5", id=1213709280889536522)
CLAN_EMOJI = ipy.PartialEmoji(name="clan", id=889118685841743922)
IMPORTANT_EMOJI = ipy.PartialEmoji(name="important", id=885813921552957450, animated=True)
GIVEAWAY_EMOJI = ipy.PartialEmoji(name="giveaway", id=822063886067826761, animated=True)
CLASHING_SWORD_EMOJI = ipy.PartialEmoji(name="swordsclashing", id=911680328660295690, animated=True)
LOADING_EMOJI = ipy.PartialEmoji(name="loading", id=823977927732625440, animated=True)
RANK_EMOJIS = ["🥇", "🥈", "🥉"]
RANK_REVERSED_EMOJIS = ["🤡", "🐷", "🌚"]
BASE_QUALITY_EMOJIS = {
    "Average": "😐",
    "Good": "🎖️",
    "Great": "🏅",
    "Excellent": "🏆",
    "Private": "🔒"
}
TOWNHALL_EMOJIS = {
    1: ipy.PartialEmoji.from_str("<:th1:1029674082947563520>"),
    2: ipy.PartialEmoji.from_str("<:th2:1198390520058216549>"),
    3: ipy.PartialEmoji.from_str("<:th3:1029674081198538783>"),
    4: ipy.PartialEmoji.from_str("<:th4:1029674079780872202>"),
    5: ipy.PartialEmoji.from_str("<:th5:1029674077994098708>"),
    6: ipy.PartialEmoji.from_str("<:th6:1029674076379283508>"),
    7: ipy.PartialEmoji.from_str("<:th7:1029674074567352351>"),
    8: ipy.PartialEmoji.from_str("<:th8:1029674073049010266>"),
    9: ipy.PartialEmoji.from_str("<:th9:1029674071362912277>"),
    10: ipy.PartialEmoji.from_str("<:th10:1029674084902109194>"),
    11: ipy.PartialEmoji.from_str("<:th11:1036199860602417182>"),
    12: ipy.PartialEmoji.from_str("<:th12:826132048548462728>"),
    13: ipy.PartialEmoji.from_str("<:th13:826516473446268979>"),
    14: ipy.PartialEmoji.from_str("<:th14:1029671664985853952>"),
    15: ipy.PartialEmoji.from_str("<:th15:1029066441678401596>"),
    16: ipy.PartialEmoji.from_str("<:th16:1184217457083301978>")
}

# Channels
MATCH_CHANNEL = 1222942524839165982
FINAL_CHANNEL = 1392523333043617992
COMMAND_CHANNEL = 1220331873079922810
STAFF_CHANNEL = 1218071208197623880
CHAT_CHANNEL = 1199770187738644652
BASE_RENEWAL_THREAD = 1283753901241274378
BASE_SUBMISSION_CHANNEL = 1542193510730694727
INFO_CHANNEL = 1224812955447853126
MANAGER_ROLE = 1079148582579470397
RAID_CHANNEL = 1055956414775963688
AUTOMATIC_MATCH_THREAD = 1541500001140613220
BASE_RENEWAL_LOGS_CHANNEL = 1545049991217811576
RAID_AUTOSTORE_LOGS_CHANNEL = 1565690705949761688

# Roles
STAFF_ROLE = 1214058812831694858
MODERATOR_ROLE = 1034850287082553414
SEN_ROLE = 1033926947312517252
ADMIN_ROLE = 1033933093633007707
PLAYER_ROLE = 1219642440831467631
TESTER_ROLE = 1315510481552670741
ELO_BLACKLIST_ROLE = 1369255395482402827

# Colors
COLOR = 0x5951FE
RED_COLOR = 0xBE0000
GREEN_COLOR = 0x9EDE73

# Users
SEN_USER = 920112329344036924

# Images
FOOTER_IMAGE = "https://cdn.discordapp.com/attachments/1213353024442073178/1442599647905316865/a735fcd211f5dbf9ecdab5222db0f9a0.webp?ex=6926051e&is=6924b39e&hm=6fec17f97508157e46c6448efe57e100de38d2753c91ec0553f41659563105d2&"
UNAVAILABLE_IMAGE = "https://cdn.discordapp.com/attachments/881073424884199435/1223206051264987188/unavailable-image.jpg?ex=66190268&is=66068d68&hm=9c4190435c2fbb230cd3249c42c26951fc9b1a34a7e3fcd85e534e2775419b93&"
GOLDPASS_IMAGE = "https://cdn.discordapp.com/attachments/1248641611676778528/1285951321928634369/7dd2d1941fdca5d48dc88c4e49c45746.png?ex=66fb4c3e&is=66f9fabe&hm=5fc8240a1a17dfe3c5ae7b38c82931395fbcab7b178f9ebfbbadddbd602aafde&"
REWARDS_IMAGE = "https://cdn.discordapp.com/attachments/1156184370508476497/1298351529069252730/Clash-of-Clans-best-clan-capital-base-layouts-june-2023.webp?ex=67193f96&is=6717ee16&hm=9b6ea9b4c1475c93c3d6e20138b43944b1b638cfe5181dd4849edf781682d393&"
BANNER_IMAGE = "https://cdn.discordapp.com/attachments/1213353024442073178/1559238872343519382/ChatGPT_Image_Oct_12_2026_05_50_18_PM_copy.png?ex=6ace59dc&is=6acd085c&hm=7117a3a1767ff243cd31bca22411a7324c359f294ffffdc8bcb636ca72f0953c&"

# Permission roles
LVL1_ROLES = [SEN_ROLE, ADMIN_ROLE, MANAGER_ROLE, 1339318723957489727]
LVL2_ROLES = LVL1_ROLES + [STAFF_ROLE]
LVL3_ROLES = LVL2_ROLES + [PLAYER_ROLE]
WHITELISTED_STAFFS = [
    1279092301825704038,  # jpk
    1131862892841533460,  # Anshu
    418247253807857664,  # ???
]

# League Information
LEAGUES = {
    "Miner Spammer": (0, 999),
    "Amateur": (1000, 1199),
    "Battle-Tested": (1200, 1399),
    "Unstoppable": (1400, 1599),
    "Touch Grass Please": (1600, 9999)
}
LEAGUE_EMOJIS = {
    "Miner Spammer": "<:SuperMiner:1117878503564652649>",
    "Amateur": "<:golme:1076988404773945414>",
    "Battle-Tested": "<:knight:1212989400611291176>",
    "Unstoppable": "<:superchad:1038633927176376381>",
    "Touch Grass Please": "<:touchgrass:1054896140824359053>"
}

# Other
LINE_SEPERATOR_GIF = "https://steemitimages.com/DQmWzwFNgstEC4fJZwFHaAtnn2SnyuQpXmxP5LddJdBjGt5/21.gif"
LB_ENTRIES_PER_PAGE = 10
K = 100  # Maximum elo that can be gained
C = 400  # Magic number
LOSS_FACTOR = 0.8
WIN_FACTOR = 1
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
LB_TYPE_OPTIONS = [
    ipy.StringSelectOption(label="Elo", value="Elo"),
    ipy.StringSelectOption(label="Elo Reversed", value="Elo Reversed"),
    ipy.StringSelectOption(label="Highest Elo", value="Highest Elo"),
    ipy.StringSelectOption(label="Addiction", value="Addiction"),
    ipy.StringSelectOption(label="Winrate", value="Winrate"),
    ipy.StringSelectOption(label="Hitrate", value="Hitrate"),
    ipy.StringSelectOption(label="Tokens", value="Tokens"),
    ipy.StringSelectOption(label="Predictions", value="Predictions"),
    ipy.StringSelectOption(label="Capital Peak Hitrate", value="Capital Peak Hitrate"),
    ipy.StringSelectOption(label="Wizard Valley Hitrate", value="Wizard Valley Hitrate"),
    ipy.StringSelectOption(label="Barbarian Camp Hitrate", value="Barbarian Camp Hitrate"),
    ipy.StringSelectOption(label="Balloon Lagoon Hitrate", value="Balloon Lagoon Hitrate"),
    ipy.StringSelectOption(label="Builder's Workshop Hitrate", value="Builder's Workshop Hitrate"),
    ipy.StringSelectOption(label="Dragon Cliffs Hitrate", value="Dragon Cliffs Hitrate"),
    ipy.StringSelectOption(label="Golem Quarry Hitrate", value="Golem Quarry Hitrate"),
    ipy.StringSelectOption(label="Skeleton Park Hitrate", value="Skeleton Park Hitrate"),
    ipy.StringSelectOption(label="Goblin Mines Hitrate", value="Goblin Mines Hitrate"),
]
MONITORED_COMPONENTS = [
    "thread_delete",
    "undo_base_storage",
    "match_abort"
]
MONITORED_COMMANDS = [
    "/base add",
    "/base remove",
    "/match base set",
    "/challenge override"
]
SPECIAL_MONITORED_COMMANDS = [
    "/match autowin",
    "/thread delete"
]
FOOTER_TEXT = "Clan capital Elo system"
BASE_LINK_PREFIX = "https://link.clashofclans.com/en?action=OpenLayout&id=TH"
PLAYER_LINK_PREFIX = "https://link.clashofclans.com/en?action=OpenPlayerProfile&tag=%23"
BASE_TIERS = {
    "Average": (0, 999),
    "Good": (1000, 1099),
    "Great": (1100, 1199),
    "Excellent": (1200, 9999)
}
ORDINAL_REPRESENTATIONS = ["1st", "2nd", "3rd", "4th", "5th", "6th", "7th"]
BASE_SUBMISSION_THRESHOLD = 1100
