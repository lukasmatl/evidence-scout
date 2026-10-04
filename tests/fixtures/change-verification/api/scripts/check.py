from pathlib import Path

state = Path("src/state.txt").read_text(encoding="utf-8")
if not state.startswith("api=1\n"):
    raise SystemExit("api state is invalid")
