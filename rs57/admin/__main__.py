"""Run the admin tool.

    python -m rs57.admin                # http://127.0.0.1:5057
    python -m rs57.admin --no-push      # the commit button commits but never pushes
    python -m rs57.admin --port 5100

Binds ``127.0.0.1`` and not ``0.0.0.0``. There are no accounts and no authentication — the tool
assumes whoever reaches it is the commissioner — so it must not be reachable from the network.
Anyone who needs it on another machine should tunnel to it rather than have this listen wider.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from rs57.admin import ROOT, create_app
from rs57.admin.derived import DERIVED, Derived
from rs57.admin.store import DATA


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The RS57 local admin tool.")
    parser.add_argument("--port", type=int, default=5057)
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="the commit button commits locally and never pushes",
    )
    parser.add_argument("--data", type=Path, default=DATA, help="the data/ directory to use")
    parser.add_argument("--debug", action="store_true", help="Flask reloader and tracebacks")
    args = parser.parse_args(argv)

    data_dir = args.data.resolve()
    derived_dir = data_dir / "derived" if args.data != DATA else DERIVED

    seasons = Derived(derived_dir=derived_dir).seasons()
    print(f"data:    {data_dir}")
    print(f"writes:  {data_dir / 'manual'} — and nothing else")
    if seasons:
        print(f"seasons: {', '.join(str(year) for year in seasons)} (from data/derived/)")
    else:
        print("seasons: none synced yet — run `python -m rs57.sync --year <yr>` first")
    if args.no_push:
        print("commit:  local only, --no-push")
    else:
        print("commit:  commits and pushes data/manual/ after you read the diff")
    print(f"\n  http://127.0.0.1:{args.port}\n")

    app = create_app(
        data_dir=data_dir,
        derived_dir=derived_dir,
        repo=ROOT,
        push=not args.no_push,
    )
    # Localhost only: no accounts, no authentication, and the commit button pushes to a public
    # repo. This is not a service to expose.
    app.run(host="127.0.0.1", port=args.port, debug=args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
