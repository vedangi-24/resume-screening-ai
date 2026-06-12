from pathlib import Path
import re

SKILLS = []

# Load skills file relative to the repository (module location), not CWD
skills_file = Path(__file__).resolve().parent.parent / "data" / "skills.txt"

if skills_file.exists():
    with skills_file.open("r", encoding="utf-8") as f:
        # ignore single-letter entries (like 'c') to avoid false positives
        SKILLS = [line.strip().lower() for line in f if line.strip() and len(line.strip()) > 1]
else:
    SKILLS = []


# A small extra set of common skills not always present in data/skills.txt
EXTRA_SKILLS = [
    'typescript', 'tailwind', 'graphql', 'next.js', 'nextjs', 'node.js', 'nodejs',
    'redux', 'jest', 'webpack', 'sass', 'less', 'styled-components', 'material-ui',
    'rest', 'restful', 'graphql', 'docker', 'kubernetes', 'aws', 'azure', 'gcp', 'firebase'
]

STOPWORDS = set([ 'and','or','with','the','for','a','an','to','in','on','of','by','is','are' ])


def _token_candidates(text):
    # return candidate tokens that look like technologies
    toks = re.findall(r"[A-Za-z0-9\.+#\-]{2,30}", text)
    toks = [t.lower().strip('. ,') for t in toks]
    # filter obvious non-tech tokens
    cand = []
    for t in toks:
        if t in STOPWORDS: continue
        if re.match(r'^[0-9]+$', t): continue
        if len(t) <= 2: continue
        cand.append(t)
    return list(dict.fromkeys(cand))


def extract_skills(text):
    """Extract skills from free text using the curated skill list plus extras
    and a small heuristic token extractor for missing tech tokens.
    """
    if not text:
        return []
    t = text.lower()
    found = set()

    # match words/phrases from canonical list
    for skill in SKILLS + EXTRA_SKILLS:
        # allow skills containing punctuation like 'next.js'
        if ' ' in skill or '.' in skill or '-' in skill or '+' in skill or '#' in skill:
            if skill in t:
                found.add(skill)
        else:
            # whole-word match to avoid false positives
            if re.search(r"\b" + re.escape(skill) + r"\b", t):
                found.add(skill)

    # heuristic fallback: pick frequent candidate tokens from text as possible skills
    candidates = _token_candidates(text)
    for c in candidates[:60]:
        # only add if token contains letters and isn't overly generic
        if len(c) > 2 and not re.search(r'^(experience|work|role|project|skills?)$', c):
            # avoid adding natural language words that are not tech tokens
            if c not in found and any(ch.isalpha() for ch in c):
                # lightweight filter: include tokens that contain at least one known tech substring
                # avoid single-letter substrings like 'c' which cause false positives
                tech_subs = ['js','script','react','node','sql','docker','aws','graphql','tailwind','css','html','java','python','php','ruby','go','kube']
                if any(sub in c for sub in tech_subs):
                    found.add(c)

    return sorted(found)