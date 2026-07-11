from __future__ import annotations

from pydantic import BaseModel, Field


class RelayAuthFields(BaseModel):
    relayId: str
    relayBaseUrl: str
    registryApiKeyId: str
    registryApiKey: str


class RegisterRequest(RelayAuthFields):
    token: str
    blockHash: str


class VerifyOverwriteRequest(RelayAuthFields):
    token: str
    blockHash: str


class HeartbeatRequest(BaseModel):
    relayId: str
    relayBaseUrl: str
    status: str = "ok"
    storedBlocks: int = Field(ge=0)
    maxBlocks: int = Field(gt=0)
    storageRate: float = Field(ge=0.0, le=1.0)
    blockMaxAgeSeconds: int = Field(default=86400, ge=1, le=86400)
    blockSweepIntervalSeconds: int = Field(default=3600, ge=1, le=86400)
    registryApiKeyId: str | None = None
    registryApiKey: str | None = None
    relayPublicKeyPem: str | None = None


class ResolveRequest(BaseModel):
    tokens: list[str] = Field(max_length=4096)


class BlockHashRegistration(BaseModel):
    token: str
    blockHash: str


class ReserveTokensRequest(BaseModel):
    blocks: list[BlockHashRegistration] = Field(max_length=4096)
    ttlSeconds: int | None = Field(default=None, ge=1, le=86400)


class ReplicaPlacementFailure(BaseModel):
    token: str
    relayId: str


class AbandonReplicaPlacementsRequest(BaseModel):
    failures: list[ReplicaPlacementFailure] = Field(max_length=4096)


class AddAllowlistEntryRequest(BaseModel):
    relayId: str
    relayBaseUrl: str | None = None


class PatchAllowlistEntryRequest(BaseModel):
    relayBaseUrl: str | None = None
    enabled: bool | None = None


class RegistrationRequestBody(BaseModel):
    installId: str
    relayBaseUrl: str
    relayPublicKeyPem: str | None = None


class ApproveRegistrationRequest(BaseModel):
    relayId: str | None = None


class DeleteAdminDbRowRequest(BaseModel):
    table: str = Field(min_length=1)
    keys: dict[str, object]
