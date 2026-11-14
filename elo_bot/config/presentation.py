import interactions as ipy

# Colors
COLOR = 0x5951FE
RED_COLOR = 0xBE0000
GREEN_COLOR = 0x9EDE73

# Images
FOOTER_IMAGE = "https://cdn.discordapp.com/attachments/1213353024442073178/1442599647905316865/a735fcd211f5dbf9ecdab5222db0f9a0.webp?ex=6926051e&is=6924b39e&hm=6fec17f97508157e46c6448efe57e100de38d2753c91ec0553f41659563105d2&"
UNAVAILABLE_IMAGE = "https://cdn.discordapp.com/attachments/881073424884199435/1223206051264987188/unavailable-image.jpg?ex=66190268&is=66068d68&hm=9c4190435c2fbb230cd3249c42c26951fc9b1a34a7e3fcd85e534e2775419b93&"
GOLDPASS_IMAGE = "https://cdn.discordapp.com/attachments/1248641611676778528/1285951321928634369/7dd2d1941fdca5d48dc88c4e49c45746.png?ex=66fb4c3e&is=66f9fabe&hm=5fc8240a1a17dfe3c5ae7b38c82931395fbcab7b178f9ebfbbadddbd602aafde&"
REWARDS_IMAGE = "https://cdn.discordapp.com/attachments/1156184370508476497/1298351529069252730/Clash-of-Clans-best-clan-capital-base-layouts-june-2023.webp?ex=67193f96&is=6717ee16&hm=9b6ea9b4c1475c93c3d6e20138b43944b1b638cfe5181dd4849edf781682d393&"
BANNER_IMAGE = "https://cdn.discordapp.com/attachments/1213353024442073178/1559238872343519382/ChatGPT_Image_Oct_12_2026_05_50_18_PM_copy.png?ex=6ace59dc&is=6acd085c&hm=7117a3a1767ff243cd31bca22411a7324c359f294ffffdc8bcb636ca72f0953c&"
LINE_SEPERATOR_GIF = "https://steemitimages.com/DQmWzwFNgstEC4fJZwFHaAtnn2SnyuQpXmxP5LddJdBjGt5/21.gif"

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
