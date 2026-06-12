import re
from datetime import datetime


def extract_contact_info(text):
    text = text or ""
    info = {
        "name": None,
        "email": None,
        "phone": None,
        "linkedin": None,
        "github": None,
    }

    # email
    m = re.search(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', text)
    if m:
        info["email"] = m.group(0)

    # phone (simple)
    m = re.search(r'(?:\+?\d{1,3}[\s\-\.]?)?(?:\(?\d{2,4}\)?[\s\-\.]?)?\d{3,4}[\s\-\.]?\d{3,4}', text)
    if m:
        info["phone"] = m.group(0)

    # linkedin
    m = re.search(r'linkedin\.com/[A-Za-z0-9_\-./]+', text, re.I)
    if m:
        info["linkedin"] = m.group(0)

    # github
    m = re.search(r'github\.com/[A-Za-z0-9_\-./]+', text, re.I)
    if m:
        info["github"] = m.group(0)
    # name — prefer explicit "Name:" pattern, else top lines heuristic
    m = re.search(r'Name[:\-]\s*([A-Z][A-Za-z ,.-]{1,80})', text)
    if m:
        info['name'] = m.group(1).strip()
    else:
        for line in text.splitlines()[:12]:
            clean = line.strip()
            if not clean:
                continue
            lower = clean.lower()
            if '@' in clean or 'linkedin' in lower or 'github' in lower or 'resume' in lower:
                continue
            parts = clean.split()
            if 1 < len(parts) <= 4 and all(re.search(r'[A-Za-z]', p) for p in parts):
                cap = sum(1 for p in parts if re.match(r'[A-Z]', p[0]))
                if cap >= 1:
                    info['name'] = clean
                    break

    return info


def infer_years_experience(text):
    years = None
    now_dt = datetime.now()
    text = text or ''

    # 1) Parse explicit date ranges (e.g., 'Jun 2018 - Nov 2020', '2018–2020', 'June 2019 to Present')
    total_days = 0
    ranges_found = False

    # helper to parse a single date token
    def parse_date_token(tok):
        tok = tok.strip()
        if not tok:
            return None
        if re.match(r'^(present|now)$', tok, re.I):
            return now_dt
        for fmt in ('%b %Y', '%B %Y', '%Y'):
            try:
                return datetime.strptime(tok, fmt)
            except Exception:
                continue
        return None

    # look for month-year to month-year or year-year ranges
    for m in re.finditer(r'([A-Za-z]{3,9}\s+\d{4}|\d{4})\s*(?:–|—|-|to)\s*(Present|Now|[A-Za-z]{3,9}\s+\d{4}|\d{4})', text, re.I):
        start_tok = m.group(1)
        end_tok = m.group(2)
        start_dt = parse_date_token(start_tok)
        end_dt = parse_date_token(end_tok)
        if start_dt and end_dt:
            ranges_found = True
            # ensure end is after start
            if end_dt < start_dt:
                # swap if mistaken
                start_dt, end_dt = end_dt, start_dt
            total_days += (end_dt - start_dt).days

    if ranges_found and total_days > 0:
        total_years = total_days / 365.0
        years = int(round(total_years))
        return years

    # 2) Fallback: look for explicit 'X years' patterns like '5 years experience'
    m = re.search(r'(\d+)\+?\s+years?', text, re.I)
    if m:
        try:
            years = int(m.group(1))
            return years
        except Exception:
            years = None

    # 3) If there's a dedicated Experience / Work Experience section, parse years inside it
    lower = text.lower()
    exp_pos = lower.find('experience')
    if exp_pos != -1:
        # take a slice after the 'experience' word to focus on employment lines
        section = text[exp_pos:exp_pos + 1500]
        years_found = []
        for m in re.finditer(r'(19|20)\d{2}', section):
            try:
                y = int(m.group(0))
                if 1900 < y <= now_dt.year:
                    years_found.append(y)
            except Exception:
                continue
        if years_found:
            span = max(years_found) - min(years_found)
            if span > 0:
                years = span
                return years

    # 4) Last-resort: approximate by span of all 4-digit years in doc (original behavior)
    years_found = []
    for m in re.finditer(r'(19|20)\d{2}', text):
        try:
            y = int(m.group(0))
            if 1900 < y <= now_dt.year:
                years_found.append(y)
        except Exception:
            continue
    if years_found:
        span = max(years_found) - min(years_found)
        if span > 0:
            years = span

    return years


def infer_experience_level(years):
    if years is None:
        return 'Unknown'
    if years < 3:
        return 'Junior'
    if years < 7:
        return 'Mid'
    return 'Senior'


def detect_red_flags(text, profile):
    flags = []
    # missing contact info
    if not profile.get('email') or not profile.get('phone'):
        flags.append('Missing contact info')

    # employment gap heuristic: look for 'gap' or 'unemployed' or large year gaps
    if re.search(r'gap year|employment gap|unemployed', text, re.I):
        flags.append('Employment gap mentioned')

    # job hopping heuristic: many short contracts (look for multiple year tokens like 2019, 2020)
    years = []
    for match in re.findall(r'(19|20)(\d{2})', text):
        try:
            years.append(int(match[0] + match[1]))
        except Exception:
            continue
    if len(years) >= 4:
        flags.append('Possible job hopping (many short stints)')

    return flags


def extract_profile(text):
    contact = extract_contact_info(text)
    years = infer_years_experience(text)
    level = infer_experience_level(years)
    flags = detect_red_flags(text, contact)

    # education heuristic: look for 'B.Tech|Bachelor|M.S|M.Sc|B.Sc|MBA|PhD'
    edu = None
    m = re.search(r'((?:Bachelor|B\.?Sc|B\.?Tech|B\.?Eng)|(?:Master|M\.?Sc|M\.?S|MBA)|(?:Ph\.?D|Doctor))', text, re.I)
    if m:
        edu = m.group(0)

    # current role heuristic: first occurrence of 'Currently' or top header lines with Title keywords
    role = None
    m = re.search(r'(?:Currently|Current role|Title)[:\-\s]+([A-Za-z0-9 \-,&/]+)', text, re.I)
    if m:
        role = m.group(1).strip()
    else:
        for line in text.splitlines()[:30]:
            if re.search(r'\b(Engineer|Developer|Manager|Analyst|Consultant|Intern|Lead|Director|Principal)\b', line, re.I):
                candidate = line.strip()
                if len(candidate) < 120:
                    role = candidate
                    break

    return {
        'name': contact.get('name'),
        'email': contact.get('email'),
        'phone': contact.get('phone'),
        'linkedin': contact.get('linkedin'),
        'github': contact.get('github'),
        'years_experience': years,
        'experience_level': level,
        'education': edu,
        'current_role': role,
        'red_flags': flags,
    }
