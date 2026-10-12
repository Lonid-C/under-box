#!/usr/bin/env python3
"""Run all current suites plus the legacy function suite, without real credentials.

Exit 0: all passed. Exit 1: new/unclassified failure, skips or missing runtime.
Exit 2: only the explicitly recorded legacy failures remain (not all passed).
Each suite runs in its own process to isolate imports, environment and state.
"""
from __future__ import annotations

import argparse
import importlib
import inspect
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]

# These exact assertions also failed in the complete pre-change deployment copy.
# Do not add failures here merely to make a later change appear to pass.
KNOWN = {
    # c02/c11 honor rosters are fixed. The next assertion is c09: its same-name
    # conflicting-school evidence already yielded none in the pre-fix snapshot.
    "test_02_fixture_statuses_match_spec": (
        "c09 规则重算得到 none，与存档 who 不一致", "assert recomputed.status == want_status,"),
    "test_03_same_name_different_school_is_who": (
        "", "assert judge.assess(claim, [ev]).needs_human is True"),
    "test_08_search_budget_stops_the_pipeline": ("", "assert exhausted2 is True"),
    "test_30_model_string_instead_of_list_is_recovered_not_crashed": (
        "字符串形式的 supports 不能被逐字符迭代吃掉", 'assert got.supports == ["赛事=某省赛"],'),
    "test_31_content_filtered_query_is_skipped_not_fatal": (
        "改述通过后应正常取到证据", 'assert found, "改述通过后应正常取到证据"'),
}


def worker(name: str, destination: Path) -> int:
    sys.path.insert(0, str(Path.cwd()))
    if name == "legacy":
        module = importlib.import_module("tests.test_rules")
        cases = []
        for case_name, fn in sorted(vars(module).items()):
            if not case_name.startswith("test_") or not callable(fn):
                continue
            started = time.monotonic()
            case = {"name": case_name, "status": "passed"}
            try:
                fn()
            except Exception as exc:  # continue so later legacy cases also run
                frame = traceback.extract_tb(exc.__traceback__)[-1]
                statement = (frame.line or "").rstrip(" \\")
                signature = KNOWN.get(case_name)
                known = (type(exc) is AssertionError and signature is not None
                         and str(exc) == signature[0] and statement.startswith(signature[1])
                         and Path(frame.filename).resolve() == Path(module.__file__).resolve())
                case.update(status="known_failure" if known else "failed",
                            error=type(exc).__name__, message=str(exc), statement=statement,
                            traceback=traceback.format_exc())
            case["seconds"] = round(time.monotonic() - started, 3)
            print(f"{case['status'].upper()}: {case_name}", flush=True)
            cases.append(case)
        result = {"tests": len(cases), "passed": sum(c["status"] == "passed" for c in cases),
                  "known_failures": [c for c in cases if c["status"] == "known_failure"],
                  "failures": [c for c in cases if c["status"] == "failed"], "skipped": [],
                  "cases": cases}
    else:
        suite = unittest.defaultTestLoader.discover("tests")
        # unittest does not collect plain test functions; run future ones too.
        # The legacy module is intentionally executed separately with its baseline.
        for source in sorted(Path("tests").glob("test*.py")):
            if name == "core" and source.stem == "test_rules":
                continue
            # discover(start_dir='tests') imports these as top-level modules.
            # Using 'tests.*' can accidentally select the other project's package.
            module = importlib.import_module(source.stem)
            for function_name, fn in sorted(vars(module).items()):
                if (function_name.startswith("test_") and inspect.isfunction(fn)
                        and fn.__module__ == module.__name__):
                    suite.addTest(unittest.FunctionTestCase(fn))
        tests = unittest.TextTestRunner(verbosity=2).run(suite)
        result = {
            "tests": tests.testsRun,
            "passed": tests.testsRun - len(tests.failures) - len(tests.errors)
                      - len(tests.skipped) - len(tests.expectedFailures)
                      - len(tests.unexpectedSuccesses),
            "failures": [{"name": test.id(), "traceback": detail}
                         for test, detail in tests.failures + tests.errors],
            "skipped": [{"name": test.id(), "reason": reason} for test, reason in tests.skipped],
            "known_failures": [],
            "expected_failures": [test.id() for test, _ in tests.expectedFailures],
            "unexpected_successes": [test.id() for test in tests.unexpectedSuccesses],
        }
        if result["expected_failures"] or result["unexpected_successes"]:
            result["failures"].append({"name": "unreviewed expected/unexpected result"})
    if not result["tests"]:
        result["failures"].append({"name": "empty suite"})
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if result["failures"] else 0


def clean_env(empty_env: Path, temporary_db: Path) -> dict[str, str]:
    prefixes = ("UNDERBOX_", "DEEPSEEK_", "LLM_", "SEARCH_", "ZHIPU_", "BRAVE_", "GLM_")
    env = {k: v for k, v in os.environ.items() if not k.startswith(prefixes)
           and k not in {"OPEN_BOX_ROOT", "PYTHONPATH", "PYTHONOPTIMIZE"}}
    env.update(UNDERBOX_ENV_FILE=str(empty_env), UNDERBOX_DB=str(temporary_db),
               OPEN_BOX_ROOT=str(ROOT / "open_box"), PYTHONUNBUFFERED="1", MODE="mock")
    return env


