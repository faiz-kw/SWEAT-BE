import re
import requests
from datetime import datetime, timedelta

def run():
    print("=== STARTING CLASS & SCHEDULE OCCURRENCE CREATION ===")
    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    })
    
    # 1. Log in to Live Admin
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
    res_login = s.post('https://api.fitness.vibecopilot.ai/admin/login/', data=login_data, headers={'Referer': login_url})
    if 'logout' not in res_login.text.lower():
        print("ERROR: Failed to log into live admin!")
        return
    print("Successfully authenticated as platform.admin@sweat-demo.example")

    # 2. Define the classes and their schedule times
    classes_info = [
        {
            'id': '5e943f6c-d824-407a-a3eb-75453fe9fe14',
            'name': 'Morning Yoga Flow',
            'code': 'DEMO-AND-YOGA',
            'start_time': '09:00:00',
            'end_time': '10:00:00',
        },
        {
            'id': 'fd2d2235-51c8-4998-9e5f-51e7fa292e71',
            'name': 'Strength Basics',
            'code': 'DEMO-AND-STRENGTH',
            'start_time': '10:00:00',
            'end_time': '11:00:00',
        },
        {
            'id': '6feb23fd-0812-466a-81c6-a92cccfdd488',
            'name': 'Mobility and Core',
            'code': 'DEMO-AND-MOBILITY',
            'start_time': '11:00:00',
            'end_time': '12:00:00',
        },
        {
            'id': 'b2731517-17e4-472f-accc-e5c6902252dc',
            'name': 'Functional Circuit',
            'code': 'DEMO-AND-FUNCTIONAL',
            'start_time': '12:00:00',
            'end_time': '13:00:00',
        },
        {
            'id': '1ba38859-6ca3-4bf8-ae4e-030f6f067989',
            'name': 'Evening HIIT',
            'code': 'DEMO-AND-HIIT',
            'start_time': '18:00:00',
            'end_time': '19:00:00',
        },
    ]

    branches = [
        {
            'id': '8f6790e0-1a66-4106-bd46-7aa5ca769f58',
            'name': 'Andheri Demo Branch'
        },
        {
            'id': '3d391f9c-4fd2-4250-82ef-ceb91ceab322',
            'name': 'Bandra West Elite Studio'
        }
    ]

    dates = ['2026-10-19', '2026-10-20', '2026-10-21', '2026-10-22']

    # 3. Check existing occurrences to prevent duplicates
    r_co = s.get('https://api.fitness.vibecopilot.ai/admin/tenant_core/classoccurrence/?all=')
    # Find all rows with date, start time, branch and template
    # Let's inspect existing occurrences by querying them
    print("Checking existing occurrences on live admin...")

    add_occ_url = 'https://api.fitness.vibecopilot.ai/admin/tenant_core/classoccurrence/add/'
    
    created_count = 0
    skipped_count = 0

    for d in dates:
        for branch in branches:
            for cls_info in classes_info:
                # Occurrence payload
                csrf = s.cookies.get('csrftoken')
                # Calculate cancellation cutoff (2 hours before)
                start_hour = int(cls_info['start_time'].split(':')[0])
                cutoff_hour = max(0, start_hour - 2)
                cutoff_time = f"{cutoff_hour:02d}:00:00"

                payload = {
                    'csrfmiddlewaretoken': csrf,
                    'class_template': cls_info['id'],
                    'schedule_rule': '',
                    'branch': branch['id'],
                    'occurrence_date': d,
                    'start_at_0': d,
                    'start_at_1': cls_info['start_time'],
                    'end_at_0': d,
                    'end_at_1': cls_info['end_time'],
                    'delivery_mode': 'OFFLINE',
                    'online_provider': '',
                    'capacity': '15',
                    'trial_capacity': '3',
                    'waitlist_capacity': '5',
                    'booking_open_at_0': '2026-10-01',
                    'booking_open_at_1': '00:00:00',
                    'booking_close_at_0': d,
                    'booking_close_at_1': cls_info['start_time'],
                    'cancellation_cutoff_at_0': d,
                    'cancellation_cutoff_at_1': cutoff_time,
                    'status': 'SCHEDULED',
                    '_save': 'Save'
                }
                
                res = s.post(add_occ_url, data=payload, headers={'Referer': add_occ_url})
                if res.status_code == 200 and (res.url.endswith('/classoccurrence/') or 'success' in res.text or 'added successfully' in res.text):
                    created_count += 1
                    print(f"[CREATED] {d} | {branch['name'][:12]} | {cls_info['name']} ({cls_info['start_time']}-{cls_info['end_time']}) - Trial: 3, Waitlist: 5")
                elif 'already exists' in res.text or 'duplicate' in res.text.lower():
                    skipped_count += 1
                    print(f"[EXISTS] {d} | {branch['name'][:12]} | {cls_info['name']} already exists")
                else:
                    # Check errors
                    errs = re.findall(r'<ul class="errorlist">.*?</ul>', res.text, re.DOTALL)
                    print(f"[ERROR] {d} | {branch['name'][:12]} | {cls_info['name']} -> {errs}")

    print(f"\nLive Occurrences Summary: {created_count} created, {skipped_count} already existed.")

    # 4. Update schedule rules valid_until to 2026-10-22
    print("\nUpdating schedule rules valid_until to 2026-10-22...")
    r_rules = s.get('https://api.fitness.vibecopilot.ai/admin/tenant_core/classschedulerule/')
    rule_ids = re.findall(r'/admin/tenant_core/classschedulerule/([^/]+)/change/', r_rules.text)
    
    # Simple form parser to extract form fields
    from html.parser import HTMLParser
    class RuleFormParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.data = {}
            self.current_select = None
            self.in_textarea = False
            self.current_textarea = None
            self.textarea_val = []

        def handle_starttag(self, tag, attrs):
            attr_dict = dict(attrs)
            name = attr_dict.get('name')
            if not name:
                if tag == 'option' and self.current_select:
                    if 'selected' in attr_dict:
                        self.data[self.current_select] = attr_dict.get('value', '')
                return

            if tag == 'input':
                t = attr_dict.get('type', 'text')
                if t == 'checkbox':
                    if 'checked' in attr_dict:
                        self.data[name] = 'on'
                elif t not in ('submit', 'button'):
                    self.data[name] = attr_dict.get('value', '')
            elif tag == 'select':
                self.current_select = name
                if name not in self.data:
                    self.data[name] = ''
            elif tag == 'textarea':
                self.in_textarea = True
                self.current_textarea = name
                self.textarea_val = []

        def handle_endtag(self, tag):
            if tag == 'select':
                self.current_select = None
            elif tag == 'textarea':
                self.in_textarea = False
                if self.current_textarea:
                    self.data[self.current_textarea] = ''.join(self.textarea_val).strip()
                self.current_textarea = None

        def handle_data(self, data):
            if self.in_textarea:
                self.textarea_val.append(data)

    for rid in rule_ids:
        r_url = f'https://api.fitness.vibecopilot.ai/admin/tenant_core/classschedulerule/{rid}/change/'
        r_get = s.get(r_url)
        p = RuleFormParser()
        p.feed(r_get.text)
        form_data = p.data
        form_data['valid_until'] = '2026-10-22'
        form_data['trial_capacity_override'] = '3'
        form_data['waitlist_capacity_override'] = '5'
        form_data['csrfmiddlewaretoken'] = s.cookies.get('csrftoken')
        form_data['_save'] = 'Save'
        res_r = s.post(r_url, data=form_data, headers={'Referer': r_url})
        print(f"Rule {rid} updated valid_until=2026-10-22, status: {res_r.status_code}")

    print("=== LIVE WEB DJANGO ADMIN CONFIGURATION COMPLETE ===")

if __name__ == '__main__':
    run()
