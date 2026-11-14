import os

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
MONGO_DB_URL = os.getenv("MONGO_DB_URL")
HELPER_API_BASE_URL = os.getenv("HELPER_API_BASE_URL")
HELPER_API_KEY = os.getenv("HELPER_API_KEY")
DATA_BASES = {
    "main": "elo_bot",
    "beta": "elo_beta"
}
