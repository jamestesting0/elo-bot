import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from PIL import Image, ImageChops, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
EMOJI_DIR = ROOT / "assets" / "emojis"
MAX_BYTES = 256 * 1024
API = "https://discord.com/api/v10"
APP_IDS = {"beta": "1245032861245898783", "main": "1212767017917218876"}


def request(path: str, token: str, payload: dict | None = None) -> dict:
    headers = {"Authorization": f"Bot {token}", "User-Agent": "DiscordBot (https://github.com/jamestesting0/elo-bot, 1)"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = Request(API + path, headers=headers, data=json.dumps(payload).encode() if payload is not None else None)
    for attempt in range(3):
        try:
            with urlopen(req, timeout=15) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code == 429 and attempt < 2:
                retry = float(json.load(error)["retry_after"])
                if retry <= 60:
                    time.sleep(retry)
                    continue
            raise RuntimeError(f"Discord returned HTTP {error.code} for {req.method} {path}") from None


def avatar_image(token: str) -> bytes:
    bot = request("/users/@me", token)
    if not bot.get("avatar"):
        raise RuntimeError("The bot has no custom avatar to use in the footer.")
    url = f"https://cdn.discordapp.com/avatars/{bot['id']}/{bot['avatar']}.png?size=128"
    response = subprocess.run(["curl", "-fsS", "--max-time", "10", url], capture_output=True, check=True)
    with Image.open(BytesIO(response.stdout)) as source:
        image = source.convert("RGBA")
    mask = Image.new("L", image.size)
    ImageDraw.Draw(mask).ellipse((0, 0, image.width - 1, image.height - 1), fill=255)
    image.putalpha(ImageChops.multiply(image.getchannel("A"), mask))
    with BytesIO() as output:
        image.save(output, format="PNG")
        return output.getvalue()


def source_image(entry: dict, *, avatar: bytes | None = None) -> str:
    animated = entry.get("animated", False)
    extension = "gif" if animated else "png"
    if entry.get("avatar"):
        if avatar is None:
            raise ValueError("The bot avatar was not supplied.")
        data = avatar
    elif filename := entry.get("file"):
        data = (EMOJI_DIR / filename).read_bytes()
    else:
        legacy = entry["legacy_id"]
        urls = (
            f"https://cdn.discordapp.com/emojis/{legacy}.{extension}",
            f"https://media.discordapp.net/emojis/{legacy}.{extension}?size=128",
        )
        for url in urls:
            response = subprocess.run(["curl", "-fsS", "--max-time", "10", url], capture_output=True, check=False)
            if response.returncode == 0:
                data = response.stdout
                break
        else:
            raise RuntimeError(f"Cannot download emoji {legacy}")
    if len(data) > MAX_BYTES:
        raise ValueError("Emoji exceeds Discord's upload limit")
    with Image.open(BytesIO(data)) as image:
        if bool(getattr(image, "is_animated", False)) != animated:
            raise ValueError("Emoji animation differs from the catalog")
        extension = image.format.lower()
        image.verify()
    return f"data:image/{extension};base64,{base64.b64encode(data).decode()}"


def sync(version: str) -> None:
    load_dotenv(ROOT / ".env")
    token = os.environ[f"BOT_{version.upper()}_TOKEN"]
    app_id = str(request("/oauth2/applications/@me", token)["id"])
    if app_id != APP_IDS[version]:
        raise RuntimeError(f"The token does not belong to the EloBot {version} application")
    endpoint = f"/applications/{app_id}/emojis"
    sources = json.loads((EMOJI_DIR / "application_sources.json").read_text())
    existing = {emoji["name"]: emoji for emoji in request(endpoint, token)["items"]}
    missing = {name: entry for name, entry in sources.items() if name not in existing}
    avatar = avatar_image(token) if any(entry.get("avatar") for entry in missing.values()) else None
    with ThreadPoolExecutor(max_workers=8) as executor:
        images = dict(zip(missing, executor.map(partial(source_image, avatar=avatar), missing.values()), strict=True))
    for name, data in images.items():
        request(endpoint, token, {"name": name, "image": data})
        print(f"{version}: uploaded {name}", flush=True)
    uploaded = {emoji["name"]: emoji for emoji in request(endpoint, token)["items"]}
    if not sources.keys() <= uploaded.keys():
        raise RuntimeError(f"Discord did not return every emoji for {version}")
    if any(uploaded[name]["animated"] != entry.get("animated", False) for name, entry in sources.items()):
        raise RuntimeError(f"Discord returned an incorrect animation flag for {version}")
    destination = EMOJI_DIR / "application_ids.json"
    all_ids = json.loads(destination.read_text()) if destination.exists() else {}
    all_ids[version] = {name: str(uploaded[name]["id"]) for name in sources}
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(all_ids, indent=2, sort_keys=True) + "\n")
    temporary.replace(destination)
    print(f"{version}: verified {len(sources)} application emojis ({len(images)} uploaded)", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Upload the EloBot emoji catalog and save its Discord IDs.")
    parser.add_argument("version", choices=APP_IDS)
    sync(parser.parse_args().version)
