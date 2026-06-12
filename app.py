from flask import Flask, render_template, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge
from utils.pdf_parser import extract_text_from_pdf
from utils.skills import extract_skills
from utils import parser as resume_parser
from utils import llm as llm_client
from werkzeug.utils import secure_filename
import json
from pathlib import Path
import time
import os
import hashlib
import re
from flask import jsonify


app = Flask(__name__)

# Secret key for session signing (deployment value)
app.secret_key = "resume_screening_ai_2026"

UPLOAD_FOLDER = "uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

# Ensure upload folder exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

SHORTLIST_SCORE = 70
REVIEW_SCORE = 40
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
CANDIDATES_FILE = DATA_DIR / "candidates.json"


def load_candidate_store():
    if CANDIDATES_FILE.exists():
        try:
            with open(CANDIDATES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_candidate_store(store):
    with open(CANDIDATES_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)


# initialize candidate store
app.candidate_store = load_candidate_store()

# Deduplicate store by filename on startup (keep the last occurrence)
def _dedupe_store_by_filename(store):
    # Prefer deduplication by text_hash when available, otherwise fallback to filename/name/id
    seen = {}
    for c in store:
        key = c.get('text_hash') or c.get('filename') or c.get('name') or c.get('id')
        seen[key] = c
    return list(seen.values())

app.candidate_store = _dedupe_store_by_filename(app.candidate_store)
save_candidate_store(app.candidate_store)


def _normalize_text_for_hash(text):
    if not text:
        return ''
    # collapse whitespace, remove excessive punctuation, lowercase
    s = re.sub(r"\s+", ' ', text).strip().lower()
    return s


def _ensure_text_hashes(store):
    changed = False
    for c in store:
        if not c.get('text_hash'):
            s = _normalize_text_for_hash(c.get('text', ''))
            c['text_hash'] = hashlib.sha1(s.encode('utf-8')).hexdigest()
            changed = True
    return changed

# ensure old records have text_hash for reliable deduplication
if _ensure_text_hashes(app.candidate_store):
    save_candidate_store(app.candidate_store)


@app.errorhandler(RequestEntityTooLarge)
def handle_large_file(error):
    return render_template(
        "index.html",
        error="File size exceeds 5 MB limit.",
        candidate_store=app.candidate_store
    )


def calculate_skill_score(resume_skills, job_skills):
    if len(job_skills) == 0:
        return 0

    matched = len(set(resume_skills).intersection(set(job_skills)))

    score = round((matched / len(job_skills)) * 100, 2)

    return score


@app.route("/", methods=["GET", "POST"])
def home():
    # Clear any previous in-memory results on simple GET so refresh doesn't re-show old analysis
    if request.method == 'GET':
        if hasattr(app, 'candidate_results'):
            app.candidate_results = None
        if hasattr(app, 'job_description'):
            app.job_description = ''

    extracted_text = ""
    match_score = None
    recommendation = ""

    job_description = ""

    resume_skills = []
    job_skills = []
    missing_skills = []
    matched_skills = []

    candidate_strengths = []
    resume_summary = ""
    insights = []
    ai_suggestions = []
    candidate_results = []

    if request.method == "POST":
        app.logger.info('Received POST / analyze request')
        job_description = request.form.get("job_description", "")

        job_skills = extract_skills(job_description)
        # detect must-have skills from JD (patterns like 'Must have: X, Y' or 'Required: X, Y')
        def extract_must_haves(text):
            if not text:
                return []
            must = []
            patterns = [r"Must(?: have)?:\s*(.+)", r"Required:?\s*(.+)", r"Must-Have:?\s*(.+)"]
            for p in patterns:
                m = re.search(p, text, re.I)
                if m:
                    part = m.group(1)
                    # split by commas or semicolons
                    parts = re.split(r'[;,]| or | and ', part)
                    for s in parts:
                        s = s.strip()
                        if s:
                            must.append(s.lower())
            # normalize with skill extractor if possible
            norm = []
            for item in must:
                ks = extract_skills(item)
                if ks:
                    norm.extend(ks)
                else:
                    norm.append(item)
            # unique
            return list(dict.fromkeys([k.lower() for k in norm]))

        must_haves = extract_must_haves(job_description)
        # Read weight sliders from form (defaults 50/30/20) and normalize
        try:
            w_skill = float(request.form.get('w_skill', 50)) / 100.0
            w_exp = float(request.form.get('w_exp', 30)) / 100.0
            w_edu = float(request.form.get('w_edu', 20)) / 100.0
        except Exception:
            w_skill, w_exp, w_edu = 0.5, 0.3, 0.2
        total_w = w_skill + w_exp + w_edu
        if total_w == 0:
            w_skill, w_exp, w_edu = 0.5, 0.3, 0.2
        else:
            w_skill, w_exp, w_edu = w_skill/total_w, w_exp/total_w, w_edu/total_w

        resumes = request.files.getlist("resume")
        app.logger.info(f'Number of resume files received: {len(resumes)}')

        # Limit number of uploaded resumes to avoid excessive processing
        if len(resumes) > 10:
            return render_template(
                "index.html",
                error="Maximum 10 resumes allowed.",
                candidate_store=app.candidate_store,
            )

        # If no files are uploaded but we have existing stored candidates, re-rank them
        if (not resumes or all(r.filename == '' for r in resumes)) and getattr(app, 'candidate_store', []):
            candidate_results = []
            for c in app.candidate_store:
                skills = c.get('skills', [])
                matched = list(set(job_skills).intersection(set(skills)))
                missing = list(set(job_skills) - set(skills))
                # compute weighted score using stored profile
                skill_score = calculate_skill_score(skills, job_skills)
                profile = c.get('profile', {}) or {}
                years = profile.get('years_experience')
                exp_score = 0
                if years is not None:
                    try:
                        years_val = float(years)
                        exp_score = min(100, max(0, (years_val / 10.0) * 100))
                    except Exception:
                        exp_score = 0

                edu = (profile.get('education') or '')
                edu_score = 0
                if edu:
                    e = edu.lower()
                    if re.search(r'ph\.?d|doctor', e):
                        edu_score = 100
                    elif re.search(r'master|m\.?s|m\.?sc|mba', e):
                        edu_score = 80
                    elif re.search(r'bachelor|b\.?sc|b\.?tech|b\.?eng', e):
                        edu_score = 60

                combined_skill = skill_score
                # note: we don't run LLM during re-rank to avoid extra API calls
                score = round((combined_skill * w_skill) + (exp_score * w_exp) + (edu_score * w_edu), 2)

                # must-have enforcement: if candidate misses any must-have, auto-reject
                missing_lower = [m.lower() for m in missing]
                if must_haves and any(mh.lower() in missing_lower or mh.lower() not in [s.lower() for s in skills] for mh in must_haves):
                    status = 'REJECT'
                else:
                    if score >= SHORTLIST_SCORE:
                        status = "SHORTLIST"
                    elif score >= REVIEW_SCORE:
                        status = "REVIEW"
                    else:
                        status = "REJECT"

                candidate_results.append({
                    'id': c.get('id'),
                    'name': c.get('name') or c.get('filename'),
                    'score': score,
                    'status': status,
                    'skills': skills,
                    'matched': matched,
                    'missing': missing,
                    'must_haves': must_haves,
                    'text': c.get('text', ''),
                })

            candidate_results.sort(key=lambda x: x['score'], reverse=True)
            app.candidate_results = candidate_results
            app.job_description = job_description
            return render_template('index.html', candidate_results=candidate_results, job_description=job_description, candidate_store=app.candidate_store, must_haves=must_haves)

        # If files were uploaded, process them and add to candidate store
        if len(resumes) == 0:
            return render_template(
                "index.html",
                error="Please upload at least one resume or use existing candidates.",
                candidate_store=app.candidate_store,
            )

        for resume in resumes:
            if resume.filename == "":
                return render_template("index.html", error="Please upload a resume.", candidate_store=app.candidate_store)

            if not resume.filename.lower().endswith(".pdf"):
                return render_template("index.html", error="Only PDF files are allowed.", candidate_store=app.candidate_store)

        for resume in resumes:
            # sanitize filename and save to uploads folder
            filename = secure_filename(resume.filename)
            filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)

            resume.save(filepath)

            text = extract_text_from_pdf(filepath)
            norm_text = _normalize_text_for_hash(text)

            # parse profile
            profile = resume_parser.extract_profile(text)

            skills = extract_skills(text)

            # If LLM key available, get semantic score and AI verdict/questions
            use_ai = bool(request.form.get('use_ai'))
            llm_key = os.environ.get('OPENAI_API_KEY')
            ai_verdict = None
            ai_questions = []
            semantic = None
            semantic_reason = None
            if use_ai and llm_key and job_description.strip():
                try:
                    semantic, semantic_reason = llm_client.semantic_score(text, job_description)
                    ai_verdict, ai_questions = llm_client.generate_verdict_and_questions(text, job_description)
                except Exception as e:
                    app.logger.exception('LLM call failed')
                    semantic = None
                else:
                    app.logger.info(f'LLM called: semantic={semantic} ai_verdict_present={bool(ai_verdict)}')


            matched = list(set(job_skills).intersection(set(skills)))

            missing = list(set(job_skills) - set(skills))

            # base skill score (keyword overlap)
            skill_score = calculate_skill_score(skills, job_skills)

            # experience score: map years to 0-100
            years = profile.get('years_experience')
            exp_score = 0
            if years is not None:
                try:
                    years_val = float(years)
                    exp_score = min(100, max(0, (years_val / 10.0) * 100))
                except Exception:
                    exp_score = 0

            # education score heuristic
            edu = profile.get('education') or ''
            edu_score = 0
            if edu:
                e = edu.lower()
                if re.search(r'ph\.?d|doctor', e):
                    edu_score = 100
                elif re.search(r'master|m\.?s|m\.?sc|mba', e):
                    edu_score = 80
                elif re.search(r'bachelor|b\.?sc|b\.?tech|b\.eng', e):
                    edu_score = 60

            # combine semantic (LLM) with skill score if available
            combined_skill = skill_score
            if semantic is not None:
                try:
                    weight = float(os.environ.get('LLM_WEIGHT', '0.7'))
                except Exception:
                    weight = 0.7
                combined_skill = round((semantic * weight) + (skill_score * (1 - weight)), 2)

            # final combined score using weights from the sliders
            score = round((combined_skill * w_skill) + (exp_score * w_exp) + (edu_score * w_edu), 2)

            # must-have enforcement
            missing_lower = [m.lower() for m in missing]
            if must_haves and any(mh.lower() in missing_lower or mh.lower() not in [s.lower() for s in skills] for mh in must_haves):
                status = 'REJECT'
            else:
                if score >= SHORTLIST_SCORE:
                    status = "SHORTLIST"
                elif score >= REVIEW_SCORE:
                    status = "REVIEW"
                else:
                    status = "REJECT"


            # compute a stable short hash for the normalized resume text to detect duplicates
            text_hash = hashlib.sha1((norm_text or '').encode('utf-8')).hexdigest()

            cid = f"{int(time.time())}-{text_hash[:8]}-{filename}"

            candidate = {
                'id': cid,
                'filename': filename,
                'name': profile.get('name') or filename,
                'text': text,
                'text_hash': text_hash,
                'size': os.path.getsize(filepath) if os.path.exists(filepath) else None,
                'uploaded_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                'skills': skills,
                'matched': matched,
                'missing': missing,
                'score': score,
                'status': status,
                'profile': profile,
                'ai_verdict': ai_verdict,
                'ai_questions': ai_questions,
                'semantic_reason': semantic_reason,
                'must_haves': must_haves,
            }

            # If a candidate with same filename or identical text hash exists, update it instead of appending
            replaced = False
            for i, existing in enumerate(app.candidate_store):
                if existing.get('filename') == filename or existing.get('text_hash') == text_hash:
                    candidate['id'] = existing.get('id') or candidate['id']
                    app.candidate_store[i] = candidate
                    replaced = True
                    break

            if not replaced:
                app.candidate_store.append(candidate)

            save_candidate_store(app.candidate_store)

            candidate_results.append(candidate)

        candidate_results.sort(key=lambda x: x["score"], reverse=True)

        if len(candidate_results) > 1:
            # expose top candidate fields so download-report works in multi-resume mode
            best_candidate = candidate_results[0]
            app.match_score = best_candidate.get("score")
            app.recommendation = best_candidate.get("status")
            app.matched_skills = best_candidate.get("matched", [])
            app.missing_skills = best_candidate.get("missing", [])
            app.candidate_results = candidate_results
            app.job_description = job_description
            return render_template(
                "index.html",
                candidate_results=candidate_results,
                job_description=job_description,
                candidate_store=app.candidate_store,
                must_haves=must_haves,
            )

        if len(candidate_results) == 1:
            top = candidate_results[0]

            match_score = top["score"]
            recommendation = top["status"]
            resume_skills = top["skills"]
            matched_skills = top["matched"]
            missing_skills = top["missing"]
            extracted_text = top["text"]
 
            # Derive candidate strengths from the job description when possible.
            # If `job_skills` is non-empty, prefer matched job skills (cleaned/title-cased).
            # Otherwise fall back to a small priority list.
            if job_skills:
                # Use matched skills that are also in the job description, preserve order
                candidate_strengths = [s.title() for s in matched_skills if s in job_skills][:5]
            else:
                priority_skills = [
                    "python",
                    "machine learning",
                    "tensorflow",
                    "pytorch",
                    "opencv",
                    "flask",
                    "react",
                    "mongodb",
                    "mysql",
                    "git",
                ]

                candidate_strengths = []

                for skill in priority_skills:
                    if skill in matched_skills and skill not in candidate_strengths:
                        candidate_strengths.append(skill)

                candidate_strengths = candidate_strengths[:5]

            # Create a concise summary of the resume (first ~80 words)
            resume_summary = " ".join(extracted_text.split()[:80]) + ("..." if len(extracted_text.split()) > 80 else "")
            ai_suggestions = []

            if len(missing_skills) == 0:
                ai_suggestions.append("Your resume matches the job description very well.")
            else:
                for skill in missing_skills:
                    ai_suggestions.append(
                        f"Your resume would match this role better if you include experience, projects, or certifications related to {skill.title()}."
                    )

            if "project" not in extracted_text.lower():
                ai_suggestions.append("Add more project details to strengthen your profile.")

            if "github" not in extracted_text.lower():
                ai_suggestions.append("Include GitHub profile links to showcase your work.")

            if "internship" not in extracted_text.lower():
                ai_suggestions.append("Adding internship experience can improve recruiter interest.")

            app.match_score = match_score
            app.recommendation = recommendation
            app.matched_skills = matched_skills
            app.missing_skills = missing_skills
            app.ai_suggestions = ai_suggestions

