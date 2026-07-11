from __future__ import annotations

import re
from dataclasses import dataclass

from fastapi import HTTPException

from ..config import HEARTBEAT_URL_POLICY_STRICT

from ..crypto.block_auth import (
    BLOCK_AUTH_ALGORITHM,
    build_block_auth_canonical,
    compute_block_auth_mac,
    derive_block_auth_key,
    encode_block_auth_key,
)
from ..identity import generate_relay_id
from ..persistence.repository import (
    AllowlistRecord,
    NextBlockAuthKey,
    NextRegistryApiKey,
    RegistrationRequestRecord,
    RegistryApiKeyRecord,
    RegistryRepository,
    RelayOverview,
    RelayTarget,
    TokenOccupiedError,
    TokenPlacement,
    TokenReserveResult,
    _PATCH_UNSET,
)

TOKEN_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
HASH_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


def normalize_token(token: str) -> str:
    value = token.lower()
    if not TOKEN_PATTERN.fullmatch(value):
        raise HTTPException(status_code=400, detail="invalid token format")
    return value


def normalize_hash(block_hash: str) -> str:
    value = block_hash.lower()
    if not HASH_PATTERN.fullmatch(value):
        raise HTTPException(status_code=400, detail="invalid blockHash format")
    return value


@dataclass(frozen=True)
class ResolveRouteResult:
    token: str
    targets: tuple[RelayTarget, ...]


@dataclass(frozen=True)
class ReserveUploadOutcome:
    routes: list[ResolveRouteResult]
    granted_ttl_seconds: int
    requested_ttl_seconds: int
    degraded: bool
    stripe_count: int
    replica_factor: int
    ideal_relay_count: int
    actual_relay_count: int


@dataclass(frozen=True)
class VerifyOverwriteResult:
    block_hash: str
    block_auth_key_id: str
    block_auth_mac: str
    block_auth_algorithm: str
    expiry_at: str


@dataclass(frozen=True)
class HeartbeatResult:
    ok: bool
    key_remaining_uses: int
    next_registry_api_key: NextRegistryApiKey | None = None
    bootstrap_registry_api_key: str | None = None
    bootstrap_key_id: str | None = None
    bootstrap_block_auth_key: str | None = None
    bootstrap_block_auth_key_id: str | None = None
    next_block_auth_key: NextBlockAuthKey | None = None


