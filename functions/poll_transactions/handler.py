import sys

sys.path.insert(0, "/opt/python")

from processor import process
from shared.logger import get_logger
from validator import validate_input

log = get_logger(__name__)


def handler(event, context):
    log.info("invoked", extra={"trigger": "schedule"})
    request = validate_input(event)
    result = process(request)
    if result.skipped_off_season:
        log.info("outside season or active hours; no work")
    else:
        log.info("poll complete", extra={"fetched": result.fetched, "enqueued": result.enqueued})
    return {"fetched": result.fetched, "enqueued": result.enqueued}
