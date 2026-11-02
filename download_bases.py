"""
Downloads all BaseLayout images from MongoDB to downloaded_bases/<district>/<id>.png

Discord CDN URLs are signed with `ex` (expiry); after that timestamp Discord returns 404.
We use Discord's POST /attachments/refresh-urls endpoint to obtain freshly-signed URLs in
batches, then download with a browser User-Agent (CDN rejects bot UAs).
"""
import asyncio
import io
from pathlib import Path

import interactions as ipy
from interactions.api.http.route import Route
import aiohttp
import certifi
from beanie import init_beanie
from dotenv import load_dotenv
from PIL import Image
from pymongo import AsyncMongoClient

load_dotenv()

from common.constants import MONGO_DB_URL, TOKENS
from database_models import BaseLayout

OUTPUT_DIR = Path("downloaded_bases")
REFRESH_BATCH = 50

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

bot = ipy.Client(token=TOKENS["main"])


async def refresh_urls(urls: list[str]) -> dict[str, str]:
    """Hit POST /attachments/refresh-urls and return {original: refreshed} mapping."""
    mapping: dict[str, str] = {}
    for i in range(0, len(urls), REFRESH_BATCH):
        chunk = urls[i:i + REFRESH_BATCH]
        try:
            result = await bot.http.request(
                Route("POST", "/attachments/refresh-urls"),
                payload={"attachment_urls": chunk},
            )
            for item in result.get("refreshed_urls", []):
                mapping[item["original"]] = item["refreshed"]
            print(f"  refreshed {len(mapping)}/{len(urls)}")
        except Exception as e:
            print(f"  refresh-urls error on chunk {i}: {e}")
    return mapping


async def download_as_png(session: aiohttp.ClientSession, url: str, dest: Path) -> bool:
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                print(f"  HTTP {resp.status}")
                return False
            data = await resp.read()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(io.BytesIO(data)) as img:
            img.convert("RGBA").save(dest, "PNG")
        return True
    except Exception as e:
        print(f"  ERROR: {e}")
        return False


@ipy.listen(ipy.events.Startup)
async def on_start():
    print("➤ Connecting to MongoDB...")
    db_client = AsyncMongoClient(
        MONGO_DB_URL,
        tls=True,
        tlsCAFile=certifi.where(),
        uuidRepresentation="standard",
        connectTimeoutMS=10000,
        serverSelectionTimeoutMS=10000
    )

    print("➤ Initializing Beanie...")
    await init_beanie(
        database=db_client.get_database(name="elo_bot"),
        document_models=[BaseLayout],
        skip_indexes=True
    )

    print("➤ Fetching bases from MongoDB...")
    bases = await BaseLayout.find_all().to_list()
    print(f"  Found {len(bases)} bases.")

    pending = [b for b in bases if not (OUTPUT_DIR / b.district / f"{b.id}.png").exists()]
    skipped = len(bases) - len(pending)
    print(f"  {skipped} already on disk, {len(pending)} to download.\n")

    print("➤ Refreshing CDN URLs in batches...")
    url_map = await refresh_urls([b.image_url for b in pending])
    print(f"  Got {len(url_map)} refreshed URLs.\n")

    ok = fail = 0
    async with aiohttp.ClientSession(headers=BROWSER_HEADERS) as session:
        for i, base in enumerate(pending, 1):
            dest = OUTPUT_DIR / base.district / f"{base.id}.png"
            url = url_map.get(base.image_url, base.image_url)
            prefix = f"[{i}/{len(pending)}] {base.district} / {base.id}.png"

            if await download_as_png(session, url, dest):
                print(f"{prefix} — ok")
                ok += 1
            else:
                print(f"{prefix} — failed")
                fail += 1

    print(f"\nDone. {ok} downloaded, {skipped} already existed, {fail} failed.")
    await db_client.close()
    await bot.stop()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(line_buffering=True)
    asyncio.run(bot.astart())
