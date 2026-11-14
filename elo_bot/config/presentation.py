import interactions as ipy

# Colors
COLOR = 0x5951FE
RED_COLOR = 0xBE0000
GREEN_COLOR = 0x9EDE73

# Images, served from assets/ on the main branch
ASSETS_URL = "https://raw.githubusercontent.com/jamestesting0/elo-bot/main/assets"
FOOTER_IMAGE = f"{ASSETS_URL}/footer.png"
UNAVAILABLE_IMAGE = f"{ASSETS_URL}/unavailable.png"
GOLDPASS_IMAGE = f"{ASSETS_URL}/goldpass.png"
REWARDS_IMAGE = f"{ASSETS_URL}/rewards.png"
BANNER_IMAGE = f"{ASSETS_URL}/banner.png"
LINE_SEPERATOR_GIF = f"{ASSETS_URL}/line_separator.gif"
# Stored on match bases and base layouts saved before the images moved to assets/.
LEGACY_UNAVAILABLE_IMAGE = "https://cdn.discordapp.com/attachments/881073424884199435/1223206051264987188/unavailable-image.jpg?ex=66190268&is=66068d68&hm=9c4190435c2fbb230cd3249c42c26951fc9b1a34a7e3fcd85e534e2775419b93&"

# Text and links
FOOTER_TEXT = "Clan capital Elo system"
BASE_LINK_PREFIX = "https://link.clashofclans.com/en?action=OpenLayout&id=TH"
PLAYER_LINK_PREFIX = "https://link.clashofclans.com/en?action=OpenPlayerProfile&tag=%23"
ORDINAL_REPRESENTATIONS = ["1st", "2nd", "3rd", "4th", "5th", "6th", "7th"]

# Leaderboards
LB_ENTRIES_PER_PAGE = 10
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