class RegistryService:
    def __init__(self, repository: RegistryRepository) -> None:
        self._repository = repository

    async def _authenticate_relay(
        self,
        *,
        relay_id: str,
        registry_api_key_id: str,
        registry_api_key: str,
        consume: bool,
    ) -> RegistryApiKeyRecord:
        if not await self._repository.is_allowlisted(relay_id):
            raise HTTPException(status_code=403, detail="relay not on allowlist")

        record = await self._repository.verify_registry_api_key(
            relay_id=relay_id,
            key_id=registry_api_key_id,
            registry_api_key=registry_api_key,
            require_remaining=True,
        )
        if record is None:
            raise HTTPException(status_code=401, detail="invalid registryApiKey")

        if consume:
            remaining = await self._repository.consume_registry_api_key_use(
                relay_id=relay_id,
                key_id=registry_api_key_id,
            )
            if remaining < 0:
                raise HTTPException(status_code=401, detail="registryApiKey exhausted")
            return RegistryApiKeyRecord(
                relay_id=record.relay_id,
                key_id=record.key_id,
                remaining_uses=remaining,
                relay_public_key_pem=record.relay_public_key_pem,
            )

        return record

    async def resolve_download_routes(
        self,
        tokens: list[str],
    ) -> list[ResolveRouteResult]:
        await self._repository.purge_expired_tokens()
        results: list[ResolveRouteResult] = []

        for token in tokens:
            resolved = await self._repository.get_resolve_targets(token)
            results.append(await self._resolve_download(token, resolved))

        await self._repository.record_token_resolution_event(
            token_count=len(tokens),
            resolved_count=sum(1 for item in results if item.targets),
        )
        return results

    async def _resolve_download(
        self,
        token: str,
        resolved: TokenReserveResult | None,
    ) -> ResolveRouteResult:
        if resolved is None:
            return ResolveRouteResult(token=token, targets=())
        return ResolveRouteResult(
            token=token,
            targets=resolved.targets,
        )

    async def reserve_upload_blocks(
        self,
        entries: list[tuple[str, str]],
        *,
        ttl_seconds: int | None = None,
    ) -> ReserveUploadOutcome:
        try:
            outcome = await self._repository.lock_tokens_with_block_hashes(
                entries,
                ttl_seconds=ttl_seconds,
            )
        except TokenOccupiedError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "one or more tokens are occupied",
                    "occupiedTokens": exc.occupied_tokens,
                },
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        routes = [
            ResolveRouteResult(
                token=route.token,
                targets=route.targets,
            )
            for route in outcome.routes
        ]
        return ReserveUploadOutcome(
            routes=routes,
            granted_ttl_seconds=outcome.granted_ttl_seconds,
            requested_ttl_seconds=outcome.requested_ttl_seconds,
            degraded=outcome.degraded,
            stripe_count=outcome.stripe_count,
            replica_factor=outcome.replica_factor,
            ideal_relay_count=outcome.ideal_relay_count,
            actual_relay_count=outcome.actual_relay_count,
        )

    async def _validate_relay_placement(
        self,
        *,
        token: str,
        relay_id: str,
        relay_base_url: str,
        block_hash: str,
    ) -> TokenPlacement:
        placement = await self._repository.get_token_placement(token, relay_id)
        if placement is None or not self._repository.is_placement_live(placement):
            raise HTTPException(status_code=404, detail="token not reserved or expired")

        if placement.relay_base_url.rstrip("/") != relay_base_url.rstrip("/"):
            raise HTTPException(status_code=403, detail="token bound to another relay")

        if placement.block_hash != block_hash:
            raise HTTPException(
                status_code=403,
                detail="block hash does not match client registration",
            )

        return placement

    async def _resolve_reported_relay_base_url(
        self,
        relay_id: str,
        relay_base_url: str,
        *,
        relay_public_key_pem: str | None = None,
    ) -> str:
        normalized = _normalize_relay_base_url(relay_base_url)
        record = await self._repository.get_allowlist_record(relay_id)
        if record is None or not record.enabled:
            raise HTTPException(
                status_code=403,
                detail="relay not on allowlist",
            )

        policy = self._repository._config.heartbeat_url_policy

        if record.relay_base_url is None:
            if policy == HEARTBEAT_URL_POLICY_STRICT:
                raise HTTPException(
                    status_code=409,
                    detail=f"relayBaseUrl not configured in allowlist for relay {relay_id}",
                )
            updated = await self._repository.patch_allowlist_entry(
                relay_id,
                relay_base_url=normalized,
            )
            if updated is None:
                raise HTTPException(status_code=403, detail="relay not on allowlist")
            return normalized

        expected = record.relay_base_url.rstrip("/")
        if expected != normalized:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"relayBaseUrl mismatch for {relay_id}: "
                    f"allowlist={expected} reported={normalized}"
                ),
            )
        return normalized

    async def register_block_from_relay(
        self,
        *,
        relay_id: str,
        relay_base_url: str,
        token: str,
        block_hash: str,
        registry_api_key_id: str,
        registry_api_key: str,
    ) -> None:
        canonical_url = await self._resolve_reported_relay_base_url(
            relay_id,
            relay_base_url,
        )
        await self._authenticate_relay(
            relay_id=relay_id,
            registry_api_key_id=registry_api_key_id,
            registry_api_key=registry_api_key,
            consume=False,
        )

        await self._validate_relay_placement(
            token=token,
            relay_id=relay_id,
            relay_base_url=canonical_url,
            block_hash=block_hash,
        )

    async def verify_overwrite(
        self,
        *,
        relay_id: str,
        relay_base_url: str,
        token: str,
        block_hash: str,
        registry_api_key_id: str,
        registry_api_key: str,
    ) -> VerifyOverwriteResult:
        canonical_url = await self._resolve_reported_relay_base_url(
            relay_id,
            relay_base_url,
        )
        await self._authenticate_relay(
            relay_id=relay_id,
            registry_api_key_id=registry_api_key_id,
            registry_api_key=registry_api_key,
            consume=False,
        )

        placement = await self._validate_relay_placement(
            token=token,
            relay_id=relay_id,
            relay_base_url=canonical_url,
            block_hash=block_hash,
        )

        auth_record = await self._repository.get_active_block_auth_key(relay_id)
        if auth_record is None:
            raise HTTPException(
                status_code=503,
                detail="blockAuthKey not provisioned for relay",
            )

        key_bytes = derive_block_auth_key(
            self._repository._config.block_auth_master_key,
            relay_id=relay_id,
            key_id=auth_record.key_id,
        )
        expiry_at = placement.expiry_at
        canonical = build_block_auth_canonical(
            block_auth_key_id=auth_record.key_id,
            token=token,
            relay_id=relay_id,
            relay_base_url=canonical_url.rstrip("/"),
            block_hash=block_hash,
            expiry_at=expiry_at,
        )
        mac = compute_block_auth_mac(key_bytes, canonical)
        return VerifyOverwriteResult(
            block_hash=block_hash,
            block_auth_key_id=auth_record.key_id,
            block_auth_mac=mac,
            block_auth_algorithm=BLOCK_AUTH_ALGORITHM,
            expiry_at=expiry_at,
        )

    async def abandon_replica_placements(
        self,
        failures: list[tuple[str, str]],
    ) -> list[dict[str, str]]:
        if not failures:
            return []

        normalized: list[tuple[str, str]] = []
        for token, relay_id in failures:
            if not isinstance(relay_id, str) or not relay_id:
                raise HTTPException(status_code=400, detail="relayId must be non-empty")
            normalized.append((normalize_token(token), relay_id))

        removed_keys = await self._repository.abandon_replica_placements(normalized)
        return [{"token": token, "relayId": relay_id} for token, relay_id in removed_keys]

    async def list_allowlist_entries(self) -> list[AllowlistRecord]:
        return await self._repository.list_allowlist_entries()

    async def list_relay_overviews(self) -> list[RelayOverview]:
        return await self._repository.list_relay_overviews()

    async def export_admin_database(self, *, row_limit: int = 500) -> dict[str, object]:
        return await self._repository.export_admin_database(row_limit=row_limit)

    async def delete_admin_db_row(
        self,
        *,
        table: str,
        keys: dict[str, object],
    ) -> None:
        try:
            deleted = await self._repository.delete_admin_db_row(table=table, keys=keys)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="row not found")
        await self._repository.record_admin_event(
            action="admin_db_delete",
            target_table=table,
            keys=keys,
        )

    async def add_allowlist_entry(
        self,
        *,
        relay_id: str,
        relay_base_url: str | None,
    ) -> AllowlistRecord:
        normalized_id = _normalize_relay_id(relay_id)
        normalized_url = _normalize_relay_base_url(relay_base_url)
        record = await self._repository.upsert_allowlist_entry(
            relay_id=normalized_id,
            relay_base_url=normalized_url,
            enabled=True,
        )
        await self._repository.record_admin_event(
            action="allowlist_upsert",
            target_table="registry_allowlist",
            keys={"relay_id": normalized_id},
        )
        return record

    async def patch_allowlist_entry(
        self,
        relay_id: str,
        *,
        relay_base_url: str | None | object = _PATCH_UNSET,
        enabled: bool | None = None,
    ) -> AllowlistRecord:
        normalized_id = _normalize_relay_id(relay_id)
        url_arg: str | None | object = relay_base_url
        if url_arg is not _PATCH_UNSET and url_arg is not None:
            url_arg = _normalize_relay_base_url(str(url_arg))
        record = await self._repository.patch_allowlist_entry(
            normalized_id,
            relay_base_url=url_arg,
            enabled=enabled,
        )
        if record is None:
            raise HTTPException(status_code=404, detail="allowlist entry not found")
        await self._repository.record_admin_event(
            action="allowlist_patch",
            target_table="registry_allowlist",
            keys={"relay_id": normalized_id},
        )
        return record

    async def remove_allowlist_entry(self, relay_id: str) -> None:
        normalized_id = _normalize_relay_id(relay_id)
        deleted = await self._repository.delete_allowlist_entry(normalized_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="allowlist entry not found")
        await self._repository.record_admin_event(
            action="allowlist_delete",
            target_table="registry_allowlist",
            keys={"relay_id": normalized_id},
        )

    async def submit_registration_request(
        self,
        *,
        install_id: str,
        relay_base_url: str,
        relay_public_key_pem: str | None,
    ) -> dict[str, object]:
        normalized_install = _normalize_install_id(install_id)
        normalized_url = _normalize_relay_base_url(relay_base_url)
        if normalized_url is None:
            raise HTTPException(status_code=400, detail="relayBaseUrl must be non-empty")
        existing = await self._repository.get_registration_request_by_install_id(
            normalized_install,
        )
        if (
            existing is not None
            and existing.status == "approved"
            and existing.relay_id
            and await self._repository.is_allowlisted(existing.relay_id)
        ):
            return {
                "status": "already_allowlisted",
                "installId": normalized_install,
                "relayId": existing.relay_id,
            }
        record = await self._repository.upsert_registration_request(
            install_id=normalized_install,
            relay_base_url=normalized_url,
            relay_public_key_pem=relay_public_key_pem,
        )
        return {
            "status": record.status,
            "installId": record.install_id,
            "relayId": record.relay_id,
        }

    async def get_registration_status(self, install_id: str) -> dict[str, object]:
        normalized_install = _normalize_install_id(install_id)
        record = await self._repository.get_registration_request_by_install_id(
            normalized_install,
        )
        if record is None:
            return {
                "status": "unassigned",
                "installId": normalized_install,
                "relayId": None,
            }
        if record.status == "approved" and record.relay_id:
            return {
                "status": "approved",
                "installId": record.install_id,
                "relayId": record.relay_id,
            }
        if record.status == "ignored":
            return {
                "status": "ignored",
                "installId": record.install_id,
                "relayId": None,
            }
        return {
            "status": "pending",
            "installId": record.install_id,
            "relayId": None,
        }

    async def list_registration_requests(
        self,
        *,
        status: str = "pending",
    ) -> list[RegistrationRequestRecord]:
        return await self._repository.list_registration_requests(status=status)

    async def count_pending_registration_requests(self) -> int:
        return await self._repository.count_registration_requests(status="pending")

    async def approve_registration_request(
        self,
        install_id: str,
        *,
        relay_id: str | None = None,
    ) -> AllowlistRecord:
        normalized_install = _normalize_install_id(install_id)
        request = await self._repository.get_registration_request_by_install_id(
            normalized_install,
        )
        if request is None or request.status != "pending":
            raise HTTPException(
                status_code=404,
                detail="pending registration request not found",
            )
        reserved = {
            entry.relay_id for entry in await self._repository.list_allowlist_entries()
        }
        assigned_id = (
            _normalize_relay_id(relay_id)
            if relay_id
            else generate_relay_id(reserved=reserved)
        )
        if assigned_id in reserved:
            raise HTTPException(
                status_code=409,
                detail=f"relayId already in allowlist: {assigned_id}",
            )
        entry = await self.add_allowlist_entry(
            relay_id=assigned_id,
            relay_base_url=request.relay_base_url,
        )
        if request.relay_public_key_pem:
            await self._repository.update_relay_public_key(
                assigned_id,
                request.relay_public_key_pem,
            )
        await self._repository.assign_registration_request(
            normalized_install,
            relay_id=assigned_id,
        )
        await self._repository.record_admin_event(
            action="registration_approve",
            target_table="relay_registration_requests",
            keys={"install_id": normalized_install, "relay_id": assigned_id},
        )
        return entry

    async def ignore_registration_request(self, install_id: str) -> None:
        normalized_install = _normalize_install_id(install_id)
        request = await self._repository.get_registration_request_by_install_id(
            normalized_install,
        )
        if request is None or request.status != "pending":
            raise HTTPException(
                status_code=404,
                detail="pending registration request not found",
            )
        updated = await self._repository.set_registration_request_status(
            normalized_install,
            status="ignored",
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="registration request not found")
        await self._repository.record_admin_event(
            action="registration_ignore",
            target_table="relay_registration_requests",
            keys={"install_id": normalized_install},
        )

    async def process_heartbeat(
        self,
        *,
        relay_id: str,
        relay_base_url: str,
        status: str,
        stored_blocks: int,
        max_blocks: int,
        storage_rate: float,
        block_max_age_seconds: int,
        block_sweep_interval_seconds: int,
        registry_api_key_id: str | None,
        registry_api_key: str | None,
        relay_public_key_pem: str | None,
    ) -> HeartbeatResult:
        canonical_url = await self._resolve_reported_relay_base_url(
            relay_id,
            relay_base_url,
            relay_public_key_pem=relay_public_key_pem,
        )

        if relay_public_key_pem:
            await self._repository.update_relay_public_key(
                relay_id,
                relay_public_key_pem,
            )

        await self._repository.upsert_relay_state(
            relay_id=relay_id,
            relay_base_url=canonical_url,
            status=status,
            stored_blocks=stored_blocks,
            max_blocks=max_blocks,
            storage_rate=storage_rate,
            block_max_age_seconds=block_max_age_seconds,
            block_sweep_interval_seconds=block_sweep_interval_seconds,
        )
        await self._repository.record_heartbeat_event(
            relay_id=relay_id,
            relay_base_url=canonical_url,
            status=status,
            stored_blocks=stored_blocks,
            max_blocks=max_blocks,
            storage_rate=storage_rate,
        )

        if registry_api_key_id is None or registry_api_key is None:
            if relay_public_key_pem is None:
                raise HTTPException(
                    status_code=400,
                    detail="relayPublicKeyPem required for initial registryApiKey bootstrap",
                )
            key_id, plaintext_key, record = await self._repository.create_registry_api_key(
                relay_id=relay_id,
                relay_public_key_pem=relay_public_key_pem,
            )
            auth_key_id, auth_key_bytes = await self._repository.create_block_auth_key(
                relay_id,
            )
            return HeartbeatResult(
                ok=True,
                key_remaining_uses=record.remaining_uses,
                bootstrap_registry_api_key=plaintext_key,
                bootstrap_key_id=key_id,
                bootstrap_block_auth_key=encode_block_auth_key(auth_key_bytes),
                bootstrap_block_auth_key_id=auth_key_id,
            )

        record = await self._authenticate_relay(
            relay_id=relay_id,
            registry_api_key_id=registry_api_key_id,
            registry_api_key=registry_api_key,
            consume=True,
        )

        next_key: NextRegistryApiKey | None = None
        next_block_auth: NextBlockAuthKey | None = None
        bootstrap_block_auth_key: str | None = None
        bootstrap_block_auth_key_id: str | None = None

        auth_record = await self._repository.get_active_block_auth_key(relay_id)
        if auth_record is None:
            public_key = relay_public_key_pem or record.relay_public_key_pem
            if public_key is None:
                raise HTTPException(
                    status_code=400,
                    detail="relayPublicKeyPem required for blockAuthKey bootstrap",
                )
            auth_key_id, auth_key_bytes = await self._repository.create_block_auth_key(
                relay_id,
            )
            bootstrap_block_auth_key = encode_block_auth_key(auth_key_bytes)
            bootstrap_block_auth_key_id = auth_key_id

        if record.remaining_uses == 0:
            public_key = relay_public_key_pem or record.relay_public_key_pem
            if public_key is None:
                raise HTTPException(
                    status_code=400,
                    detail="relayPublicKeyPem required for registryApiKey rotation",
                )
            _, _, next_key = await self._repository.rotate_registry_api_key(
                relay_id=relay_id,
                relay_public_key_pem=public_key,
            )
            next_block_auth = await self._repository.rotate_block_auth_key(
                relay_id=relay_id,
                relay_public_key_pem=public_key,
            )

        return HeartbeatResult(
            ok=True,
            key_remaining_uses=record.remaining_uses,
            next_registry_api_key=next_key,
            bootstrap_block_auth_key=bootstrap_block_auth_key,
            bootstrap_block_auth_key_id=bootstrap_block_auth_key_id,
            next_block_auth_key=next_block_auth,
        )


