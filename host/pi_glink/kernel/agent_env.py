"""Prepare the pi configuration directory for one run.

pi reads its configuration from ``PI_CODING_AGENT_DIR``. The repository ships a
template directory (``agent/``) for the parts that should be reviewed and
versioned: settings, AGENTS.md, extensions, skills, prompts.

**The runtime directory belongs to one run.** It is rendered into the run's own
directory (``<data_root>/runs/<run_id>/pi-agent``) together with the run's
session directory, so two runs can never share a configuration file, a
credential seed or pi's own mutable state (``auth.json``, ``models-store.json``,
``npm/``). The repository template is input only; it is copied, never written
to. Rendering replaces each template entry instead of merging into an existing
directory, so a removed extension or credential does not survive as a stale
file.

Operator-provided values cannot live in those files. pi resolves environment
interpolation for ``apiKey`` and ``headers`` only — not for ``baseUrl`` and not
for model ids (verified against pi 0.86.1: a ``$OPENAI_BASE_URL`` baseUrl
reaches the provider as a literal string and every request fails with
``Invalid URL``). So the host renders a small runtime directory:

    <run_dir>/pi-agent/
    ├── models.json          generated: literal baseUrl + literal model id
    ├── auth.json            generated: model credential in pi's own store (0600)
    ├── web-search.json      generated: pi-web-access credential (0600), if configured
    └── settings.json, AGENTS.md, extensions/, …   copied from the template

**Container faces load prebaked packages from the image.** Docker and E2B run
inside the self-contained worker image built from ``docker/Dockerfile.worker`` /
``docker/e2b/Dockerfile.e2b``. Those images preinstall the pi packages into
``/opt/pi-packages`` (see :data:`IMAGE_PACKAGE_ROOT`), and the renderer rewrites
each ``npm:`` declaration of a prebaked package to its image-local path — the
documented local-package form, which pi loads "from the resolved path without
copying". A container run therefore never runs ``npm install`` against the
registry: the per-run cost and the dependency on registry reachability are gone,
and the version pin is enforced where the image is built. The in-process face
keeps the (now pinned) ``npm:`` declaration and installs into its own runtime
directory. ``agent/settings.json`` is the repository template; only the per-run
copy is rewritten.

**The model credential lives in ``auth.json``, not in the process environment.**
That is pi's official credential store — the same file ``/login`` writes — and
using it keeps the key out of the kernel's environment, where its shell tool
would otherwise read it with ``env``. The file sits in the run's runtime
directory (outside the repository) with mode 0600 and is rewritten on every run.
The same reasoning applies to the pi-web-access credential, which the plugin can
only read from a file anyway.

No model price table is rendered: this host does not know the operator's rates,
and inventing ``cost: 0`` would make pi report a fabricated price. Usage is
reported as the provider's own counters (see ``runner.py``).
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..config import ConfigError, HostConfig

#: Provider id used inside the generated models.json.
PROVIDER_ID = "frontier"
#: OpenRouter-style thinking controls (``reasoning: {effort}``).
#:
#: Without this, pi sends no thinking parameter for a custom openai-completions
#: provider, the upstream omits reasoning entirely, and the workbench has
#: nothing to render for ``thinking_delta``. Verified against
#: openrouter.ai/api/v1: with this format the same model returns reasoning.
THINKING_FORMAT = "openrouter"
#: Template entries copied verbatim into the runtime directory.
#:
#: ``agents`` is pi-subagents' user-scope agent directory
#: (``<PI_CODING_AGENT_DIR>/agents``, src/agents/agents.ts of pi-subagents 0.40.0);
#: the workbench ships its own ``researcher`` definition there so the dispatch tool
#: finds it in every face. ``extensions`` carries both the pi-glink
#: foreground-dispatch guard and pi-subagents' own extension config.
TEMPLATE_ENTRIES = ("settings.json", "AGENTS.md", "agents", "extensions", "skills", "prompts")
#: Backwards-compatible private alias (the list is part of the module contract
#: for the E2B upload manifest, so it is exported under a public name).
_TEMPLATE_ENTRIES = TEMPLATE_ENTRIES
#: Faces that run pi inside a self-contained image/template. They load pi
#: packages from the image (see :data:`CONTAINER_LOCAL_PACKAGES`) instead of
#: installing them into the mounted runtime directory on every run.
CONTAINER_EXECUTORS = ("docker", "e2b")
#: Image-local pi package root, installed at build time by both
#: ``docker/Dockerfile.worker`` and ``docker/e2b/Dockerfile.e2b``. It is
#: deliberately outside ``PI_CODING_AGENT_DIR``: the runtime directory is a
#: host mount (Docker) or an upload (E2B) that is re-rendered every run, so
#: anything preinstalled there would be shadowed or rebuilt.
IMAGE_PACKAGE_ROOT = "/opt/pi-packages"
IMAGE_PACKAGE_NODE_MODULES = f"{IMAGE_PACKAGE_ROOT}/node_modules"
#: Guard script both images carry. pi silently ignores a local package path
#: that does not exist, so the container faces run this script first; it fails
#: with exit 78 when a package the runtime settings point at is missing. The
#: Docker face uses it as ``--entrypoint`` (a pre-change image fails at
#: ``docker run`` itself), the E2B face calls it inside the command chain.
IMAGE_PACKAGE_GUARD = f"{IMAGE_PACKAGE_ROOT}/check-packages.sh"
#: Packages the container faces may load from the image: an ``npm:`` entry
#: naming one of these is rendered as its image-local path. Both images install
#: ``pi-web-access`` and ``pi-subagents`` by pinned ARG.
#:
#: ``pi-subagents`` is loaded since GLINK-SUB-02: the product forces foreground
#: synchronous dispatch through its own ``tool_call`` guard
#: (``agent/extensions/subagent-foreground-guard.ts``) plus a conservative
#: extension config, so the async/detached blocker GLINK-SUB-01 found is handled
#: on the pi-glink side instead of by a package switch. See
#: docs/pi-integration-notes.md (pi-subagents 0.40.0 dispatch surface).
CONTAINER_LOCAL_PACKAGES = ("pi-web-access", "pi-subagents")

#: Environment names handed to the pi process verbatim.
#:
#: The host deliberately does **not** inherit its whole environment: doing so
#: would hand pi's tools every credential the operator's shell happens to export
#: (other providers, cloud keys, CI tokens). Only what the kernel needs to run
#: and reach the network is passed through; every credential is injected
#: explicitly below and nothing else. (pi's tools can still read the model key
#: the kernel itself uses — that is inherent to running the agent in-process and
#: is recorded in docs/architecture.md §6.)
PASSTHROUGH_ENV = (
    "PATH", "HOME", "SHELL", "USER", "LOGNAME", "TMPDIR", "TERM",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "NODE_OPTIONS",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
)
#: pi-web-access credential file (read from PI_CODING_AGENT_DIR).
WEB_SEARCH_FILENAME = "web-search.json"
#: pi's own credential store (same file `/login` writes).
AUTH_FILENAME = "auth.json"
#: Default provider key used when the credential comes from the environment.
WEB_SEARCH_ENV_PROVIDER_KEY = "exaApiKey"


@dataclass(frozen=True, slots=True)
class RunCredentials:
    """The credentials one run may render, and where they came from.

    ``origin`` is ``"host"`` in the single-user surface (the frozen
    configuration and, for search, the operator's own pi file), ``"tenant"``
    in multi-tenant mode when **only** the run's tenant may supply values, and
    ``"shared"`` when the host provides one frozen credential for every tenant
    (the explicit SaaS policy). ``tenant`` and ``shared`` both refuse to fall
    back to the host's global ``.env`` key or to the operator's ``~/.pi``
    search configuration — ``shared`` *is* the host configuration, but without
    the operator's personal search file. The dataclass keeps the values out of
    ``repr`` so an accidental log line cannot print them.
    """

    model_api_key: str = field(repr=False, default="")
    web_search_key: str = field(repr=False, default="")
    web_search_config_path: Path | None = None
    origin: str = "host"

    @classmethod
    def from_host(cls, config: HostConfig) -> "RunCredentials":
        """The single-user local surface: the frozen host configuration."""
        return cls(
            model_api_key=config.openai_api_key,
            web_search_key=config.web_search_key,
            web_search_config_path=config.web_search_config_path,
            origin="host",
        )

    @classmethod
    def from_shared_host(cls, config: HostConfig) -> "RunCredentials":
        """The explicit SaaS ``shared`` policy: one host-frozen credential.

        Every tenant renders this credential; no tenant entry is consulted.
        ``web_search_config_path`` stays ``None`` on purpose: the shared policy
        may use the host's own ``PI_GLINK_WEB_SEARCH_KEY``, but it must never
        substitute the operator's personal ``~/.pi/web-search.json``.
        """
        return cls(
            model_api_key=config.openai_api_key,
            web_search_key=config.web_search_key,
            web_search_config_path=None,
            origin="shared",
        )


@dataclass(frozen=True, slots=True)
class AgentRuntime:
    """Where pi will read its configuration for one run, and which model."""

    directory: Path
    provider_id: str
    model_id: str
    models_path: Path
    #: The run's own pi session directory (never a shared host directory).
    session_dir: Path

    @property
    def model_ref(self) -> str:
        return f"{self.provider_id}/{self.model_id}"


def prepare_agent_dir(
    config: HostConfig,
    *,
    runtime_dir: Path,
    session_dir: Path | None = None,
    credentials: RunCredentials | None = None,
) -> AgentRuntime:
    """Render the per-run pi configuration directory and return its handle.

    ``runtime_dir`` is required: it must be the run's own directory. Rendering
    into a shared directory would let concurrent runs overwrite each other's
    credentials and extensions. ``session_dir`` defaults to a sibling
    ``sessions`` directory; the host always passes the run's own path.

    ``credentials`` selects what the rendered ``auth.json`` / ``web-search.json``
    contain. The default derives them from the host configuration (local mode);
    multi-tenant runs pass the run's own tenant credentials, so one tenant's
    key is never rendered into another tenant's directory.
    """
    if not config.openai_base_url:
        raise ConfigError("OPENAI_BASE_URL is required to launch the pi kernel")
    if not config.openai_model:
        raise ConfigError("OPENAI_MODEL is required to launch the pi kernel")

    source = credentials if credentials is not None else RunCredentials.from_host(config)
    directory = Path(runtime_dir)
    sessions = Path(session_dir) if session_dir is not None else directory.parent / "sessions"
    # One run's runtime configuration and session directory are host-owned data
    # (they carry the rendered credentials): create them private.
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    sessions.mkdir(parents=True, exist_ok=True, mode=0o700)
    _sync_template(config.agent_config_dir, directory)
    if config.executor in CONTAINER_EXECUTORS:
        _map_prebaked_packages(directory)
    _seed_web_search_config(source, directory)
    _write_auth_json(source, directory)

    models_path = directory / "models.json"
    models_path.write_text(
        json.dumps(_models_document(config), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    models_path.chmod(0o600)
    return AgentRuntime(
        directory=directory,
        provider_id=PROVIDER_ID,
        model_id=config.openai_model,
        models_path=models_path,
        session_dir=sessions,
    )


def template_manifest(template_dir: Path) -> list[tuple[str, Path, bool]]:
    """Every reviewable template entry as ``(relative, absolute, is_dir)``.

    Symlinks are never listed: the runtime configuration must be a copy of
    reviewed repository content, not a path into the operator's filesystem. The
    manifest is shared by the local faces (which copy the entries) and the E2B
    face (which uploads them), so all three deliver the same configuration.
    """
    manifest: list[tuple[str, Path, bool]] = []
    if not template_dir.is_dir():
        return manifest
    for entry in TEMPLATE_ENTRIES:
        source = template_dir / entry
        if source.is_symlink() or not source.exists():
            continue
        if source.is_dir():
            manifest.append((entry, source, True))
            for child in sorted(source.rglob("*")):
                if child.is_symlink():
                    continue
                relative = f"{entry}/{child.relative_to(source).as_posix()}"
                manifest.append((relative, child, child.is_dir()))
        else:
            manifest.append((entry, source, False))
    return manifest


#: Prompt-template directory inside the agent template. Its ``*.md`` file stems
#: are the ``/`` command names pi expands (docs/prompt-templates.md) and, because
#: of that, the only ``/`` messages the host admits. Adding a file there is the
#: single source of truth for the accepted command set.
PROMPTS_DIRNAME = "prompts"


def prompt_template_names(template_dir: Path) -> tuple[str, ...]:
    """The ``/`` command names this configuration accepts, from the template directory.

    pi expands a prompt template only when the message starts with ``/`` and its
    first token exactly equals a template file's stem (``expandPromptTemplate``:
    ``^/([^\\s]+)`` plus a case-sensitive name match). Every other ``/...``
    message is either an extension command or plain text; the host admits only
    this set, so a new command is reviewed repository content rather than
    something a loaded package registers at runtime.

    Symlinks are skipped for the same reason :func:`template_manifest` skips
    them: the accepted set must be repository content, not a path into the
    operator's filesystem.
    """
    directory = Path(template_dir) / PROMPTS_DIRNAME
    if not directory.is_dir():
        return ()
    return tuple(
        sorted(
            path.stem
            for path in directory.glob("*.md")
            if path.is_file() and not path.is_symlink()
        )
    )


def local_package_source(source: str) -> str | None:
    """The image-local path for a prebaked ``npm:`` package, else ``None``.

    Only the documented npm spec form is recognized: ``npm:<name>`` with an
    optional ``@<range>`` (scoped names included). Ranges are accepted because
    the image copy *is* the pinned version (the repository pins it in the
    Dockerfile and in ``agent/settings.json``, tied together by
    ``tests/test_repository_boundaries.py``); the path itself carries no version
    so the mapping cannot drift when the pin moves.
    """
    if not isinstance(source, str) or not source.startswith("npm:"):
        return None
    name = _npm_package_name(source[len("npm:"):].strip())
    if name not in CONTAINER_LOCAL_PACKAGES:
        return None
    return f"{IMAGE_PACKAGE_NODE_MODULES}/{name}"


def _npm_package_name(spec: str) -> str:
    """The package name part of an npm spec (``@scope/name[@range]``)."""
    if spec.startswith("@"):
        slash = spec.find("/")
        if slash <= 1:
            return spec
        at = spec.find("@", slash)
        return spec[:at] if at > 0 else spec
    at = spec.find("@")
    return spec[:at] if at > 0 else spec


def _map_prebaked_packages(directory: Path) -> Path | None:
    """Point a container-face ``settings.json`` at the image-local packages.

    The runtime directory is a copy of the reviewed template, so the rewrite
    happens per run and never touches the repository template. Every ``npm:``
    entry naming a prebaked package becomes its image path — the documented
    local-package source, "loaded from the resolved path without copying":
    no ``npm install`` runs inside the container and the run does not depend on
    the registry. Entries that name anything else (other npm packages, git
    sources, local paths) are left exactly as declared; this function narrows
    nothing and invents nothing.

    A malformed ``settings.json`` is a clear failure instead of a silent
    fallback: pi would reject the file anyway, and a run that quietly lost its
    search extension is worse than a refused one.
    """
    path = directory / "settings.json"
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"容器面无法改写 settings.json: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError("容器面 settings.json 必须是 JSON 对象")
    packages = document.get("packages")
    if not isinstance(packages, list) or not packages:
        return None
    rewritten: list[Any] = []
    changed = False
    for entry in packages:
        replacement, mapped = _map_package_entry(entry)
        rewritten.append(replacement)
        changed = changed or mapped
    if not changed:
        return None
    document["packages"] = rewritten
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def _map_package_entry(entry: Any) -> tuple[Any, bool]:
    """One ``packages`` entry, rewritten to its image path when prebaked.

    The object form (``{"source": …, "skills": []}``) keeps its filters: only
    the source is replaced, and the entry is copied instead of mutated because
    it came straight from the parsed document.
    """
    if isinstance(entry, str):
        local = local_package_source(entry)
        return (local, True) if local is not None else (entry, False)
    if isinstance(entry, dict) and isinstance(entry.get("source"), str):
        local = local_package_source(entry["source"])
        if local is not None:
            rewritten = dict(entry)
            rewritten["source"] = local
            return rewritten, True
    return entry, False


def build_pi_argv(
    config: HostConfig, runtime: AgentRuntime, *, extra: Sequence[str] = ()
) -> list[str]:
    """Command line for the headless pi process."""
    argv = [
        config.pi_bin,
        "--mode",
        "rpc",
        "--provider",
        runtime.provider_id,
        "--model",
        runtime.model_id,
    ]
    argv.extend(extra)
    return argv


def build_pi_env(
    config: HostConfig,
    runtime: AgentRuntime,
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Child environment: minimal passthrough, and **no model credential**.

    The credential is not an environment variable any more. It lives in the
    runtime ``auth.json`` (mode 0600, outside the repository) that pi reads
    itself, which keeps it out of the kernel process environment — a shell tool
    running `env` cannot see it. ``base_env`` is still filtered through
    :data:`PASSTHROUGH_ENV` so a caller handing in ``os.environ`` cannot widen
    the child's view of host credentials.
    """
    source = dict(base_env) if base_env is not None else dict(os.environ)
    env = {name: source[name] for name in PASSTHROUGH_ENV if name in source}
    env["PI_CODING_AGENT_DIR"] = str(runtime.directory)
    env["PI_CODING_AGENT_SESSION_DIR"] = str(runtime.session_dir)
    env["PI_SKIP_VERSION_CHECK"] = "1"
    env["PI_TELEMETRY"] = "0"
    return env


