"""One-off: generate lpr_keywords_industry.py from plan markdown section 7."""

from __future__ import annotations

from pathlib import Path

PLAN = Path(r"c:\Users\ventilator\.cursor\plans\анализ_полей_лпр_e01699eb.plan.md")
OUT = Path(__file__).resolve().parents[1] / "app" / "services" / "lpr_keywords_industry.py"


def main() -> None:
    text = PLAN.read_text(encoding="utf-8")
    marker = "## 7. Рекомендуемый список ключевых слов ЛПР"
    if marker not in text:
        raise SystemExit(f"Section 7 not found in {PLAN}")
    text = text.split(marker, 1)[1]
    in_block = False
    keywords: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "```":
            in_block = not in_block
            continue
        if in_block and stripped and not stripped.startswith("#"):
            kw = stripped
            if "НИИПИ" in kw and "rector" in kw:
                kw = "Директор НИИПИ"
            keywords.append(kw)

    seen: set[str] = set()
    deduped: list[str] = []
    for k in keywords:
        key = k.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(k)

    lines = [
        '"""Industry-specific LPR keyword phrases (municipal / genplan sector)."""',
        "",
        "INDUSTRY_LPR_KEYWORDS: list[str] = [",
    ]
    for k in deduped:
        lines.append(f"    {k!r},")
    lines.append("]")
    lines.append("")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {len(deduped)} keywords to {OUT}")


if __name__ == "__main__":
    main()
