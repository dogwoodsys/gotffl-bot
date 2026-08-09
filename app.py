#!/usr/bin/env python3
import os
import sys

import aws_cdk as cdk

sys.path.insert(0, os.path.dirname(__file__))

from infra.gotffl_stack import GotfflStack

app = cdk.App()

GotfflStack(
    app,
    "GotfflStack",
    stack_name="gotffl",
    alert_email=app.node.try_get_context("alert_email") or "mallorymgrills@gmail.com",
    # No league ID yet. UNSET deploys fine (nothing is enabled) and every
    # handler refuses rather than guessing, so a forgotten value cannot post
    # to the wrong league.
    league_key=app.node.try_get_context("league_key") or "UNSET",
    env=cdk.Environment(account="159198628641", region="ca-central-1"),
    description="Game of Throws Bot - Yahoo Fantasy to X relay",
)

app.synth()
