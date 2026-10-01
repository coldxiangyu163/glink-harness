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
    ├── models.json          generated: egress-proxy baseUrl + literal model id
    ├── auth.json            generated: the run's egress token in pi's own store (0600)
    ├── web-search.json      generated: egress token + proxy base URL (0600), if the
    │                        run has a search credential
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
directory. ``harnesses/pi/settings.json`` is the repository template; only the per-run
copy is rewritten.

**No real credential is rendered (docs/adr/0008).** The host's credential
egress proxy (:mod:`glink_harness.egress`) holds the real model and search keys; a
run is given only an :class:`EgressEndpoint` — the proxy base URLs as its
execution face sees them plus one short-lived token bound to the run. The token
goes where pi and pi-web-access expect a key (``auth.json``, pi's own
credential store, and ``web-search.json``'s ``exaApiKey`` next to
``exaBaseUrl``), so pi needs no change; the proxy swaps it for the real key.
The token is still a secret while the run lives (it joins the run's redaction
boundary) and is revoked when the run's verdict is committed.

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
from .sweep import SessionSweep

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
#: finds it in every face. ``extensions`` carries both the glink-harness
#: foreground-dispatch guard and pi-subagents' own extension config.
TEMPLATE_ENTRIES = ("settings.json", "AGENTS.md", "agents", "extensions", "skills")
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
#: Which packages the container faces load from the image is no longer a list
#: here: it is the pi pack manifest's ``role = "pi-package"`` entries
#: (``HarnessPack.prebaked_pi_packages()``), the same entries the image ARGs and
#: ``settings.json`` are generated from. ``pi-subagents`` is loaded since
#: GLINK-SUB-02: the product forces foreground synchronous dispatch through its
#: own ``tool_call`` guard (``harnesses/pi/extensions/subagent-foreground-guard.ts``)
#: plus a conservative extension config. See docs/pi-integration-notes.md
#: (pi-subagents 0.40.0 dispatch surface).

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
#: pi-web-access config file (read from PI_CODING_AGENT_DIR).
WEB_SEARCH_FILENAME = "web-search.json"
#: pi's own credential store (same file `/login` writes).
AUTH_FILENAME = "auth.json"
#: pi-web-access's Exa key / base URL fields (pi-web-access 0.31.0 ``exa.ts``).
WEB_SEARCH_KEY_FIELD = "exaApiKey"
WEB_SEARCH_BASE_URL_FIELD = "exaBaseUrl"


@dataclass(frozen=True, slots=True)
class RunCredentials:
    """The real credentials one run's egress grant unlocks, and their origin.

    These values stay in the host process: the egress proxy holds them and the
    run only ever renders an :class:`EgressEndpoint` token (docs/adr/0008).
    ``origin`` is ``"host"`` in the single-user surface (the frozen
    configuration), ``"tenant"`` in multi-tenant mode when **only** the run's
    tenant may supply values, and ``"shared"`` when the host provides one frozen
    credential for every tenant (the explicit SaaS policy). ``tenant`` never
    falls back to the host's global ``.env`` key. The dataclass keeps the values
    out of ``repr`` so an accidental log line cannot print them.
    """

    model_api_key: str = field(repr=False, default="")
    web_search_key: str = field(repr=False, default="")
    origin: str = "host"

    @classmethod
    def from_host(cls, config: HostConfig) -> "RunCredentials":
        """The single-user local surface: the frozen host configuration."""
        return cls(
            model_api_key=config.openai_api_key,
            web_search_key=config.web_search_key,
            origin="host",
        )

    @classmethod
    def from_shared_host(cls, config: HostConfig) -> "RunCredentials":
        """The explicit SaaS ``shared`` policy: one host-frozen credential."""
        return cls(
            model_api_key=config.openai_api_key,
            web_search_key=config.web_search_key,
            origin="shared",
        )


@dataclass(frozen=True, slots=True)
class EgressEndpoint:
    """What a run is given instead of a credential (docs/adr/0008).

    ``model_base_url`` / ``search_base_url`` are the egress proxy's routes as
    the run's execution face reaches them (:meth:`Sandbox.egress_origin`);
    ``search_base_url`` is ``None`` when the run has no search credential
    (pi-web-access then uses its keyless fallback). ``token`` is the run's
    short-lived grant — a secret, kept out of ``repr``.
    """

    model_base_url: str
    token: str = field(repr=False)
    search_base_url: str | None = None


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

    def session_sweeps(self) -> tuple[SessionSweep, ...]:
        """pi 写出的会话路径：本 run 专属的 session 目录，允许重写。

        pi owns its session log, so redaction cannot happen on the way in; the
        host masks it on the way out. The whole directory is swept with the
        same boundary because subagent forks and their artifacts share it on
        the faces where it lives on the host (in-process, Docker); the main
        session file is checked separately from the run record (its path is a
        kernel-reported fact, refused if it leaves this root).
        """
        # The runtime directory holds only the run's egress token (revoked at
        # the verdict commit, docs/adr/0008) — no real credential, so there is
        # nothing to remove at close-out.
        return (SessionSweep(root=self.session_dir, carries_session_file=True),)


