"""Preserve matched student identity, with optional board marks/status data."""

from dataclasses import dataclass, field
from html.parser import HTMLParser
import re
from .exams import exam_details

PARSER_VERSION = "ssc-part2-v3"
IDENTITY_FIELDS = {
    "date_of_birth": "lblDOB",
    "student_cnic": "lblBFARM",  # The board calls this B-form for younger students.
    "father_cnic": "lblFatherNIC",
    "registration_number": "lblRegNum",
}


def parser_version(exam_type, course='SSC'):
    exam_details(exam_type, course)
    if course == 'HSSC':return 'hssc-part2-v1'
    return 'ssc-part1-v1' if str(exam_type) == '1' else PARSER_VERSION


class ReviewRequired(ValueError):
    pass


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    def text(self):
        return re.sub(r"\s+", " ", " ".join(c.text() if isinstance(c, Node) else c for c in self.children)).strip()

    def elements(self, tag):
        for child in self.children:
            if isinstance(child, Node):
                if child.tag == tag:
                    yield child
                yield from child.elements(tag)


class Document(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack = [self.root]
        self.ids = {}
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if node.attrs.get("id"):
            self.ids[node.attrs["id"]] = node
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)

    def text(self, ident):
        return self.ids[ident].text() if ident in self.ids else ""


def integer(value):
    return int(value) if re.fullmatch(r"[0-9]+", value) else None


def board_status(total_raw, statement):
    for status in ("MARKS NOT IMPROVED", "MARKS IMPROVED", "ABSENT", "WITHHELD", "COMPARTMENT"):
        if status in total_raw.upper():
            return status
    for pattern, status in [(r"\bPASS(?:ED)?\b", "PASS"), (r"\bFAIL(?:ED)?\b", "FAIL")]:
        if re.search(pattern, total_raw + " " + statement, re.I):
            return status
    return ""


