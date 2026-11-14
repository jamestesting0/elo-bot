from dotenv import load_dotenv

load_dotenv()

from elobot.app.bootstrap import run

if __name__ == "__main__":
    run(__file__)
