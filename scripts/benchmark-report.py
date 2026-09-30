#!/usr/bin/env python3
"""Render benchmark artifacts, optionally comparing an earlier run."""
import collections
import csv
import math
import os
from pathlib import Path
import re
import statistics
import shutil
import sys


def read_cli(directory):
    groups = collections.defaultdict(list)
    with (directory / "cli.csv").open() as handle:
        for row in csv.DictReader(handle):
            groups[(row["benchmark"], row["name"])].append(row)
    if len(groups) != 27:
        raise ValueError(f"Expected 27 CLI variants in {directory}, found {len(groups)}")
    results = {}
    for key, rows in groups.items():
        lines = {row["lines"] or "0" for row in rows}
        if len(rows) != 10 or len(lines) != 1:
            raise ValueError(f"Incomplete samples or unstable output counts: {key}")
        times = [float(row["duration"]) * 1000 for row in rows]
        results[key] = (statistics.mean(times), statistics.stdev(times), lines.pop())
    return results


def read_glob(directory):
    matches = re.findall(
        r"test (\S+)\s+\.\.\. bench:\s+([\d,.]+) ns/iter \(\+/- ([\d,.]+)\)",
        (directory / "glob.log").read_text(),
    )
    if len(matches) != 8:
        raise ValueError(f"Expected 8 glob measurements in {directory}, found {len(matches)}")
    return {name: (float(mean.replace(",", "")), float(spread.replace(",", "")))
            for name, mean, spread in matches}


STYLES = {
    "heading": "1;36", "FASTER": "1;32", "SLOWER": "1;31",
    "SAME": "33", "muted": "2", "warning": "1;33",
}


def paint(text: str, style: str, enabled: bool) -> str:
    """Style terminal text without changing its visible width."""
    return f"\033[{STYLES[style]}m{text}\033[0m" if enabled else text


def color_enabled() -> bool:
    """Honor explicit color preferences before checking terminal capabilities."""
    if "NO_COLOR" in os.environ:
        return False
    if os.environ.get("FORCE_COLOR", "0") != "0":
        return True
    return sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


def change(current: float, previous: float, noise: float) -> tuple[str, str]:
    """Return elapsed-time change and a noise-aware comparison label."""
    delta = current - previous
    percent = delta / previous * 100
    threshold = max(previous * 0.03, noise)
    status = "SAME" if abs(delta) <= threshold else ("FASTER" if delta < 0 else "SLOWER")
    return f"{percent:+.1f}%", status


