import coc
import interactions as ipy

from elobot.discord import runtime
from elobot.config.ids import RAID_CHANNEL
from elobot.features.base_store.slots import clear_all_base_store_slots


@ipy.listen(ipy.events.Startup)
async def on_start():
    print('Logged in as')
    print(runtime.bot.user.username)
    print(runtime.bot.user.id)
    print(ipy.__version__)
    print(coc.__version__)
    print('------')


@coc.ClientEvents.raid_weekend_end()
async def on_raid_weekend_end():
    await clear_all_base_store_slots(bot=runtime.bot)

    channel = await runtime.bot.fetch_channel(RAID_CHANNEL)

    for message in await channel.fetch_messages():
        if message.interaction and int(message.interaction.bot.user.id) == int(runtime.bot.user.id):
            await message.delete()