def prepare_agent_dir(
    config: HostConfig,
    *,
    runtime_dir: Path,
    session_dir: Path | None = None,
    egress: EgressEndpoint,
    image_packages: bool = False,
) -> AgentRuntime:
    """Render the per-run pi configuration directory and return its handle.

    ``runtime_dir`` is required: it must be the run's own directory. Rendering
    into a shared directory would let concurrent runs overwrite each other's
    credentials and extensions. ``session_dir`` defaults to a sibling
    ``sessions`` directory; the host always passes the run's own path.

    ``egress`` is the run's proxy endpoint: ``models.json`` points pi at its
    model route, ``auth.json`` carries its token, and ``web-search.json``
    (only when the run has a search grant) carries the token plus the search
    route. No real credential is ever written here.

    ``image_packages`` is the run's sandbox fact (``Sandbox.isolated``): an
    isolated kernel (container or cloud sandbox) resolves the prebaked pi
    packages at their image-local paths, a host kernel at the template's.
    """
    if not config.openai_base_url:
        raise ConfigError("OPENAI_BASE_URL is required to launch the pi kernel")
    if not config.openai_model:
        raise ConfigError("OPENAI_MODEL is required to launch the pi kernel")

    directory = Path(runtime_dir)
    sessions = Path(session_dir) if session_dir is not None else directory.parent / "sessions"
    # One run's runtime configuration and session directory are host-owned data
    # (they carry the run's token): create them private.
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    sessions.mkdir(parents=True, exist_ok=True, mode=0o700)
    _sync_template(config.agent_config_dir, directory)
    if image_packages:
        _map_prebaked_packages(directory, config.pack.prebaked_pi_packages())
    _write_web_search_config(egress, directory)
    _write_auth_json(egress, directory)

    models_path = directory / "models.json"
    _write_private(models_path, _models_document(config, egress))
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


def local_package_source(source: str, prebaked: Sequence[str]) -> str | None:
    """The image-local path for a prebaked ``npm:`` package, else ``None``.

    ``prebaked`` is the pack manifest's prebaked package names. Only the
    documented npm spec form is recognized: ``npm:<name>`` with an optional
    ``@<range>`` (scoped names included). Ranges are accepted because the image
    copy *is* the pinned version (the Dockerfile ARG and ``settings.json`` are
    both generated from the same manifest entry); the path itself carries no
    version so the mapping cannot drift when the pin moves.
    """
    if not isinstance(source, str) or not source.startswith("npm:"):
        return None
    name = _npm_package_name(source[len("npm:"):].strip())
    if name not in prebaked:
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


def _map_prebaked_packages(directory: Path, prebaked: Sequence[str]) -> Path | None:
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
        replacement, mapped = _map_package_entry(entry, prebaked)
        rewritten.append(replacement)
        changed = changed or mapped
    if not changed:
        return None
    document["packages"] = rewritten
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def _map_package_entry(entry: Any, prebaked: Sequence[str]) -> tuple[Any, bool]:
    """One ``packages`` entry, rewritten to its image path when prebaked.

    The object form (``{"source": …, "skills": []}``) keeps its filters: only
    the source is replaced, and the entry is copied instead of mutated because
    it came straight from the parsed document.
    """
    if isinstance(entry, str):
        local = local_package_source(entry, prebaked)
        return (local, True) if local is not None else (entry, False)
    if isinstance(entry, dict) and isinstance(entry.get("source"), str):
        local = local_package_source(entry["source"], prebaked)
        if local is not None:
            rewritten = dict(entry)
            rewritten["source"] = local
            return rewritten, True
    return entry, False


def passthrough_env(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The minimal host environment a kernel process (or the docker CLI) inherits.

    ``base_env`` (default ``os.environ``) is filtered through
    :data:`PASSTHROUGH_ENV`, so a caller handing in ``os.environ`` cannot widen
    the child's view of host credentials. No model credential is ever an
    environment variable: pi reads it from the rendered ``auth.json``.
    """
    source = dict(base_env) if base_env is not None else dict(os.environ)
    return {name: source[name] for name in PASSTHROUGH_ENV if name in source}


def _models_document(config: HostConfig, egress: EgressEndpoint) -> dict[str, Any]:
    return {
        "providers": {
            PROVIDER_ID: {
                # The egress proxy's model route, never the upstream itself:
                # the upstream URL and key stay in the host (docs/adr/0008).
                "baseUrl": egress.model_base_url,
                "api": "openai-completions",
                # No apiKey here: the token comes from pi's own auth.json (see
                # _write_auth_json), so `env` inside the kernel never sees it.
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


def _write_private(path: Path, document: Mapping[str, Any]) -> Path:
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _write_auth_json(egress: EgressEndpoint, directory: Path) -> Path:
    """Store the run's egress token the way pi stores a key (``auth.json``).

    pi's official credential store — the same file ``/login`` writes, mode
    0600 — keeps the token out of the kernel's process environment. It is
    rewritten on every render, so nothing from a previous render survives.
    """
    return _write_private(
        directory / AUTH_FILENAME,
        {PROVIDER_ID: {"type": "api_key", "key": egress.token}},
    )


def _sync_template(template_dir: Path, target_dir: Path) -> None:
    """Replace every template entry in the runtime directory.

    Replacement (not merge) is the point: a run must not inherit an extension or
    skill that a previous render left in the same directory. Each
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


def _write_web_search_config(egress: EgressEndpoint, directory: Path) -> Path | None:
    """Point pi-web-access at the egress proxy's search route, or remove the file.

    pi-web-access reads ``web-search.json`` from ``PI_CODING_AGENT_DIR``. With a
    search grant the file carries the run token as ``exaApiKey`` and the proxy
    route as ``exaBaseUrl`` (the plugin sends the key as ``x-api-key``; the
    proxy swaps it). Without one the file is removed and the plugin falls back
    to keyless Exa MCP search — a supported state that carries no secret. The
    operator's own ``~/.pi`` search file is never read (docs/adr/0008).
    """
    target = directory / WEB_SEARCH_FILENAME
    if egress.search_base_url is None:
        target.unlink(missing_ok=True)
        return None
    return _write_private(
        target,
        {
            WEB_SEARCH_KEY_FIELD: egress.token,
            WEB_SEARCH_BASE_URL_FIELD: egress.search_base_url,
        },
    )