def serialize_allowlist_entry(record: AllowlistRecord) -> dict[str, object]:
    payload: dict[str, object] = {
        "relayId": record.relay_id,
        "addedAt": record.added_at,
        "enabled": record.enabled,
    }
    if record.relay_base_url is not None:
        payload["relayBaseUrl"] = record.relay_base_url
    return payload


def serialize_registration_request(
    record: RegistrationRequestRecord,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "installId": record.install_id,
        "relayBaseUrl": record.relay_base_url,
        "status": record.status,
        "requestedAt": record.requested_at,
        "lastSeenAt": record.last_seen_at,
    }
    if record.relay_id is not None:
        payload["relayId"] = record.relay_id
    if record.relay_public_key_pem is not None:
        payload["hasPublicKey"] = True
    return payload


def serialize_relay_overview(overview: RelayOverview) -> dict[str, object]:
    payload: dict[str, object] = {
        "relayId": overview.relay_id,
        "enabled": overview.enabled,
        "addedAt": overview.added_at,
        "healthStatus": overview.health_status,
    }
    if overview.relay_base_url is not None:
        payload["relayBaseUrl"] = overview.relay_base_url
    if overview.heartbeat_status is not None:
        payload["heartbeatStatus"] = overview.heartbeat_status
    if overview.stored_blocks is not None:
        payload["storedBlocks"] = overview.stored_blocks
    if overview.max_blocks is not None:
        payload["maxBlocks"] = overview.max_blocks
    if overview.storage_rate is not None:
        payload["storageRate"] = overview.storage_rate
    if overview.last_heartbeat_at is not None:
        payload["lastHeartbeatAt"] = overview.last_heartbeat_at
    if overview.block_max_age_seconds is not None:
        payload["blockMaxAgeSeconds"] = overview.block_max_age_seconds
    if overview.block_sweep_interval_seconds is not None:
        payload["blockSweepIntervalSeconds"] = overview.block_sweep_interval_seconds
    return payload


def _normalize_install_id(install_id: str) -> str:
    value = install_id.strip()
    if not value:
        raise HTTPException(status_code=400, detail="installId must be non-empty")
    return value


def _normalize_relay_id(relay_id: str) -> str:
    value = relay_id.strip()
    if not value:
        raise HTTPException(status_code=400, detail="relayId must be non-empty")
    return value


def _normalize_relay_base_url(relay_base_url: str | None) -> str | None:
    if relay_base_url is None:
        return None
    value = relay_base_url.strip().rstrip("/")
    if not value:
        raise HTTPException(status_code=400, detail="relayBaseUrl must be non-empty")
    return value
