#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Temporary benchmark support. Exclude this file from the implementation PR.

import argparse
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time

import yaml

BASELINE = "2447d8389802f56b33f8640c3167c6e859c7a582"
WORKFLOW = ".github/workflows/studio-frontend-ci.yml"
SERIAL_NAMES = {
    "Browser smoke for ANSI tool output",
    "Composer icon alignment",
    "Browser smoke for reopening the research plan",
    "Browser smoke for the settings tab panels",
    "Data settings deletion choices",
    "Browser smoke for keyboard shortcuts",
    "Browser smoke for find in page",
    "Browser smoke for a settings panel that cannot load",
}
PARALLEL_NAME = "Browser smoke groups for data settings and frontend controls"


def load_steps(repo):
    baseline = subprocess.check_output(
        ["git", "show", f"{BASELINE}:{WORKFLOW}"], cwd = repo, text = True,
    )
    serial = [step for step in yaml.safe_load(baseline)["jobs"]["build"]["steps"]
              if step.get("name") in SERIAL_NAMES]
    current = yaml.safe_load((repo / WORKFLOW).read_text())
    parallel = [step for step in current["jobs"]["build"]["steps"]
                if step.get("name") == PARALLEL_NAME]
    if len(serial) != 8 or {step["name"] for step in serial} != SERIAL_NAMES:
        raise ValueError("The baseline must contain all eight serial steps.")
    if len(parallel) != 1:
        raise ValueError("The current workflow must contain one parallel step.")
    return {"serial": serial, "parallel": parallel}


def stop(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout = 15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def run_step(repo, step, env, output, index):
    name = f"step-{index:02d}"
    (output / f"{name}.sh").write_text(step["run"])
    step_env = {**env, **{key: str(value) for key, value in step.get("env", {}).items()}}
    budget = 60 * step.get("timeout-minutes", 20)
    start = time.monotonic()
    with (output / f"{name}.log").open("w") as log:
        process = subprocess.Popen(
            ["bash", "-e", "-o", "pipefail", "-c", step["run"]], cwd = repo,
            env = step_env, stdout = log, stderr = subprocess.STDOUT,
            start_new_session = True,
        )
        try:
            code = process.wait(timeout = budget)
        except subprocess.TimeoutExpired:
            stop(process)
            code = 124
        except BaseException:
            stop(process)
            raise
    return {"name": step["name"], "seconds": round(time.monotonic() - start, 3),
            "exit_code": code, "timeout_seconds": budget, "log": f"{name}.log",
            "env": {key: value for key, value in step_env.items()
                    if key.startswith(("PW_", "VITE_")) or key == "RUNNER_TEMP"}}


def run_case(repo, output, mode, cache_state, steps):
    label = f"{mode}-{cache_state}"
    case_dir = output / label
    case_dir.mkdir()
    cache = output / "caches" / mode
    if cache_state == "cold" and cache.exists():
        shutil.rmtree(cache)
    cache.mkdir(parents = True, exist_ok = True)
    env = {**os.environ, "RUNNER_TEMP": str(cache)}
    # The parallel step supplies one cache per group through RUNNER_TEMP.
    env.pop("VITE_TEST_CACHE_DIR", None)
    if mode == "serial":
        env["VITE_TEST_CACHE_DIR"] = str(cache / "vite-serial")
    logs = repo / "logs"
    if logs.exists():
        raise ValueError(f"Move existing logs before this benchmark: {logs}")
    rows = []
    start = time.monotonic()
    report = {"label": label, "mode": mode, "cache_state": cache_state, "steps": rows}
    try:
        for index, step in enumerate(steps, 1):
            row = run_step(repo, step, env, case_dir, index)
            rows.append(row)
            print(json.dumps({"run": label, **row}), flush = True)
    finally:
        report["seconds"] = round(time.monotonic() - start, 3)
        report["passed"] = len(rows) == len(steps) and all(row["exit_code"] == 0 for row in rows)
        if logs.exists():
            shutil.move(str(logs), str(case_dir / "reports"))
        (case_dir / "result.json").write_text(json.dumps(report, indent = 2) + "\n")
    return report


def write_summary(output, metadata, reports):
    by_name = {report["label"]: report for report in reports}
    comparisons = {}
    lines = ["# Temporary frontend browser benchmark", "",
             f"Baseline: `{BASELINE}`. Candidate: `{metadata['candidate']}`.", "",
             "| Run | Seconds | Passed |", "| --- | ---: | --- |"]
    for report in reports:
        lines.append(f"| {report['label']} | {report['seconds']:.3f} | {report['passed']} |")
    lines += ["", "| Cache state | Wall time reduction | Speedup |",
              "| --- | ---: | ---: |"]
    for state in ("cold", "warm"):
        serial = by_name[f"serial-{state}"]["seconds"]
        parallel = by_name[f"parallel-{state}"]["seconds"]
        reduction = 100 * (1 - parallel / serial)
        speedup = serial / parallel
        comparisons[state] = {"wall_time_reduction_percent": round(reduction, 2),
                              "speedup": round(speedup, 3)}
        lines.append(f"| {state} | {reduction:.2f}% | {speedup:.3f}x |")
    lines += ["", "Cold means an empty Vite cache for each mode. Warm reuses that mode's cache.",
              "All runs share one runner, checkout, dependency install, and browser install.",
              "The run order is fixed. OS caches and runner load can affect these samples.",
              "One sample per cache state does not establish a stable median.",
              "A timing from a failed check does not measure equivalent successful work.", ""]
    summary = "\n".join(lines)
    (output / "summary.md").write_text(summary)
    (output / "summary.json").write_text(json.dumps(
        {"metadata": metadata, "runs": reports, "comparisons": comparisons}, indent = 2,
    ) + "\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as target:
            target.write(summary)
    print(summary, flush = True)


def main():
    parser = argparse.ArgumentParser(description = "Compare the fixed serial and parallel browser checks.")
    parser.add_argument("--output", type = Path, default = Path("benchmark-results"))
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    output.mkdir(parents = True, exist_ok = False)
    steps = load_steps(repo)
    metadata = {
        "baseline": BASELINE,
        "candidate": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd = repo, text = True).strip(),
        "python": sys.version, "playwright": version("playwright"), "platform": platform.platform(),
        "cpu_count": os.cpu_count(), "image_os": os.environ.get("ImageOS"),
        "image_version": os.environ.get("ImageVersion"), "run_id": os.environ.get("GITHUB_RUN_ID"),
        "node": subprocess.check_output(["node", "--version"], text = True).strip(),
        "npm": subprocess.check_output(["npm", "--version"], text = True).strip(),
    }
    (output / "metadata.json").write_text(json.dumps(metadata, indent = 2) + "\n")
    reports = []
    for mode, state in (("serial", "cold"), ("parallel", "cold"),
                        ("parallel", "warm"), ("serial", "warm")):
        reports.append(run_case(repo, output, mode, state, steps[mode]))
    write_summary(output, metadata, reports)
    return int(any(not report["passed"] for report in reports))


def cancel(signum, frame):
    raise KeyboardInterrupt(f"Benchmark received signal {signum}.")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, cancel)
    sys.exit(main())
