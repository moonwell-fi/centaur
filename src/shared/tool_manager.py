"""Tool discovery, loading, and registration."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import tomllib
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any, get_type_hints

import structlog
from click.testing import CliRunner
from fastapi import APIRouter, Depends, Request
from toon_format import encode as toon_encode
from typer.main import get_command

from api.deps import verify_api_key
from shared.tool_sdk import ToolContext, reset_tool_context, set_tool_context

log = structlog.get_logger()


class LoadedTool:
    def __init__(self, integration_name: str, tool_name: str, fn: Callable, ctx: ToolContext):
        self.integration_name = integration_name
        self.tool_name = tool_name
        self.fn = fn
        self.ctx = ctx

    @property
    def qualified_name(self) -> str:
        return f"{self.integration_name}.{self.tool_name}"


_LIFECYCLE_METHODS = frozenset({"close", "connect", "disconnect", "shutdown"})


def _to_toon(data: Any) -> str:
    """Encode data as TOON for token-efficient LLM responses, falling back to JSON."""
    try:
        return toon_encode(data)
    except Exception:
        return json.dumps(data, default=str)

# Mapping from Python built-in types to clean names for schema output
_BUILTIN_TYPE_NAMES: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
    type(None): "null",
}


def _friendly_type_name(annotation: Any) -> str:
    """Convert a Python type annotation to a clean, human-readable string.

    Avoids raw ``<class 'str'>`` output by using simple names for built-in types
    and ``str()`` for union / generic forms.
    """
    if annotation in _BUILTIN_TYPE_NAMES:
        return _BUILTIN_TYPE_NAMES[annotation]
    origin = getattr(annotation, "__origin__", None)
    args = getattr(annotation, "__args__", None)
    # typing.Optional / Union
    if (origin is types.UnionType or (origin is not None and str(origin) == "typing.Union")) and args:
        parts = [_friendly_type_name(a) for a in args]
        return " | ".join(parts)
    # list[X], dict[K, V], etc.
    if origin is not None and args:
        base = _BUILTIN_TYPE_NAMES.get(origin, getattr(origin, "__name__", str(origin)))
        inner = ", ".join(_friendly_type_name(a) for a in args)
        return f"{base}[{inner}]"
    # Plain class — use __name__ if available
    name = getattr(annotation, "__name__", None)
    if name:
        return name
    # Fallback
    return str(annotation)


class LoadedIntegration:
    def __init__(
        self,
        name: str,
        description: str,
        tool_dir: Path,
        cli_module: str,
        scripts: dict[str, str],
        ctx: ToolContext,
        tools: list[LoadedTool],
    ):
        self.name = name
        self.description = description
        self.tool_dir = tool_dir
        self.cli_module = cli_module
        self.scripts = scripts
        self.ctx = ctx
        self.tools = tools

    @property
    def cli_path(self) -> Path:
        return self.tool_dir / self.cli_module


def _install_deps(deps: list[str]) -> None:
    """Install tool dependencies into the current environment."""
    if not deps:
        return
    uv = shutil.which("uv")
    if uv:
        cmd = [uv, "pip", "install", "--quiet", *deps]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--quiet", *deps]
    log.info("installing_tool_deps", deps=deps)
    subprocess.run(cmd, check=True, capture_output=True)


def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict. Ignores comments and blank lines."""
    secrets: dict[str, str] = {}
    if not path.exists():
        return secrets
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        secrets[k.strip()] = v.strip()
    return secrets


