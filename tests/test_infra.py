"""Infrastructure assertions.

These enforce the properties the design depends on. They are here so a future
edit that quietly widens a policy or drops a DLQ fails CI instead of passing
review.
"""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from infra.gotffl_stack import GotfflStack

READERS = ["gotffl-polltransactions", "gotffl-postscores", "gotffl-poststandings",
           "gotffl-postmatchups"]


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    stack = GotfflStack(
        app,
        "TestStack",
        alert_email="test@example.com",
        env=cdk.Environment(account="111111111111", region="ca-central-1"),
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def resources(template: Template) -> dict:
    return json.loads(json.dumps(template.to_json()))["Resources"]


def _policies_for_role(resources: dict, role_logical_prefix: str) -> list[dict]:
    """Every inline policy statement attached to roles matching a name prefix."""
    role_ids = {
        rid
        for rid, r in resources.items()
        if r["Type"] == "AWS::IAM::Role"
        and str(r["Properties"].get("RoleName", "")).startswith(role_logical_prefix)
    }
    statements = []
    for r in resources.values():
        if r["Type"] != "AWS::IAM::Policy":
            continue
        for ref in r["Properties"].get("Roles", []):
            if isinstance(ref, dict) and ref.get("Ref") in role_ids:
                statements.extend(r["Properties"]["PolicyDocument"]["Statement"])
    return statements


def _resource_strings(statement: dict) -> str:
    return json.dumps(statement.get("Resource", ""))


# ------------------------------------------------------------------ topology


def test_five_lambdas_exist(template: Template):
    template.resource_count_is("AWS::Lambda::Function", 5)


def test_each_lambda_has_its_own_role(resources: dict):
    """A shared execution role would collapse the reader/publisher split."""
    roles = [
        json.dumps(r["Properties"]["Role"], sort_keys=True)
        for r in resources.values()
        if r["Type"] == "AWS::Lambda::Function"
    ]
    assert len(roles) == 5
    assert len(set(roles)) == 5, f"lambdas share an execution role: {roles}"


def test_state_table_is_retained_and_has_ttl(template: Template):
    template.has_resource(
        "AWS::DynamoDB::Table",
        {
            "DeletionPolicy": "Retain",
            "Properties": {
                "TimeToLiveSpecification": {"AttributeName": "ttl", "Enabled": True},
                "BillingMode": "PAY_PER_REQUEST",
            },
        },
    )


# ---------------------------------------------------------------- reliability


def test_outbox_has_redrive_to_fifo_dlq(template: Template):
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {
            "QueueName": "gotffl-outbox.fifo",
            "FifoQueue": True,
            "RedrivePolicy": Match.object_like({"maxReceiveCount": 3}),
        },
    )


def test_failure_dlq_is_standard_not_fifo(resources: dict):
    """Lambda async destinations and EventBridge Scheduler cannot write to FIFO
    queues - they do not set a MessageGroupId."""
    dlq = [
        r
        for r in resources.values()
        if r["Type"] == "AWS::SQS::Queue"
        and r["Properties"].get("QueueName") == "gotffl-failure-dlq"
    ]
    assert len(dlq) == 1
    assert "FifoQueue" not in dlq[0]["Properties"]


def test_every_reader_has_an_async_failure_destination(resources: dict):
    """Standard: DLQ on every async invocation, no exceptions."""
    configured = {
        json.dumps(r["Properties"]["FunctionName"], sort_keys=True)
        for r in resources.values()
        if r["Type"] == "AWS::Lambda::EventInvokeConfig"
        and "OnFailure" in r["Properties"].get("DestinationConfig", {})
    }
    assert len(configured) == 4, f"expected 4 readers with failure destinations, got {configured}"


def test_publisher_is_serialized(template: Template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"FunctionName": "gotffl-publish", "ReservedConcurrentExecutions": 1},
    )


def test_poller_is_serialized(template: Template):
    """Two concurrent pollers would race the Yahoo token refresh."""
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"FunctionName": "gotffl-polltransactions", "ReservedConcurrentExecutions": 1},
    )


# -------------------------------------------------------------------- safety