def parse_marks(doc):
    result = {"subjects": [], "subjects_raw": [], "total_raw": "", "maximum_raw": "",
              "obtained": None, "maximum": None, "percentage": None,
              "data_completeness": "identity_only", "data_notes": ""}
    grid = doc.ids.get("GridStudentData")
    if grid is None:
        result["data_notes"] = "Subject marks unavailable"
        return result
    rows = list(grid.elements("tr"))
    data_rows = [[cell.text() for cell in row.elements("td")] for row in rows]
    data_rows = [cells for cells in data_rows if cells]
    total_rows = [cells for cells in data_rows if cells[0].upper() == "TOTAL"]
    result["subjects_raw"] = [cells for cells in data_rows if cells[0].upper() != "TOTAL"]
    notes = []
    top = [cell.text().upper() for cell in rows[0].elements("th")] if rows else []
    sub = [cell.text().upper() for cell in rows[1].elements("th")] if len(rows) > 1 else []
    known_columns = (top == ["SUBJECT", "MAXIMUM MARKS", "MARKS OBTAINED", "TOTAL", "PERCENTILE SCORE", "REL. GRADE", "STATUS"]
                     and sub[:6] == ["TH", "PR", "TOTAL", "TH-I", "TH-II", "PR-II"])
    fields = ["subject", "max_theory_raw", "max_practical_raw", "max_total_raw", "theory_i_raw", "theory_ii_raw", "practical_ii_raw", "obtained_raw", "percentile_raw", "grade", "status"]
    if result["subjects_raw"] and not known_columns:
        notes.append("Subject column layout unrecognized; original values retained")
    if known_columns:
        for cells in result["subjects_raw"]:
            if len(cells) != len(fields):
                notes.append("Some subject marks unavailable")
                continue
            subject = dict(zip(fields, cells))
            subject["max_total"] = integer(subject["max_total_raw"])
            subject["obtained"] = integer(subject["obtained_raw"])
            if subject["max_total"] is not None and subject["max_total"] <= 0:
                subject["max_total"] = None
            if subject["obtained"] is not None and subject["max_total"] is not None and subject["obtained"] > subject["max_total"]:
                subject["obtained"] = None
                notes.append("Subject marks inconsistent; original values retained")
            result["subjects"].append(subject)
    if len(total_rows) == 1 and len(total_rows[0]) == 3:
        _, result["maximum_raw"], result["total_raw"] = total_rows[0]
        maximum = integer(result["maximum_raw"])
        result["maximum"] = maximum if maximum is not None and maximum > 0 else None
        match = re.search(r"MARKS\s+OBTAINED\s*:\s*(?:(?:PASS(?:ED)?|FAIL(?:ED)?)\s+)?(\d+)\b", result["total_raw"], re.I)
        result["obtained"] = int(match[1]) if match else None
    elif total_rows:
        result["total_raw"] = " | ".join(" | ".join(cells[1:]) for cells in total_rows)
        notes.append("Overall marks ambiguous; original values retained")
    else:
        notes.append("Overall marks unavailable")
    subjects = result["subjects"]
    if subjects and len(subjects) == len(result["subjects_raw"]):
        if result["maximum"] is not None and all(s["max_total"] is not None for s in subjects) and sum(s["max_total"] for s in subjects) != result["maximum"]:
            result["maximum"] = None
            notes.append("Maximum marks do not reconcile; original values retained")
        if result["obtained"] is not None and all(s["obtained"] is not None for s in subjects) and sum(s["obtained"] for s in subjects) != result["obtained"]:
            result["obtained"] = None
            notes.append("Obtained marks do not reconcile; original values retained")
    if result["obtained"] is not None and result["maximum"] is not None:
        if result["obtained"] > result["maximum"]:
            result["obtained"] = None
            notes.append("Obtained marks exceed maximum; original values retained")
        else:
            result["percentage"] = round(result["obtained"] * 100 / result["maximum"], 2)
    if result["obtained"] is None and re.search(r"\bpassed\b", doc.text("lblResultinSentences"), re.I):
        notes.append("Numeric total unavailable")
    if not result["subjects_raw"] and result["total_raw"]:
        result["data_completeness"] = "summary_only"
    elif result["subjects_raw"] or result["total_raw"]:
        result["data_completeness"] = "complete" if subjects and not notes and result["obtained"] is not None and result["maximum"] is not None else "partial"
    result["data_notes"] = "; ".join(dict.fromkeys(notes))
    return result


