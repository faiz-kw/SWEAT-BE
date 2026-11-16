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
    DEFAULT_DID = ""
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
            or ""
        ).strip()

        endpoint_prefix = (
            config_data.get("endpoint_prefix")
            or os.getenv("VOBIZ_ENDPOINT_PREFIX")
            or "crm"
        ).strip().lower()

        try:
            ring_timeout = int(config_data.get("ring_timeout_sec") or os.getenv("VOBIZ_RING_TIMEOUT", cls.DEFAULT_TIMEOUT_SEC))
            ring_timeout = max(5, min(60, ring_timeout))
        except (ValueError, TypeError):
            ring_timeout = cls.DEFAULT_TIMEOUT_SEC

        record_all = config_data.get("record_all_calls", True)
        if isinstance(record_all, str):
            record_all = record_all.lower() in ("true", "1", "yes")

        application_id = (
            config_data.get("application_id")
            or os.getenv("VOBIZ_APPLICATION_ID", "")
        ).strip()

        raw_sip_pool = config_data.get("sip_endpoints") or os.getenv("VOBIZ_SIP_ENDPOINTS") or os.getenv("VOBIZ_DEFAULT_SIP_USER") or ""
        if isinstance(raw_sip_pool, list):
            sip_endpoints = [str(ep).strip() for ep in raw_sip_pool if str(ep).strip()]
        else:
            sip_endpoints = [ep.strip() for ep in str(raw_sip_pool).split(",") if ep.strip()]

        return {
            "provider": "VOBIZ",
            "auth_id": auth_id,
            "auth_token": auth_token,
            "application_id": application_id,
            "sip_endpoints": sip_endpoints,
            "from_number": from_number,
            "registrar_host": registrar_host,
            "webrtc_gateway_url": webrtc_gateway,
            "ring_timeout_sec": ring_timeout,
            "record_all_calls": bool(record_all),
            "endpoint_password": endpoint_password,
            "endpoint_prefix": endpoint_prefix,
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
        for key in ["from_number", "auth_id", "application_id", "sip_endpoints", "registrar_host", "webrtc_gateway_url", "ring_timeout_sec", "record_all_calls", "endpoint_prefix"]:
            if key in payload:
                new_cfg[key] = payload[key]

        # Handle secret auth_token & endpoint_password safely: only update if non-empty and not masked
        if payload.get("auth_token") and "••••" not in str(payload["auth_token"]):
            new_cfg["auth_token"] = str(payload["auth_token"]).strip()
        if payload.get("endpoint_password") and "••••" not in str(payload["endpoint_password"]):
            new_cfg["endpoint_password"] = str(payload["endpoint_password"]).strip()

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
            .exclude(user__status__in=["INACTIVE", "DISABLED", "SUSPENDED", "DEACTIVATED"])
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
        Removes all hardcoded SIP usernames and user caps:
        1. Reuses the user's existing registered AgentPresence SIP endpoint.
        2. Claims any unclaimed endpoint from tenant configuration or live Vobiz Account API.
        3. Auto-provisions a dedicated Vobiz SIP endpoint via REST API for new users (scales to 100+).
        4. Falls back cleanly to ``unassigned_<id>`` (OFFLINE) if Vobiz API is unconfigured/unavailable.
        """
        alias = db_alias or get_tenant_db_alias() or "default"
        import uuid
        from django.db import IntegrityError, transaction
        from apps.tenant_core.models_crm import AgentPresence
        from apps.tenant_core.models_org import Organization

        vobiz_cfg = cls.get_vobiz_config(alias)
        uid_hex = user.id.hex[:12] if hasattr(user.id, "hex") else str(user.id)[:12]
        unassigned_sip = f"unassigned_{uid_hex}"
        display_name = f"{user.first_name} {user.last_name or ''}".strip() or user.email

        def _is_registered_sip(sip_name: Optional[str]) -> bool:
            return bool(sip_name and not sip_name.startswith(("unassigned_", "agent_")))

        def _is_ep_available(ep_name: str, exclude_id=None) -> bool:
            qs = AgentPresence.objects.using(alias).filter(sip_username=ep_name)
            if exclude_id is not None:
                qs = qs.exclude(id=exclude_id)
            return not qs.exists()

        def _resolve_or_provision_endpoint(exclude_id=None) -> Optional[str]:
            # 1. Check any explicitly configured pool in Integration/env (zero hardcoded usernames)
            for ep in vobiz_cfg.get("sip_endpoints") or []:
                if _is_registered_sip(ep) and _is_ep_available(ep, exclude_id=exclude_id):
                    return ep

            # 2. Dynamically discover & auto-provision via Vobiz REST API
            auth_id = vobiz_cfg.get("auth_id", "")
            auth_token = vobiz_cfg.get("auth_token", "")
            if not auth_id or not auth_token:
                return None

            from apps.tenant_core.communication.adapters.voice_vobiz import VobizVoiceAdapter
            adapter = VobizVoiceAdapter(configuration=vobiz_cfg)
            app_id = adapter.resolve_default_application_id()

            # 2a. Reuse any unclaimed live endpoint on the Vobiz account (excluding Playground)
            for ep_obj in adapter.list_endpoints():
                ep_user = str(ep_obj.get("username") or "").strip()
                if not ep_user or not _is_registered_sip(ep_user):
                    continue
                ep_alias = str(ep_obj.get("alias") or "").lower()
                ep_app = str(ep_obj.get("application") or ep_obj.get("app_id") or "").strip()
                if "playground" in ep_alias or ep_user.startswith("play"):
                    continue
                if app_id and ep_app and ep_app != app_id:
                    continue
                if _is_ep_available(ep_user, exclude_id=exclude_id):
                    return ep_user

            # 2b. All existing endpoints are claimed -> auto-create a dedicated endpoint for this user
            ep_password = vobiz_cfg.get("endpoint_password")
            if not ep_password:
                import secrets
                ep_password = f"Vz!{secrets.token_urlsafe(12)}"
                vobiz_cfg["endpoint_password"] = ep_password
                try:
                    cls.update_vobiz_config({"endpoint_password": ep_password}, db_alias=alias)
                except Exception as exc:
                    logger.warning("Could not persist auto-generated endpoint_password to Integration: %s", exc)
            ep_prefix = vobiz_cfg.get("endpoint_prefix") or "crm"
            for candidate in (f"{ep_prefix}{uid_hex}", f"{ep_prefix}{uid_hex[:8]}{uuid.uuid4().hex[:4]}"):
                created = adapter.create_endpoint(
                    username=candidate,
                    password=ep_password,
                    alias=f"{display_name} ({user.email})",
                    application_id=app_id,
                )
                if created.get("ok") and created.get("username"):
                    logger.info(
                        "Auto-provisioned Vobiz SIP endpoint %s for user %s (app_id=%s)",
                        created["username"],
                        user.email,
                        app_id or "default",
                    )
                    return created["username"]

            return None

        # Look up or initialize AgentPresence for this user safely under concurrency
        presence = AgentPresence.objects.using(alias).filter(user=user).first()
        if not presence:
            user_org = getattr(user, 'organization', None) or Organization.objects.using(alias).first()
            assigned_ep = _resolve_or_provision_endpoint()
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
            # Auto-heal placeholder/unassigned rows dynamically via Vobiz pool or auto-provisioning
            assigned_ep = _resolve_or_provision_endpoint(exclude_id=presence.id)
            if assigned_ep:
                try:
                    with transaction.atomic(using=alias):
                        presence.sip_username = assigned_ep
                        presence.status = "AVAILABLE"
                        presence.save(using=alias, update_fields=["sip_username", "status"])
                        logger.info("Provisioned real SIP endpoint for user %s -> %s", user.email, assigned_ep)
                except IntegrityError:
                    presence = AgentPresence.objects.using(alias).filter(user=user).first() or presence
            elif not presence.sip_username or presence.sip_username.startswith("agent_"):
                try:
                    with transaction.atomic(using=alias):
                        presence.sip_username = unassigned_sip
                        presence.status = "OFFLINE"
                        presence.save(using=alias, update_fields=["sip_username", "status"])
                        logger.info("No Vobiz line available; normalized agent %s -> %s (OFFLINE)", user.email, unassigned_sip)
                except IntegrityError:
                    presence = AgentPresence.objects.using(alias).filter(user=user).first() or presence

        sip_user = presence.sip_username
        is_registered_endpoint = _is_registered_sip(sip_user)
        sip_domain = vobiz_cfg["registrar_host"]
        sip_uri = f"sip:{sip_user}@{sip_domain}" if is_registered_endpoint else ""

        return {
            "username": sip_user if is_registered_endpoint else "",
            "sip_username": sip_user,
            "password": vobiz_cfg.get("endpoint_password", "") if is_registered_endpoint else "",
            "sip_password": vobiz_cfg.get("endpoint_password", "") if is_registered_endpoint else "",
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
            "display_name": display_name,
        }

