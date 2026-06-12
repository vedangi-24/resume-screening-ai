import os
import re
import json

def _load_openai():
    try:
        import openai
    except Exception as e:
        raise RuntimeError('openai package not installed. Add openai to requirements.txt') from e

    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key:
        raise RuntimeError('OPENAI_API_KEY not set in environment')
    openai.api_key = api_key
    return openai


def semantic_score(resume_text, job_description, model=None):
    """Return a semantic match score 0-100 and brief reason using LLM.
    Falls back to None on error.
    """
    openai = None
    try:
        openai = _load_openai()
    except Exception:
        return None, None

    model = model or os.environ.get('LLM_MODEL', 'gpt-4o-mini')
    prompt = (
        "You are a recruiter assistant. Given a job description and a candidate resume text, "
        "provide a JSON object with two fields: \n  {\n    \"score\": <integer 0-100>,\n    \"reason\": <short one-sentence reason>\n  }\n"
    )

    messages = [
        {"role":"system","content":"Recruiter assistant for semantic fit scoring."},
        {"role":"user","content": prompt + "\nJob Description:\n" + job_description + "\n\nResume:\n" + resume_text}
    ]

    # simple retry logic
    for attempt in range(2):
        try:
            resp = openai.ChatCompletion.create(model=model, messages=messages, temperature=0.0, max_tokens=200)
            text = resp['choices'][0]['message']['content']
            # extract JSON from response
            m = re.search(r'\{.*\}', text, re.S)
            if m:
                try:
                    j = json.loads(m.group(0))
                    score = int(j.get('score', 0))
                    reason = j.get('reason', '')
                    return max(0, min(100, score)), reason
                except Exception:
                    pass

            # fallback: try to pull a number
            m2 = re.search(r'(\d{1,3})', text)
            if m2:
                return max(0, min(100, int(m2.group(1)))), text.strip()
        except Exception:
            time.sleep(0.5)
            continue

    return None, None


def generate_verdict_and_questions(resume_text, job_description, model=None):
    """Return a 3-line verdict string and a list of interview questions.
    """
    openai = None
    try:
        openai = _load_openai()
    except Exception:
        return None, []

    model = model or os.environ.get('LLM_MODEL', 'gpt-4o-mini')
    prompt = (
        "You are a helpful assistant. Given a job description and a resume, produce a compact JSON object: {\n"
        "  \"verdict\": \"3-line verdict why hire / why not\",\n"
        "  \"questions\": [\"q1\", \"q2\"]\n}\nOnly output valid JSON.\n\n"
    )

    messages = [
        {"role":"system","content":"Recruiter assistant that crafts verdicts and tailored interview questions."},
        {"role":"user","content": prompt + "\nJob Description:\n" + job_description + "\n\nResume:\n" + resume_text}
    ]

    for attempt in range(2):
        try:
            resp = openai.ChatCompletion.create(model=model, messages=messages, temperature=0.2, max_tokens=400)
            text = resp['choices'][0]['message']['content']
            m = re.search(r'\{.*\}', text, re.S)
            if m:
                try:
                    j = json.loads(m.group(0))
                    verdict = j.get('verdict')
                    questions = j.get('questions') or []
                    return verdict, questions
                except Exception:
                    pass

            return text.strip(), []
        except Exception:
            time.sleep(0.5)
            continue

    return None, []
