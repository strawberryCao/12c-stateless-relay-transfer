from __future__ import annotations

import json
import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path


def _import_local_secrets():
    server_root = Path(__file__).resolve().parents[2]
    if str(server_root) not in sys.path:
        sys.path.insert(0, str(server_root))
    import local_secrets

    return local_secrets

DEFAULT_CONFIG_FILENAME = "relay_server.config.json"


@dataclass(frozen=True)
class RegistryConfig:
    url: str
    http_proxy: str | None = None
    auto_register_on_startup: bool = False


@dataclass(frozen=True)
class RelayServerConfig:
    host: str
    port: int
    public_base_url: str
    max_body_bytes: int
    max_blocks: int
    data_dir: Path
    database_path: Path
    heartbeat_interval_seconds: int
    registry: RegistryConfig
    secrets_dir: Path
    relay_rsa_key_path: Path
    registry_api_key_store_path: Path
    registry_api_key_initial_uses: int
    block_auth_key_store_path: Path
    block_max_age_seconds: int
    block_sweep_interval_seconds: int
    admin_api_key: str | None = None
    cors_allowed_origins: tuple[str, ...] = ("*",)

    @classmethod
    def from_dict(cls, value: dict) -> RelayServerConfig:
        host = _require_str(value.get("host"), "host", default="0.0.0.0")
        port = _require_int(value.get("port"), "port", default=9090)
        public_base_url = _require_str(
            os.environ.get("RELAY_PUBLIC_BASE_URL") or value.get("publicBaseUrl"),
            "publicBaseUrl",
            default=f"http://127.0.0.1:{port}",
        )
        max_body_bytes = _require_int(
            value.get("maxBodyBytes"),
            "maxBodyBytes",
            default=16 * 1024 * 1024,
        )
        max_blocks = _require_int(value.get("maxBlocks"), "maxBlocks", default=100_000)
        data_dir = Path(
            _require_str(value.get("dataDir"), "dataDir", default="./data/blocks"),
        )
        database_path = Path(
            _require_str(
                value.get("databasePath"),
                "databasePath",
                default="./data/relay.db",
            ),
        )
        heartbeat_interval_seconds = _require_int(
            value.get("heartbeatIntervalSeconds"),
            "heartbeatIntervalSeconds",
            default=30,
        )
        registry = _parse_registry(value.get("registry"))
        secrets_dir = Path(
            _require_str(value.get("secretsDir"), "secretsDir", default="./data/secrets"),
        )
        relay_rsa_key_path = Path(
            _require_str(
                value.get("relayRsaKeyPath"),
                "relayRsaKeyPath",
                default=str(secrets_dir / "relay_rsa.pem"),
            ),
        )
        registry_api_key_store_path = Path(
            _require_str(
                value.get("registryApiKeyStorePath"),
                "registryApiKeyStorePath",
                default=str(secrets_dir / "registry_api_key.json"),
            ),
        )
        registry_api_key_initial_uses = _require_int(
            value.get("registryApiKeyInitialUses"),
            "registryApiKeyInitialUses",
            default=100,
        )
        block_auth_store = value.get("blockAuthKeyStorePath")
        block_auth_key_store_path = Path(
            _require_str(
                block_auth_store,
                "blockAuthKeyStorePath",
                default=str(secrets_dir / "block_auth_key.json"),
            ),
        )
        block_max_age_seconds = _require_int(
            value.get("blockMaxAgeSeconds"),
            "blockMaxAgeSeconds",
            default=86_400,
        )
        block_sweep_interval_seconds = _require_int(
            value.get("blockSweepIntervalSeconds"),
            "blockSweepIntervalSeconds",
            default=3600,
        )
        admin_api_key = _load_admin_api_key(value.get("adminApiKey"))
        cors_allowed_origins = _parse_cors_allowed_origins(
            value.get("corsAllowedOrigins"),
        )
        return cls(
            host=host,
            port=port,
            public_base_url=public_base_url.rstrip("/"),
            max_body_bytes=max_body_bytes,
            max_blocks=max_blocks,
            data_dir=data_dir,
            database_path=database_path,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            registry=registry,
            secrets_dir=secrets_dir,
            relay_rsa_key_path=relay_rsa_key_path,
            registry_api_key_store_path=registry_api_key_store_path,
            registry_api_key_initial_uses=registry_api_key_initial_uses,
            block_auth_key_store_path=block_auth_key_store_path,
            block_max_age_seconds=block_max_age_seconds,
            block_sweep_interval_seconds=block_sweep_interval_seconds,
            admin_api_key=admin_api_key,
            cors_allowed_origins=cors_allowed_origins,
        )


