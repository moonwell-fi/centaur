"""The per-run Claude model override threads from start_agent to backend.create.

These are signature/plumbing guards (no DB/k8s): they pin the chain
start_agent → agent_turn.Input → do_agent_turn → spawn_assignment → get_or_spawn
→ backend.create so a future refactor can't quietly drop the `model` hop that
lets a workflow run on Sonnet while the deployment default stays Opus.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest

from api import agent, runtime_control, workflow_engine
from api.sandbox import base as sandbox_base
from api.workflows import agent_turn


def test_model_param_present_through_chain():
    assert "model" in inspect.signature(workflow_engine.WorkflowContext.start_agent).parameters
    assert "model" in inspect.signature(workflow_engine.do_agent_turn).parameters
    assert "model" in inspect.signature(runtime_control.spawn_assignment).parameters
    assert "model" in inspect.signature(agent.get_or_spawn).parameters
    # The backend already accepts model (the env→CLAUDE_MODEL hop).
    assert "model" in inspect.signature(sandbox_base.SandboxBackend.create).parameters


def test_agent_turn_input_carries_model():
    inp = agent_turn.Input(text="hi", model="claude-sonnet-4-6")
    assert inp.model == "claude-sonnet-4-6"
    # Default stays None → unchanged (deployment-default) behavior.
    assert agent_turn.Input(text="hi").model is None


@pytest.mark.asyncio
async def test_start_agent_puts_model_in_run_input(monkeypatch):
    captured: dict = {}

    async def _fake_start_workflow(name, *, workflow_name, run_input, **kw):
        captured.update(run_input)
        return {"run_id": "wfr_test"}

    ctx = workflow_engine.WorkflowContext.__new__(workflow_engine.WorkflowContext)
    monkeypatch.setattr(ctx, "start_workflow", _fake_start_workflow, raising=False)

    await ctx.start_agent("t", text="go", model="claude-sonnet-4-6")
    assert captured.get("model") == "claude-sonnet-4-6"
