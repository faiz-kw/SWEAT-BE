import re
import requests
from html.parser import HTMLParser

class FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs = []
        self.selects = {}
        self.current_select = None
        self.table_rows = []
        self.in_tbody = False
        self.current_row = []
        self.current_cell = []
        self.in_cell = False

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        if tag == 'input':
            self.inputs.append((attr_dict.get('name'), attr_dict.get('type'), attr_dict.get('value', '')))
        elif tag == 'select':
            self.current_select = attr_dict.get('name')
            self.selects[self.current_select] = []
        elif tag == 'option':
            if self.current_select:
                self.selects[self.current_select].append((attr_dict.get('value'), attr_dict.get('selected') is not None))
        elif tag == 'tbody':
            self.in_tbody = True
        elif tag == 'tr' and self.in_tbody:
            self.current_row = []
        elif tag in ('td', 'th') and self.in_tbody:
            self.in_cell = True
            self.current_cell = []

    def handle_endtag(self, tag):
        if tag == 'select':
            self.current_select = None
        elif tag == 'tbody':
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


def inspect_environment():
    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    })
    login_url = 'https://api.fitness.vibecopilot.ai/admin/login/?next=/admin/'
    r = s.get(login_url)
    csrf = s.cookies.get('csrftoken')
    if not csrf:
        m = re.search(r'name=["\']csrfmiddlewaretoken["\'] value=["\']([^"\']+)["\']', r.text)
        if m:
            csrf = m.group(1)
            
    login_data = {
        'username': 'platform.admin@sweat-demo.example',
        'password': 'Sw!9UsCKWGRUsOW_3oFgb8o7',
        'csrfmiddlewaretoken': csrf,
        'next': '/admin/'
    }
    res = s.post('https://api.fitness.vibecopilot.ai/admin/login/', data=login_data, headers={'Referer': login_url})
    print("Logged in successfully. Status:", res.status_code)
    
    # 1. Inspect Branches
    r_branches = s.get('https://api.fitness.vibecopilot.ai/admin/tenant_core/branch/')
    p_b = FormParser()
    p_b.feed(r_branches.text)
    print(f"\n--- Branches ({len(p_b.table_rows)}) ---")
    for r in p_b.table_rows:
        print(" ", r)

    # 2. Inspect Organizations
    r_orgs = s.get('https://api.fitness.vibecopilot.ai/admin/tenant_core/organization/')
    p_o = FormParser()
    p_o.feed(r_orgs.text)
    print(f"\n--- Organizations ({len(p_o.table_rows)}) ---")
    for r in p_o.table_rows:
        print(" ", r)

    # 3. Inspect Class Templates
    r_classes = s.get('https://api.fitness.vibecopilot.ai/admin/tenant_core/classtemplate/')
    p_c = FormParser()
    p_c.feed(r_classes.text)
    print(f"\n--- Class Templates ({len(p_c.table_rows)}) ---")
    for r in p_c.table_rows:
        print(" ", r)

    # 4. Check add form for classtemplate
    r_add_tmpl = s.get('https://api.fitness.vibecopilot.ai/admin/tenant_core/classtemplate/add/')
    p_t = FormParser()
    p_t.feed(r_add_tmpl.text)
    print("\n--- ClassTemplate Add Form Inputs ---")
    for inp in p_t.inputs:
        print("  Input:", inp)
    print("\n--- ClassTemplate Selects ---")
    for k, v in p_t.selects.items():
        print(f"  Select {k}: {len(v)} options")
        if len(v) <= 10:
            print("   ", v)

    # 5. Check add form for classoccurrence
    r_add_occ = s.get('https://api.fitness.vibecopilot.ai/admin/tenant_core/classoccurrence/add/')
    p_occ = FormParser()
    p_occ.feed(r_add_occ.text)
    print("\n--- ClassOccurrence Add Form Inputs ---")
    for inp in p_occ.inputs:
        print("  Input:", inp)
    print("\n--- ClassOccurrence Selects ---")
    for k, v in p_occ.selects.items():
        print(f"  Select {k}: {len(v)} options")
        if len(v) <= 10:
            print("   ", v)

if __name__ == '__main__':
    inspect_environment()
