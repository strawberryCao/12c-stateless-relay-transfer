from __future__ import annotations

import base64
import json
import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

from .scheduling.policy import PlacementPolicy


def _import_local_secrets():
    server_root = Path(__file__).resolve().parents[2]
    if str(server_root) not in sys.path:
        sys.path.insert(0, str(server_root))
    import local_secrets

    return local_secrets

DEFAULT_CONFIG_FILENAME = "registry_server.config.json"

HEARTBEAT_URL_POLICY_SYNC_IF_UNSET = "sync_if_unset"
HEARTBEAT_URL_POLICY_STRICT = "strict"
VALID_HEARTBEAT_URL_POLICIES = frozenset(
    {
        HEARTBEAT_URL_POLICY_SYNC_IF_UNSET,
        HEARTBEAT_URL_POLICY_STRICT,
    },
)


@dataclass(frozen=True)
class AllowlistEntry:
    relay_id: str
    relay_base_url: str | None = None


@dataclass(frozen=True)
class RegistryServerConfig:
    host: str
    port: int
    database_path: Path
    token_ttl_seconds: int
    registry_api_key_initial_uses: int
    block_auth_master_key: bytes
    allowlist: tuple[AllowlistEntry, ...]
    placement_policy: PlacementPolicy
    relay_heartbeat_stale_seconds: int
    admin_api_key: str | None = None
    heartbeat_url_policy: str = HEARTBEAT_URL_POLICY_SYNC_IF_UNSET
    cors_allowed_origins: tuple[str, ...] = ("*",)

    @classmethod
    def from_dict(cls, value: dict) -> RegistryServerConfig:
        host = _require_str(value.get("host"), "host", default="0.0.0.0")
        port = _require_int(value.get("port"), "port", default=8080)
        database_path = Path(
            _require_str(
                value.get("databasePath"),
                "databasePath",
                default="./data/registry.db",
            ),
        )
        token_ttl_seconds = _require_int(
            value.get("tokenTtlSeconds"),
            "tokenTtlSeconds",
            default=3600,
        )
        registry_api_key_initial_uses = _require_int(
            value.get("registryApiKeyInitialUses"),
            "registryApiKeyInitialUses",
            default=100,
        )
        allowlist = _parse_allowlist(value.get("allowlist"))
        block_auth_master_key = load_block_auth_master_key(
            value.get("blockAuthMasterKey"),
        )
        placement_policy = parse_placement_policy(value)
        relay_heartbeat_stale_seconds = _require_int(
            value.get("relayHeartbeatStaleSeconds"),
            "relayHeartbeatStaleSeconds",
            default=120,
        )
        admin_api_key = _load_admin_api_key(value.get("adminApiKey"))
        heartbeat_url_policy = _parse_heartbeat_url_policy(value.get("heartbeatUrlPolicy"))
        cors_allowed_origins = _parse_cors_allowed_origins(
            value.get("corsAllowedOrigins"),
        )
        return cls(
            host=host,
            port=port,
            database_path=database_path,
            token_ttl_seconds=token_ttl_seconds,
            registry_api_key_initial_uses=registry_api_key_initial_uses,
            block_auth_master_key=block_auth_master_key,
            allowlist=allowlist,
            placement_policy=placement_policy,
            relay_heartbeat_stale_seconds=relay_heartbeat_stale_seconds,
            admin_api_key=admin_api_key,
            heartbeat_url_policy=heartbeat_url_policy,
            cors_allowed_origins=cors_allowed_origins,
        )


def parse_placement_policy(root: dict) -> PlacementPolicy:
    nested = root.get("placementPolicy")
    if nested is not None and not isinstance(nested, dict):
        raise ValueError("placementPolicy must be an object")

    def field(name: str) -> object:
        if isinstance(nested, dict) and name in nested:
            return nested.get(name)
        return root.get(name)

    stripe_target_relays = _require_int(
        field("stripeTargetRelays"),
        "stripeTargetRelays",
        default=3,
    )
    max_file_replica_count = _require_int_nonneg(
        field("maxFileReplicaCount"),
        "maxFileReplicaCount",
        default=1,
    )
    max_replicas_per_block = _require_int(
        field("maxReplicasPerBlock"),
        "maxReplicasPerBlock",
        default=2,
    )
    min_grant_ttl_seconds = _require_int(
        field("minGrantTtlSeconds"),
        "minGrantTtlSeconds",
        default=3600,
    )
    clock_skew_seconds = _require_int(
        field("clockSkewSeconds"),
        "clockSkewSeconds",
        default=60,
    )
    max_requested_ttl_seconds = _require_int(
        field("maxRequestedTtlSeconds"),
        "maxRequestedTtlSeconds",
        default=86400,
    )
    ttl_degrade_step_divisor = _require_int(
        field("ttlDegradeStepDivisor"),
        "ttlDegradeStepDivisor",
        default=4,
    )
    return PlacementPolicy(
        stripe_target_relays=stripe_target_relays,
        max_file_replica_count=max_file_replica_count,
        max_replicas_per_block=max_replicas_per_block,
        min_grant_ttl_seconds=min_grant_ttl_seconds,
        clock_skew_seconds=clock_skew_seconds,
        max_requested_ttl_seconds=max_requested_ttl_seconds,
        ttl_degrade_step_divisor=ttl_degrade_step_divisor,
    )