def render_table(headers, rows, numeric, color, terminal_width):
    """Align before coloring; use stacked rows when a table would wrap."""
    widths = [max(len(header), *(len(row[i]) for row in rows))
              for i, header in enumerate(headers)]
    status_index = headers.index("Result") if "Result" in headers else None
    lines = []
    if sum(widths) + 3 * len(headers) + 1 > terminal_width:
        for row in rows:
            lines.append(paint(row[0], "heading", color))
            for i, value in enumerate(row[1:], 1):
                style = row[status_index] if status_index is not None else "muted"
                label = f"  {headers[i]}: {value}"
                lines.append(paint(label, style, color) if headers[i] in ("Change", "Result")
                             else label)
            lines.append("")
        return lines
    lines.append(paint("| " + " | ".join(h.ljust(w) for h, w in zip(headers, widths))
                       + " |", "heading", color))
    lines.append("|" + "|".join("-" * (width + 2) for width in widths) + "|")
    for row in rows:
        cells = []
        for i, (value, width) in enumerate(zip(row, widths)):
            cell = value.rjust(width) if headers[i] in numeric else value.ljust(width)
            if status_index is not None and headers[i] in ("Result", "Change"):
                cell = paint(cell, row[status_index], color)
            cells.append(cell)
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def main():
    output = Path(os.environ["BENCH_OUTPUT_DIR"])
    baseline = os.environ.get("BENCH_BASELINE_DIR")
    baseline = Path(baseline) if baseline else None
    cli, glob = read_cli(output), read_glob(output)
    environment_changed = False
    if baseline:
        old_cli, old_glob = read_cli(baseline), read_glob(baseline)
        if cli.keys() != old_cli.keys() or glob.keys() != old_glob.keys():
            raise ValueError("Baseline and current benchmark names differ")
        for key in cli:
            if cli[key][2] != old_cli[key][2]:
                raise ValueError(f"Output line count changed from baseline: {key}")
        # Compiler, binary version/features and host must match. Commit and
        # working-tree status may differ because that's what we're comparing.
        old_env = (baseline / "environment.txt").read_text().splitlines()
        new_env = (output / "environment.txt").read_text().splitlines()
        def normalize(lines):
            metadata = []
            for line in lines:
                if re.fullmatch(r"[0-9a-f]{40,64}", line):
                    break
                metadata.append(re.sub(r" \(rev [^)]+\)", "", line))
            return metadata
        environment_changed = normalize(old_env) != normalize(new_env)

    color = color_enabled()
    terminal_width = shutil.get_terminal_size((120, 24)).columns
    plain_lines = []

    def emit(text="", style=None):
        plain_lines.append(text)
        print(paint(text, style, color) if style else text)

    def table(headers, rows):
        numeric = {"Before", "Now", "Mean", "SD", "Spread", "Change", "Lines"}
        plain_lines.extend(render_table(headers, rows, numeric, False, 10000))
        print("\n".join(render_table(headers, rows, numeric, color, terminal_width)))

    cli_rows, glob_rows = [], []
    assessments = collections.Counter()
    for (name, variant), (mean, sd, lines) in cli.items():
        label = name.removeprefix("subtitles_")
        label += " / " + variant.removeprefix("rg").strip(" ()") if variant != "rg" else ""
        row = [label]
        if baseline:
            old_mean, old_sd, _ = old_cli[(name, variant)]
            noise = 2 * math.sqrt(sd * sd / 10 + old_sd * old_sd / 10)
            percent, status = change(mean, old_mean, noise)
            row += [f"{old_mean:,.2f}", f"{mean:,.2f}", percent, status]
            assessments[status] += 1
        else:
            row += [f"{mean:,.2f}"]
        row += [f"{sd:.2f}", lines]
        cli_rows.append(row)

    for name, (mean, spread) in glob.items():
        row = [name]
        if baseline:
            old_mean, old_spread = old_glob[name]
            percent, status = change(mean, old_mean, spread + old_spread)
            row += [f"{old_mean:.2f}", f"{mean:.2f}", percent, status]
            assessments[status] += 1
        else:
            row += [f"{mean:.2f}"]
        row += [f"{spread:.2f}"]
        glob_rows.append(row)

    emit("\nBENCHMARK RESULTS", "heading")
    if environment_changed:
        emit("WARNING: Environment differs; inspect both environment.txt files.", "warning")
    if baseline:
        emit(f"Baseline: {baseline}")
        emit("Negative change = less time (faster). Positive change = more time (slower).")
        emit()
        for status in ("FASTER", "SLOWER", "SAME"):
            emit(f"  {status:6}  {assessments[status]:2} / 35 benchmarks", status)
        if assessments["SLOWER"]:
            emit("\nREGRESSIONS DETECTED - review the SLOWER rows.", "SLOWER")
        elif assessments["FASTER"]:
            emit("\nIMPROVEMENTS DETECTED - no regressions flagged.", "FASTER")
        else:
            emit("\nNO CLEAR PERFORMANCE CHANGE", "SAME")
        emit("SAME means no clear change beyond noise or the 3% minimum threshold.")
    else:
        emit("BASELINE RUN - no earlier results selected for comparison.", "warning")
        emit("To detect improvements or regressions, use --baseline RESULTS_DIRECTORY.")

    timing_headers = ["Before", "Now", "Change", "Result"] if baseline else ["Mean"]
    emit("\nCLI SEARCH  |  milliseconds (lower is better)", "heading")
    emit("27 variants; 3 warmups + 10 measurements each. SD = sample standard deviation.")
    table(["Benchmark"] + timing_headers + ["SD", "Lines"], cli_rows)

    emit("\nGLOB MATCHING  |  nanoseconds/iteration (lower is better)", "heading")
    emit("8 microbenchmarks. Spread = the benchmark harness's reported +/- variation.")
    table(["Benchmark"] + timing_headers + ["Spread"], glob_rows)

    if baseline:
        emit("\nComparison thresholds", "heading")
        emit("CLI: larger of 3% and twice the combined standard error.")
        emit("Glob: larger of 3% and the sum of both reported spreads.")
        emit("These are screening signals. Repeat matching runs to confirm changes.")
    emit("\nCOMPLETE  |  270 CLI measurements + 8 glob benchmarks", "heading")
    emit("Not run: 11 Linux-source scenarios (require a Linux kernel build).")
    emit(f"Results and logs: {output}")
    (output / "summary.md").write_text("\n".join(plain_lines) + "\n")


if __name__ == "__main__":
    main()
