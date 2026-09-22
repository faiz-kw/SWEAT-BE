"""
apps/tenant_core/communication/adapters package.
"""

from .base import (
    BaseCommunicationAdapter,
    ProviderSendResult,
    ProviderStatusEvent,
    ProviderInboundMessage,
)
from .whatsapp_meta import MetaWhatsAppAdapter
from .whatsapp_gupshup import GupshupWhatsAppAdapter
from .email_smtp import SMTPEmailAdapter
from .email_ses import SESEmailAdapter
from .sms_twilio import TwilioSMSAdapter
from .sms_msg91 import MSG91SMSAdapter
from .sms_gupshup import GupshupSMSAdapter

__all__ = [
    'BaseCommunicationAdapter',
    'ProviderSendResult',
    'ProviderStatusEvent',
    'ProviderInboundMessage',
    'MetaWhatsAppAdapter',
    'GupshupWhatsAppAdapter',
    'SMTPEmailAdapter',
    'SESEmailAdapter',
    'TwilioSMSAdapter',
    'MSG91SMSAdapter',
    'GupshupSMSAdapter',
]
