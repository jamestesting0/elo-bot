from typing import Final

import interactions


class ChannelUserConcurrencyBucket:
    name = "channel and user"

    async def __call__(self, context: interactions.BaseContext) -> tuple[int, int]:
        return context.channel.id, context.author.id


CHANNEL_USER_CONCURRENCY_BUCKET: Final = ChannelUserConcurrencyBucket()
