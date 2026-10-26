"""A matched student's identity must survive optional result-data gaps."""

from pathlib import Path
import re
import unittest

from bise_results.parser import parse_result, ReviewRequired

FIXTURES = Path(__file__).parent / "fixtures"


class PartialResultTests(unittest.TestCase):
    def test_real_pass_prefix_layout(self):
        result = parse_result((FIXTURES / "ssc-part2-pass-prefix.html").read_text(), "001234")
        self.assertEqual((result["obtained"], result["maximum"]), (516, 1200))
        self.assertEqual(result["result_status"], "PASS")
        self.assertEqual(len(result["subjects"]), 9)
        self.assertEqual(result["data_completeness"], "complete")

    def test_real_absent_and_not_improved_summaries(self):
        for fixture, status in [("absent", "ABSENT"), ("not-improved", "MARKS NOT IMPROVED")]:
            with self.subTest(status=status):
                result = parse_result((FIXTURES / f"ssc-part2-{fixture}.html").read_text(), "001234")
                self.assertEqual(result["name"], "Sample Candidate")
                self.assertEqual(result["date_of_birth"], "09/05/2008")
                self.assertEqual(result["result_status"], status)
                self.assertEqual(result["total_raw"], status)
                self.assertEqual(result["data_completeness"], "summary_only")
                self.assertEqual(result["subjects"], [])
                for field in ("obtained", "maximum", "percentage"):
                    self.assertIsNone(result[field])

    def test_identity_without_a_subject_table_is_a_result(self):
        source = (FIXTURES / "ssc-part2.html").read_text()
        source = re.sub(r'<table\b.*?</table>', '', source, flags=re.S)
        result = parse_result(source, "001234")
        self.assertEqual(result["name"], "Sample Candidate")
        self.assertEqual(result["father_cnic"], "00000-0765432-0")
        self.assertEqual(result["data_completeness"], "identity_only")
        self.assertIsNone(result["obtained"])
        self.assertIsNone(result["percentage"])

    def test_unknown_columns_preserve_identity_and_raw_table(self):
        source = (FIXTURES / "ssc-part2.html").read_text().replace("TH-II", "NEW-COLUMN")
        result = parse_result(source, "001234")
        self.assertEqual(result["name"], "Sample Candidate")
        self.assertEqual(result["subjects"], [])
        self.assertTrue(result["subjects_raw"])
        self.assertEqual(result["data_completeness"], "partial")

    def test_inconsistent_totals_preserve_identity_without_inventing_marks(self):
        source = (FIXTURES / "ssc-part2.html").read_text().replace("MARKS OBTAINED:551", "MARKS OBTAINED:552")
        result = parse_result(source, "001234")
        self.assertEqual(result["name"], "Sample Candidate")
        self.assertIsNone(result["obtained"])
        self.assertIsNone(result["percentage"])
        self.assertIn("552", result["total_raw"])
        self.assertTrue(result["data_notes"])

    def test_blank_institution_and_statement_do_not_discard_identity(self):
        source = (FIXTURES / "ssc-part2.html").read_text().replace("Example Institution / District", "")
        source = re.sub(r'(<label id="lblResultinSentences">).*?(</label>)', r'\1\2', source, flags=re.S)
        result = parse_result(source, "001234")
        self.assertEqual(result["institution"], "")
        self.assertEqual(result["name"], "Sample Candidate")
        self.assertEqual(result["obtained"], 551)

    def test_missing_numeric_total_keeps_identity(self):
        source = (FIXTURES / "ssc-part2.html").read_text().replace("MARKS OBTAINED:551 C MARKS IMPROVED", "PASS")
        result = parse_result(source, "001234")
        self.assertEqual(result["result_status"], "PASS")
        self.assertIsNone(result["obtained"])
        self.assertIsNone(result["percentage"])
        self.assertEqual(result["name"], "Sample Candidate")

    def test_mismatched_roll_exam_or_missing_name_still_requires_review(self):
        source = (FIXTURES / "ssc-part2.html").read_text()
        for content, roll in [(source, "999999"), (source.replace("2026", "2025"), "001234"), (source.replace("Sample Candidate", ""), "001234"), ("<html>Invalid Captcha</html>", "001234")]:
            with self.subTest(roll=roll), self.assertRaises(ReviewRequired):
                parse_result(content, roll)

    def test_duplicate_totals_keep_identity_and_raw_values_without_guessing(self):
        source = (FIXTURES / "ssc-part2.html").read_text()
        row = re.search(r'<tr[^>]*>\s*<td[^>]*>TOTAL</td>.*?</tr>', source, re.S)[0]
        source = source.replace(row, row + row.replace("MARKS OBTAINED:551", "MARKS OBTAINED:552"))
        result = parse_result(source, "001234")
        self.assertEqual(result["name"], "Sample Candidate")
        self.assertIsNone(result["obtained"])
        self.assertIn("551", result["total_raw"])
        self.assertIn("552", result["total_raw"])

    def test_incomplete_subject_row_retains_identity_and_other_subjects(self):
        source = (FIXTURES / "ssc-part2.html").read_text()
        source = source.replace('<td align="center" valign="middle" style="width:50px;">40</td>', "", 1)
        result = parse_result(source, "001234")
        self.assertEqual(result["name"], "Sample Candidate")
        self.assertEqual(len(result["subjects_raw"]), 8)
        self.assertEqual(len(result["subjects"]), 7)
        self.assertEqual(result["data_completeness"], "partial")


if __name__ == "__main__":
    unittest.main()