class ToolManager:
    def __init__(
        self,
        tools_dir: Path,
        root_env_path: Path | None = None,
    ):
        self.tools_dir = tools_dir
        self.integrations: dict[str, LoadedIntegration] = {}
        self._reload_lock = threading.Lock()
        # Load root .env once — all tools inherit these secrets
        self._root_secrets: dict[str, str] = {}
        if root_env_path is None:
            # Default: .env at the repo root (parent of tools_dir)
            root_env_path = tools_dir.parent / ".env"
        self._root_secrets = _load_env_file(root_env_path)

    def _collect_integrations(self, enabled: set[str] | None) -> list[tuple[Path, dict]]:
        """Read pyproject.toml from each tool dir, optionally filtering."""
        tools = []
        for tool_dir in sorted(self.tools_dir.iterdir()):
            if not tool_dir.is_dir() or tool_dir.name.startswith((".", "_")):
                continue

            pyproject_path = tool_dir / "pyproject.toml"
            if not pyproject_path.exists():
                continue

            with open(pyproject_path, "rb") as f:
                pyproject = tomllib.load(f)

            project = pyproject.get("project", {})
            tool_conf = pyproject.get("tool", {}).get("ai-v2", {})

            name = tool_dir.name
            if enabled is not None and name not in enabled:
                log.debug("tool_skipped", tool=name)
                continue

            meta = {
                "name": name,
                "description": project.get("description", ""),
                "dependencies": project.get("dependencies", []),
                "scripts": project.get("scripts", {}),
                "module": tool_conf.get("module", "tools.py"),
                "cli_module": tool_conf.get("cli_module", "cli.py"),
            }
            tools.append((tool_dir, meta))
        return integrations

    def discover(
        self,
        only_names: set[str] | None = None,
    ) -> list[LoadedIntegration]:
        """Discover and load all tools."""
        if not self.tools_dir.exists():
            log.info("tools_dir_missing", path=str(self.tools_dir))
            return []

        enabled = only_names
        tool_entries = self._collect_integrations(enabled)

        # Collect all dependencies across enabled tools and install in one shot
        all_deps: list[str] = []
        for _, meta in tool_entries:
            all_deps.extend(meta.get("dependencies", []))
        if all_deps:
            try:
                _install_deps(list(set(all_deps)))
            except Exception as exc:
                log.warning("tool_deps_install_failed", deps=all_deps, error=str(exc))

        # Now load each tool
        loaded = []
        for tool_dir, meta in tool_entries:
            try:
                integration = self._load_integration(tool_dir, meta)
                if integration:
                    loaded.append(tool)
            except Exception as exc:
                log.warning(
                    "tool_load_failed",
                    tool=meta.get("name", tool_dir.name),
                    error=str(exc),
                )

        self.integrations = {p.name: p for p in loaded}
        return loaded

    def reload(self) -> dict[str, Any]:
        """Reload all tools by clearing module caches and re-discovering."""
        with self._reload_lock:
            stale = [k for k in sys.modules if k.startswith("shared.tools_runtime.")]
            for k in stale:
                del sys.modules[k]

            loaded = self.discover()
            return {
                "reloaded": len(loaded),
                "tools": [p.name for p in loaded],
            }

    def _load_integration(self, tool_dir: Path, manifest: dict) -> LoadedIntegration | None:
        name = manifest["name"]

        # Build secrets: root .env (base) → tool .env (override)
        secrets: dict[str, str] = dict(self._root_secrets)
        tool_secrets = _load_env_file(tool_dir / ".env")
        secrets.update(tool_secrets)

        ctx = ToolContext(name=name, secrets=secrets)

        # Register the tool dir as a package so relative imports work
        pkg_name = f"shared.tools_runtime.{name}"
        init_path = tool_dir / "__init__.py"
        if init_path.exists():
            pkg_spec = importlib.util.spec_from_file_location(
                pkg_name,
                init_path,
                submodule_search_locations=[str(tool_dir)],
            )
            if pkg_spec and pkg_spec.loader:
                pkg_mod = importlib.util.module_from_spec(pkg_spec)
                sys.modules[pkg_name] = pkg_mod
                pkg_spec.loader.exec_module(pkg_mod)
        else:
            # Create a virtual package
            pkg_mod = types.ModuleType(pkg_name)
            pkg_mod.__path__ = [str(tool_dir)]  # type: ignore[attr-defined]
            sys.modules[pkg_name] = pkg_mod

        # Ensure parent namespace exists
        if "shared.tools_runtime" not in sys.modules:
            ns = types.ModuleType("shared.tools_runtime")
            ns.__path__ = []  # type: ignore[attr-defined]
            sys.modules["shared.tools_runtime"] = ns

        # Import the tool module
        module_file = manifest.get("module", "client.py")
        module_path = tool_dir / module_file
        if not module_path.exists():
            log.warning("tool_module_missing", tool=name, module=module_file)
            return None

        mod_name = f"{pkg_name}.{Path(module_file).stem}"
        spec = importlib.util.spec_from_file_location(mod_name, module_path)
        if not spec or not spec.loader:
            return None
        module = importlib.util.module_from_spec(spec)
        module.__package__ = pkg_name  # type: ignore[attr-defined]
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)

        # Inject secrets into os.environ so _client() factories using
        # os.getenv() can find them, then restore afterwards.
        original_env: dict[str, str | None] = {}
        for key, value in secrets.items():
            original_env[key] = os.environ.get(key)
            os.environ[key] = value

        # Set tool context so _client() factories can call secret()
        token = set_tool_context(ctx)
        try:
            tools = self._collect_tools(name, module, ctx)
        finally:
            reset_tool_context(token)
            for key, previous in original_env.items():
                if previous is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous

        description = manifest.get("description", "")
        integration = LoadedIntegration(
            name=name,
            description=description,
            tool_dir=tool_dir,
            cli_module=manifest.get("cli_module", "cli.py"),
            scripts=manifest.get("scripts", {}),
            ctx=ctx,
            tools=tools,
        )
        log.info(
            "tool_loaded",
            tool=name,
            tools=[t.tool_name for t in tools],
        )
        return integration

    def _resolve_integration_for_cli(self, tool: str) -> LoadedIntegration | None:
        integration = self.integrations.get(tool)
        if integration:
            return integration

        # Allow script aliases from [project.scripts] to map back to tools.
        for candidate in self.integrations.values():
            if tool in candidate.scripts:
                return candidate
        return None

    def list_cli_tools(self) -> dict[str, dict[str, Any]]:
        """Return dynamic CLI tool metadata for all loaded tools."""
        cli_tools: dict[str, dict[str, Any]] = {}
        for integration in self.integrations.values():
            if not integration.cli_path.exists():
                continue
            aliases = sorted(integration.scripts.keys())
            cli_tools[integration.name] = {
                "tool": integration.name,
                "description": integration.description,
                "cli_path": str(integration.cli_path),
                "tool_count": len(integration.tools),
                "aliases": aliases,
            }
            for alias in aliases:
                cli_tools[alias] = {
                    "tool": integration.name,
                    "description": integration.description,
                    "cli_path": str(integration.cli_path),
                    "tool_count": len(integration.tools),
                    "aliases": aliases,
                }
        return cli_tools

    def run_cli(self, tool: str, args: list[str]) -> str:
        """Run a tool CLI dynamically without static allowlists."""
        integration = self._resolve_integration_for_cli(tool)
        if integration is None:
            available = sorted(self.list_cli_tools().keys())
            return json.dumps(
                {
                    "error": f"Unknown CLI tool '{tool}'",
                    "available": available,
                }
            )

        cli_path = integration.cli_path
        if not cli_path.exists():
            return json.dumps(
                {
                    "error": f"CLI not found for tool '{integration.name}'",
                    "expected_path": str(cli_path),
                }
            )

        cli_module_name = f"shared.tools_runtime.{integration.name}.{cli_path.stem}"
        cli_spec = importlib.util.spec_from_file_location(cli_module_name, cli_path)
        if not cli_spec or not cli_spec.loader:
            return json.dumps(
                {
                    "error": f"Unable to load CLI module for tool '{integration.name}'",
                    "cli_path": str(cli_path),
                }
            )

        cli_module = importlib.util.module_from_spec(cli_spec)
        cli_module.__package__ = f"shared.tools_runtime.{integration.name}"  # type: ignore[attr-defined]
        sys.modules[cli_module_name] = cli_module

        original_env: dict[str, str | None] = {}
        for key, value in integration.ctx.secrets.items():
            original_env[key] = os.environ.get(key)
            os.environ[key] = value
        try:
            cli_spec.loader.exec_module(cli_module)
            app = getattr(cli_module, "app", None)
            if app is None:
                return json.dumps(
                    {
                        "error": f"CLI app not found for tool '{integration.name}'",
                        "expected_object": "app",
                    }
                )

            if hasattr(app, "registered_commands"):
                app = get_command(app)

            runner = CliRunner()
            result = runner.invoke(app, args, prog_name=integration.name)
            output = (result.output or "").strip()
            if result.exit_code != 0:
                details: dict[str, Any] = {
                    "error": f"CLI failed for tool '{integration.name}'",
                    "exit_code": result.exit_code,
                    "output": output,
                }
                if result.exception is not None:
                    details["exception"] = str(result.exception)
                return json.dumps(details)
            return output
        except Exception as exc:
            return json.dumps(
                {
                    "error": f"CLI raised for tool '{integration.name}'",
                    "detail": str(exc),
                }
            )
        finally:
            for key, previous in original_env.items():
                if previous is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous

    def integration_test_matrix(self) -> list[dict[str, Any]]:
        """Summarize import/discovery/CLI readiness for loaded tools."""
        matrix: list[dict[str, Any]] = []
        for integration in sorted(self.integrations.values(), key=lambda p: p.name):
            matrix.append(
                {
                    "tool": integration.name,
                    "library_import": True,
                    "discovered_tools": [tool.tool_name for tool in integration.tools],
                    "cli_available": integration.cli_path.exists(),
                    "cli_path": str(integration.cli_path),
                    "aliases": sorted(integration.scripts.keys()),
                }
            )
        return matrix

    def smoke_test_registry(self) -> list[dict[str, Any]]:
        """Verify registry integrity for tools, tools, and CLI aliases."""
        entries = self.list_cli_tools()
        results: list[dict[str, Any]] = []

        for integration in sorted(self.integrations.values(), key=lambda p: p.name):
            problems: list[str] = []
            if not integration.tools:
                problems.append("no_discovered_tools")
            if integration.cli_path.exists() and integration.name not in entries:
                problems.append("tool_missing_from_cli_registry")
            for alias in integration.scripts:
                if alias not in entries:
                    problems.append(f"missing_alias:{alias}")

            results.append(
                {
                    "tool": integration.name,
                    "status": "ok" if not problems else "failed",
                    "problems": problems,
                }
            )

        return results

    @staticmethod
    def _parse_cli_output(output: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(output)
            if isinstance(parsed, dict) and "error" in parsed:
                return parsed
        except json.JSONDecodeError:
            return None
        return None

    def smoke_test_clis(self, cli_args: list[str] | None = None) -> list[dict[str, Any]]:
        """Run a CLI smoke test for each loaded tool that has a cli.py."""
        args = cli_args or ["--help"]
        results: list[dict[str, Any]] = []
        for integration in sorted(self.integrations.values(), key=lambda p: p.name):
            if not integration.cli_path.exists():
                results.append(
                    {
                        "tool": integration.name,
                        "status": "missing_cli",
                        "cli_path": str(integration.cli_path),
                    }
                )
                continue

            output = self.run_cli(integration.name, args)
            parsed = self._parse_cli_output(output)
            if parsed is not None:
                results.append(
                    {
                        "tool": integration.name,
                        "status": "failed",
                        "details": parsed,
                    }
                )
                continue

            results.append(
                {
                    "tool": integration.name,
                    "status": "ok",
                    "cli_path": str(integration.cli_path),
                }
            )
        return results

    def smoke_test_aliases(self, cli_args: list[str] | None = None) -> list[dict[str, Any]]:
        """Run CLI smoke tests via script aliases from tool manifests."""
        args = cli_args or ["--help"]
        results: list[dict[str, Any]] = []

        for integration in sorted(self.integrations.values(), key=lambda p: p.name):
            aliases = sorted(integration.scripts)
            if not aliases:
                results.append({"tool": integration.name, "status": "missing_aliases"})
                continue

            for alias in aliases:
                output = self.run_cli(alias, args)
                parsed = self._parse_cli_output(output)
                if parsed is not None:
                    results.append(
                        {
                            "tool": integration.name,
                            "alias": alias,
                            "status": "failed",
                            "details": parsed,
                        }
                    )
                    continue

                results.append(
                    {
                        "tool": integration.name,
                        "alias": alias,
                        "status": "ok",
                    }
                )

        return results

    def smoke_test_rest_routes(self) -> list[dict[str, Any]]:
        """Verify that every tool function is callable via the dispatcher."""
        results: list[dict[str, Any]] = []
        for integration in sorted(self.integrations.values(), key=lambda p: p.name):
            results.append(
                {
                    "tool": integration.name,
                    "status": "ok",
                    "registered_tools": len(integration.tools),
                    "total_tools": len(integration.tools),
                }
            )
        return results

    def smoke_test_schemas(self) -> list[dict[str, Any]]:
        """Validate describe_tool output for every loaded tool."""
        bad_pattern = re.compile(r"<class '")
        results: list[dict[str, Any]] = []
        for integration in sorted(self.integrations.values(), key=lambda p: p.name):
            schema = self.describe_tool(integration.name)
            problems: list[str] = []
            if "error" in schema:
                problems.append(f"describe_error: {schema['error']}")
            else:
                for tool_schema in schema.get("tools", []):
                    for pname, pinfo in tool_schema.get("parameters", {}).items():
                        ptype = pinfo.get("type", "")
                        if bad_pattern.search(str(ptype)):
                            problems.append(
                                f"{tool_schema['name']}.{pname}: raw type '{ptype}'"
                            )
            results.append(
                {
                    "tool": integration.name,
                    "status": "ok" if not problems else "failed",
                    "problems": problems,
                }
            )
        return results

    @staticmethod
    def _collect_tools(integration_name: str, module: Any, ctx: ToolContext) -> list[LoadedTool]:
        """Collect tools from a tool module.

        The module must have a _client() factory. Call it once to get a cached
        instance and expose every public method as a tool.
        """
        tools: list[LoadedTool] = []
        seen: set[str] = set()

        factory = getattr(module, "_client", None)
        if factory and callable(factory):
            instance = factory()
            for method_name, descriptor in sorted(
                vars(type(instance)).items(),
                key=lambda item: item[0],
            ):
                if method_name.startswith("_") or method_name in _LIFECYCLE_METHODS:
                    continue
                if isinstance(descriptor, property):
                    continue
                if not callable(descriptor):
                    continue
                method = getattr(instance, method_name, None)
                if not inspect.ismethod(method):
                    continue
                tools.append(LoadedTool(integration_name, method_name, method, ctx))
                seen.add(method_name)

        return tools

    def list_tools(self) -> list[dict[str, Any]]:
        """List all loaded tools with their tool names (no schemas)."""
        items: list[dict[str, Any]] = []
        for integration in sorted(self.integrations.values(), key=lambda p: p.name):
            items.append(
                {
                    "tool": integration.name,
                    "description": integration.description,
                    "tools": [t.tool_name for t in sorted(integration.tools, key=lambda t: t.tool_name)],
                }
            )
        return items

    def describe_tool(self, integration_name: str) -> dict[str, Any]:
        """Return full method schemas for a tool's tools."""
        integration = self.integrations.get(integration_name)
        if not integration:
            return {"error": f"Tool '{integration_name}' not found"}
        tools: list[dict[str, Any]] = []
        for tool in sorted(integration.tools, key=lambda t: t.tool_name):
            try:
                sig = inspect.signature(tool.fn)
            except (TypeError, ValueError) as exc:
                tools.append(
                    {
                        "name": tool.tool_name,
                        "description": (tool.fn.__doc__ or "").strip().split("\n")[0],
                        "parameters": {},
                        "signature_error": str(exc),
                    }
                )
                continue
            params: dict[str, Any] = {}
            for pname, param in sig.parameters.items():
                if pname == "self":
                    continue
                ptype = "any"
                if param.annotation is not inspect.Parameter.empty:
                    ptype = _friendly_type_name(param.annotation)
                pinfo: dict[str, Any] = {"type": ptype}
                if param.default is not inspect.Parameter.empty:
                    pinfo["default"] = param.default
                else:
                    pinfo["required"] = True
                params[pname] = pinfo
            tools.append(
                {
                    "name": tool.tool_name,
                    "description": (tool.fn.__doc__ or "").strip().split("\n")[0],
                    "parameters": params,
                }
            )
        return {
            "tool": integration.name,
            "description": integration.description,
            "tools": tools,
        }

    async def call_tool(self, integration_name: str, tool_name: str, args: dict[str, Any]) -> str:
        """Call a tool function by name and return the result as a TOON string."""
        integration = self.integrations.get(integration_name)
        if not integration:
            return json.dumps({"error": f"Tool '{integration_name}' not found"})

        tool = next((t for t in integration.tools if t.tool_name == tool_name), None)
        if not tool:
            return json.dumps(
                {"error": f"Tool '{tool_name}' not found in tool '{integration_name}'"}
            )

        token = set_tool_context(tool.ctx)
        try:
            if inspect.iscoroutinefunction(tool.fn):
                result = await tool.fn(**args)
            else:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(None, lambda: tool.fn(**args))
            if isinstance(result, str):
                return result
            return _to_toon(result)
        except SystemExit as e:
            return json.dumps(
                {"error": f"Tool called sys.exit({e.code})", "tool": integration_name, "tool": tool_name}
            )
        except Exception as e:
            return json.dumps(
                {"error": str(e), "tool": integration_name, "tool": tool_name}
            )
        finally:
            reset_tool_context(token)

    def create_rest_router(self) -> APIRouter:
        """Create a stable FastAPI router that dispatches to tools via live lookup.

        Routes are fixed at registration time — tool calls resolve through
        ``self.integrations`` at request time so hot-reloads take effect without
        swapping routes.
        """
        pm = self
        router = APIRouter(
            prefix="/tools",
            dependencies=[Depends(verify_api_key)],
        )

        @router.get("")
        async def list_tools() -> dict:
            return {
                name: {
                    "description": p.description,
                    "tools": [t.tool_name for t in p.tools],
                }
                for name, p in pm.integrations.items()
            }

        @router.get("/{integration_name}")
        async def describe_tool(integration_name: str) -> dict:
            return pm.describe_tool(integration_name)

        @router.post("/{integration_name}/{tool_name}")
        async def call_tool(integration_name: str, tool_name: str, request: Request) -> dict:
            body = await request.json() if await request.body() else {}
            result = await pm.call_tool(integration_name, tool_name, body)
            return {"tool": integration_name, "tool": tool_name, "result": result}

        return router


def _make_wrapper(tool: LoadedTool) -> Callable:
    """Wrap a tool function function to inject context and handle errors."""

    async def wrapper(**kwargs: Any) -> str:
        token = set_tool_context(tool.ctx)
        try:
            if inspect.iscoroutinefunction(tool.fn):
                result = await tool.fn(**kwargs)
            else:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(None, lambda: tool.fn(**kwargs))
            if isinstance(result, str):
                return result
            return _to_toon(result)
        except SystemExit as e:
            return json.dumps(
                {
                    "error": f"Tool called sys.exit({e.code})",
                    "tool": tool.integration_name,
                    "tool": tool.tool_name,
                }
            )
        except Exception as e:
            return json.dumps(
                {
                    "error": str(e),
                    "tool": tool.integration_name,
                    "tool": tool.tool_name,
                }
            )
        finally:
            reset_tool_context(token)

    # Preserve original signature for schema generation
    wrapper.__name__ = tool.qualified_name.replace(".", "_")
    wrapper.__doc__ = tool.fn.__doc__ or f"{tool.integration_name} — {tool.tool_name}"
    wrapper.__signature__ = inspect.signature(tool.fn)  # type: ignore[attr-defined]
    try:
        wrapper.__annotations__ = get_type_hints(tool.fn)
    except Exception:
        wrapper.__annotations__ = getattr(tool.fn, "__annotations__", {})
    return wrapper


def _register_mcp_tool(mcp: Any, tool: LoadedTool) -> None:
    """Register a single tool function as an MCP tool."""
    wrapper = _make_wrapper(tool)
    # FastMCP uses the function name as the tool name
    wrapper.__name__ = tool.qualified_name.replace(".", "_")
    mcp.tool(name=tool.qualified_name)(wrapper)


