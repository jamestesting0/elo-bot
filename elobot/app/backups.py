import interactions as ipy
from pymongo import AsyncMongoClient

from elobot.app import runtime
from elobot.config.settings import DATA_BASES


async def backup_collection(src_db, dst_db, coll_name):
    src_coll = src_db[coll_name]
    dst_coll = dst_db[coll_name]

    async for doc in src_coll.find({}):
        await dst_coll.replace_one({"_id": doc["_id"]}, doc, upsert=True)

    src_ids = await src_coll.distinct("_id")
    await dst_coll.delete_many({"_id": {"$nin": src_ids}})


async def sync_database(src_db, dst_db):
    collections = [
        name for name in await src_db.list_collection_names()
        if not name.startswith("system.")
    ]

    for coll in collections:
        await backup_collection(src_db, dst_db, coll)


@ipy.Task.create(ipy.IntervalTrigger(hours=2))
async def backup_database1(client: AsyncMongoClient):
    src_db = client[DATA_BASES[runtime.VERSION]]

    collections = [
        name for name in await src_db.list_collection_names()
        if not name.startswith("system.")
    ]

    for coll in collections:
        await backup_collection(src_db, client["elo_backup1"], coll)


@ipy.Task.create(ipy.IntervalTrigger(hours=24))
async def backup_database2(client: AsyncMongoClient):
    src_db = client[DATA_BASES[runtime.VERSION]]

    collections = [
        name for name in await src_db.list_collection_names()
        if not name.startswith("system.")
    ]

    for coll in collections:
        await backup_collection(src_db, client["elo_backup2"], coll)