def _parse_allowlist(value: object) -> tuple[AllowlistEntry, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("allowlist must be an array")
    entries: list[AllowlistEntry] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("allowlist item must be an object")
        relay_id = item.get("relayId")
        if not isinstance(relay_id, str) or not relay_id:
            raise ValueError("allowlist.relayId must be a non-empty string")
        relay_base_url = item.get("relayBaseUrl")
        if relay_base_url is not None and (
            not isinstance(relay_base_url, str) or not relay_base_url
        ):
            raise ValueError("allowlist.relayBaseUrl must be a non-empty string")
        entries.append(
            AllowlistEntry(
                relay_id=relay_id,
                relay_base_url=relay_base_url.rstrip("/") if relay_base_url else None,
            ),
        )
    return tuple(entries)


def _parse_heartbeat_url_policy(value: object) -> str:
    if value is None:
        return HEARTBEAT_URL_POLICY_SYNC_IF_UNSET
    if not isinstance(value, str) or value not in VALID_HEARTBEAT_URL_POLICIES:
        allowed = ", ".join(sorted(VALID_HEARTBEAT_URL_POLICIES))
        raise ValueError(f"heartbeatUrlPolicy must be one of: {allowed}")
    return value


def _load_admin_api_key(value: object) -> str | None:
    env = os.environ.get("REGISTRY_ADMIN_API_KEY")
    if env:
        if not env.strip():
            raise ValueError("REGISTRY_ADMIN_API_KEY must be non-empty when set")
        return env.strip()
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("adminApiKey must be a non-empty string")
    return value


def load_config(config_path: str | None = None) -> RegistryServerConfig:
    env_path = os.environ.get("STATELESS_RELAY_REGISTRY_CONFIG")
    path = Path(config_path or env_path or DEFAULT_CONFIG_FILENAME)
    if not path.is_absolute():
        path = Path.cwd() / path

    if path.is_file():
        return _load_from_path(path)

    package_root = Path(__file__).resolve().parents[1]
    fallback = package_root / DEFAULT_CONFIG_FILENAME
    if fallback.is_file():
        return _load_from_path(fallback)

    raise FileNotFoundError(
        f"registry config not found; copy registry_server.config.example.json to "
        f"{DEFAULT_CONFIG_FILENAME}",
    )


def _load_from_path(path: Path) -> RegistryServerConfig:
    ls = _import_local_secrets()
    secrets_path = ls.secrets_path_for_config(path)
    ls.migrate_secrets_from_config(
        path,
        secrets_path,
        ("adminApiKey", "blockAuthMasterKey"),
    )

    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} root must be a JSON object")

    block_auth_master_key = ls.resolve_env_or_secret(
        os.environ.get("REGISTRY_BLOCK_AUTH_MASTER_KEY"),
        secrets_path,
        "blockAuthMasterKey",
        generate_block_auth_master_key,
    )
    admin_api_key = ls.resolve_env_or_secret(
        os.environ.get("REGISTRY_ADMIN_API_KEY"),
        secrets_path,
        "adminApiKey",
        lambda: secrets.token_urlsafe(32),
    )

    data = {
        key: value
        for key, value in data.items()
        if key not in ("adminApiKey", "blockAuthMasterKey")
    }
    config = RegistryServerConfig.from_dict(
        {
            **data,
            "blockAuthMasterKey": block_auth_master_key,
            "adminApiKey": admin_api_key,
        },
    )
    base_dir = path.parent
    database_path = (
        config.database_path
        if config.database_path.is_absolute()
        else (base_dir / config.database_path).resolve()
    )
    return RegistryServerConfig(
        host=config.host,
        port=config.port,
        database_path=database_path,
        token_ttl_seconds=config.token_ttl_seconds,
        registry_api_key_initial_uses=config.registry_api_key_initial_uses,
        block_auth_master_key=config.block_auth_master_key,
        allowlist=config.allowlist,
        placement_policy=config.placement_policy,
        relay_heartbeat_stale_seconds=config.relay_heartbeat_stale_seconds,
        admin_api_key=config.admin_api_key,
        heartbeat_url_policy=config.heartbeat_url_policy,
        cors_allowed_origins=config.cors_allowed_origins,
    )


def _parse_cors_allowed_origins(value: object) -> tuple[str, ...]:
    env = os.environ.get("CORS_ALLOWED_ORIGINS")
    if env is not None:
        return tuple(item.strip() for item in env.split(",") if item.strip())
    if value is None:
        return ("*",)
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError("corsAllowedOrigins must be an array of non-empty strings")
    return tuple(item.strip() for item in value)


def load_block_auth_master_key(value: object) -> bytes:
    env = os.environ.get("REGISTRY_BLOCK_AUTH_MASTER_KEY")
    raw = env if env else value
    if not isinstance(raw, str) or not raw:
        raise ValueError(
            "blockAuthMasterKey required in config or REGISTRY_BLOCK_AUTH_MASTER_KEY env",
        )
    padded = raw + "=" * (-len(raw) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception:
        decoded = base64.b64decode(raw.encode("ascii"))
    if len(decoded) < 32:
        raise ValueError("blockAuthMasterKey must decode to at least 32 bytes")
    return decoded


def generate_block_auth_master_key() -> str:
    return secrets.token_urlsafe(32)


def generate_registry_api_key() -> str:
    return secrets.token_urlsafe(32)


def _require_str(value: object, field: str, *, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _require_int(value: object, field: str, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value <= 0:
        raise ValueError(f"{field} must be positive")
    return value


def _require_int_nonneg(value: object, field: str, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value < 0:
        raise ValueError(f"{field} must be non-negative")
    return value
