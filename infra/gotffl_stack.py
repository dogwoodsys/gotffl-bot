"""GOTFFL bot infrastructure.

Shape: reader Lambdas fetch from Yahoo, render a post payload, and enqueue it.
A single publisher Lambda drains the queue and is the only component that holds
X credentials. Readers cannot post; the publisher cannot read Yahoo. That split
is the main security property of this stack and the tests assert it.
"""

from aws_cdk import Aws, CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from aws_cdk import aws_sqs as sqs
from constructs import Construct

# Parameter Store layout. Readers touch /gotffl/yahoo/*, the publisher touches
# /gotffl/x/*, and neither can reach the other's prefix.
SSM_ROOT = "/gotffl"
YAHOO_PREFIX = f"{SSM_ROOT}/yahoo"
X_PREFIX = f"{SSM_ROOT}/x"

# The league runs Thursday-Monday; the poller is gated to the NFL season in code
# so the off-season costs nothing. See handlers/poll_transactions.py.
POLL_RATE_MINUTES = 2

# Fixed-time posts. EventBridge Scheduler is timezone-aware, so these stay
# correct across the November DST change. Classic cron rules are UTC-only and
# would silently shift by an hour.
SCHEDULE_TIMEZONE = "America/Toronto"
SCHEDULES = {
    "scores": "cron(0 6 ? * TUE *)",
    "standings": "cron(0 12 ? * TUE *)",
    "matchups": "cron(0 12 ? * WED *)",
    # Wednesday's matchups may run before Yahoo rolls the week over. The handler
    # no-ops when the week hasn't advanced, and this second firing catches it.
    "matchups-retry": "cron(0 15 ? * WED *)",
}


class GotfflStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        alert_email: str,
        league_key: str,
        **kwargs,
    ):
        super().__init__(scope, construct_id, **kwargs)

        Tags.of(self).add("Project", "gotffl")

        # Passed in rather than looked up at runtime: a wrong league key would
        # post another league's transactions, so it belongs in the reviewed
        # template, not in a mutable parameter.
        self.league_key = league_key

        self.alerts = self._alerts_topic(alert_email)
        self.state_table = self._state_table()
        self.outbox_dlq, self.failure_dlq, self.outbox = self._queues()
        self.publisher = self._publisher()
        self.readers = self._readers()
        self._schedules()
        self._alarms()
        self._outputs()

    # ---------------------------------------------------------------- topics

    def _alerts_topic(self, alert_email: str) -> sns.Topic:
        topic = sns.Topic(self, "Alerts", topic_name="gotffl-alerts")
        topic.add_subscription(subs.EmailSubscription(alert_email))
        return topic

    # ----------------------------------------------------------------- state

    def _state_table(self) -> ddb.Table:
        """Single table. Item shapes:

        TXN#<transaction_key>   / SEEN    - dedup marker, 90d TTL
        POST#<type>#<week>      / POSTED  - idempotency for scheduled posts
        POSTED#<idempotency_key>/ <ts>    - published tweet ids
        SHADOW#<iso_ts>         / <type>  - rendered-but-not-sent, shadow mode
        HEARTBEAT               / LAST    - staleness detection
        """
        return ddb.Table(
            self,
            "StateTable",
            table_name="gotffl-state",
            partition_key=ddb.Attribute(name="pk", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            # State is reconstructible from Yahoo, but destroying it on a stack
            # delete would let the bot re-post every historical transaction.
            removal_policy=RemovalPolicy.RETAIN,
        )

    # ---------------------------------------------------------------- queues

    def _queues(self) -> tuple[sqs.Queue, sqs.Queue, sqs.Queue]:
        # Redrive target for the FIFO outbox. A FIFO queue can only dead-letter
        # to another FIFO queue.
        outbox_dlq = sqs.Queue(
            self,
            "OutboxDlq",
            queue_name="gotffl-outbox-dlq.fifo",
            fifo=True,
            retention_period=Duration.days(14),
            enforce_ssl=True,
        )
        # Standard queue for Lambda async-invoke failures and Scheduler
        # dead-letters. Neither service sets a MessageGroupId, so neither can
        # write to a FIFO queue - they need their own target.
        failure_dlq = sqs.Queue(
            self,
            "FailureDlq",
            queue_name="gotffl-failure-dlq",
            retention_period=Duration.days(14),
            enforce_ssl=True,
        )
        outbox = sqs.Queue(
            self,
            "Outbox",
            queue_name="gotffl-outbox.fifo",
            fifo=True,
            # Dedup is explicit per message (transaction key / post idempotency
            # key), not derived from body hash - two identical-looking trades on
            # different days must both post.
            content_based_deduplication=False,
            visibility_timeout=Duration.seconds(180),
            retention_period=Duration.days(4),
            enforce_ssl=True,
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=outbox_dlq),
        )
        return outbox_dlq, failure_dlq, outbox

    # -------------------------------------------------------------- functions

    @property
    def shared_layer(self) -> lambda_.LayerVersion:
        """Cross-Lambda code, mounted at /opt/python. Handlers put that on
        sys.path, so `from shared.logger import get_logger` resolves the same
        way locally and in Lambda."""
        if not hasattr(self, "_shared_layer"):
            self._shared_layer = lambda_.LayerVersion(
                self,
                "SharedLayer",
                layer_version_name="gotffl-shared",
                code=lambda_.Code.from_asset("layers/shared"),
                compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
                compatible_architectures=[lambda_.Architecture.ARM_64],
                description="gotffl shared modules",
            )
        return self._shared_layer

    def _fn(
        self,
        name: str,
        source_dir: str,
        *,
        timeout: Duration,
        environment: dict[str, str],
        reserved_concurrent_executions: int | None = None,
    ) -> lambda_.Function:
        """A Lambda with its own role. No shared execution role anywhere in this
        stack - that is what keeps the reader/publisher split real."""
        role = iam.Role(
            self,
            f"{name}Role",
            role_name=f"gotffl-{name.lower()}-role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        log_group = logs.LogGroup(
            self,
            f"{name}Logs",
            log_group_name=f"/aws/lambda/gotffl-{name.lower()}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        return lambda_.Function(
            self,
            name,
            function_name=f"gotffl-{name.lower()}",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            code=lambda_.Code.from_asset(f"functions/{source_dir}"),
            handler="handler.handler",
            role=role,
            memory_size=256,
            timeout=timeout,
            layers=[self.shared_layer],
            # Standard: X-Ray active on every Lambda. Without it a slow poll is
            # a number in a log; with it you can see which call was slow.
            tracing=lambda_.Tracing.ACTIVE,
            environment={"STATE_TABLE": self.state_table.table_name, **environment},
            log_group=log_group,
            reserved_concurrent_executions=reserved_concurrent_executions,
        )

    def _publisher(self) -> lambda_.Function:
        fn = self._fn(
            "Publish",
            "publish",
            timeout=Duration.seconds(60),
            environment={"X_PREFIX": X_PREFIX, "SHADOW_MODE_PARAM": f"{SSM_ROOT}/shadow_mode"},
            # No reserved concurrency: this account's total limit is 10 and AWS
            # holds 10 back as unreserved, so any reservation is rejected.
            # Serialization comes from the FIFO outbox instead - every message
            # uses one MessageGroupId, and SQS keeps a single group strictly in
            # order with one message in flight at a time. That is what makes
            # thread replies chain correctly.
        )
        self.state_table.grant_read_write_data(fn)
        fn.add_event_source(
            sources.SqsEventSource(self.outbox, batch_size=1, report_batch_item_failures=True)
        )
        # X credentials and the shadow flag - and nothing under /gotffl/yahoo.
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:GetParameters"],
                resources=[
                    f"arn:aws:ssm:{Aws.REGION}:{Aws.ACCOUNT_ID}:parameter{X_PREFIX}/*",
                    f"arn:aws:ssm:{Aws.REGION}:{Aws.ACCOUNT_ID}:parameter{SSM_ROOT}/shadow_mode",
                ],
            )
        )
        self._grant_kms_decrypt(fn)
        return fn

    def _readers(self) -> dict[str, lambda_.Function]:
        readers: dict[str, lambda_.Function] = {}

        specs = [
            ("PollTransactions", "poll_transactions", Duration.seconds(60), None),
            ("PostScores", "post_scores", Duration.seconds(120), None),
            ("PostStandings", "post_standings", Duration.seconds(120), None),
            ("PostMatchups", "post_matchups", Duration.seconds(120), None),
        ]
        for name, module, timeout, concurrency in specs:
            fn = self._fn(
                name,
                module,
                timeout=timeout,
                environment={
                    "YAHOO_PREFIX": YAHOO_PREFIX,
                    "OUTBOX_URL": self.outbox.queue_url,
                    "YAHOO_LEAGUE_KEY": self.league_key,
                },
                reserved_concurrent_executions=concurrency,
            )
            self.state_table.grant_read_write_data(fn)
            self.outbox.grant_send_messages(fn)
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["ssm:GetParameter", "ssm:GetParameters"],
                    resources=[
                        f"arn:aws:ssm:{Aws.REGION}:{Aws.ACCOUNT_ID}:parameter{YAHOO_PREFIX}/*"
                    ],
                )
            )
            self._grant_kms_decrypt(fn)
            # Async invocation from EventBridge - failures must land somewhere.
            fn.configure_async_invoke(
                retry_attempts=2, on_failure=lambda_destinations_sqs(self.failure_dlq)
            )
            readers[name] = fn

        # Only the poller refreshes the Yahoo token, so only it can write one
        # back. Yahoo rotates refresh tokens on use, hence the write at all.
        readers["PollTransactions"].add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:PutParameter"],
                resources=[f"arn:aws:ssm:{Aws.REGION}:{Aws.ACCOUNT_ID}:parameter{YAHOO_PREFIX}/*"],
            )
        )
        return readers

    def _grant_kms_decrypt(self, fn: lambda_.Function) -> None:
        """SecureString parameters use the account default SSM key."""
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt"],
                resources=[f"arn:aws:kms:{Aws.REGION}:{Aws.ACCOUNT_ID}:key/*"],
                conditions={"StringEquals": {"kms:ViaService": f"ssm.{Aws.REGION}.amazonaws.com"}},
            )
        )

    # ------------------------------------------------------------- schedules

    def _schedules(self) -> None:
        # The poller runs on a classic rate rule. Disabled at deploy time: it
        # stays off until Yahoo credentials are in Parameter Store, so a fresh
        # deploy cannot spin against a 401 every two minutes.
        events.Rule(
            self,
            "PollSchedule",
            rule_name="gotffl-poll",
            schedule=events.Schedule.rate(Duration.minutes(POLL_RATE_MINUTES)),
            targets=[targets.LambdaFunction(self.readers["PollTransactions"])],
            enabled=False,
        )

        role = iam.Role(
            self,
            "SchedulerRole",
            role_name="gotffl-scheduler-role",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        fn_for = {
            "scores": self.readers["PostScores"],
            "standings": self.readers["PostStandings"],
            "matchups": self.readers["PostMatchups"],
            "matchups-retry": self.readers["PostMatchups"],
        }
        for key, expression in SCHEDULES.items():
            fn = fn_for[key]
            fn.grant_invoke(role)
            scheduler.CfnSchedule(
                self,
                f"Schedule{key.title().replace('-', '')}",
                name=f"gotffl-{key}",
                flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
                schedule_expression=expression,
                schedule_expression_timezone=SCHEDULE_TIMEZONE,
                state="DISABLED",
                target=scheduler.CfnSchedule.TargetProperty(
                    arn=fn.function_arn,
                    role_arn=role.role_arn,
                    retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(
                        maximum_retry_attempts=2
                    ),
                    dead_letter_config=scheduler.CfnSchedule.DeadLetterConfigProperty(
                        arn=self.failure_dlq.queue_arn
                    ),
                ),
            )
        self.failure_dlq.grant_send_messages(role)

    # ---------------------------------------------------------------- alarms

    def _alarms(self) -> None:
        action = cw_actions.SnsAction(self.alerts)

        for name, fn in {"Publish": self.publisher, **self.readers}.items():
            cw.Alarm(
                self,
                f"{name}Errors",
                alarm_name=f"gotffl-{name.lower()}-errors",
                metric=fn.metric_errors(period=Duration.minutes(15)),
                threshold=0,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            ).add_alarm_action(action)

        # Anything in either DLQ is work that will never complete. Always an alarm.
        for name, queue in (("Outbox", self.outbox_dlq), ("Failure", self.failure_dlq)):
            cw.Alarm(
                self,
                f"{name}DlqDepth",
                alarm_name=f"gotffl-{name.lower()}-dlq-not-empty",
                metric=queue.metric_approximate_number_of_messages_visible(
                    period=Duration.minutes(5)
                ),
                threshold=0,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            ).add_alarm_action(action)

        # Spend and embarrassment guard: a dedup bug looks like a burst of posts.
        # Catch it the same day rather than on the invoice.
        cw.Alarm(
            self,
            "PostRate",
            alarm_name="gotffl-post-rate-high",
            metric=cw.Metric(
                namespace="Gotffl",
                metric_name="PostsPublished",
                statistic="Sum",
                period=Duration.hours(24),
            ),
            threshold=25,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        ).add_alarm_action(action)

        # The failure that actually bites: the bot is up, healthy, erroring on
        # nothing - and silently posting nothing. A dead bot is obvious; this is
        # not. The handler emits this metric on every successful poll.
        cw.Alarm(
            self,
            "Staleness",
            alarm_name="gotffl-no-posts-7-days",
            metric=cw.Metric(
                namespace="Gotffl",
                metric_name="PostsPublished",
                statistic="Sum",
                period=Duration.days(1),
            ),
            threshold=1,
            # CloudWatch caps EvaluationPeriods * Period at one week (604800s)
            # for periods >= 1 hour, so 7 days is the longest lookback
            # available. That is still ample: posts land Tue 06:00, Tue 12:00
            # and Wed 12:00, so the largest legitimate gap is Wed noon to the
            # following Tue 06:00 - about 5.75 days.
            evaluation_periods=7,
            datapoints_to_alarm=7,
            comparison_operator=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.BREACHING,
        ).add_alarm_action(action)

    # --------------------------------------------------------------- outputs

    def _outputs(self) -> None:
        CfnOutput(self, "StateTableName", value=self.state_table.table_name)
        CfnOutput(self, "OutboxUrl", value=self.outbox.queue_url)
        CfnOutput(self, "AlertsTopicArn", value=self.alerts.topic_arn)


def lambda_destinations_sqs(queue: sqs.Queue):
    from aws_cdk import aws_lambda_destinations as destinations

    return destinations.SqsDestination(queue)