# Single resume mode
            app.candidate_results = None

            app.job_description = job_description

    return render_template(
        "index.html",
        extracted_text=extracted_text,
        match_score=match_score,
        recommendation=recommendation,
        job_description=job_description,
        resume_skills=resume_skills,
        job_skills=job_skills,
        missing_skills=missing_skills,
        matched_skills=matched_skills,
        candidate_strengths=candidate_strengths,
        resume_summary=resume_summary,
        insights=insights,
        ai_suggestions=ai_suggestions,
        candidate_results=candidate_results,
        candidate_store=app.candidate_store,
    )


@app.route("/download-report")
def download_report():
    # Allow report generation for either a single analyzed resume or multiple candidate results
    if not hasattr(app, "match_score") and not hasattr(app, "candidate_results"):
        return "Please analyze a resume first."

    with open("resume_report.txt", "w", encoding="utf-8") as f:
        # If multiple candidates were analyzed, output a ranking report
        if getattr(app, "candidate_results", None):
            f.write("AI Resume Screening Report - Candidate Ranking\n")
            f.write("=" * 60 + "\n")
            f.write(f'{"Rank":<6}{"Candidate":<40}{"Score":>8}\n')
            f.write("-" * 60 + "\n")
            for i, c in enumerate(app.candidate_results, start=1):
                name = c.get("name", "N/A")
                score = f"{c.get('score', 0)}%"
                f.write(f"{i:<6}{name:<40}{score:>8}\n")
            f.write("\n")
        else:
            f.write("AI Resume Screening Report\n")
            f.write("=" * 40 + "\n\n")

            f.write(f"ATS Score: {app.match_score}%\n")

            f.write(f"Recommendation: {app.recommendation}\n\n")

            f.write("Matched Skills:\n")

            for skill in app.matched_skills:
                f.write(f"- {skill}\n")

            f.write("\nMissing Skills:\n")

            for skill in app.missing_skills:
                f.write(f"- {skill}\n")

            f.write("\nAI Suggestions:\n")

            for item in getattr(app, "ai_suggestions", []):
                f.write(f"- {item}\n")

    return send_file("resume_report.txt", as_attachment=True)


