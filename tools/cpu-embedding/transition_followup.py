"""Serial query/watcher measurements after the full private E5 index completes.

Requires psutil only in this supervisor, not in the measured environments.
"""

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time

import psutil


def measured(command, root, name, env):
    observed, peak, peak_processes = {}, 0, 0
    with (root / f"{name}.log").open("w") as log:
        child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env)
        process = psutil.Process(child.pid)
        started = time.monotonic()
        try:
            while child.poll() is None:
                if time.monotonic() - started > 1800:
                    raise TimeoutError("Owned measurement exceeded 30 minutes")
                try:
                    tree = [process, *process.children(recursive=True)]
                    rss = 0
                    for member in tree:
                        try:
                            rss += member.memory_info().rss
                            cpu = member.cpu_times()
                            observed[(member.pid, member.create_time())] = cpu.user + cpu.system
                        except psutil.NoSuchProcess:
                            continue
                    peak = max(peak, rss)
                    peak_processes = max(peak_processes, len(tree))
                except psutil.NoSuchProcess:
                    pass
                time.sleep(0.05)
            code = child.wait()
        finally:
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
                try:
                    child.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    # Only descendants of this exact owned process are eligible.
                    descendants = process.children(recursive=True)
                    for member in descendants:
                        try:
                            member.terminate()
                        except psutil.NoSuchProcess:
                            pass
                    _, alive = psutil.wait_procs(descendants, timeout=10)
                    for member in alive:
                        member.kill()
                    psutil.wait_procs(alive, timeout=10)
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
    report = {
        "exit_code": code,
        "wall_s": time.monotonic() - started,
        "sampled_tree_cpu_s": sum(observed.values()),
        "sampled_tree_peak_rss_bytes": peak,
        "max_processes": peak_processes,
        "sample_interval_s": 0.05,
        "rss_note": "RSS sum includes shared pages; not exclusive physical RAM",
    }
    (root / f"{name}-resources.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"completed": name, **report}), flush=True)
    if code:
        raise RuntimeError(f"Measurement failed: {name}; inspect private log")


def main(args):
    root = args.snapshot.resolve()
    deadline = time.monotonic() + 3600
    while not (root / "full-C/result.json").exists():
        if time.monotonic() > deadline:
            raise TimeoutError("Full E5 index did not complete")
        time.sleep(2)
    result = json.loads((root / "full-C/result.json").read_text())
    if result.get("errors"):
        raise RuntimeError("Full E5 index has errors; followup refused")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("MEMTOMEM_") and key != "PYTHONPATH"
    }
    env.update(
        MEMTOMEM_FASTEMBED_CACHE=args.cache, HF_HUB_OFFLINE="1", TOKENIZERS_PARALLELISM="false"
    )
    tools = Path(__file__).resolve().parent
    for profile, python in [("B", args.after_python), ("C", args.after_python)]:
        name = f"queries-runtime-{profile}"
        if (root / name / "result.json").exists():
            print(json.dumps({"reuse_completed": name}), flush=True)
            continue
        command = [
            python,
            str(tools / "transition_query_worker.py"),
            "--snapshot",
            str(root),
            "--profile",
            profile,
            "--output",
            str(root / name),
        ]
        measured(command, root, name, env)
    for profile, python in [
        ("A", args.before_python),
        ("B", args.after_python),
        ("C", args.after_python),
    ]:
        for count in [1, 3]:
            name = f"watchers-initialized-{profile}-{count}"
            command = [
                python,
                str(tools / "transition_watchers.py"),
                "--snapshot",
                str(root),
                "--profile",
                profile,
                "--clients",
                str(count),
                "--output",
                str(root / name),
            ]
            measured(command, root, name, env)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--before-python", required=True)
    parser.add_argument("--after-python", required=True)
    parser.add_argument("--cache", required=True)
    main(parser.parse_args())
