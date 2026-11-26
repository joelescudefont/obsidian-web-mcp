"""Read tools for the Obsidian vault MCP server."""

import logging
from collections import Counter

import frontmatter

from .. import config
from ..config import MAX_HEADING_LENGTH, MAX_HEADING_MATCHES
from ..serialization import dumps
from ..vault import (
    MarkdownSection,
    find_sections,
    heading_key,
    markdown_lines,
    markdown_sections,
    read_file,
    resolve_vault_path,
)

logger = logging.getLogger(__name__)

# What a caller does with the outline the switch sent in place of the note.
_AFTER_THE_SWITCH = (
    "Read one section with heading (a path '# A > ## B' when a heading repeats), or the whole "
    "note with whole=true. A section marked over_limit may not fit: read one of its subheadings."
)


def _parsed_frontmatter(content: str):
    try:
        post = frontmatter.loads(content)
        if post.metadata:
            return post.metadata
    except Exception:
        pass
    return None


def _is_markdown(path: str, metadata: dict) -> bool:
    # Extracted text (a PDF, say) has no headings of its own, whatever its name.
    return path.lower().endswith((".md", ".markdown")) and not metadata.get("extracted")


def _section_body(path: str, content: str, metadata: dict, section) -> dict:
    return {
        "path": path,
        "heading": " > ".join(section.path),
        "start_line": section.line,
        "end_line": section.end_line,
        "content": content[section.start:section.end],
        "metadata": metadata,
    }


def _whole_body(path: str, content: str, metadata: dict) -> dict:
    return {
        "path": path,
        "content": content,
        "metadata": metadata,
        "frontmatter": _parsed_frontmatter(content),
    }


def _repeat_reply(path: str, sections, found) -> dict:
    """The error for a heading that names several sections: each match with the path that
    names it, at most MAX_HEADING_MATCHES of them, so a crafted note can't make it huge.

    A match's path names it alone unless another section has the same path, level by level,
    or, for a heading with no parent, the same heading anywhere; unless it is longer than
    heading takes; and unless one of its parts holds " > " itself. Each heading is parsed
    and searched once, by its identity, however many paths hold it.
    """
    keys: dict[int, tuple[int, str]] = {}
    separator: dict[int, bool] = {}

    def key(written: str) -> tuple[int, str]:
        if id(written) not in keys:
            keys[id(written)] = heading_key(written)
            separator[id(written)] = " > " in written
        return keys[id(written)]

    same_path = Counter(tuple(key(h) for h in s.path) for s in sections)
    same_heading = Counter(key(s.heading) for s in sections)
    matches = []
    told_apart = True
    for s in found:
        length = sum(len(h) for h in s.path) + len(" > ") * (len(s.path) - 1)
        if length > MAX_HEADING_LENGTH:
            alone = False
        elif len(s.path) == 1:
            alone = same_heading[key(s.heading)] == 1
        else:
            alone = same_path[tuple(key(h) for h in s.path)] == 1 and not any(separator[id(h)] for h in s.path)
        told_apart = told_apart and alone
        if len(matches) < MAX_HEADING_MATCHES:
            # A path too long to pass back is left out rather than sent.
            named = " > ".join(s.path) if length <= MAX_HEADING_LENGTH else None
            matches.append({"heading": named, "line": s.line, "chars": s.end - s.start})

    error = f"The heading matches {len(found)} sections; pass one of their headings from matches"
    if len(found) > MAX_HEADING_MATCHES:
        error += f" (the first {MAX_HEADING_MATCHES}), or a longer path"
    if not told_apart:
        error += (
            ". Some of them no path tells apart: read the section above them, or the whole note with whole=true"
        )
    return {"error": error, "path": path, "matches": matches}


_EMPTY_SECTION = MarkdownSection(line=0, end_line=0, level=1, heading="", path=(), start=0, end=0)


def _json_length(text: str) -> int:
    """A string's length inside a JSON reply, without its quotes; it adds up over the string's parts."""
    return len(dumps(text)) - 2


def _section_reply_lengths(sections, path: str, content: str, metadata: dict) -> list[int]:
    """Each section's reply length, as len(dumps(_section_body(...))) gives it, in time linear in the note.

    Serialising every section's reply would repeat a parent's heading in each child's path and
    a note's text in each section holding it: under a long parent with many short children
    that grows with both. A string's JSON length adds up over its parts instead, so a section's
    content comes from sums over the note's lines, and its path from each heading's, counted once.
    """
    pieces = content.split("\n")
    upto = [0]  # the JSON length of the note's first i lines, newlines included
    for index, piece in enumerate(pieces):
        upto.append(upto[-1] + _json_length(piece) + (2 if index < len(pieces) - 1 else 0))
    empty = len(dumps(_section_body(path, "", metadata, _EMPTY_SECTION))) - 2  # less its two "0"s
    heading_lengths: dict[int, int] = {}  # by the heading string's identity: parents are shared
    lengths = []
    for s in sections:
        for heading in s.path:
            if id(heading) not in heading_lengths:
                heading_lengths[id(heading)] = _json_length(heading)
        path_length = sum(heading_lengths[id(heading)] for heading in s.path) + len(" > ") * (len(s.path) - 1)
        content_length = upto[s.end_line] - upto[s.line - 1]
        lengths.append(empty + len(str(s.line)) + len(str(s.end_line)) + path_length + content_length)
    return lengths


