"""Tenant onboarding: a new, empty company with one plant, a default calendar, a live scenario and an
administrator. Used by the setup wizard, the CLI and the acceptance tests. No demo data is created."""

from __future__ import annotations

import re
import uuid
from datetime import time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..core.db import new_session
from ..core.errors import Conflict, ValidationFailed
from ..models import Calendar, CalendarShift, Company, OptimizationProfile, Plant, Scenario, Site, Tenant
from .auth import create_user, ensure_roles


def create_tenant(
    name: str,
    slug: str,
    admin_username: str,
    admin_email: str,
    admin_password: str,
    plant_code: str = "P1",
    plant_name: str = "Main plant",
    timezone: str = "UTC",
    currency: str = "EUR",
    shifts: list[tuple[str, str]] | None = None,
    workdays: int = 5,
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,78}", slug):
        raise ValidationFailed("slug must be lower-case letters, digits and dashes")
    try:
        ZoneInfo(timezone)
    except Exception as exc:  # noqa: BLE001
        raise ValidationFailed(f"Unknown time zone {timezone}") from exc
    with new_session(None, "setup") as s:
        if s.scalar(select(Tenant).where(Tenant.slug == slug)) is not None:
            raise Conflict(f"Tenant {slug} already exists")
        t = Tenant(name=name, slug=slug, settings={})
        s.add(t)
        s.commit()
        tid = t.id
    with new_session(tid, "setup") as s:
        ensure_roles(s, tid)
        co = Company(tenant_id=tid, code=slug.upper()[:40], name=name, currency=currency, locale="en")
        s.add(co)
        s.flush()
        site = Site(tenant_id=tid, company_id=co.id, code=plant_code, name=plant_name)
        s.add(site)
        cal = Calendar(tenant_id=tid, code=f"{plant_code}-STD", name=f"{plant_name} standard", timezone=timezone)
        s.add(cal)
        s.flush()
        for wd in range(workdays):
            for k, (a, b) in enumerate(shifts or [("06:00", "14:00"), ("14:00", "22:00")]):
                s.add(CalendarShift(tenant_id=tid, calendar_id=cal.id, weekday=wd, shift_code=f"S{k + 1}", start_time=time.fromisoformat(a), end_time=time.fromisoformat(b), kind="REGULAR", breaks=[]))
        plant = Plant(tenant_id=tid, site_id=site.id, code=plant_code, name=plant_name, timezone=timezone, default_calendar_id=cal.id, settings={})
        s.add(plant)
        s.flush()
        for p in preset_profiles():
            s.add(OptimizationProfile(tenant_id=tid, **p))
        live = Scenario(tenant_id=tid, plant_id=plant.id, name="Live plan", is_live=True, kind="LIVE", owner=admin_username, config={"horizon_days": 28, "frozen_hours": 24, "profile_code": "BALANCED"})
        s.add(live)
        s.flush()
        plant.live_scenario_id = live.id
        create_user(s, tid, admin_username, admin_email, "Administrator", admin_password, ["COMPANY_ADMIN", "PLANNER"])
        s.commit()
        return {"tenant_id": str(tid), "plant_id": str(plant.id), "scenario_id": str(live.id), "calendar": cal.code}


def preset_profiles() -> list[dict[str, Any]]:
    """Editable optimisation profiles created from the engine presets (configuration, not demo data)."""
    from monxuplan_engine.objectives import PRESETS

    return [
        {
            "code": code,
            "name": p["label"],
            "preset": code,
            "objectives": {"mode": p["mode"], "weights": p["weights"], "levels": p.get("levels", []), "preset": code},
            "constraints": {"allow_overtime": False, "materials": "HARD"},
            "solver": {"provider": "hybrid", "profile": "QUICK", "time_limit_s": 20, "dispatch_rules": ["HYBRID_APS"]},
            "is_default": code == "BALANCED",
            "description": p.get("description"),
        }
        for code, p in PRESETS.items()
    ]


_ = uuid
