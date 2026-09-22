"""
apps/tenant_core/communication package.
"""

from .service import CommunicationService, ConsentViolationError
from .registry import CommunicationProviderRegistry

__all__ = [
    'CommunicationService',
    'ConsentViolationError',
    'CommunicationProviderRegistry',
]
