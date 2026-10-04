import sys
from pathlib import Path
source, target = map(Path, sys.argv[1:3])
managed = [source/name for name in (source/'scripts/deployment_files.txt').read_text().splitlines()]
stale = [str(p.relative_to(source)) for p in managed
         if (target/p.relative_to(source)).is_file()
         and p.read_bytes() != (target/p.relative_to(source)).read_bytes()]
if stale:
    raise SystemExit('Managed target files differ. Review your customizations and rerun with --update-files to back up and replace them:\n' + '\n'.join(stale))
