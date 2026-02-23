from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..cli_tools import ALLOWED_CLIS, run_cli
from ..deps import verify_api_key

router = APIRouter(prefix="/api/tools", dependencies=[Depends(verify_api_key)])


class CliRequest(BaseModel):
    tool: str
    args: list[str] = Field(default_factory=list)
    timeout: int = Field(default=60, ge=1, le=300)


@router.post("/cli")
async def run_cli_tool(body: CliRequest) -> dict:
    """Run a CLI tool by name with arguments."""
    output = await run_cli(body.tool, body.args, timeout=body.timeout)
    return {"tool": body.tool, "args": body.args, "output": output}


@router.get("/list")
async def list_cli_tools() -> dict[str, str]:
    """List available CLI tools."""
    return ALLOWED_CLIS
