import asyncio
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import interactions as ipy
from interactions.api.http.route import Route

from common.base_fingerprints.artifacts import MAX_IMAGE_BYTES
from common.base_fingerprints.extractor import decode_image
from common.base_fingerprints.types import FingerprintError, ImageAttachment
from elobot.config.emojis import ERROR_EMOJI


BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}
DISCORD_IMAGE_HOSTS = {"cdn.discordapp.com", "media.discordapp.net"}
MAX_SCREENSHOT_BYTES = 10 * 1024 * 1024


class PublicResolver(aiohttp.resolver.DefaultResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        addresses = await super().resolve(host, port, family)
        if any(not ipaddress.ip_address(address["host"]).is_global for address in addresses):
            raise OSError("Private image hosts are forbidden")
        return addresses


def attachment_identity(message) -> ImageAttachment:
    return ImageAttachment(channel_id=str(message.channel.id), message_id=str(message.id),
                           attachment_id=str(message.attachments[0].id))


async def download_base_screenshot(bot: ipy.Client, ctx: ipy.SlashContext, url: str, filename: str) -> bool:
    path = Path(filename)
    accepted = False

    def validate() -> None:
        with decode_image(path) as image:
            if path.stat().st_size > MAX_SCREENSHOT_BYTES:
                for quality in range(90, 19, -10):
                    image.save(path, "WEBP", quality=quality, optimize=True)
                    if path.stat().st_size <= MAX_SCREENSHOT_BYTES:
                        break

    try:
        await download_image(bot, url, None, path)
        await asyncio.to_thread(validate)
        accepted = True
        return True
    except (FingerprintError, OSError):
        await ctx.send(
            f"{ERROR_EMOJI} The screenshot could not be downloaded or is not a supported image. "
            "Upload a static PNG, JPEG or WebP screenshot of your base.", ephemeral=True,
        )
        return False
    finally:
        if not accepted:
            await asyncio.to_thread(path.unlink, missing_ok=True)


async def download_image(bot, url: str, attachment: ImageAttachment | None, path: Path) -> str:
    try:
        async with asyncio.timeout(30):
            if attachment is not None:
                try:
                    channel = await bot.fetch_channel(attachment.channel_id)
                    message = await channel.fetch_message(attachment.message_id, force=True) if channel else None
                    refreshed = next((item for item in message.attachments
                                      if str(item.id) == attachment.attachment_id), None) if message else None
                    if refreshed is not None:
                        url = refreshed.url
                except ipy.errors.HTTPException:
                    pass  # The stored URL may still be usable or refreshable without the message.
            refresh_attempted = False
            connector = aiohttp.TCPConnector(resolver=PublicResolver(), limit=1)
            async with aiohttp.ClientSession(connector=connector, trust_env=False, headers=BROWSER_HEADERS) as session:
                for _ in range(4):
                    parsed = urlparse(url)
                    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.port not in (None, 443):
                        raise FingerprintError("invalid_image_url")
                    try:
                        address = ipaddress.ip_address(parsed.hostname)
                    except ValueError:
                        address = None
                    if address is not None and not address.is_global:
                        raise FingerprintError("invalid_image_url")
                    async with session.get(url, allow_redirects=False) as response:
                        if response.status in {301, 302, 303, 307, 308}:
                            from urllib.parse import urljoin
                            url = urljoin(url, response.headers.get("Location", ""))
                            continue
                        if (response.status in {403, 404} and not refresh_attempted and
                                parsed.hostname in DISCORD_IMAGE_HOSTS and parsed.path.startswith("/attachments/")):
                            refresh_attempted = True
                            result = await bot.http.request(Route("POST", "/attachments/refresh-urls"),
                                                            payload={"attachment_urls": [url]})
                            renewed = next((item.get("refreshed") for item in result.get("refreshed_urls", [])
                                            if item.get("original") == url), None)
                            if renewed:
                                target = urlparse(renewed)
                                # A renewed signature must still refer to the exact stored attachment.
                                if (target.scheme == "https" and target.hostname in DISCORD_IMAGE_HOSTS and
                                        target.path == parsed.path):
                                    url = renewed
                                    continue
                        if response.status != 200:
                            raise FingerprintError("source_unavailable")
                        if response.content_length and response.content_length > MAX_IMAGE_BYTES:
                            raise FingerprintError("image_too_large")
                        content = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            content.extend(chunk)
                            if len(content) > MAX_IMAGE_BYTES:
                                raise FingerprintError("image_too_large")
                        await asyncio.to_thread(path.write_bytes, content)
                        return url
                raise FingerprintError("too_many_image_redirects")
    except (ipy.errors.HTTPException, aiohttp.ClientError, OSError, TimeoutError, ValueError) as exc:
        raise FingerprintError("source_unavailable") from exc
