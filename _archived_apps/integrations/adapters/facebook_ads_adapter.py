"""
Facebook Lead Ads Ingestion Adapter implementation.
Parses inbound webhooks from Meta Graph API and converts leadgen data into CRM leads.
"""

from typing import Dict, Any
from .base import BaseLeadSourceAdapter

class FacebookLeadAdsAdapter(BaseLeadSourceAdapter):
    def parse_lead_webhook(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extracts lead contact fields from Facebook Lead Gen webhook.
        Handles both raw Meta payload and normalized payloads.
        """
        # If payload already has normalized fields:
        name = payload.get('full_name') or payload.get('name', 'Facebook Lead')
        phone = payload.get('phone_number') or payload.get('phone', '')
        email = payload.get('email', '')
        goal = payload.get('fitness_goal') or payload.get('goal', 'General Fitness')
        campaign = payload.get('campaign_name') or payload.get('ad_name', 'Facebook Lead Ad Campaign')

        # If payload is Meta webhook format with 'field_data':
        if 'field_data' in payload:
            fields = {f['name']: f['values'][0] for f in payload['field_data'] if f.get('values')}
            name = fields.get('full_name', name)
            phone = fields.get('phone_number', phone)
            email = fields.get('email', email)
            goal = fields.get('fitness_goal', goal)

        return {
            'name': name,
            'phone': phone,
            'email': email,
            'source': 'Meta Ads',
            'goal': goal,
            'interested_service': 'Strength Training',
            'notes': f"Captured via Facebook Lead Ad Campaign: {campaign}",
            'raw_payload': payload,
        }

