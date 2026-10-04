from pathlib import Path

api = Path("api/src/state.txt").read_text(encoding="utf-8")
client = Path("client/src/state.txt").read_text(encoding="utf-8")
schema = Path("schemas/contract.txt").read_text(encoding="utf-8")

if "contract=ready\n" not in api or "contract=ready\n" not in client:
    raise SystemExit("api and client must both declare contract=ready")
if schema != "contract=ready\n":
    raise SystemExit("reference schema changed")
