import asyncio
import os
import time
from collections import defaultdict

import certifi
import coc
from beanie import init_beanie
from pymongo import AsyncMongoClient

from elo_bot.features.bases.fingerprints.service import FingerprintService
from elo_bot.features.finals.persistence import register_final_indexes
from elo_bot.app import root_callbacks
from elo_bot.discord import runtime
from elo_bot.app.backups import backup_database1, backup_database2, sync_database
from elo_bot.app.client import create_client
from elo_bot.app.events import on_raid_weekend_end
from elo_bot.app.extensions import EXTENSIONS
from elo_bot.config.settings import COC_API_EMAIL, COC_API_KEYS, COC_API_PASSWORD, DATA_BASES, MONGO_DB_URL
from elo_bot.db import (
    AUDIT_LOG_INDEXES,
    GIVEAWAY_INDEXES,
    OVERRIDE_CODE_INDEXES,
    AuditLogEntry,
    BaseExtractionRun,
    BaseFingerprintTask,
    BaseLayout,
    Clan,
    EloFinal,
    Giveaway,
    Helper,
    HelperTask,
    Match,
    OverrideCode,
    PendingBaseSubmission,
    Player,
    Queue,
    RecordedMatch,
    Season,
    ServerState,
    StickyMessage,
)
from elo_bot.db.final_models import FinalMatchResult, FinalPlayerProfile, FinalPrediction
from elo_bot.db.fingerprint_models import BASE_FINGERPRINT_INDEXES, EXTRACTION_RUN_INDEXES, FINGERPRINT_TASK_INDEXES
from elo_bot.db.token_models import FinalBet, FinalBettingMarket, TokenLedgerEntry, TokenWallet

BETA_SYNC_FROM_MAIN = False


async def main():
    start_time = time.time()

    try:
        print("➤ Logging into CoC API...")
        coc_start = time.time()
        coc_client = coc.EventsClient(key_names=COC_API_KEYS[runtime.VERSION])
        coc_client.add_events(on_raid_weekend_end)
        await coc_client.login(COC_API_EMAIL, COC_API_PASSWORD)
        print(f"  ✓ CoC API login completed in {time.time() - coc_start:.2f}s")
    except coc.InvalidCredentials as error:
        exit(error)

    print("➤ Connecting to MongoDB...")
    db_start = time.time()
    db_client = AsyncMongoClient(
        MONGO_DB_URL,
        tls=True,
        tlsCAFile=certifi.where(),
        uuidRepresentation="standard",
        connectTimeoutMS=10000,
        serverSelectionTimeoutMS=10000
    )
    print(f"  ✓ MongoDB connection established in {time.time() - db_start:.2f}s")

    if runtime.VERSION == "beta" and BETA_SYNC_FROM_MAIN:
        print("➤ Syncing beta database from main...")
        sync_start = time.time()
        await sync_database(db_client[DATA_BASES["main"]], db_client[DATA_BASES["beta"]])
        print(f"  ✓ Beta database sync completed in {time.time() - sync_start:.2f}s")
    elif runtime.VERSION == "beta":
        print("➤ Beta database sync is disabled (`BETA_SYNC_FROM_MAIN=False`).")

    print("➤ Initializing Beanie...")
    beanie_start = time.time()
    await init_beanie(
        database=db_client.get_database(name=DATA_BASES[runtime.VERSION]),
        document_models=[
            Match,
            Clan,
            HelperTask,
            BaseLayout,
            Queue,
            Player,
            RecordedMatch,
            Season,
            EloFinal,
            Helper,
            ServerState,
            OverrideCode,
            PendingBaseSubmission,
            AuditLogEntry,
            Giveaway,
            StickyMessage,
            BaseExtractionRun,
            BaseFingerprintTask,
            FinalMatchResult,
            FinalPlayerProfile,
            FinalPrediction,
            TokenWallet,
            TokenLedgerEntry,
            FinalBettingMarket,
            FinalBet,
        ],
        skip_indexes=True
    )
    await OverrideCode.get_pymongo_collection().create_indexes(OVERRIDE_CODE_INDEXES)
    await register_final_indexes()
    await AuditLogEntry.get_pymongo_collection().create_indexes(AUDIT_LOG_INDEXES)
    await Giveaway.get_pymongo_collection().create_indexes(GIVEAWAY_INDEXES)
    await StickyMessage.get_pymongo_collection().create_index("channel_id")
    await BaseLayout.get_pymongo_collection().create_indexes(BASE_FINGERPRINT_INDEXES)
    await BaseFingerprintTask.get_pymongo_collection().create_indexes(FINGERPRINT_TASK_INDEXES)
    await BaseExtractionRun.get_pymongo_collection().create_indexes(EXTRACTION_RUN_INDEXES)
    await ServerState.current()
    print(f"  ✓ Beanie initialized in {time.time() - beanie_start:.2f}s")

    # back up logic
    if runtime.VERSION == "main":
        print("➤ Starting backup tasks...")
        backup_start = time.time()
        initial_backup = asyncio.create_task(backup_database1(db_client))
        initial_backup.set_name("initial_backup_task")
        backup_database1.start(db_client)
        backup_database2.start(db_client)
        print(f"  ✓ Backup tasks started in {time.time() - backup_start:.2f}s")

    # bot fields
    runtime.bot.coc_client = coc_client
    runtime.bot.file_locks = defaultdict(asyncio.Lock)
    runtime.bot.coc_client_lock = asyncio.Lock()
    runtime.bot.match_id_choices = {}
    runtime.bot.builder_names = {}
    runtime.bot.base_fingerprints = FingerprintService(runtime.bot, DATA_BASES[runtime.VERSION])

    # load cogs
    print("➤ Loading cogs...")
    cogs_start = time.time()
    for extension in EXTENSIONS:
        runtime.bot.load_extension(extension)
    print(f"  ✓ Cogs loaded in {time.time() - cogs_start:.2f}s")

    print(f"➤ Total startup time: {time.time() - start_time:.2f}s")
    print("➤ Starting bot...")
    root_callbacks.register(runtime.bot)
    try:
        await runtime.bot.astart()
    finally:
        await runtime.bot.base_fingerprints.close()


def run(entry_file: str) -> None:
    version = "main" if "main" in os.path.basename(entry_file) else "beta"
    create_client(version)
    asyncio.run(main())