def _models_document(config: HostConfig) -> dict[str, Any]:
    return {
        "providers": {
            PROVIDER_ID: {
                "baseUrl": config.openai_base_url,
                "api": "openai-completions",
                # No apiKey here on purpose: the credential comes from pi's own
                # auth.json (see _write_auth_json), so `env` inside the kernel
                # never sees it. pi resolves provider credentials from both
                # sources; leaving this field out selects the safe one.
                "models": [
                    {
                        "id": config.openai_model,
                        "name": f"Frontier research model ({config.openai_model})",
                        "reasoning": True,
                        "input": ["text"],
                        "contextWindow": config.context_window,
                        "maxTokens": config.max_output_tokens,
                        "compat": {
                            "thinkingFormat": THINKING_FORMAT,
                            "supportsReasoningEffort": True,
                        },
                        # No `cost` block: the host does not know the operator's
                        # rates. A zero table would make pi report a fabricated
                        # price; without it pi reports only the provider's
                        # token counters, which is what the run record sums.
                    }
                ],
            }
        }
    }


def _write_auth_json(credentials: RunCredentials, directory: Path) -> Path | None:
    """Store the run's model credential the way pi itself stores it.

    ``auth.json`` is pi's official credential store — the same file ``/login``
    writes, mode 0600, inside the run's runtime config directory (which lives
    outside the repository). Using it instead of an environment variable is what
    keeps the key **out of the kernel's process environment**, and therefore out
    of reach of its shell tool: the objective allows "environment variables or
    pi's official credential mechanism", and this is the narrower one.

    The file is rewritten on every render, so a credential pi might clear while
    rotating its own state is always restored before the next kernel start, and
    a credential removed from the configuration is removed from the directory
    rather than left behind.
    """
    target = directory / AUTH_FILENAME
    if not credentials.model_api_key:
        target.unlink(missing_ok=True)
        return None
    payload = {PROVIDER_ID: {"type": "api_key", "key": credentials.model_api_key}}
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    target.chmod(0o600)
    return target