@app.route('/export-csv')
def export_csv():
    import csv

    rows = []
    if getattr(app, 'candidate_results', None):
        rows = app.candidate_results
    else:
        rows = app.candidate_store

    out_file = 'candidates_export.csv'
    with open(out_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['id', 'name', 'filename', 'score', 'status', 'ai_verdict', 'email', 'phone', 'years_experience'])
        for c in rows:
            profile = c.get('profile', {}) if isinstance(c, dict) else {}
            writer.writerow([
                c.get('id'),
                c.get('name'),
                c.get('filename', ''),
                c.get('score', ''),
                c.get('status', ''),
                c.get('ai_verdict', ''),
                profile.get('email', ''),
                profile.get('phone', ''),
                profile.get('years_experience', ''),
            ])

    return send_file(out_file, as_attachment=True)


@app.route('/insights')
def insights():
    # compute simple insights only from the most recent in-memory analysis (do not use persistent store)
    rows = getattr(app, 'candidate_results', None) or []
    scores = [c.get('score', 0) for c in rows]
    import statistics
    insights = {
        'count': len(rows),
        'avg_score': round(statistics.mean(scores),2) if scores else 0,
        'median_score': round(statistics.median(scores),2) if scores else 0,
        'min_score': min(scores) if scores else 0,
        'max_score': max(scores) if scores else 0,
    }

    # top missing skills across pool
    from collections import Counter
    missing = Counter()
    for c in rows:
        for m in (c.get('missing') or []):
            missing[m.lower()] += 1

    top_missing = missing.most_common(10)
    return render_template('insights.html', insights=insights, top_missing=top_missing)


