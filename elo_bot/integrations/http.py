import os

import aiohttp
import validators
from PIL import Image, UnidentifiedImageError


async def download_file(url: str, filename: str) -> bool:
    max_size = 10 * 1024 * 1024  # 8 MB

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status != 200:
                return False

            # temporary save of original file
            with open(filename, 'wb') as f:
                async for chunk in response.content.iter_chunked(1024):
                    f.write(chunk)

    # check size
    if os.path.getsize(filename) > max_size:
        try:
            with Image.open(filename) as img:
                quality = 90
                # save as webp
                img.save(filename, "WEBP", quality=quality, optimize=True)

                # if still over 10MB, keep reducing quality
                while os.path.getsize(filename) > max_size and quality > 20:
                    quality -= 10
                    img.save(filename, "WEBP", quality=quality, optimize=True)

            return True
        except (OSError, IOError, UnidentifiedImageError):
            return False

    return True


async def is_url_image(image_url: str) -> True:
    if not validators.url(image_url):
        return False

    image_formats = ("image/png", "image/jpeg", "image/jpg")
    async with aiohttp.ClientSession() as session:
        async with session.head(image_url) as r:
            content_type = r.headers.get("content-type")
            if content_type in image_formats:
                return True
            return False