def _outline(sections, path: str, content: str, metadata: dict) -> list[dict]:
    limit = config.VAULT_MCP_READ_OUTLINE_THRESHOLD_CHARS
    # Section reads don't switch: flag the ones whose reply would pass the limit, measured as
    # the switch measures a note's, so the caller can read a subheading instead.
    lengths = _section_reply_lengths(sections, path, content, metadata) if limit > 0 else None
    entries = []
    for index, s in enumerate(sections):
        entry = {"line": s.line, "heading": s.heading, "chars": s.end - s.start}
        if lengths is not None and lengths[index] > limit:
            entry["over_limit"] = True
        entries.append(entry)
    return entries


def _outline_reply(path: str, content: str, metadata: dict, switched_at: int | None = None) -> dict:
    reply = {"path": path}
    if switched_at is not None:
        reply.update(over_limit=True, limit=switched_at, next=_AFTER_THE_SWITCH)
    reply.update(
        size={"chars": len(content), "lines": len(markdown_lines(content))},
        outline=_outline(markdown_sections(content), path, content, metadata),
        frontmatter=_parsed_frontmatter(content),
        metadata=metadata,
    )
    return reply


def _section_reply(path: str, content: str, metadata: dict, heading: str) -> dict:
    sections = markdown_sections(content)
    found = find_sections(sections, heading)
    if not found:
        if not sections:
            return {"error": "The note has no headings, so it has no sections to read", "path": path, "outline": []}
        outline = _outline(sections, path, content, metadata)
        if len(dumps(outline)) >= len(dumps(_whole_body(path, content, metadata))):
            # A note of little but headings: its outline would outweigh the note itself.
            return {
                "error": (
                    "No heading matches, and the outline would be bigger than the note: "
                    "read the whole note with whole=true"
                ),
                "path": path,
            }
        return {"error": "No heading matches; the outline lists the note's headings", "path": path, "outline": outline}
    if len(found) > 1:
        return _repeat_reply(path, sections, found)
    return _section_body(path, content, metadata, found[0])


def vault_read(path: str, heading: str | None = None, outline: bool = False, whole: bool = False) -> str:
    """Read a file from the vault, returning content, metadata, and parsed frontmatter.

    With ``heading``, only the section it names, its subheadings included; with
    ``outline``, the note's headings, each with its line and its section's size, instead
    of its content. Both only on a Markdown note.

    With VAULT_MCP_READ_OUTLINE_THRESHOLD_CHARS set, a plain read of a Markdown note whose reply
    would pass it returns the outline instead, unless ``whole`` is set; a note with no
    headings, or one whose outline would be no smaller than the note (a note of little
    but headings), has nothing smaller to offer and comes back whole.
    """
    try:
        resolved = resolve_vault_path(path)
        content, metadata = read_file(path, extract=True)

        if heading is not None or outline:
            if not _is_markdown(path, metadata):
                return dumps({"error": "Headings exist only in Markdown notes", "path": path})
            if outline:
                return dumps(_outline_reply(path, content, metadata))
            return dumps(_section_reply(path, content, metadata, heading))

        reply = dumps(_whole_body(path, content, metadata))
        limit = config.VAULT_MCP_READ_OUTLINE_THRESHOLD_CHARS
        if whole or limit <= 0 or len(reply) <= limit or not _is_markdown(path, metadata):
            return reply
        switched = _outline_reply(path, content, metadata, switched_at=limit)
        outline_reply = dumps(switched)
        return outline_reply if switched["outline"] and len(outline_reply) < len(reply) else reply
    except ValueError as e:
        return dumps({"error": str(e), "path": path})
    except FileNotFoundError:
        return dumps({"error": f"File not found: {path}", "path": path})
    except Exception as e:
        logger.error(f"vault_read error for {path}: {e}")
        return dumps({"error": str(e), "path": path})


def vault_batch_read(paths: list[str], include_content: bool = True) -> str:
    """Read multiple files from the vault in one call."""
    results = []
    found = 0
    missing = 0

    for path in paths:
        try:
            content, metadata = read_file(path, extract=True)

            fm_data = None
            try:
                post = frontmatter.loads(content)
                if post.metadata:
                    fm_data = post.metadata
            except Exception:
                pass

            entry = {
                "path": path,
                "metadata": metadata,
                "frontmatter": fm_data,
            }
            if include_content:
                entry["content"] = content

            results.append(entry)
            found += 1
        except (ValueError, FileNotFoundError) as e:
            results.append({"path": path, "error": str(e)})
            missing += 1
        except Exception as e:
            results.append({"path": path, "error": str(e)})
            missing += 1

    return dumps({"files": results, "found": found, "missing": missing})
