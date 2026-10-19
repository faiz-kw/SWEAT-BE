import re
import requests

def inspect_occurrences():
    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0'
    })
    login_url = 'https://api.fitness.vibecopilot.ai/admin/login/?next=/admin/'
    r = s.get(login_url)
    csrf = s.cookies.get('csrftoken')
    if not csrf:
        m = re.search(r'name=["\']csrfmiddlewaretoken["\'] value=["\']([^"\']+)["\']', r.text)
        if m: csrf = m.group(1)
            
    login_data = {
        'username': 'platform.admin@sweat-demo.example',
        'password': 'Sw!9UsCKWGRUsOW_3oFgb8o7',
        'csrfmiddlewaretoken': csrf,
        'next': '/admin/'
    }
    s.post('https://api.fitness.vibecopilot.ai/admin/login/', data=login_data, headers={'Referer': login_url})
    
    # Check occurrences on live admin
    # Let's get the list page with pagination or all
    r = s.get('https://api.fitness.vibecopilot.ai/admin/tenant_core/classoccurrence/?all=')
    # Or inspect first occurrence change page to see exact fields
    first_occ_id = 'ff8b93bf-c92a-4f42-87dc-9ee9e6548f31'
    r_occ = s.get(f'https://api.fitness.vibecopilot.ai/admin/tenant_core/classoccurrence/{first_occ_id}/change/')
    print("Occurrence change page status:", r_occ.status_code)
    
    # Find all input values in occurrence change page
    inputs = re.findall(r'<input[^>]+name=["\']([^"\']+)["\'][^>]+value=["\']([^"\']*)["\']', r_occ.text)
    print("Occurrence fields:", inputs)
    
    # Check selects
    selects = re.findall(r'<select[^>]+name=["\']([^"\']+)["\']', r_occ.text)
    print("Occurrence selects:", selects)
    
    # Let's see what dates occurrences exist for
    # Let's check occurrence_date from changelist
    dates = re.findall(r'<td class="field-occurrence_date nowrap">([^<]+)</td>', r.text)
    branches = re.findall(r'<td class="field-branch nowrap">([^<]+)</td>', r.text)
    print(f"Total dates found: {len(dates)}")
    print("Dates sample:", set(dates))
    print("Branches sample:", set(branches))

    # Also let's inspect the class templates to see if allow_trial and allow_waitlist are enabled on them!
    for tmpl_id, tmpl_name in [('fd2d2235-51c8-4998-9e5f-51e7fa292e71', 'Strength Basics'), ('5e943f6c-d824-407a-a3eb-75453fe9fe14', 'Morning Yoga Flow')]:
        r_t = s.get(f'https://api.fitness.vibecopilot.ai/admin/tenant_core/classtemplate/{tmpl_id}/change/')
        has_allow_trial = 'name="allow_trial" checked' in r_t.text
        has_allow_waitlist = 'name="allow_waitlist" checked' in r_t.text
        print(f"Template {tmpl_name}: allow_trial={has_allow_trial}, allow_waitlist={has_allow_waitlist}")

if __name__ == '__main__':
    inspect_occurrences()