def run_command(command: list[str], cwd: Path, env: dict, log: Path, timeout: int) -> dict:
    started = time.monotonic()
    with log.open("w", encoding="utf-8") as stream:
        try:
            process = subprocess.run(command, cwd=cwd, env=env, stdout=stream,
                                     stderr=subprocess.STDOUT, timeout=timeout, check=False)
            result = {"returncode": process.returncode}
        except (OSError, subprocess.TimeoutExpired) as exc:
            stream.write(f"\n{type(exc).__name__}: {exc}\n")
            result = {"returncode": -1, "error": str(exc)}
    return {**result, "seconds": round(time.monotonic() - started, 3), "log": str(log)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default=os.environ.get("NODE", "node"))
    parser.add_argument("--output", type=Path, help="Output directory; a timestamped one is the default")
    parser.add_argument("--timeout", type=int, default=600, help="Per-suite timeout in seconds")
    parser.add_argument("--worker", choices=("core", "service", "legacy"), help=argparse.SUPPRESS)
    parser.add_argument("--result", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker, args.result)
    output = (args.output or ROOT / "outputs" / "regression" /
              datetime.now().strftime("%Y%m%d-%H%M%S-%f")).resolve()
    output.mkdir(parents=True, exist_ok=True)
    summary = {"started_at": datetime.now(timezone.utc).isoformat(), "suites": {},
               "browser_checked": False, "live_provider_checked": False, "deployment_checked": False}
    with tempfile.TemporaryDirectory(prefix="underbox-regression-") as temporary:
        temporary = Path(temporary)
        empty_env = temporary / "empty.env"
        empty_env.write_text("", encoding="utf-8")
        env = clean_env(empty_env, temporary / "unused.db")
        for name, directory in (("core", "open_box"), ("service", "underbox"), ("legacy", "open_box")):
            print(f"Running {name}…", flush=True)
            result_file = output / f"{name}.json"
            result_file.unlink(missing_ok=True)
            command = [sys.executable, str(Path(__file__).resolve()), "--worker", name,
                       "--result", str(result_file)]
            run = run_command(command, ROOT / directory, env, output / f"{name}.log", args.timeout)
            # Failed imports/crashes/timeouts cannot be mistaken for an empty passing suite.
            try:
                detail = json.loads(result_file.read_text(encoding="utf-8"))
                if not isinstance(detail, dict) or not detail.get("tests"):
                    raise ValueError("No completed test results")
            except (OSError, ValueError):
                detail = {"failures": [{"name": "missing or incomplete suite results", **run}]}
            if run["returncode"] != 0 and not detail.get("failures"):
                detail["failures"] = [{"name": "suite could not complete", **run}]
            summary["suites"][name] = {**detail, **run}
            print(f"  {detail.get('passed', 0)}/{detail.get('tests', 0)} passed; "
                  f"{len(detail.get('known_failures', []))} known failures; "
                  f"{len(detail.get('failures', []))} new failures; "
                  f"{len(detail.get('skipped', []))} skipped", flush=True)
        node = shutil.which(args.node)
        for file in sorted((ROOT / "underbox" / "tests").glob("test*.cjs")):
            name = file.stem
            print(f"Running {name}…", flush=True)
            run = run_command([node or args.node, str(file)], ROOT, env,
                              output / f"{name}.log", args.timeout)
            summary["suites"][name] = {**run, "failures": [] if run["returncode"] == 0
                                      else [{"name": "frontend check failed", **run}]}
            print("  passed" if run["returncode"] == 0 else "  FAILED", flush=True)
        if not list((ROOT / "underbox" / "tests").glob("test*.cjs")):
            summary["suites"]["frontend"] = {"failures": [{"name": "frontend suite missing"}]}
    new = sum(len(s.get("failures", [])) for s in summary["suites"].values())
    known = sum(len(s.get("known_failures", [])) for s in summary["suites"].values())
    skipped = sum(len(s.get("skipped", [])) for s in summary["suites"].values())
    summary.update(finished_at=datetime.now(timezone.utc).isoformat(), new_failures=new,
                   known_failures=known, skipped=skipped,
                   tests=sum(s.get("tests", 0) for s in summary["suites"].values()),
                   passed=sum(s.get("passed", 0) for s in summary["suites"].values()),
                   status="failed" if new else "incomplete" if skipped
                          else "known_failures" if known else "passed")
    report = output / "summary.json"
    report.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Result: {summary['status']}; report: {report}")
    print("Browser layout and live provider/deployment checks are separate; see REGRESSION.md.")
    return 1 if new or skipped else 2 if known else 0


if __name__ == "__main__":
    raise SystemExit(main())
