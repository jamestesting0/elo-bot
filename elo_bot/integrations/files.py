import asyncio
import json
import os

import aiofiles


async def delete_file(file_path: str):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, os.remove, file_path)


async def read_json(file_name: str, dir_path: str = "data/"):
    for i in range(3):
        try:
            async with aiofiles.open(f"{dir_path}{file_name}", 'r') as file:
                data = await file.read()
                return json.loads(data)
        except json.decoder.JSONDecodeError:
            await asyncio.sleep(0.1)


async def write_json(file_name: str, data, dir_path: str = "data/"):
    async with aiofiles.open(f"{dir_path}{file_name}", 'w') as file:
        await file.write(json.dumps(data, indent=4))
