import sys

sys.path.insert(0, "/opt/python")

from processor import process
from shared.logger import get_logger
from validator import validate_input

log = get_logger(__name__)


def handler(event, context):
    log.info("invoked", extra={"trigger": "schedule", "job": "scores"})
    result = process(validate_input(event))
    if result.week is None:
        log.info("final scores deferred; Yahoo week has not rolled over")
    else:
        log.info("final scores enqueued", extra={"week": result.week})
    return {"week": result.week, "enqueued": result.enqueued}
