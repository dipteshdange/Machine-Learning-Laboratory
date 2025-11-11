from flask import Flask, render_template, request, redirect, url_for, flash
import pdfplumber
import pandas as pd
import re
from pathlib import Path
import os
import io
import zipfile
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# -------------------- Core Classes --------------------

class MarksheetVerifier:
    def __init__(self):
        self.grade_points = {
            'A+': 10, 'A': 9, 'B+': 8, 'B': 7, 'C+': 6,
            'C': 5, 'D': 4, 'F': 0, 'FF': 0, 'U': 0, 'UU': 0,
            'P': 5, 'PP': 5, 'PASS': 5, 'COMP': 5
        }

    def calculate_egp(self, courses):
        egp = 0
        for course in courses:
            grade = course['grade'].upper()
            earned = course['earned']
            point = self.grade_points.get(grade, 0)
            egp += point * earned
        return egp

    def calculate_total_credits(self, courses):
        return sum(course['earned'] for course in courses)

    def calculate_sgpa(self, courses):
        total_credits = self.calculate_total_credits(courses)
        if total_credits == 0:
            return 0
        egp = self.calculate_egp(courses)
        return round(egp / total_credits, 2)


class UniversalMarksheetExtractor:
    def __init__(self):
        self.courses = []

    def extract_text_from_pdf(self, pdf_path):
        text = ""
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages:
                    if page.extract_text():
                        text += page.extract_text() + "\n"
                    for table in page.extract_tables():
                        for row in table:
                            clean_row = [str(cell or '').strip() for cell in row]
                            text += ' | '.join(clean_row) + "\n"
        except Exception as e:
            print(f"Error reading PDF: {e}")
        return text

    def clean_text(self, text):
        return re.sub(r'\s+', ' ', text).strip()

    def is_valid_grade(self, grade):
        if not grade:
            return False
        grade = re.sub(r'[^A-Z\+\-]', '', grade.upper().strip())
        valid = ['A','A+','B','B+','C','C+','D','D+','F','FF','U','UU','P','PP','PASS','COMP']
        return grade in valid

    def is_valid_course_code(self, code):
        if not code:
            return False
        code = code.upper().strip()
        patterns = [
            r'^[A-Z]{2,4}\d{3,4}[A-Z]?\*?$',
            r'^[A-Z]{2,4}-\d{3,4}[A-Z]?\*?$'
        ]
        return any(re.match(p, code) for p in patterns)

    def is_valid_course_data(self, code, credit, earned, grade):
        if not self.is_valid_course_code(code): return False
        if not (0.5 <= credit <= 5.0): return False
        if not (0 <= earned <= credit): return False
        if not self.is_valid_grade(grade): return False
        return True

    def extract_courses_using_column_alignment(self, text):
        courses = []
        for line in text.split('\n'):
            line_clean = self.clean_text(line)
            if not line_clean: continue
            code_match = re.search(r'([A-Z]{2,4}\d{3,4}[A-Z]?\*?)', line_clean)
            if not code_match: continue
            code = code_match.group(1).upper()
            elements = line_clean.split()
            try: code_idx = elements.index(code_match.group(1))
            except: continue
            grade, g_idx = None, -1
            for i in range(len(elements)-1, max(len(elements)-5, code_idx), -1):
                if self.is_valid_grade(elements[i]):
                    grade, g_idx = elements[i].upper(), i
                    break
            if not grade: continue
            nums = [float(e) for e in elements[code_idx+1:g_idx] if re.match(r'^\d+\.?\d*$', e)]
            if len(nums) >= 2:
                credit, earned = nums[-2], nums[-1]
                if self.is_valid_course_data(code, credit, earned, grade):
                    courses.append({'course_code': code, 'credit': credit, 'earned': earned, 'grade': grade})
        return courses

    def extract_courses_using_fixed_patterns(self, text):
        patterns = [
            r'([A-Z]{2,4}\d{3,4}[A-Z]?\*?)\s+(\d+\.?\d*)\s+(\d+\.?\d*)\s+([A-Z][+-]?)',
            r'([A-Z]{2,4}\d{3,4}[A-Z]?\*?)\s+[A-Za-z\s]+\s+(\d+\.?\d*)\s+(\d+\.?\d*)\s+([A-Z][+-]?)'
        ]
        courses = []
        for line in text.split('\n'):
            l = self.clean_text(line)
            for p in patterns:
                for m in re.finditer(p, l):
                    try:
                        code, credit, earned, grade = m.group(1).upper(), float(m.group(2)), float(m.group(3)), m.group(4).upper()
                        if self.is_valid_course_data(code, credit, earned, grade):
                            courses.append({'course_code': code, 'credit': credit, 'earned': earned, 'grade': grade})
                            break
                    except: continue
        return courses

    def remove_duplicates(self, courses):
        seen, unique = set(), []
        for c in courses:
            if c['course_code'] not in seen:
                seen.add(c['course_code'])
                unique.append(c)
        return unique

    def process_pdf(self, pdf_path):
        text = self.extract_text_from_pdf(pdf_path)
        if not text.strip(): return []
        c1 = self.extract_courses_using_column_alignment(text)
        c2 = self.extract_courses_using_fixed_patterns(text)
        all_courses = self.remove_duplicates(c1 + c2)
        return all_courses


