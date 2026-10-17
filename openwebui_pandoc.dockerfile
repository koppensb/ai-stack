# syntax=docker/dockerfile:1.7
ARG OPENWEBUI_VERSION=latest
FROM openwebui/open-webui:${OPENWEBUI_VERSION}

# Keep the upstream Python wrapper and dependencies unchanged. Install only the
# matching binary wheel into an isolated directory, outside Python's import path.
RUN python - <<'PY_INSTALL'
from importlib.metadata import version
from pathlib import Path
import subprocess
import sys

wrapper_version = version("pypandoc")
requirement = f"pypandoc-binary=={wrapper_version}"
print(f"Installing {requirement} for the upstream Pypandoc wrapper", flush=True)
subprocess.run([
    sys.executable, "-m", "pip", "install", "--no-cache-dir", "--no-deps",
    "--only-binary=:all:", "--target", "/opt/ai-stack-pandoc", requirement,
], check=True)
binary = Path("/opt/ai-stack-pandoc/pypandoc/files/pandoc")
if not binary.is_file():
    raise RuntimeError(f"The matching wheel did not provide the expected binary: {binary}")
PY_INSTALL

# Never fall back to the distribution's Pandoc, whose external data files may
# be inaccessible in sandbox mode. The package version and Pandoc version differ.
ENV PYPANDOC_PANDOC=/opt/ai-stack-pandoc/pypandoc/files/pandoc

RUN python - <<'PY_VERIFY'
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile
import pypandoc

with TemporaryDirectory() as temporary:
    root = Path(temporary)
    source = root / "probe.md"
    odt = root / "probe.odt"
    docx = root / "probe.docx"
    marker = "AI stack Pandoc conversion check"
    source.write_text(marker, encoding="utf-8")
    pypandoc.convert_file(str(source), "odt", format="markdown",
                          outputfile=str(odt), sandbox=True)
    pypandoc.convert_file(str(odt), "docx", format="odt",
                          outputfile=str(docx), sandbox=True)
    with ZipFile(docx) as archive:
        if "[Content_Types].xml" not in archive.namelist():
            raise RuntimeError("Pandoc produced a DOCX without content types")
    text = pypandoc.convert_file(str(docx), "plain", format="docx", sandbox=True)
    if marker not in text:
        raise RuntimeError("Pandoc did not preserve text in the ODT/DOCX round trip")
print(f"Verified sandboxed ODT/DOCX conversion with Pandoc {pypandoc.get_pandoc_version()}")
PY_VERIFY