def test_schedules_are_disabled_on_deploy(resources: dict):
    """Nothing fires until credentials are in Parameter Store."""
    for r in resources.values():
        if r["Type"] == "AWS::Scheduler::Schedule":
            assert r["Properties"]["State"] == "DISABLED"
        if r["Type"] == "AWS::Events::Rule":
            assert r["Properties"]["State"] == "DISABLED"


def test_schedules_are_timezone_aware(resources: dict):
    """UTC cron would drift an hour at the DST change."""
    schedules = [r for r in resources.values() if r["Type"] == "AWS::Scheduler::Schedule"]
    assert len(schedules) == 4
    for s in schedules:
        assert s["Properties"]["ScheduleExpressionTimezone"] == "America/Toronto"


def test_every_schedule_has_a_dead_letter_config(resources: dict):
    for r in resources.values():
        if r["Type"] == "AWS::Scheduler::Schedule":
            assert "DeadLetterConfig" in r["Properties"]["Target"]


def test_no_secrets_in_lambda_environment(resources: dict):
    """Environment holds parameter *names* and resource identifiers only.
    Credentials are fetched from SSM at runtime."""
    banned = ("token", "secret", "password", "apikey", "api_key", "consumer")
    for r in resources.values():
        if r["Type"] != "AWS::Lambda::Function":
            continue
        for key, value in r["Properties"].get("Environment", {}).get("Variables", {}).items():
            lowered = f"{key}{json.dumps(value)}".lower()
            for word in banned:
                assert word not in lowered, f"possible secret in env var {key}"


# ----------------------------------------------------------------------- IAM


def test_publisher_cannot_read_yahoo_credentials(resources: dict):
    for statement in _policies_for_role(resources, "gotffl-publish-role"):
        assert "/gotffl/yahoo" not in _resource_strings(statement)


def test_readers_cannot_read_x_credentials(resources: dict):
    """A compromised or buggy reader must not be able to post."""
    for reader in READERS:
        for statement in _policies_for_role(resources, f"{reader}-role"):
            assert "/gotffl/x" not in _resource_strings(statement)


def test_only_the_poller_can_write_parameters(resources: dict):
    """Yahoo rotates refresh tokens on use, so the poller writes back. Nothing
    else has any need to."""
    for reader in [*READERS, "gotffl-publish"]:
        statements = _policies_for_role(resources, f"{reader}-role")
        writes = [
            s
            for s in statements
            if "ssm:PutParameter" in json.dumps(s.get("Action", ""))
        ]
        if reader == "gotffl-polltransactions":
            assert writes, "poller must be able to write the rotated refresh token"
        else:
            assert not writes, f"{reader} should not write parameters"


def test_no_wildcard_resource_on_ssm_or_dynamodb(resources: dict):
    for r in resources.values():
        if r["Type"] != "AWS::IAM::Policy":
            continue
        for statement in r["Properties"]["PolicyDocument"]["Statement"]:
            actions = json.dumps(statement.get("Action", ""))
            if "ssm:" in actions or "dynamodb:" in actions:
                assert _resource_strings(statement) != '"*"', "wildcard resource on scoped service"


def test_kms_decrypt_is_constrained_to_ssm(resources: dict):
    """kms:Decrypt on key/* is only acceptable because ViaService pins it to SSM."""
    for r in resources.values():
        if r["Type"] != "AWS::IAM::Policy":
            continue
        for statement in r["Properties"]["PolicyDocument"]["Statement"]:
            if "kms:Decrypt" in json.dumps(statement.get("Action", "")):
                via = statement.get("Condition", {}).get("StringEquals", {}).get("kms:ViaService")
                assert via is not None, "unconstrained kms:Decrypt"


# -------------------------------------------------------------------- alarms


def test_every_lambda_has_an_error_alarm(template: Template):
    template.resource_count_is("AWS::CloudWatch::Alarm", 9)


def test_staleness_alarm_treats_missing_data_as_breaching(resources: dict):
    """A bot posting nothing emits no datapoints. If missing data were treated
    as OK, the silent-failure alarm would never fire - which is the whole point
    of having it."""
    alarms = [
        r
        for r in resources.values()
        if r["Type"] == "AWS::CloudWatch::Alarm"
        and r["Properties"].get("AlarmName") == "gotffl-no-posts-8-days"
    ]
    assert len(alarms) == 1
    assert alarms[0]["Properties"]["TreatMissingData"] == "breaching"
