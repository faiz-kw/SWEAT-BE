import os
import re
import requests

def inspect_template_form():
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
    
    t_id = '5e943f6c-d824-407a-a3eb-75453fe9fe14' # Morning Yoga Flow
    r_t = s.get(f'https://api.fitness.vibecopilot.ai/admin/tenant_core/classtemplate/{t_id}/change/')
    print("Template form status:", r_t.status_code)
    
    # Extract all inputs
    inputs = re.findall(r'<input[^>]+name=["\']([^"\']+)["\'][^>]*', r_t.text)
    print("All input tags:", inputs)

if __name__ == '__main__':
    inspect_template_form()
