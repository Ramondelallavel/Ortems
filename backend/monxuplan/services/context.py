"""Per-request security context."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace

from ..core.errors import Forbidden


@dataclass
class Ctx:
    tenant_id: uuid.UUID
    user_id: uuid.UUID | None
    username: str
    permissions: set[str] = field(default_factory=set)
    roles: set[str] = field(default_factory=set)
    plant_ids: set[uuid.UUID] | None = None  # None = all plants
    request_id: str | None = None
    ip: str | None = None
    via: str = "session"  # session | bearer | api_key | oidc | system
    locale: str = "en"

    def has(self, perm: str) -> bool:
        return perm in self.permissions

    def require(self, perm: str) -> None:
        if perm not in self.permissions:
            raise Forbidden(f"Your role does not allow this action ({perm}).", code="NO_PERMISSION", context={"permission": perm})

    def can_access_plant(self, plant_id: uuid.UUID | None) -> bool:
        return plant_id is None or self.plant_ids is None or plant_id in self.plant_ids

    def require_plant(self, plant_id: uuid.UUID | None) -> None:
        if not self.can_access_plant(plant_id):
            raise Forbidden("You do not have access to this plant.", code="NO_PLANT_ACCESS")

    def elevated(self, *perms: str) -> Ctx:
        """A copy with extra permissions for one internal read the caller's own rights justify (same
        user, tenant and plant scope). Never use it for writes."""
        return replace(self, permissions=set(self.permissions) | set(perms))


def system_ctx(tenant_id: uuid.UUID, username: str = "system") -> Ctx:
    from ..core.security import PERMISSIONS

    return Ctx(tenant_id=tenant_id, user_id=None, username=username, permissions=set(PERMISSIONS), roles={"SYSTEM"}, via="system")