# -------------------- Helper Functions --------------------

def extract_reported_values(text):
    egp = credits = sgpa = 0
    match = re.search(r'Credits\s*(\d+\.?\d*)\s*EGP\s*(\d+\.?\d*)\s*SGPA\s*(\d+\.\d+)', text, re.I)
    if match:
        credits, egp, sgpa = float(match.group(1)), float(match.group(2)), float(match.group(3))
    else:
        nums = re.findall(r'\d+\.?\d*', text)
        if len(nums) >= 3:
            possible = [float(x) for x in nums]
            for i in range(len(possible)-2):
                if 10 <= possible[i] <= 40 and 100 <= possible[i+1] <= 400 and 5.0 <= possible[i+2] <= 10.0:
                    credits, egp, sgpa = possible[i], possible[i+1], possible[i+2]
                    break
    return egp, credits, sgpa


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ['pdf', 'zip']


# -------------------- Routes --------------------

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        flash('No file selected', 'error')
        return redirect(url_for('index'))
    file = request.files['file']
    if file.filename == '':
        flash('No file selected', 'error')
        return redirect(url_for('index'))

    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(path)

        extractor = UniversalMarksheetExtractor()
        courses = extractor.process_pdf(path)
        if not courses:
            flash('No data extracted.', 'error')
            return redirect(url_for('index'))

        text = extractor.extract_text_from_pdf(path)
        rep_egp, rep_cred, rep_sgpa = extract_reported_values(text)

        verifier = MarksheetVerifier()
        calc_egp = verifier.calculate_egp(courses)
        calc_cred = verifier.calculate_total_credits(courses)
        calc_sgpa = verifier.calculate_sgpa(courses)

        verification = {
            'egp': {'calculated': calc_egp, 'reported': rep_egp, 'match': abs(calc_egp - rep_egp) < 0.1},
            'credits': {'calculated': calc_cred, 'reported': rep_cred, 'match': abs(calc_cred - rep_cred) < 0.1},
            'sgpa': {'calculated': calc_sgpa, 'reported': rep_sgpa, 'match': abs(calc_sgpa - rep_sgpa) < 0.1}
        }

        return render_template('results.html', courses=courses, verification=verification, filename=filename)

    flash('Invalid file type.', 'error')
    return redirect(url_for('index'))


@app.route('/upload_bulk', methods=['POST'])
def upload_bulk():
    if 'bulk_files' not in request.files:
        flash('No files selected', 'error')
        return redirect(url_for('index'))

    files = request.files.getlist('bulk_files')
    results = []

    for file in files:
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(save_path)

            if filename.lower().endswith('.zip'):
                with zipfile.ZipFile(save_path, 'r') as zip_ref:
                    zip_ref.extractall(app.config['UPLOAD_FOLDER'])
                for f in os.listdir(app.config['UPLOAD_FOLDER']):
                    if f.lower().endswith('.pdf'):
                        process_path = os.path.join(app.config['UPLOAD_FOLDER'], f)
                        results.append(process_single_pdf(process_path, f))
            else:
                results.append(process_single_pdf(save_path, filename))

    return render_template('bulk_results.html', results=results)


def process_single_pdf(filepath, filename):
    try:
        extractor = UniversalMarksheetExtractor()
        courses = extractor.process_pdf(filepath)
        if not courses:
            return {'filename': filename, 'error': 'No data extracted'}

        text = extractor.extract_text_from_pdf(filepath)
        rep_egp, rep_cred, rep_sgpa = extract_reported_values(text)

        verifier = MarksheetVerifier()
        calc_egp = verifier.calculate_egp(courses)
        calc_cred = verifier.calculate_total_credits(courses)
        calc_sgpa = verifier.calculate_sgpa(courses)

        all_match = (
            abs(calc_egp - rep_egp) < 0.1 and
            abs(calc_cred - rep_cred) < 0.1 and
            abs(calc_sgpa - rep_sgpa) < 0.1
        )

        return {
            'filename': filename,
            'calculated': {'egp': calc_egp, 'credits': calc_cred, 'sgpa': calc_sgpa},
            'reported': {'egp': rep_egp, 'credits': rep_cred, 'sgpa': rep_sgpa},
            'status': '✅ Correct' if all_match else '❌ Wrong'
        }
    except Exception as e:
        return {'filename': filename, 'error': str(e)}


# -------------------- Main --------------------

if __name__ == '__main__':
    app.run(debug=True)