def _sync_template(template_dir: Path, target_dir: Path) -> None:
    """Replace every template entry in the runtime directory.

    Replacement (not merge) is the point: a run must not inherit an extension,
    skill or prompt that a previous render left in the same directory. Each
    top-level entry is removed and re-copied; files inside template directories
    are copied individually so symlinks cannot smuggle content in.
    """
    manifest = template_manifest(template_dir)
    present = {entry.split("/", 1)[0] for entry, _source, _is_dir in manifest}
    for name in TEMPLATE_ENTRIES:
        target = target_dir / name
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        if name not in present:
            continue
        source = template_dir / name
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
        else:
            shutil.copy2(source, target)
    for entry, source, is_dir in manifest:
        if "/" not in entry:
            continue
        target = target_dir / entry
        if is_dir:
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
        else:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(source, target)


def _seed_web_search_config(credentials: RunCredentials, directory: Path) -> Path | None:
    """Seed pi-web-access credentials into the runtime config directory.

    pi-web-access resolves its config directory from ``PI_CODING_AGENT_DIR``
    (verified in its ``utils.ts``), so redirecting that variable would silently
    hide the operator's own ``web-search.json`` and downgrade search to the
    keyless fallback. Local mode has two sources, in order:

    1. ``PI_GLINK_WEB_SEARCH_KEY`` — rendered as ``{"exaApiKey": …}``;
    2. the operator's own file (``~/.pi/agent/web-search.json`` or
       ``~/.pi/web-search.json``), copied verbatim.

    Multi-tenant mode passes a :class:`RunCredentials` with ``origin="tenant"``
    and an explicit ``web_search_config_path`` of ``None``: only the run's own
    tenant key may be rendered, and a tenant without one gets no search
    credential (keyless fallback) instead of another tenant's or the operator's.
    The explicit SaaS ``shared`` policy is the same except that its search key
    comes from the host's ``PI_GLINK_WEB_SEARCH_KEY``: any origin other than
    ``host`` must never inherit the operator's personal file.

    Having no credential at all is a supported state: pi-web-access still works
    through keyless Exa MCP search, and the run contract does not require a key.
    The target is removed when no source has a credential, so a per-run
    directory can never keep a previous credential behind.
    """
    target = directory / WEB_SEARCH_FILENAME
    if credentials.web_search_key:
        payload = {WEB_SEARCH_ENV_PROVIDER_KEY: credentials.web_search_key}
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        target.chmod(0o600)
        return target
    source = credentials.web_search_config_path
    if credentials.origin != "host":
        # Multi-tenant (per-tenant or shared): never inherit the operator's own
        # credential file, even if a caller hands one in by mistake.
        source = None
    if source is not None and source.is_file():
        shutil.copy2(source, target)
        target.chmod(0o600)
        return target
    target.unlink(missing_ok=True)
    return None
