"""Small local benchmark; run ``uv run python -m benchmarks.benchmark --count 100``.

Reports medians of repeated runs. Cold includes compilation; warm uses an already
compiled filler. Both include the original model's validation.
No external benchmark or factory dependencies are required.
"""

import argparse
import gc
import platform
import timeit
from statistics import median

import pydantic
import z3

from examples.meeting import Meeting
from modelfill import ModelFiller


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()
    if args.count < 1 or args.repeat < 1:
        parser.error("--count and --repeat must be positive")

    # Distinct class identities prevent the global model cache from hiding the
    # cost of introspection and extraction in the cold measurement.
    def cold():
        for index in range(args.count):
            model = type(f"ColdMeeting{index}", (Meeting,), {})
            ModelFiller(model, seed=42).fill()

    filler = ModelFiller(Meeting, seed=42)
    filler.fill()

    def warm():
        for _ in range(args.count):
            filler.fill()

    # timeit disables garbage collection while timing; setup=gc.enable keeps it on.
    timings = {
        name: median(timeit.repeat(run, setup=gc.enable, number=1, repeat=args.repeat))
        for name, run in {"cold": cold, "warm": warm}.items()
    }
    print(
        f"Python {platform.python_version()}, Pydantic {pydantic.__version__}, "
        f"Z3 {z3.get_version_string()}, {platform.system()} {platform.machine()}"
    )
    print(f"{args.count} instances/run, median of {args.repeat} runs")
    print("Cold includes creating a fresh Pydantic subclass to defeat caching.")
    for name, seconds in timings.items():
        print(
            f"{name:>5}: {seconds * 1000:.2f} ms total, "
            f"{seconds * 1000 / args.count:.3f} ms/instance"
        )


if __name__ == "__main__":
    main()