def _load_admin_api_key(value: object) -> str | None:
    env = os.environ.get("RELAY_ADMIN_API_KEY")
    if env:
        if not env.strip():
            raise ValueError("RELAY_ADMIN_API_KEY must be non-empty when set")
        return env.strip()
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("adminApiKey must be a non-empty string")
    return value


def _parse_registry(value: object) -> RegistryConfig:
    if not isinstance(value, dict):
        raise ValueError('config missing "registry" object with "url"')
    url = os.environ.get("REGISTRY_URL") or value.get("url")
    if not isinstance(url, str) or not url:
        raise ValueError('registry.url must be a non-empty string')
    http_proxy = value.get("httpProxy")
    if http_proxy is None:
        normalized_proxy = None
    elif not isinstance(http_proxy, str) or not http_proxy.strip():
        raise ValueError('registry.httpProxy must be a non-empty string when set')
    else:
        normalized_proxy = http_proxy.strip()
    auto_register_on_startup = value.get("autoRegisterOnStartup", False)
    if not isinstance(auto_register_on_startup, bool):
        raise ValueError("registry.autoRegisterOnStartup must be a boolean")
    return RegistryConfig(
        url=url.rstrip("/"),
        http_proxy=normalized_proxy,
        auto_register_on_startup=auto_register_on_startup,
    )


def load_config(config_path: str | None = None) -> RelayServerConfig:
    env_path = os.environ.get("STATELESS_RELAY_SERVER_CONFIG")
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
        f"relay server config not found; copy relay_server.config.example.json to "
        f"{DEFAULT_CONFIG_FILENAME}",
    )


def _load_from_path(path: Path) -> RelayServerConfig:
    ls = _import_local_secrets()
    secrets_path = ls.secrets_path_for_config(path)
    ls.migrate_secrets_from_config(path, secrets_path, ("adminApiKey",))

    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} root must be a JSON object")

    secrets_path = ls.secrets_path_for_config(path)
    admin_api_key = ls.resolve_env_or_secret(
        os.environ.get("RELAY_ADMIN_API_KEY"),
        secrets_path,
        "adminApiKey",
        lambda: secrets.token_urlsafe(32),
    )

    data = {key: value for key, value in data.items() if key != "adminApiKey"}
    config = RelayServerConfig.from_dict({**data, "adminApiKey": admin_api_key})
    base_dir = path.parent
    return RelayServerConfig(
        host=config.host,
        port=config.port,
        public_base_url=config.public_base_url,
        max_body_bytes=config.max_body_bytes,
        max_blocks=config.max_blocks,
        data_dir=_resolve_path(base_dir, config.data_dir),
        database_path=_resolve_path(base_dir, config.database_path),
        heartbeat_interval_seconds=config.heartbeat_interval_seconds,
        registry=config.registry,
        secrets_dir=_resolve_path(base_dir, config.secrets_dir),
        relay_rsa_key_path=_resolve_path(base_dir, config.relay_rsa_key_path),
        registry_api_key_store_path=_resolve_path(
            base_dir,
            config.registry_api_key_store_path,
        ),
        registry_api_key_initial_uses=config.registry_api_key_initial_uses,
        block_auth_key_store_path=_resolve_path(
            base_dir,
            config.block_auth_key_store_path,
        ),
        block_max_age_seconds=config.block_max_age_seconds,
        block_sweep_interval_seconds=config.block_sweep_interval_seconds,
        admin_api_key=config.admin_api_key,
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


def _resolve_path(base_dir: Path, value: Path) -> Path:
    return value if value.is_absolute() else (base_dir / value).resolve()


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
