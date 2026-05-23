from pathlib import Path

HERE = Path(__file__).parent


def load(name: str) -> str:
    return (HERE / name).read_text(encoding="utf-8")
