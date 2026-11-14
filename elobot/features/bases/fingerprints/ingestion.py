from elobot.features.bases.fingerprints.types import ExtractionState, FingerprintError, ImageAttachment, PIPELINE_ID
from elobot.db import BaseLayout


def source_filter(base: BaseLayout) -> dict:
    query = {"_id": base.id, "district": base.district, "image_url": base.image_url}
    if base.image_revision == 0:
        query["$or"] = [{"image_revision": 0}, {"image_revision": {"$exists": False}}]
    else:
        query["image_revision"] = base.image_revision
    return query


async def replace_source(base: BaseLayout, image_url: str, district: str, *,
                         attachment: ImageAttachment | None = None, metadata: dict | None = None) -> None:
    revision = base.image_revision + 1
    state = ExtractionState(source_revision=revision, pipeline_id=PIPELINE_ID)
    fields = dict(metadata or {})
    fields.update(image_url=image_url, district=district, image_revision=revision,
                  image_attachment=attachment.model_dump() if attachment else None,
                  fingerprint_extraction=state.model_dump())
    result = await base.get_pymongo_collection().update_one(source_filter(base), {"$set": fields})
    if result.matched_count != 1:
        raise FingerprintError("Base changed or was deleted; retry with its current screenshot.")
    for name, value in fields.items():
        setattr(base, name, value)
    base.fingerprint_extraction = state
    base.image_attachment = attachment
