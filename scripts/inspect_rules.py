import os
import re
import requests
from html.parser import HTMLParser

def inspect_rules():
    s = requests.Session()
    s.headers.update({'User-Agent': 'Mozilla/5.0'})
    login_url = 'https://api.fitness.vibecopilot.ai/admin/login/?next=/admin/'
    r = s.get(login_url)
    csrf = s.cookies.get('csrftoken')
    if not csrf:
        m = re.search(r'name=["\']csrfmiddlewaretoken["\'] value=["\']([^"\']+)["\']', r.text)
        if m: csrf = m.group(1)
            
    login_data = {
        'username': os.environ.get('PLATFORM_ADMIN_USERNAME', 'platform.admin@sweat-demo.example'),
        'password': os.environ.get('PLATFORM_ADMIN_PASSWORD', ''),
        'csrfmiddlewaretoken': csrf,
        'next': '/admin/'
    }
    s.post('https://api.fitness.vibecopilot.ai/admin/login/', data=login_data, headers={'Referer': login_url})
    
    r_rules = s.get('https://api.fitness.vibecopilot.ai/admin/tenant_core/classschedulerule/')
    rule_ids = re.findall(r'/admin/tenant_core/classschedulerule/([^/]+)/change/', r_rules.text)
    print("Found rule IDs:", rule_ids)
    
    for rid in rule_ids:
        r_r = s.get(f'https://api.fitness.vibecopilot.ai/admin/tenant_core/classschedulerule/{rid}/change/')
        # get all input name & values
        inputs = dict(re.findall(r'<input[^>]+name=["\']([^"\']+)["\'][^>]+value=["\']([^"\']*)["\']', r_r.text))
        # get selected options
        selecteds = re.findall(r'<select[^>]+name=["\']([^"\']+)["\'][^>]*>[\s\S]*?<option[^>]+value=["\']([^"\']*)["\'][^>]*selected', r_r.text)
        print(f"\nRule {rid}:")
        print("  Inputs:", {k: v for k, v in inputs.items() if k not in ('csrfmiddlewaretoken',)})
        print("  Selecteds:", selecteds)

if __name__ == '__main__':
    inspect_rules()
