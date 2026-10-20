import os
import re
import requests
from html.parser import HTMLParser

class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.table_rows = []
        self.in_tbody = False
        self.current_row = []
        self.current_cell = []
        self.in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag == 'tbody':
            self.in_tbody = True
        elif tag == 'tr' and self.in_tbody:
            self.current_row = []
        elif tag in ('td', 'th') and self.in_tbody:
            self.in_cell = True
            self.current_cell = []

    def handle_endtag(self, tag):
        if tag == 'tbody':
            self.in_tbody = False
        elif tag in ('td', 'th') and self.in_tbody:
            self.in_cell = False
            self.current_row.append(''.join(self.current_cell).strip())
        elif tag == 'tr' and self.in_tbody:
            if self.current_row:
                self.table_rows.append(self.current_row)

    def handle_data(self, data):
        if self.in_cell:
            self.current_cell.append(data)

def check_details():
    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    })
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
    
    # Check Branches detail with IDs
    r_b = s.get('https://api.fitness.vibecopilot.ai/admin/tenant_core/branch/')
    branch_links = re.findall(r'href=["\']/admin/tenant_core/branch/([^/]+)/change/["\']>([^<]+)<', r_b.text)
    print("Branch links:", branch_links)

    # Check Class Templates detail with IDs
    r_ct = s.get('https://api.fitness.vibecopilot.ai/admin/tenant_core/classtemplate/')
    tmpl_links = re.findall(r'href=["\']/admin/tenant_core/classtemplate/([^/]+)/change/["\']>([^<]+)<', r_ct.text)
    print("ClassTemplate links:", tmpl_links)

    # Check Class Schedule Rules
    r_sr = s.get('https://api.fitness.vibecopilot.ai/admin/tenant_core/classschedulerule/')
    sr_links = re.findall(r'href=["\']/admin/tenant_core/classschedulerule/([^/]+)/change/["\']>([^<]+)<', r_sr.text)
    print("ClassScheduleRule links:", sr_links)

    # Check Class Occurrences currently existing
    r_co = s.get('https://api.fitness.vibecopilot.ai/admin/tenant_core/classoccurrence/')
    co_links = re.findall(r'href=["\']/admin/tenant_core/classoccurrence/([^/]+)/change/["\']>([^<]+)<', r_co.text)
    print("Existing ClassOccurrence count:", len(co_links), "Sample:", co_links[:5])

    # Check User Branches
    r_ub = s.get('https://api.fitness.vibecopilot.ai/admin/tenant_core/userbranch/')
    p_ub = TableParser()
    p_ub.feed(r_ub.text)
    print("\n--- User Branches ---")
    for row in p_ub.table_rows:
        print(" ", row)

if __name__ == '__main__':
    check_details()
