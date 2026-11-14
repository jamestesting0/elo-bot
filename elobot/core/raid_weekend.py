from datetime import datetime, timedelta, timezone

import coc


def is_raid_weekend():
    now = datetime.utcnow()
    return coc.utils.get_raid_weekend_start(now) <= now <= coc.utils.get_raid_weekend_end(now)


def latest_completed_raid_weekend_end_timestamp() -> int:
    now = datetime.utcnow()
    raid_end = coc.utils.get_raid_weekend_end(now)
    if raid_end > now:
        raid_end -= timedelta(days=7)
    return int(raid_end.replace(tzinfo=timezone.utc).timestamp())