@app.route('/candidate-action', methods=['POST'])
def candidate_action():
    # simple endpoint to update candidate status/notes
    cid = request.form.get('id')
    action = request.form.get('action')
    note = request.form.get('note', '')

    changed = False
    for c in app.candidate_store:
        if c.get('id') == cid:
            if action in ('SHORTLIST', 'REVIEW', 'REJECT'):
                c['status'] = action
            if note:
                c.setdefault('notes', []).append(note)
            changed = True
            break

    if changed:
        save_candidate_store(app.candidate_store)
        return ('', 204)
    return ('Candidate not found', 404)


@app.route('/remove-candidate', methods=['POST'])
def remove_candidate():
    cid = request.form.get('id')
    if not cid:
        return ('Missing id', 400)

    removed = False
    for i, c in enumerate(list(app.candidate_store)):
        if c.get('id') == cid or c.get('filename') == cid:
            # attempt to remove uploaded file from disk
            try:
                fp = os.path.join(app.config.get('UPLOAD_FOLDER', 'uploads'), c.get('filename') or '')
                if fp and os.path.exists(fp):
                    os.remove(fp)
            except Exception:
                pass
            del app.candidate_store[i]
            removed = True
            break

    if removed:
        save_candidate_store(app.candidate_store)
        return ('', 204)
    return ('Not found', 404)


@app.route('/candidates.json')
def candidates_json():
    # Only return results produced during the current analysis request.
    # Do not expose the persistent candidate store here so a page refresh
    # does not automatically render previously uploaded files.
    data = getattr(app, 'candidate_results', None) or []
    return jsonify(data)


if __name__ == "__main__":
    app.run()