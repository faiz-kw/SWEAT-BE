import os
import re
import requests
from html.parser import HTMLParser

class ChangeFormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.data = {}
        self.in_textarea = False
        self.current_textarea = None
        self.textarea_val = []
        self.current_select = None

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
            # default empty if nothing selected
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


def update_templates_with_trial_and_waitlist():
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
    
    r_ct = s.get('https://api.fitness.vibecopilot.ai/admin/tenant_core/classtemplate/')
    tmpl_ids = re.findall(r'/admin/tenant_core/classtemplate/([^/]+)/change/', r_ct.text)
    print("Found templates to update:", tmpl_ids)
    
    for tid in tmpl_ids:
        url = f'https://api.fitness.vibecopilot.ai/admin/tenant_core/classtemplate/{tid}/change/'
        r_get = s.get(url)
        parser = ChangeFormParser()
        parser.feed(r_get.text)
        form_data = parser.data
        
        # Enable trial and waitlist
        form_data['allow_trial'] = 'on'
        form_data['allow_waitlist'] = 'on'
        form_data['allow_booking'] = 'on'
        form_data['default_trial_capacity'] = '3'
        form_data['default_waitlist_capacity'] = '5'
        form_data['_save'] = 'Save'
        
        # CSRF token
        form_data['csrfmiddlewaretoken'] = s.cookies.get('csrftoken')
        
        res = s.post(url, data=form_data, headers={'Referer': url})
        print(f"Updated template {tid} ({form_data.get('name')}): Status {res.status_code}")
        if 'errornote' in res.text:
            print("  ERRORS:", re.findall(r'<ul class="errorlist">.*?</ul>', res.text, re.DOTALL))

if __name__ == '__main__':
    update_templates_with_trial_and_waitlist()
