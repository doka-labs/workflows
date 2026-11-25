"""Links of the documents as the site shows them.

A relative link in a document of the repository names a file of the checkout. The site holds only the documents
under docs/, their images and the README as index.md, so a link to any other file of the repository, such as a
source file, a test or LICENSE, is shown as a GitHub link to that file at the released tag. Links to documents of
the site stay relative, a link to README.md becomes a link to index.md, and a link to nothing stays as written so
that DocFX reports it. Text in code blocks and code spans is never changed.

WHY: DocFX resolves a relative link only to a file of its build and warns with InvalidFileLink otherwise; it can
copy a file into the build as a resource, but it has no setting that points a link to the repository instead.
Projects that build with DocFX (Polly, BenchmarkDotNet, Akka.NET, DocFX, NUnit) link source in their documents as
absolute GitHub URLs. The documents here keep relative links, because madr-check and as-built read them in the
checkout, so this module writes the GitHub URL into the copy the site is built from.
https://dotnet.github.io/docfx/docs/links-and-cross-references.html#link-to-local-files
https://dotnet.github.io/docfx/docs/links-and-cross-references.html#links-to-mapped-resources
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote

# WHY: The site config builds docs/**/*.md and copies docs/**/*.png and docs/**/*.svg; only these files exist there.
SITE_SUFFIXES = (".md", ".png", ".svg")
IMAGE_SUFFIXES = (".png", ".svg")
# WHY: A fence is at least three backticks or tildes, indented by at most three spaces; the closing fence uses the
# same character, is at least as long as the opening one, and holds nothing else.
# https://spec.commonmark.org/0.31.2/#fenced-code-blocks
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
# WHY: A code span begins and ends with backtick strings of equal length; it may run over a line break but not over
# a blank line, because a blank line ends the paragraph whose inlines it belongs to. Without that bound a single
# stray backtick would hide every link of the following paragraphs.
# https://spec.commonmark.org/0.31.2/#code-spans
# https://spec.commonmark.org/0.31.2/#paragraphs
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)((?:(?!\n[ \t]*\n)[\s\S])+?)(?<!`)\1(?!`)")
# WHY: Link text may hold balanced brackets, escapes and line breaks; the destination is either <...> without a line
# break or a run without spaces and parentheses, followed by an optional quoted title.
# https://spec.commonmark.org/0.31.2/#inline-link
# https://spec.commonmark.org/0.31.2/#link-destination
INLINE = re.compile(r"(?P<image>!?)\[(?:[^\[\]\\]|\\.|\[[^\[\]]*\])*\]"
                    r"\((?P<destination><[^>\n]*>|[^()\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
# WHY: A link reference definition starts a line, indented by at most three spaces; links that use its label take
# its destination. The Doka repositories use them, the MySql README most of all.
# https://spec.commonmark.org/0.31.2/#link-reference-definitions
DEFINITION = re.compile(r"^ {0,3}\[[^\]\n]+\]:[ \t]*(?P<destination><[^>\n]*>|\S+)", re.MULTILINE)
# WHY: DocFX resolves [!code...] and [!INCLUDE...] relative to the document on disk; the copy lies elsewhere, so the
# path would break. No Doka repository uses them, so the site refuses them instead of guessing.
# https://dotnet.github.io/docfx/docs/markdown.html#code-snippet
EMBED = re.compile(r"\[!(?:code|INCLUDE)")
# WHY: A destination with a scheme (https:, mailto:) is absolute and leaves the repository by design.
# https://www.rfc-editor.org/rfc/rfc3986#section-3.1
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


class LinkError(RuntimeError):
    """A document holds a link the site cannot show; the message names the file, the line and the fix."""


def in_site(path: PurePosixPath) -> bool:
    """Return whether a repository path is part of the site: the README and the documents and images under docs/."""
    return path == PurePosixPath("README.md") or (path.parts[:1] == ("docs",) and path.suffix in SITE_SUFFIXES)


def target(destination: str, source: PurePosixPath, context: dict) -> str:
    """Return the destination of one link as the site shows it."""
    bare = destination[1:-1] if destination.startswith("<") else destination
    # WHY: Anchors, root paths, DocFX ~/ paths and absolute URLs do not name a file relative to the document.
    if not bare or bare.startswith(("#", "/", "~")) or SCHEME.match(bare):
        return destination

    cut = min((index for index in (bare.find("#"), bare.find("?")) if index >= 0), default=len(bare))
    path, rest = bare[:cut], bare[cut:]
    if not path:
        return destination

    # WHY: A destination may percent-encode characters such as spaces; the file on disk has the decoded name.
    resolved = posixpath.normpath(posixpath.join(source.parent.as_posix(), unquote(path)))
    where = f"{source}:{context['line']}"
    if resolved == ".." or resolved.startswith("../"):
        raise LinkError(f"{where}: '{bare}' leaves the repository.")

    repository_path = PurePosixPath(resolved)
    if in_site(repository_path):
        if repository_path != PurePosixPath("README.md"):
            return destination

        # WHY: The site publishes README.md as its start page index.md.
        index = posixpath.relpath("index.md", source.parent.as_posix())
        return _wrap(destination, index + rest)

    on_disk = context["root"] / resolved
    # WHY: A GitHub blob URL is an HTML page, not an image, so an image outside the site would not show.
    if context["image"] and on_disk.exists():
        raise LinkError(f"{where}: the image '{bare}' must lie under docs/ as {' or '.join(IMAGE_SUFFIXES)}.")

    if on_disk.is_file():
        kind = "blob"
    elif on_disk.is_dir():
        kind = "tree"
    else:
        # WHY: A link to nothing stays as written, so DocFX reports it and the build with warnings as errors fails.
        return destination

    # WHY: blob/<ref>/<path> shows a file and tree/<ref>/<path> a directory at that ref; the released tag keeps the
    # link on the code of this release instead of the moving head of a branch.
    # https://docs.github.com/en/repositories/working-with-files/using-files/getting-permanent-links-to-files
    url = f"https://github.com/{context['repository']}/{kind}/{context['tag']}/{quote(resolved)}{rest}"
    return _wrap(destination, url)


def _wrap(destination: str, value: str) -> str:
    """Keep the angle brackets of a destination that had them."""
    return f"<{value}>" if destination.startswith("<") else value


def _masked(text: str) -> str:
    """Return the text with fenced code blocks and code spans blanked, keeping every position and line break.

    The links are searched in the masked text and replaced in the original, so equal positions are required.
    """
    lines = text.split("\n")
    fence = None
    for number, line in enumerate(lines):
        opening = FENCE.match(line)
        if fence is not None:
            if opening and opening[1][0] == fence[0] and len(opening[1]) >= len(fence) and not line.strip(" `~"):
                fence = None
            lines[number] = "\0" * len(line)
            continue

        if opening:
            fence = opening[1]
            lines[number] = "\0" * len(line)

    return CODE_SPAN.sub(lambda span: span[1] + "".join(c if c == "\n" else "\0" for c in span[2]) + span[1],
                         "\n".join(lines))


def rewrite(text: str, source: PurePosixPath, root: Path, repository: str, tag: str) -> str:
    """Return a document of the repository with every link as the site shows it.

    The text is searched as a whole, so a link whose text runs over several lines is found too.
    """
    masked = _masked(text)
    embedded = EMBED.search(masked)
    if embedded:
        number = masked.count("\n", 0, embedded.start()) + 1
        raise LinkError(f"{source}:{number}: an embedded file is not supported by the site; link to it instead.")

    found = [(match, False) for match in DEFINITION.finditer(masked)]
    found += [(match, match["image"] == "!") for match in INLINE.finditer(masked)]
    changes = []
    for match, image in found:
        start, end = match.span("destination")
        context = {"root": root, "repository": repository, "tag": tag, "image": image,
                   "line": masked.count("\n", 0, start) + 1}
        changes.append((start, end, target(text[start:end], source, context)))

    # WHY: Replacing from the end keeps the positions of the earlier links valid.
    for start, end, value in sorted(changes, reverse=True):
        text = text[:start] + value + text[end:]
    return text
