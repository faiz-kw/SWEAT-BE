import re
import requests

def verify():
    s = requests.Session()
    s.headers.update({'User-Agent': 'Mozilla/5.0'})
    login_url = 'https://api.fitness.vibecopilot.ai/admin/login/?next=/admin/'
    r = s.get(login_url)
    m = re.search(r'name=["\']csrfmiddlewaretoken["\'] value=["\']([^"\']+)["\']', r.text)
    csrf = m.group(1) if m else s.cookies.get('csrftoken')
    
    res = s.post('https://api.fitness.vibecopilot.ai/admin/login/', data={
        'username': 'platform.admin@sweat-demo.example',
        'password': 'Sw!9UsCKWGRUsOW_3oFgb8o7',
        'csrfmiddlewaretoken': csrf,
        'next': '/admin/'
    }, headers={'Referer': login_url})
    
    print("Logged in successfully:", 'logout' in res.text.lower())
    
    # Query occurrences
    r_co = s.get('https://api.fitness.vibecopilot.ai/admin/tenant_core/classoccurrence/')
    paginator = re.findall(r'<p class="paginator">\s*(.*?)\s*</p>', r_co.text, re.DOTALL)
    print("Paginator text:", paginator)
    
    # Let's inspect rows on the first page
    rows = re.findall(r'/admin/tenant_core/classoccurrence/([a-f0-9-]+)/change/', r_co.text)
    print(f"Occurrences found on current page: {len(rows)}")

    # Let's check occurrences for Oct 19 - Oct 22
    for date_str in ['2026-10-19', '2026-10-20', '2026-10-21', '2026-10-22']:
        count_date = r_co.text.count(date_str)
        # Note: dates in changelist display might be 'Oct. 19, 2026' or '2026-10-19'
        count_display = r_co.text.count(f"Oct. {int(date_str.split('-')[2])}, 2026")
        print(f"Date {date_str}: matches in page = {count_date + count_display}")

if __name__ == '__main__':
    verify()
