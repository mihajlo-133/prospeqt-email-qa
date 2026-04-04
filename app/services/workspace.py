import os
import re

WORKSPACE_ENV_PATTERN = re.compile(r'^WORKSPACE_([A-Z0-9_]+)_API_KEY$')

_registry: dict[str, str] = {}


def load_from_env() -> None:
    """Read all WORKSPACE_*_API_KEY env vars and populate the registry."""
    global _registry
    _registry = {}
    for key, value in os.environ.items():
        match = WORKSPACE_ENV_PATTERN.match(key)
        if match and value:
            raw_name = match.group(1)
            # Transform: ENAVRA -> enavra, HEYREACH_CLIENT -> heyreach-client
            display_name = raw_name.lower().replace("_", "-")
            _registry[display_name] = value


def list_workspaces() -> list[dict]:
    """Return a list of workspace dicts with name and last-4 key preview."""
    result = []
    for name, key in _registry.items():
        preview = key[-4:] if len(key) >= 4 else key
        result.append({"name": name, "key_preview": f"...{preview}"})
    return result


def get_api_key(name: str) -> str | None:
    """Return the API key for the given workspace name, or None if not found."""
    return _registry.get(name)


def add_workspace(name: str, api_key: str) -> None:
    """Add or update a workspace in the registry and set the corresponding env var."""
    _registry[name] = api_key
    env_var_name = f"WORKSPACE_{name.upper().replace('-', '_')}_API_KEY"
    os.environ[env_var_name] = api_key


def remove_workspace(name: str) -> bool:
    """Remove a workspace from the registry. Returns True if it existed, False otherwise."""
    if name not in _registry:
        return False
    del _registry[name]
    env_var_name = f"WORKSPACE_{name.upper().replace('-', '_')}_API_KEY"
    os.environ.pop(env_var_name, None)
    return True
