"""Windows reads text files in the system code page unless told otherwise (before
Python 3.15), so a UTF-8 scenario with a degree sign would load garbled there. A Mac
never shows the problem, so check the source instead: every text read names its
encoding."""

import re
from pathlib import Path

SRC = Path(__file__).parent.parent.parent / "src" / "der_opt"
TEXT_IO = re.compile(r"\.(read_text|write_text)\(|(?<![\w.])open\(")
BINARY_MODE = re.compile(r"open\([^)]*['\"][rwax+]*b")


def _unspecified(line: str) -> bool:
    """A text read or write on this line with no encoding= after it (binary opens and
    archive/pdf .open() calls are exempt)."""
    match = TEXT_IO.search(line)
    return bool(match) and "encoding=" not in line[match.start():] and not BINARY_MODE.search(line)


def test_every_text_read_names_its_encoding():
    offenders = [
        f"{path.relative_to(SRC)}:{number}: {line.strip()}"
        for path in SRC.rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _unspecified(line)
    ]
    assert not offenders, "add encoding=\"utf-8\":\n" + "\n".join(offenders)


def test_a_utf8_scenario_with_non_ascii_text_loads(tmp_path):
    from der_opt.scenarios import load_scenario_config

    fixture = Path(__file__).parent.parent / "fixtures" / "scenario_basic.yaml"
    text = "# heat wave, 41 °C — résumé of the run\n" + fixture.read_text(encoding="utf-8")
    path = tmp_path / "scenario.yaml"
    path.write_bytes(text.encode("utf-8"))
    assert load_scenario_config(path).name
