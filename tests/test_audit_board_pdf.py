import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.audit_board_pdf import Finding, inspect_pdfinfo, parse_page_sizes


class ParsePageSizesTests(unittest.TestCase):
    def test_parses_default_and_page_numbered_pdfinfo_sizes(self):
        output = """Page size: 595.28 x 841.89 pts (A4)
Page    1 size: 595.28 x 841.89 pts (A4)
Page    2 size: 841.89 x 595.28 pts (A4)
"""
        self.assertEqual(
            parse_page_sizes(output),
            [(1, 595.28, 841.89), (1, 595.28, 841.89), (2, 841.89, 595.28)],
        )

    def test_applies_page_rotation_before_returning_dimensions(self):
        output = """Page    1 size: 841.89 x 595.28 pts (A4)
Page    1 rot: 0
Page    2 size: 595.28 x 841.89 pts (A4)
Page    2 rot: 90
Page    3 size: 595.28 x 841.89 pts (A4)
Page    3 rot: 270
"""
        self.assertEqual(
            parse_page_sizes(output),
            [
                (1, 841.89, 595.28),
                (2, 841.89, 595.28),
                (3, 841.89, 595.28),
            ],
        )


class InspectPdfInfoTests(unittest.TestCase):
    def test_fails_when_a_later_page_has_the_wrong_size(self):
        default_info = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Pages: 2\nPage size: 595.28 x 841.89 pts (A4)\n",
            stderr="",
        )
        per_page_info = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "Pages: 2\n"
                "Page    1 size: 595.28 x 841.89 pts (A4)\n"
                "Page    2 size: 841.89 x 595.28 pts (A4)\n"
            ),
            stderr="",
        )
        findings: list[Finding] = []

        with (
            patch("scripts.audit_board_pdf.shutil.which", return_value="/usr/bin/pdfinfo"),
            patch("scripts.audit_board_pdf.run", side_effect=[default_info, per_page_info]) as run_mock,
        ):
            inspect_pdfinfo(Path("board.pdf"), findings, 2, (210, 297), 0.5)

        self.assertEqual(run_mock.call_count, 2)
        self.assertEqual(
            run_mock.call_args_list[1].args[0],
            ["/usr/bin/pdfinfo", "-f", "1", "-l", "2", "board.pdf"],
        )
        size_finding = next(finding for finding in findings if finding.check == "page-size")
        self.assertEqual(size_finding.level, "FAIL")
        self.assertIn("page 2", size_finding.detail)

    def test_checks_the_default_size_for_a_single_page_pdf(self):
        pdf_info = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Pages: 1\nPage size: 595.28 x 841.89 pts (A4)\n",
            stderr="",
        )
        findings: list[Finding] = []

        with (
            patch("scripts.audit_board_pdf.shutil.which", return_value="/usr/bin/pdfinfo"),
            patch("scripts.audit_board_pdf.run", return_value=pdf_info) as run_mock,
        ):
            inspect_pdfinfo(Path("board.pdf"), findings, 1, (210, 297), 0.5)

        run_mock.assert_called_once()
        size_finding = next(finding for finding in findings if finding.check == "page-size")
        self.assertEqual(size_finding.level, "PASS")
        self.assertIn("all 1 page(s)", size_finding.detail)

    def test_accepts_landscape_extent_for_a_rotated_portrait_cropbox(self):
        default_info = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Pages: 2\nPage size: 841.89 x 595.28 pts (A4)\nPage rot: 0\n",
            stderr="",
        )
        per_page_info = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "Pages: 2\n"
                "Page    1 size: 841.89 x 595.28 pts (A4)\n"
                "Page    1 rot: 0\n"
                "Page    2 size: 595.28 x 841.89 pts (A4)\n"
                "Page    2 rot: 90\n"
            ),
            stderr="",
        )
        findings: list[Finding] = []

        with (
            patch("scripts.audit_board_pdf.shutil.which", return_value="/usr/bin/pdfinfo"),
            patch("scripts.audit_board_pdf.run", side_effect=[default_info, per_page_info]),
        ):
            inspect_pdfinfo(Path("board.pdf"), findings, 2, (297, 210), 0.5)

        size_finding = next(finding for finding in findings if finding.check == "page-size")
        self.assertEqual(size_finding.level, "PASS")
        self.assertIn("all 2 page(s)", size_finding.detail)


if __name__ == "__main__":
    unittest.main()
