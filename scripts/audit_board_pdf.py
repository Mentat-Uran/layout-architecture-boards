#!/usr/bin/env python3
"""Audit an architectural board PDF with Poppler and qpdf when available."""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


PT_PER_MM = 72.0 / 25.4
BASE_14_FONTS = {
    "Courier",
    "Courier-Bold",
    "Courier-BoldOblique",
    "Courier-Oblique",
    "Helvetica",
    "Helvetica-Bold",
    "Helvetica-BoldOblique",
    "Helvetica-Oblique",
    "Symbol",
    "Times-Bold",
    "Times-BoldItalic",
    "Times-Italic",
    "Times-Roman",
    "ZapfDingbats",
}


@dataclass
class Finding:
    level: str
    check: str
    detail: str


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def parse_size(value: str) -> tuple[float, float]:
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*[xX×]\s*([0-9]+(?:\.[0-9]+)?)\s*", value)
    if not match:
        raise argparse.ArgumentTypeError("size must look like 600x1800")
    return float(match.group(1)), float(match.group(2))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def add(findings: list[Finding], level: str, check: str, detail: str) -> None:
    findings.append(Finding(level, check, detail))


def command_or_warning(findings: list[Finding], name: str) -> str | None:
    resolved = shutil.which(name)
    if resolved is None:
        add(findings, "WARN", name, f"command not found; run an equivalent {name} check manually")
    return resolved


def inspect_pdfinfo(
    pdf: Path,
    findings: list[Finding],
    expected_pages: int | None,
    expected_size: tuple[float, float] | None,
    tolerance_mm: float,
) -> None:
    executable = command_or_warning(findings, "pdfinfo")
    if executable is None:
        return
    result = run([executable, str(pdf)])
    if result.returncode != 0:
        add(findings, "FAIL", "pdfinfo", result.stderr.strip() or "pdfinfo failed")
        return

    pages_match = re.search(r"^Pages:\s+(\d+)\s*$", result.stdout, re.MULTILINE)
    if pages_match:
        pages = int(pages_match.group(1))
        if expected_pages is not None and pages != expected_pages:
            add(findings, "FAIL", "pages", f"expected {expected_pages}, found {pages}")
        else:
            add(findings, "PASS", "pages", str(pages))
    else:
        add(findings, "WARN", "pages", "could not parse page count")

    size_match = re.search(
        r"^Page size:\s+([0-9.]+)\s+x\s+([0-9.]+)\s+pts",
        result.stdout,
        re.MULTILINE,
    )
    if size_match:
        width_mm = float(size_match.group(1)) / PT_PER_MM
        height_mm = float(size_match.group(2)) / PT_PER_MM
        detail = f"{width_mm:.3f} x {height_mm:.3f} mm"
        if expected_size is not None:
            width_ok = abs(width_mm - expected_size[0]) <= tolerance_mm
            height_ok = abs(height_mm - expected_size[1]) <= tolerance_mm
            if not (width_ok and height_ok):
                add(
                    findings,
                    "FAIL",
                    "page-size",
                    f"expected {expected_size[0]:g} x {expected_size[1]:g} mm, found {detail}",
                )
            else:
                add(findings, "PASS", "page-size", detail)
        else:
            add(findings, "PASS", "page-size", detail)
    else:
        add(findings, "WARN", "page-size", "could not parse page size")


def inspect_qpdf(pdf: Path, findings: list[Finding]) -> None:
    executable = command_or_warning(findings, "qpdf")
    if executable is None:
        return
    result = run([executable, "--check", str(pdf)])
    if result.returncode == 0:
        add(findings, "PASS", "qpdf", "PDF structure check passed")
    else:
        detail = (result.stderr or result.stdout).strip()
        add(findings, "FAIL", "qpdf", detail or "PDF structure check failed")


def inspect_fonts(pdf: Path, findings: list[Finding]) -> None:
    executable = command_or_warning(findings, "pdffonts")
    if executable is None:
        return
    result = run([executable, str(pdf)])
    if result.returncode != 0:
        add(findings, "FAIL", "fonts", result.stderr.strip() or "pdffonts failed")
        return
    rows: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        match = re.search(r"\s+(yes|no)\s+(yes|no)\s+(yes|no)\s+\d+\s+\d+\s*$", line)
        if match:
            rows.append((line.split()[0], match.group(1)))
    if not rows:
        add(findings, "PASS", "fonts", "no font rows detected")
        return
    unembedded = [name for name, embedded in rows if embedded == "no"]
    non_base14 = [name for name in unembedded if name not in BASE_14_FONTS]
    base14 = [name for name in unembedded if name in BASE_14_FONTS]
    if non_base14:
        add(
            findings,
            "FAIL",
            "fonts",
            f"non-embedded non-Base-14 font(s): {', '.join(non_base14)}",
        )
    else:
        embedded_count = len(rows) - len(unembedded)
        add(findings, "PASS", "fonts", f"embedded font rows: {embedded_count}/{len(rows)}")
    if base14:
        add(
            findings,
            "WARN",
            "base14-fonts",
            "not embedded: " + ", ".join(base14) + "; confirm the output workflow accepts PDF Base 14 fonts",
        )


def extract_text(pdf: Path, findings: list[Finding]) -> str | None:
    executable = command_or_warning(findings, "pdftotext")
    if executable is None:
        return None
    result = run([executable, "-layout", str(pdf), "-"])
    if result.returncode != 0:
        add(findings, "FAIL", "text-extraction", result.stderr.strip() or "pdftotext failed")
        return None
    add(findings, "PASS", "text-extraction", f"extracted {len(result.stdout)} characters")
    return result.stdout