def parse_part1_marks(doc):
    result = {'subjects': [], 'subjects_raw': [], 'total_raw': '', 'maximum_raw': '',
              'obtained': None, 'maximum': None, 'percentage': None,
              'data_completeness': 'identity_only', 'data_notes': ''}
    grid = doc.ids.get('GridStudentData')
    if grid is None:
        result['data_notes'] = 'Subject marks unavailable'
        return result
    rows = list(grid.elements('tr'))
    headers = [n.text().upper() for n in rows[0].elements('th')] if rows else []
    known = headers == ['NAME OF SUBJECT', 'MAX. MARKS', 'MARKS OBTAINED', 'RELATIVE GRADE', 'PERCENTILE SCORE', 'RESULT STATUS']
    notes, totals = [], []
    for row in rows:
        cells = [n.text() for n in row.elements('td')]
        if not cells:
            continue
        if 'TOTAL' in [v.upper() for v in cells]:
            totals.append(cells)
        else:
            result['subjects_raw'].append(cells)
            if known and len(cells) == 6:
                name, maximum, obtained, grade, percentile, status = cells
                subject = {'subject': name, 'max_theory_raw': maximum, 'max_practical_raw': '',
                           'max_total_raw': maximum, 'theory_i_raw': obtained, 'theory_ii_raw': '',
                           'practical_ii_raw': '', 'obtained_raw': obtained, 'grade': grade,
                           'percentile_raw': percentile, 'status': status,
                           'max_total': integer(maximum), 'obtained': integer(obtained)}
                if subject['max_total'] is not None and subject['max_total'] <= 0:
                    subject['max_total'] = None
                if subject['obtained'] is not None and subject['max_total'] is not None and subject['obtained'] > subject['max_total']:
                    subject['obtained'] = None
                    notes.append('Subject marks inconsistent; original values retained')
                result['subjects'].append(subject)
            elif known:
                notes.append('Some subject marks unavailable')
    if not known:
        notes.append('Subject column layout unrecognized; original values retained')
    if len(totals) == 1:
        result['total_raw'] = ' | '.join(v for v in totals[0] if v and v.upper() != 'TOTAL')
        match = re.fullmatch(r'(?:(?:PASS(?:ED)?|FAIL(?:ED)?)\s+)?([0-9]+)', result['total_raw'], re.I)
        result['obtained'] = int(match[1]) if match else None
    else:
        result['total_raw'] = ' | '.join(' | '.join(v) for v in totals)
        notes.append('Overall marks ambiguous' if totals else 'Overall marks unavailable')
    subjects = result['subjects']
    if subjects and len(subjects) == len(result['subjects_raw']):
        if all(v['max_total'] is not None for v in subjects):
            result['maximum'] = sum(v['max_total'] for v in subjects)
            result['maximum_source'] = 'sum_of_subject_maxima'
        if result['obtained'] is not None and all(v['obtained'] is not None for v in subjects) and sum(v['obtained'] for v in subjects) != result['obtained']:
            result['obtained'] = None
            notes.append('Obtained marks do not reconcile; original values retained')
    if result['obtained'] is not None and result['maximum']:
        if result['obtained'] > result['maximum']:
            result['obtained'] = None
            notes.append('Obtained marks exceed maximum; original values retained')
        else:
            result['percentage'] = round(100 * result['obtained'] / result['maximum'], 2)
    if not result['subjects_raw'] and result['total_raw']:
        result['data_completeness'] = 'summary_only'
    elif result['subjects_raw'] or result['total_raw']:
        result['data_completeness'] = 'complete' if subjects and not notes and result['obtained'] is not None and result['maximum'] else 'partial'
    result['data_notes'] = '; '.join(dict.fromkeys(notes))
    return result


def parse_result(source: str, expected_roll: str, year: int = 2026, exam_type='2', course='SSC') -> dict:
    exam_type = str(exam_type)
    exam_details(exam_type, course)
    doc = Document(source)
    if doc.text("lblRollNoval") != expected_roll:
        raise ReviewRequired("Returned roll number is missing or does not match")
    heading = doc.text("lblSession")
    course_matches = (bool(re.search(r"\bINTERMEDIATE\s*\(PART\s+I\s*&\s*II\s*\)", heading, re.I)) if course == 'HSSC'
                      else bool(re.search(r"\bSECONDARY SCHOOL CERTIFICATE\b", heading, re.I)) and not re.search(r"\bHIGHER\b", heading, re.I))
    if not course_matches or not re.search(rf"\b{year}\b", heading) or "ANNUAL" not in heading.upper() or re.search(r"SUPPLEMENTARY|SECOND\s+ANNUAL", heading, re.I):
        raise ReviewRequired("Exam heading does not confirm requested course, Annual session and year")
    ninth = bool(re.search(r'\b9TH\s+CLASS\b', heading, re.I))
    if (exam_type == '1') != ninth:
        raise ReviewRequired('Exam heading does not match requested class/part')
    if not doc.text("Name"):
        raise ReviewRequired("Student identity is missing")
    marks = parse_part1_marks(doc) if exam_type == '1' else parse_marks(doc)
    statement = doc.text("lblResultinSentences") or (marks['total_raw'] if exam_type == '1' else '')
    return {
        "parser_version": parser_version(exam_type, course), "roll": expected_roll,
        "course": course, "exam_type": exam_type, "year": year, "exam_heading": heading,
        "name": doc.text("Name"), "father_name": doc.text("lblFatherName"),
        **{field: doc.text(ident) for field, ident in IDENTITY_FIELDS.items()},
        "institution": doc.text("lblExamCenter"), "group": doc.text("lblGroup"),
        "result_statement": statement, "result_status": board_status(marks["total_raw"], statement),
        "declared_on": doc.text("lblResultdate"), **marks,
    }
