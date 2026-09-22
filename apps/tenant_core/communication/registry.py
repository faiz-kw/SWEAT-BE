"""
apps/tenant_core/communication/registry.py — Provider Adapter Registry.

Maps (channel, provider) pairs to concrete BaseCommunicationAdapter classes.
Isolates provider-specific mechanics from core business logic.
"""

from typing import Dict, Tuple, Type, Optional, List, Any
import logging
from .adapters import (
    BaseCommunicationAdapter,
    MetaWhatsAppAdapter,
    GupshupWhatsAppAdapter,
    SMTPEmailAdapter,
    SESEmailAdapter,
    TwilioSMSAdapter,
    MSG91SMSAdapter,
    GupshupSMSAdapter,
)

logger = logging.getLogger(__name__)


class CommunicationProviderRegistry:
    """
    Central registry of communication adapters keyed by (channel, provider).
    Thread-safe class dictionary.
    """
    _registry: Dict[Tuple[str, str], Type[BaseCommunicationAdapter]] = {}

    @classmethod
    def register(cls, channel: str, provider: str, adapter_cls: Type[BaseCommunicationAdapter]) -> None:
        key = (channel.strip().upper(), provider.strip().upper())
        cls._registry[key] = adapter_cls

    @classmethod
    def get_adapter_class(cls, channel: str, provider: str) -> Optional[Type[BaseCommunicationAdapter]]:
        key = (channel.strip().upper(), provider.strip().upper())
        return cls._registry.get(key)

    @classmethod
    def get_adapter_instance(
        cls,
        channel: str,
        provider: str,
        configuration: Optional[Dict[str, Any]] = None,
        credentials: Optional[Dict[str, Any]] = None,
    ) -> Optional[BaseCommunicationAdapter]:
        adapter_cls = cls.get_adapter_class(channel, provider)
        if not adapter_cls:
            return None
        return adapter_cls(configuration=configuration, secret_credentials=credentials)

    @classmethod
    def list_supported_adapters(cls) -> List[Dict[str, Any]]:
        results = []
        for (channel, provider), adapter_cls in cls._registry.items():
            results.append({
                'channel': channel,
                'provider': provider,
                'adapter_class': adapter_cls.__name__,
                'capabilities': getattr(adapter_cls, 'capabilities', []),
            })
        return results

    @classmethod
    def resolve_for_tenant(
        cls,
        channel: str,
        provider_preference: Optional[str] = None,
    ) -> Tuple[Optional[BaseCommunicationAdapter], Optional[Any]]:
        """
        Inspects active tenant DB's Integration table.
        Resolves the appropriate configured provider adapter.
        """
        from apps.tenant_core.models_infra import Integration
        from config.secrets import SecretResolver

        channel_upper = channel.strip().upper()
        # Map channel to Integration.INTEGRATION_TYPE
        integration_type_map = {
            'WHATSAPP': 'WHATSAPP',
            'EMAIL': 'EMAIL',
            'SMS': 'OTHER',  # Integration.INTEGRATION_TYPE uses OTHER or custom for SMS
        }
        target_type = integration_type_map.get(channel_upper, channel_upper)

        query = Integration.objects.filter(status='ACTIVE')
        if channel_upper in ('WHATSAPP', 'EMAIL'):
            query = query.filter(integration_type=target_type)
        else:
            # For SMS, allow matching provider in (TWILIO, MSG91, GUPSHUP_SMS) or type
            query = query.filter(provider__in=['TWILIO', 'MSG91', 'GUPSHUP_SMS', 'Twilio', 'MSG91', 'Gupshup'])

        if provider_preference:
            query = query.filter(provider__iexact=provider_preference)

        integration = query.first()
        if not integration:
            # Fallback for SMTP email if default SMTP is available in settings
            if channel_upper == 'EMAIL':
                adapter_cls = cls.get_adapter_class('EMAIL', 'SMTP')
                if adapter_cls:
                    inst = adapter_cls()
                    if inst.is_configured():
                        return inst, None
            return None, None

        provider_name = integration.provider.upper()
        # Normalize provider name if needed
        if channel_upper == 'SMS' and provider_name == 'GUPSHUP':
            provider_name = 'GUPSHUP_SMS'

        adapter_cls = cls.get_adapter_class(channel_upper, provider_name)
        if not adapter_cls:
            logger.warning("No registered adapter found for channel=%s provider=%s", channel_upper, provider_name)
            return None, integration

        # Resolve credentials safely without exposing secrets
        credentials = {}
        if integration.secret_reference:
            try:
                credentials = SecretResolver.resolve(integration.secret_reference) or {}
                if not isinstance(credentials, dict):
                    credentials = {'api_token': credentials}
            except Exception as e:
                logger.error("Failed to resolve secret for integration %s: %s", integration.id, e)
                credentials = {}

        # Also merge non-sensitive configuration
        config = integration.configuration or {}
        adapter_instance = adapter_cls(configuration=config, secret_credentials=credentials)
        return adapter_instance, integration


# Register initial canonical adapters
CommunicationProviderRegistry.register('WHATSAPP', 'META', MetaWhatsAppAdapter)
CommunicationProviderRegistry.register('WHATSAPP', 'GUPSHUP', GupshupWhatsAppAdapter)
CommunicationProviderRegistry.register('EMAIL', 'SMTP', SMTPEmailAdapter)
CommunicationProviderRegistry.register('EMAIL', 'SES', SESEmailAdapter)
CommunicationProviderRegistry.register('SMS', 'TWILIO', TwilioSMSAdapter)
CommunicationProviderRegistry.register('SMS', 'MSG91', MSG91SMSAdapter)
CommunicationProviderRegistry.register('SMS', 'GUPSHUP_SMS', GupshupSMSAdapter)
CommunicationProviderRegistry.register('SMS', 'GUPSHUP', GupshupSMSAdapter)
