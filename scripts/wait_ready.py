"""Block until every Deployment and StatefulSet in a namespace has finished rolling out."""

import argparse
import subprocess
import sys
import time


def kubectl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["kubectl", *args], capture_output=True, text=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("namespace")
    parser.add_argument("--timeout", type=int, default=600, help="overall seconds to wait")
    args = parser.parse_args()

    listing = kubectl("get", "deployments,statefulsets", "-n", args.namespace, "-o", "name")
    if listing.returncode != 0:
        sys.exit(listing.stderr.strip())
    workloads = listing.stdout.split()
    if not workloads:
        sys.exit(f"no deployments or statefulsets in namespace {args.namespace!r}")

    deadline = time.monotonic() + args.timeout
    for workload in workloads:
        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            sys.exit(f"timed out before checking {workload}")
        print(f"waiting for {args.namespace}/{workload} (up to {remaining}s)...", flush=True)
        status = kubectl(
            "rollout", "status", workload, "-n", args.namespace, f"--timeout={remaining}s"
        )
        if status.returncode != 0:
            sys.exit(f"{workload} not ready: {status.stderr.strip() or status.stdout.strip()}")

    print(f"all {len(workloads)} workloads in {args.namespace} are ready")


if __name__ == "__main__":
    main()
