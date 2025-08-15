#!/usr/bin/env python3
"""
check_duplicate_lines.py

Rask sjekk av duplikate linjer i en tekstfil (linjebasert ordliste).

Standard antar en ordliste som `banned_words.txt` der tomme linjer og linjer
som starter med '#' er kommentarer – disse kan filtreres bort med flagg.

Bruk:
  python check_duplicate_lines.py banned_words.txt

Valg:
  --ignore-case     : Sammenlign linjer case-insensitive
  --strip           : Trim whitespace i begge ender før sammenligning
  --keep-comments   : Ta med kommentarlinjer (#...) i duplikatsjekk
  --keep-empty      : Ta med tomme linjer i duplikatsjekk
  --in-place        : Skriv tilbake renset fil (første forekomst beholdes)
  --sort            : Når --in-place, sorter unike linjer alfabetisk
  --reverse         : Når --in-place + --sort, sorter synkende
  --report-only     : (default) Bare vis funn, ikke skriv fil
    --fix             : Shortcut: samme som --in-place --sort (lager .bak backup først)

Eksempel:
  python check_duplicate_lines.py banned_words.txt --ignore-case --strip
  python check_duplicate_lines.py banned_words.txt --in-place --strip --ignore-case

Avslutter med exit code 0 hvis ingen duplikater, ellers 1 (nyttig i CI).
"""
from __future__ import annotations
import argparse, sys, pathlib

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Finn duplikate linjer i fil")
    p.add_argument("file", help="banned_words.txt")
    p.add_argument("--ignore-case", action="store_true", dest="ignore_case", help="Case-insensitive sammenligning")
    p.add_argument("--strip", action="store_true", help="Trim leading/trailing whitespace før sammenligning")
    p.add_argument("--keep-comments", action="store_true", help="Inkluder linjer som starter med # i duplikatsjekk")
    p.add_argument("--keep-empty", action="store_true", help="Inkluder tomme linjer i duplikatsjekk")
    p.add_argument("--in-place", action="store_true", help="Skriv tilbake filen uten duplikater")
    p.add_argument("--sort", action="store_true", help="Sorter unike linjer alfabetisk ved --in-place")
    p.add_argument("--reverse", action="store_true", help="Reverser sorteringen ved --sort")
    p.add_argument("--report-only", action="store_true", help="Bare rapporter (default hvis --in-place ikke er satt)")
    p.add_argument("--fix", action="store_true", help="Auto: in-place dedupe + sort (lager backup .bak)")
    return p.parse_args()

def normalize(line: str, strip: bool, ignore_case: bool) -> str:
    if strip:
        line = line.strip()
    return line.lower() if ignore_case else line

def main():
    args = parse_args()
    # Expand --fix into composite flags
    if args.fix:
        args.in_place = True
        args.sort = True
        # Om bruker ikke eksplisitt satte strip/ignore-case, gi fornuftig default
        if not args.ignore_case:
            args.ignore_case = True
        if not args.strip:
            args.strip = True
    path = pathlib.Path(args.file)
    if not path.exists():
        print(f"ERROR: Filen finnes ikke: {path}", file=sys.stderr)
        return 2
    raw_lines = path.read_text(encoding="utf-8").splitlines()

    seen = {}
    duplicates = []  # (linjenummer, original_line)

    processed = []  # (original_line, norm_line)
    for idx, original in enumerate(raw_lines, start=1):
        norm_candidate = original
        if not args.keep_comments and original.lstrip().startswith('#'):
            # Hopp over kommentarer fra sjekk men behold dem i output hvis in-place
            processed.append((original, None))
            continue
        if not args.keep_empty and (original.strip() == ""):
            processed.append((original, None))
            continue
        norm = normalize(norm_candidate, args.strip, args.ignore_case)
        if norm in seen:
            duplicates.append((idx, original))
        else:
            seen[norm] = idx
        processed.append((original, norm))

    if duplicates:
        print(f"Fant {len(duplicates)} duplikat-linje(r):")
        for ln, txt in duplicates:
            first = seen[normalize(txt if args.keep_empty else txt, args.strip, args.ignore_case)]
            print(f"  Linje {ln} (dupe av linje {first}): {txt}")
    else:
        print("Ingen duplikater funnet.")

    # Hvis bruker ønsker sortert liste, men ikke nødvendigvis endre filen.
    if args.sort and not args.in_place:
        # Bygg unik liste på samme måte som i in-place logikk, men bare vis.
        unique_order = []
        added = set()
        for original, norm in processed:
            if norm is None:
                # Hopp over kommentarer / blanke i visning? Vi viser dem øverst i original rekkefølge.
                continue
            if norm in added:
                continue
            added.add(norm)
            unique_order.append(original.strip() if args.strip else original)
        unique_order.sort(key=lambda s: s.lower() if args.ignore_case else s)
        print("\nSorter A-Z (unike linjer):")
        for line in unique_order:
            print(line)

    if args.in_place:
        # Lag backup hvis --fix (eller eksplisitt ønskes kan utvides senere)
        if args.fix:
            bak = path.with_suffix(path.suffix + ".bak")
            if not bak.exists():
                bak.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")
                print(f"[Backup] Lagret original til {bak.name}")
        # Bygg ny liste: behold første forekomst etter valgte filterregler
        unique_order = []
        added = set()
        for original, norm in processed:
            if norm is None:
                # Kommentar eller tom linje (behold som er)
                unique_order.append(original)
                continue
            if norm in added:
                continue
            added.add(norm)
            unique_order.append(original.strip() if args.strip else original)
        if args.sort:
            # Bevar kommentarer og blanke linjer i relative posisjon? For enkelhet: flytt dem øverst uendret.
            comments_or_blank = [l for l in unique_order if (l.strip() == '' or l.lstrip().startswith('#'))]
            words = [l for l in unique_order if not (l.strip() == '' or l.lstrip().startswith('#'))]
            words.sort(reverse=args.reverse, key=lambda s: s.lower() if args.ignore_case else s)
            unique_order = comments_or_blank + words
        new_text = "\n".join(unique_order) + "\n"
        if new_text != path.read_text(encoding="utf-8"):
            path.write_text(new_text, encoding="utf-8")
            print(f"[Skrevet] Oppdatert fil uten duplikater: {path}")
        else:
            print("Ingen endring nødvendig (fil allerede deduplisert).")

    # Exit code 1 hvis duplikater funnet (nyttig i CI)
    return 1 if duplicates else 0

if __name__ == "__main__":
    raise SystemExit(main())
