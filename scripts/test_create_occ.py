import os
import re
import requests

def test_create_occurrence():
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
    
    add_url = 'https://api.fitness.vibecopilot.ai/admin/tenant_core/classoccurrence/add/'
    r_form = s.get(add_url)
    csrf = s.cookies.get('csrftoken')
    
    payload = {
        'csrfmiddlewaretoken': csrf,
        'class_template': '5e943f6c-d824-407a-a3eb-75453fe9fe14', # Morning Yoga Flow
        'schedule_rule': '',
        'branch': '8f6790e0-1a66-4106-bd46-7aa5ca769f58', # Andheri Demo Branch
        'occurrence_date': '2026-10-19',
        'start_at_0': '2026-10-19',
        'start_at_1': '09:00:00',
        'end_at_0': '2026-10-19',
        'end_at_1': '10:00:00',
        'delivery_mode': 'OFFLINE',
        'online_provider': '',
        'capacity': '15',
        'trial_capacity': '3',
        'waitlist_capacity': '5',
        'booking_open_at_0': '2026-10-15',
        'booking_open_at_1': '00:00:00',
        'booking_close_at_0': '2026-10-19',
        'booking_close_at_1': '08:30:00',
        'cancellation_cutoff_at_0': '2026-10-19',
        'cancellation_cutoff_at_1': '07:00:00',
        'status': 'SCHEDULED',
        '_save': 'Save'
    }
    
    res = s.post(add_url, data=payload, headers={'Referer': add_url})
    print("Post occurrence status:", res.status_code)
    print("Final URL:", res.url)
    if 'errornote' in res.text:
        print("ERRORS:", re.findall(r'<ul class="errorlist">.*?</ul>', res.text, re.DOTALL))
    elif 'success' in res.text or 'added successfully' in res.text or res.url.endswith('/classoccurrence/'):
        print("SUCCESSFULLY CREATED OCCURRENCE!")

if __name__ == '__main__':
    test_create_occurrence()
