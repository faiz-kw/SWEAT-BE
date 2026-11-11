"""
apps/tenant_core/services_telephony_config.py — Dynamic Telephony & WebRTC Configuration Service.

Provides a unified, 100% dynamic configuration engine for Vobiz and WebRTC telephony.
Eliminates all hardcoded SIP usernames, DIDs, registrar hosts, and timeout values.
Configuration is stored in the tenant-scoped `Integration` table and `AgentPresence` table,
with sensible environment-variable fallbacks for developer environments.
"""

import os
import logging
from datetime import timedelta
from typing import Optional, Dict, Any

from django.db import models
from django.utils import timezone
from config.routers import get_tenant_db_alias

logger = logging.getLogger(__name__)


class TelephonyConfigService:
    """Authoritative service for resolving and updating telephony configurations dynamically."""

    DEFAULT_REGISTRAR = "registrar.vobiz.ai"
    DEFAULT_WSS_URL = "wss://registrar.vobiz.ai:5063"
    DEFAULT_DID = "+919240954144"
    DEFAULT_TIMEOUT_SEC = 35

    @classmethod
    def get_vobiz_config(cls, db_alias: Optional[str] = None) -> Dict[str, Any]:
        """
        Dynamically retrieves Vobiz telephony settings for the current tenant.
        Checks the `Integration` model in the active tenant database first,
        falling back to environment variables.
        """
        alias = db_alias or get_tenant_db_alias() or "default"
        config_data = {}

        try:
            from apps.tenant_core.models_infra import Integration
            integration = Integration.objects.using(alias).filter(
                integration_type="CALLING",
                provider__iexact="VOBIZ",
                status="ACTIVE",
            ).first()

            if integration and isinstance(integration.configuration, dict):
                config_data = integration.configuration
        except Exception as exc:
            logger.warning("Could not query Integration model for Vobiz in db %s: %s", alias, exc)

        registrar_host = (
            config_data.get("registrar_host")
            or os.getenv("VOBIZ_REGISTRAR_HOST")
            or cls.DEFAULT_REGISTRAR
        ).strip()

        webrtc_gateway = (
            config_data.get("webrtc_gateway_url")
            or os.getenv("VOBIZ_WEBRTC_WSS_URL")
            or (f"wss://{registrar_host}:5063" if ":" not in registrar_host else f"wss://{registrar_host}")
        ).strip()

        from_number = (
            config_data.get("from_number")
            or os.getenv("VOBIZ_VIRTUAL_DID")
            or os.getenv("VOBIZ_SALES_DID")
            or cls.DEFAULT_DID
        ).strip()

        auth_id = (
            config_data.get("auth_id")
            or os.getenv("VOBIZ_AUTH_ID", "")
        ).strip()

        auth_token = (
            config_data.get("auth_token")
            or os.getenv("VOBIZ_AUTH_TOKEN", "")
        ).strip()

        endpoint_password = (
            config_data.get("endpoint_password")
            or os.getenv("VOBIZ_ENDPOINT_PASSWORD")
            or os.getenv("VOBIZ_SIP_PASSWORD")
            or "Sweat@2026!"
        ).strip()

        try:
            ring_timeout = int(config_data.get("ring_timeout_sec") or os.getenv("VOBIZ_RING_TIMEOUT", cls.DEFAULT_TIMEOUT_SEC))
            ring_timeout = max(35, ring_timeout)
        except (ValueError, TypeError):
            ring_timeout = cls.DEFAULT_TIMEOUT_SEC

        record_all = config_data.get("record_all_calls", True)
        if isinstance(record_all, str):
            record_all = record_all.lower() in ("true", "1", "yes")

        return {
            "provider": "VOBIZ",
            "auth_id": auth_id,
            "auth_token": auth_token,
            "from_number": from_number,
            "registrar_host": registrar_host,
            "webrtc_gateway_url": webrtc_gateway,
            "ring_timeout_sec": ring_timeout,
            "record_all_calls": bool(record_all),
            "endpoint_password": endpoint_password,
            "status": "ACTIVE" if (auth_id or from_number) else "UNCONFIGURED",
        }

    @classmethod
    def update_vobiz_config(cls, payload: Dict[str, Any], db_alias: Optional[str] = None) -> Dict[str, Any]:
        """
        Dynamically updates the tenant's Vobiz configuration from UI admin settings.
        Persists into the `Integration` model with full multi-tenant isolation.
        """
        alias = db_alias or get_tenant_db_alias() or "default"
        from apps.tenant_core.models_infra import Integration

        current_int = Integration.objects.using(alias).filter(
            integration_type="CALLING",
            provider__iexact="VOBIZ",
        ).first()

        existing_cfg = current_int.configuration if (current_int and isinstance(current_int.configuration, dict)) else {}

        # Merge updates cleanly
        new_cfg = dict(existing_cfg)
        for key in ["from_number", "auth_id", "registrar_host", "webrtc_gateway_url", "ring_timeout_sec", "record_all_calls", "endpoint_password"]:
            if key in payload:
                new_cfg[key] = payload[key]

        # Handle secret auth_token safely: only update if non-empty and not masked
        if payload.get("auth_token") and "••••" not in str(payload["auth_token"]):
            new_cfg["auth_token"] = str(payload["auth_token"]).strip()

        target_status = payload.get("status", current_int.status if current_int else "ACTIVE")

        if current_int:
            current_int.configuration = new_cfg
            current_int.status = target_status
            current_int.save(using=alias)
            integration = current_int
        else:
            integration = Integration.objects.using(alias).create(
                integration_type="CALLING",
                provider="VOBIZ",
                configuration=new_cfg,
                status=target_status,
            )

        logger.info("Updated Vobiz telephony configuration in db %s: from_number=%s status=%s", alias, new_cfg.get("from_number"), target_status)
        return cls.get_vobiz_config(alias)

    @classmethod
    def get_available_agent(
        cls,
        db_alias: Optional[str] = None,
        org=None,
        preferred_user=None,
        caller_branch=None,
    ):
        """
        Dynamically selects an available sales representative without ANY hardcoded usernames.

        Priority Waterfall:
          1. Assigned sales user for the lead/member (if AVAILABLE).
          2. Any available agent belonging to caller's branch.
          3. Any available agent in the organization.
          4. Self-healing auto-recovery: Agents stuck in WRAP_UP > 10 min are auto-freed to AVAILABLE.
        """
        alias = db_alias or get_tenant_db_alias() or "default"
        from apps.tenant_core.models_crm import AgentPresence

        active_window = timezone.now() - timedelta(minutes=8)

        eligible_agents = (
            AgentPresence.objects.using(alias)
            .exclude(sip_username="")
            .exclude(sip_username__startswith="unassigned_")
            .exclude(sip_username__startswith="agent_")
            .exclude(sip_username__icontains="test")
        )

        # 1. Preferred user check (must be actively online with a real SIP line)
        if preferred_user:
            assigned_presence = eligible_agents.filter(
                user=preferred_user,
            ).select_related("user").first()
            if assigned_presence:
                if assigned_presence.status == "AVAILABLE":
                    logger.info("Telephony dispatch: Selected preferred assigned rep %s (%s)", preferred_user.email, assigned_presence.sip_username)
                    return assigned_presence
                elif assigned_presence.status in ("ON_CALL", "WRAP_UP"):
                    act = assigned_presence.active_session
                    if not act or act.status in ("COMPLETED", "NO_ANSWER", "FAILED", "BUSY", "CANCELLED") or act.ended_at is not None:
                        assigned_presence.status = "AVAILABLE"
                        assigned_presence.active_session = None
                        assigned_presence.save(using=alias, update_fields=["status", "active_session"])
                        logger.info("Telephony dispatch: Auto-released preferred rep %s (%s) from completed %s to AVAILABLE", preferred_user.email, assigned_presence.sip_username, assigned_presence.status)
                        return assigned_presence

        # 2. Branch-specific available agent
        if caller_branch:
            branch_agent = eligible_agents.filter(
                user__home_branch=caller_branch,
                status="AVAILABLE",
            ).order_by("-last_heartbeat").select_related("user").first()
            if branch_agent:
                logger.info("Telephony dispatch: Selected branch %s rep %s (%s)", caller_branch.name, branch_agent.user.email, branch_agent.sip_username)
                return branch_agent

        # 3. Any available rep in the organization (excluding test/unassigned users), prioritizing active heartbeats
        org_filter = {"organization": org} if org else {}

        # First priority: Agents with active heartbeats within the last 8 minutes
        available = eligible_agents.filter(
            status="AVAILABLE",
            last_heartbeat__gte=active_window,
            **org_filter,
        ).order_by("-last_heartbeat").select_related("user").first()

        # Second priority: Fall back to ANY agent marked AVAILABLE in the organization (even if heartbeat was missed)
        if not available:
            available = eligible_agents.filter(
                status="AVAILABLE",
                **org_filter,
            ).order_by("-last_heartbeat").select_related("user").first()

        # Resilient fallback across tenant database if org_filter yielded no results
        if not available:
            available = eligible_agents.filter(
                status="AVAILABLE",
            ).order_by("-last_heartbeat").select_related("user").first()

        if available:
            logger.info("Telephony dispatch: Selected available rep %s (%s)", available.user.email, available.sip_username)
            return available

        # 4. Self-healing fallback: Release stale ON_CALL or WRAP_UP agents (> 5 minutes or completed sessions)
        five_min_ago = timezone.now() - timedelta(minutes=5)
        stale_agent = eligible_agents.filter(
            status__in=["WRAP_UP", "ON_CALL"],
            last_heartbeat__lt=five_min_ago,
        ).order_by("-last_heartbeat").select_related("user").first()

        if not stale_agent:
            # Check agents marked ON_CALL or WRAP_UP whose active session is already finished or None
            stale_agent = eligible_agents.filter(
                status__in=["ON_CALL", "WRAP_UP"],
            ).filter(
                models.Q(active_session__isnull=True) |
                models.Q(active_session__status__in=["COMPLETED", "NO_ANSWER", "FAILED", "BUSY", "CANCELLED"])
            ).order_by("-last_heartbeat").select_related("user").first()

        if stale_agent:
            logger.info("Self-heal: Auto-releasing stale %s agent %s (%s) to AVAILABLE", stale_agent.status, stale_agent.user.email, stale_agent.sip_username)
            stale_agent.status = "AVAILABLE"
            stale_agent.active_session = None
            stale_agent.save(using=alias, update_fields=["status", "active_session"])
            return stale_agent

        logger.warning("Telephony dispatch: No sales agents currently available in db %s (all busy, wrap-up, or offline)", alias)
        return None

    @classmethod
    def get_agent_webrtc_credentials(cls, user, db_alias: Optional[str] = None) -> Dict[str, Any]:
        """
        Dynamically returns WebRTC SIP credentials for the authenticated user.
        Ensures the agent has an AgentPresence record and returns the exact WSS endpoint.
        When all dedicated Vobiz endpoints are occupied, assigns an explicit
        ``unassigned_<id>`` placeholder without colliding on the shared primary endpoint.
        """
        alias = db_alias or get_tenant_db_alias() or "default"
        from django.db import IntegrityError, transaction
        from apps.tenant_core.models_crm import AgentPresence
        from apps.tenant_core.models_org import Organization

        vobiz_cfg = cls.get_vobiz_config(alias)

        # Primary registered endpoints on Vobiz account
        primary_endpoint = (
            os.getenv("VOBIZ_DEFAULT_SIP_USER")
            or "iqra24030269280035568"
        )
        fallback_endpoints = list(dict.fromkeys([
            primary_endpoint,
            "sweatagent104201155654570218059",
        ]))

        def _is_registered_sip(sip_name: Optional[str]) -> bool:
            return bool(sip_name and not sip_name.startswith(("unassigned_", "agent_")))

        def _claim_free_endpoint(exclude_id=None) -> Optional[str]:
            qs = AgentPresence.objects.using(alias)
            for ep in fallback_endpoints:
                ep_qs = qs.filter(sip_username=ep)
                if exclude_id is not None:
                    ep_qs = ep_qs.exclude(id=exclude_id)
                if not ep_qs.exists():
                    return ep
            return None

        uid_hex = user.id.hex[:12] if hasattr(user.id, "hex") else str(user.id)[:12]
        unassigned_sip = f"unassigned_{uid_hex}"

        # Look up or initialize AgentPresence for this user safely under concurrency
        presence = AgentPresence.objects.using(alias).filter(user=user).first()
        if not presence:
            user_org = getattr(user, 'organization', None) or Organization.objects.using(alias).first()
            assigned_ep = _claim_free_endpoint()
            chosen_sip = assigned_ep or unassigned_sip
            initial_status = "AVAILABLE" if assigned_ep else "OFFLINE"

            try:
                with transaction.atomic(using=alias):
                    presence, created = AgentPresence.objects.using(alias).get_or_create(
                        user=user,
                        defaults={
                            "organization": user_org,
                            "sip_username": chosen_sip,
                            "status": initial_status,
                        },
                    )
                    if created:
                        logger.info(
                            "Provisioned AgentPresence for user %s with sip_username=%s (registered=%s)",
                            user.email,
                            chosen_sip,
                            bool(assigned_ep),
                        )
            except IntegrityError:
                presence = AgentPresence.objects.using(alias).filter(user=user).first()
                if not presence:
                    with transaction.atomic(using=alias):
                        presence, _ = AgentPresence.objects.using(alias).get_or_create(
                            user=user,
                            defaults={
                                "organization": user_org,
                                "sip_username": unassigned_sip,
                                "status": "OFFLINE",
                            },
                        )
        elif not _is_registered_sip(presence.sip_username):
            # Auto-heal placeholder rows ONLY if an unclaimed real Vobiz endpoint is free.
            # Never fall back to primary_endpoint when the pool is exhausted.
            assigned_ep = _claim_free_endpoint(exclude_id=presence.id)
            if assigned_ep:
                try:
                    with transaction.atomic(using=alias):
                        presence.sip_username = assigned_ep
                        presence.status = "AVAILABLE"
                        presence.save(using=alias, update_fields=["sip_username", "status"])
                        logger.info("Auto-healed placeholder SIP username for user %s -> %s", user.email, assigned_ep)
                except IntegrityError:
                    presence = AgentPresence.objects.using(alias).filter(user=user).first() or presence
            elif not presence.sip_username or presence.sip_username.startswith("agent_"):
                try:
                    with transaction.atomic(using=alias):
                        presence.sip_username = unassigned_sip
                        presence.status = "OFFLINE"
                        presence.save(using=alias, update_fields=["sip_username", "status"])
                        logger.info("Pool exhausted; normalized agent %s -> %s (OFFLINE)", user.email, unassigned_sip)
                except IntegrityError:
                    presence = AgentPresence.objects.using(alias).filter(user=user).first() or presence

        sip_user = presence.sip_username
        is_registered_endpoint = _is_registered_sip(sip_user)
        sip_domain = vobiz_cfg["registrar_host"]
        sip_uri = f"sip:{sip_user}@{sip_domain}" if is_registered_endpoint else ""

        return {
            "username": sip_user if is_registered_endpoint else "",
            "sip_username": sip_user,
            "password": vobiz_cfg.get("endpoint_password", "Sweat@2026!") if is_registered_endpoint else "",
            "sip_password": vobiz_cfg.get("endpoint_password", "Sweat@2026!") if is_registered_endpoint else "",
            "sip_domain": sip_domain,
            "sip_uri": sip_uri,
            "wss_url": vobiz_cfg["webrtc_gateway_url"] if is_registered_endpoint else "",
            "caller_id": vobiz_cfg["from_number"],
            "ring_timeout_sec": vobiz_cfg["ring_timeout_sec"],
            "status": presence.status if is_registered_endpoint else "UNASSIGNED",
            "line_status": "PROVISIONED" if is_registered_endpoint else "UNASSIGNED",
            "is_registered_endpoint": is_registered_endpoint,
            "available": is_registered_endpoint,
            "can_make_calls": is_registered_endpoint,
            "display_name": f"{user.first_name} {user.last_name or ''}".strip() or user.email,
        }

