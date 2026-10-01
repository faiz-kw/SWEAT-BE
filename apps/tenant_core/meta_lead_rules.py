"""Pure mapping rules shared by validation, simulation and a future live adapter."""
import hashlib
import hmac
import json

DESTINATION_FIELDS = {
    'full_name': 'Full name (split into first and last name)',
    'first_name': 'First name', 'last_name': 'Last name',
    'email': 'Email', 'phone': 'Phone', 'fitness_goal': 'Fitness goal',
    'area': 'Area', 'country': 'Country',
    'gender': 'Gender',
    'occupation': 'Occupation',
    'company_name': 'Company name',
    'date_of_birth': 'Date of birth (YYYY-MM-DD)',
    'consent_whatsapp': 'WhatsApp consent',
    'consent_email': 'Email consent',
    'consent_sms': 'SMS consent',
}


def normalize_field_data(field_data):
    if not isinstance(field_data, list) or len(field_data) > 100:
        raise ValueError('Provide at most 100 form answers.')
    answers = {}
    for item in field_data:
        if not isinstance(item, dict) or set(item) != {'name', 'values'}:
            raise ValueError('Each answer must contain name and values only.')
        name, values = item['name'], item['values']
        if not isinstance(name, str) or not name.strip() or len(name) > 100 or name in answers:
            raise ValueError('Question names must be nonempty, unique and at most 100 characters.')
        if not isinstance(values, list) or not values or len(values) > 30 or any(not isinstance(v, str) or len(v) > 2000 for v in values):
            raise ValueError('Each answer needs a nonempty list of bounded text values.')
        answers[name] = values
    return answers


def map_answers(field_data, field_mappings, field_defaults=None):
    answers = normalize_field_data(field_data)
    mapped = {}
    defaults = {
        k: v for k, v in (field_defaults or {}).items()
        if k not in {
            'full_name', 'first_name', 'last_name',
            'email', 'phone',
            'consent_whatsapp', 'consent_email', 'consent_sms',
        }
    }
    for destination, source in field_mappings.items():
        if destination not in DESTINATION_FIELDS:
            raise ValueError('Unsupported CRM destination field.')
        values = answers.get(source, [])
        if len(values) > 1:
            raise ValueError(f'{DESTINATION_FIELDS[destination]} accepts one answer.')
        val = values[0].strip() if values else ''
        if not val and destination in defaults:
            val = str(defaults[destination]).strip()
        mapped[destination] = val

    # Apply defaults for any configured fields not explicitly in field_mappings
    for dest, def_val in defaults.items():
        if dest in DESTINATION_FIELDS and dest not in mapped and str(def_val).strip():
            mapped[dest] = str(def_val).strip()

    full_name = mapped.pop('full_name', '')
    if full_name:
        first, _, last = full_name.partition(' ')
        mapped.setdefault('first_name', first)
        mapped.setdefault('last_name', last.strip())
    if not mapped.get('first_name'):
        raise ValueError('The mapped first name or full name is required.')
    mapped.setdefault('last_name', '')
    if not (mapped.get('email') or mapped.get('phone')):
        raise ValueError('At least one mapped email or phone is required.')
    return mapped, answers


def payload_digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def verify_signature(raw_body, signature, secret):
    """Missing secrets always fail, including tests. Test requests are signed."""
    if not secret or not isinstance(signature, str) or not signature.startswith('sha256='):
        return False
    if len(signature) != 71 or any(c not in '0123456789abcdef' for c in signature[7:]):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature[7:])
