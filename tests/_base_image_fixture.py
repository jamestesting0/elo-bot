from unittest.mock import AsyncMock, MagicMock


OLD_URL = "https://cdn.discordapp.com/attachments/100/200/base.png?ex=1&hm=old"

FRESH_URL = "https://cdn.discordapp.com/attachments/100/200/base.png?ex=ffffffff&hm=new"

def image_response(status=200, content=b"", headers=None, length=None):
    async def chunks(_size):
        yield content

    response = MagicMock(status=status, headers=headers or {}, content_length=length)
    response.content.iter_chunked = chunks
    response.__aenter__ = AsyncMock(return_value=response)
    return response
