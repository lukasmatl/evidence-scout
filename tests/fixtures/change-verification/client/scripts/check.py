from pathlib import Path

state = Path("src/state.txt").read_text(encoding="utf-8")
if not state.startswith("client=1\n"):
    raise SystemExit("client state is invalid")