def inspect_text(
    text: str | None,
    findings: list[Finding],
    required: list[str],
    forbidden: list[str],
) -> None:
    if text is None:
        if required or forbidden:
            add(findings, "WARN", "text-rules", "required/forbidden text could not be checked")
        return
    for value in required:
        if value in text:
            add(findings, "PASS", "required-text", repr(value))
        else:
            add(findings, "FAIL", "required-text", f"missing {value!r}")
    for value in forbidden:
        if value in text:
            add(findings, "FAIL", "forbidden-text", f"found {value!r}")
        else:
            add(findings, "PASS", "forbidden-text", repr(value))


def inspect_images(pdf: Path, findings: list[Finding], minimum_ppi: float | None) -> None:
    executable = command_or_warning(findings, "pdfimages")
    if executable is None:
        return
    result = run([executable, "-list", str(pdf)])
    if result.returncode != 0:
        add(findings, "FAIL", "images", result.stderr.strip() or "pdfimages failed")
        return
    rows = [
        line
        for line in result.stdout.splitlines()
        if re.match(r"^\s*\d+\s+\d+\s+\S+\s+", line)
    ]
    ppi_values: list[float] = []
    for line in rows:
        fields = line.split()
        if len(fields) < 8:
            continue
        try:
            x_ppi = float(fields[-4])
            y_ppi = float(fields[-3])
        except (ValueError, IndexError):
            continue
        ppi_values.extend([x_ppi, y_ppi])
    if ppi_values:
        lowest = min(ppi_values)
        highest = max(ppi_values)
        add(
            findings,
            "PASS",
            "images",
            f"detected {len(rows)} embedded image row(s); effective PPI range {lowest:g}-{highest:g}",
        )
        if minimum_ppi is not None:
            if lowest < minimum_ppi:
                add(
                    findings,
                    "FAIL",
                    "image-ppi",
                    f"minimum observed {lowest:g} ppi is below required {minimum_ppi:g} ppi",
                )
            else:
                add(
                    findings,
                    "PASS",
                    "image-ppi",
                    f"minimum observed {lowest:g} ppi meets required {minimum_ppi:g} ppi",
                )
    else:
        add(findings, "PASS", "images", f"detected {len(rows)} embedded image row(s); PPI unavailable")


def render_pdf(pdf: Path, findings: list[Finding], dpi: float, render_dir: Path | None) -> None:
    if dpi <= 0:
        return
    executable = command_or_warning(findings, "pdftoppm")
    if executable is None:
        return
    destination = render_dir or Path.cwd() / f"{pdf.stem}-qa-render"
    destination.mkdir(parents=True, exist_ok=True)
    prefix = destination / pdf.stem
    result = run([executable, "-png", "-r", f"{dpi:g}", str(pdf), str(prefix)])
    if result.returncode != 0:
        add(findings, "FAIL", "render", result.stderr.strip() or "pdftoppm failed")
        return
    outputs = sorted(destination.glob(f"{pdf.stem}-*.png"))
    if not outputs:
        add(findings, "FAIL", "render", "pdftoppm returned success but no PNG was found")
    else:
        add(findings, "PASS", "render", f"created {len(outputs)} PNG page(s) in {destination}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="PDF board to audit")
    parser.add_argument("--expected-pages", type=int)
    parser.add_argument("--expected-size-mm", type=parse_size, metavar="WIDTHxHEIGHT")
    parser.add_argument("--size-tolerance-mm", type=float, default=0.5)
    parser.add_argument("--required-text", action="append", default=[])
    parser.add_argument("--forbid-text", action="append", default=[])
    parser.add_argument(
        "--min-image-ppi",
        type=float,
        help="fail when the minimum x/y PPI reported by pdfimages is lower than this value",
    )
    parser.add_argument("--render-dpi", type=float, default=0.0)
    parser.add_argument("--render-dir", type=Path)
    args = parser.parse_args()

    pdf = args.pdf.expanduser().resolve()
    findings: list[Finding] = []
    if not pdf.is_file():
        print(f"FAIL file: PDF not found: {pdf}")
        return 2
    if pdf.suffix.lower() != ".pdf":
        print(f"FAIL file: expected a .pdf file: {pdf}")
        return 2

    add(findings, "PASS", "file", f"{pdf} ({pdf.stat().st_size} bytes)")
    add(findings, "PASS", "sha256", sha256(pdf))
    inspect_pdfinfo(
        pdf,
        findings,
        args.expected_pages,
        args.expected_size_mm,
        args.size_tolerance_mm,
    )
    inspect_qpdf(pdf, findings)
    inspect_fonts(pdf, findings)
    text = extract_text(pdf, findings)
    inspect_text(text, findings, args.required_text, args.forbid_text)
    inspect_images(pdf, findings, args.min_image_ppi)
    render_pdf(pdf, findings, args.render_dpi, args.render_dir)

    for finding in findings:
        print(f"{finding.level:<4} {finding.check}: {finding.detail}")

    failures = sum(finding.level == "FAIL" for finding in findings)
    warnings = sum(finding.level == "WARN" for finding in findings)
    print(f"SUMMARY failures={failures} warnings={warnings} checks={len(findings)}")
    if failures:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
