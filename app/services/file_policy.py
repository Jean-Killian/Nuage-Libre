"""Business rules about cloud files: names, types and safe preview.

Pure functions with no I/O (no database, no disk, no HTTP), so each rule can
be unit-tested on its own (tests/test_file_policy.py) and reused by the
service and by the API schemas without duplication.
"""

import mimetypes
import re
import unicodedata

# Same limit as the files.filename column (VARCHAR(255)).
MAX_FILENAME_LENGTH = 255
# Same limit as the folders.name column (VARCHAR(255)).
MAX_FOLDER_NAME_LENGTH = 255
# How deep folders can be nested (a folder at the root has depth 1). Real
# trees rarely go past 10 levels; the limit keeps the recursive "path of a
# folder" query bounded and stops a script from building an absurd chain.
MAX_FOLDER_DEPTH = 32
# When a name is too long, the extension is kept (it tells the user, and the
# server, what the file is) as long as it is a plausible one.
_MAX_KEPT_EXTENSION = 16

# Unicode categories removed from names: control characters (Cc: NUL, CR,
# LF, TAB, DEL, C1 codes...) and line/paragraph separators. CR/LF in a name
# could otherwise break the Content-Disposition header of the download
# (header injection) or the layout of the file list.
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})
# Bidirectional controls: U+202E (RIGHT-TO-LEFT OVERRIDE) can make
# "invoice\u202Efdp.exe" DISPLAY as "invoiceexe.pdf", a classic trick to
# disguise a dangerous file. They have no legitimate use in a file name.
_BIDI_CONTROLS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")

# mimetypes reports compression separately ("x.tar.gz" -> x-tar + gzip).
# The bytes ARE compressed, so the compression format is the real type.
_COMPRESSION_TYPES = {
    "gzip": "application/gzip",
    "bzip2": "application/x-bzip2",
    "xz": "application/x-xz",
    "br": "application/x-brotli",
    "compress": "application/x-compress",
}

GENERIC_TYPE = "application/octet-stream"

# Types a browser can display "inline" WITHOUT running any script in the
# page's origin. Deliberately absent: text/html, image/svg+xml, XML-based
# formats... An HTML or SVG file opened inline from our server would run its
# JavaScript as if it were Nuage Libre itself (stored XSS): it could call the
# API and read or upload files. Those types are always downloaded instead.
_INLINE_AS_IS = frozenset(
    {
        # Raster images (no scripting possible).
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/avif",
        "image/bmp",
        # PDF: rendered by the browser's own sandboxed viewer (pdf.js in
        # Firefox, PDFium in Chromium), never by the page itself.
        "application/pdf",
        # Audio / video, played by the browser's native media player.
        "audio/mpeg",
        "audio/ogg",
        "audio/wav",
        "audio/x-wav",
        "audio/webm",
        "audio/flac",
        "audio/aac",
        "audio/mp4",
        "video/mp4",
        "video/webm",
        "video/ogg",
    }
)
# Text formats that are shown as plain text when viewed: the source code of
# an .html, .md, .py or .json file is displayed, never interpreted.
_TEXT_LIKE = frozenset({"application/json", "application/xml"})


class InvalidFileNameError(ValueError):
    """The proposed file name is empty or meaningless once cleaned."""


class InvalidFolderNameError(ValueError):
    """The proposed folder name is empty, reserved, too long or contains a separator."""


def _without_invisible_characters(name: str) -> str:
    """Remove control, line/paragraph separator and bidirectional characters."""
    return "".join(
        char
        for char in name
        if unicodedata.category(char) not in _FORBIDDEN_CATEGORIES and char not in _BIDI_CONTROLS
    )


def clean_filename(raw: str) -> str:
    """Turn the name sent by the client into a safe DISPLAY name.

    This name is never used on disk (see FileStorage), so the goal is not
    to block path traversal but to store a name that is tidy, unambiguous
    and safe to display and to put back in an HTTP header:

    1. Unicode NFC normalisation. macOS (the MacBook client) sends names in
       decomposed form ("e" + combining accent, NFD); NFC stores "é" as one
       character, so identical-looking names are really identical.
    2. Only the last path component is kept: some clients send
       "C:\\Users\\me\\report.pdf" or "../../report.pdf".
    3. Control, separator and bidirectional characters are removed.
    4. Surrounding whitespace is stripped; "", "." and ".." are refused.
    5. Names longer than 255 characters are shortened, keeping the extension.
    """
    name = unicodedata.normalize("NFC", raw)
    name = re.split(r"[\\/]", name)[-1]
    name = _without_invisible_characters(name).strip()
    if name in {"", ".", ".."}:
        raise InvalidFileNameError("The file name is empty once cleaned.")
    if len(name) > MAX_FILENAME_LENGTH:
        stem, dot, extension = name.rpartition(".")
        if dot and stem and len(extension) <= _MAX_KEPT_EXTENSION:
            name = stem[: MAX_FILENAME_LENGTH - len(extension) - 1].rstrip() + "." + extension
        else:
            name = name[:MAX_FILENAME_LENGTH].rstrip()
    return name


def clean_folder_name(raw: str) -> str:
    """Turn a folder name sent by the client into a safe DISPLAY name.

    Folders only exist in the database (see the Folder model): their names
    never reach the disk, so, as for files, the point is a tidy and
    unambiguous name, not path traversal. Same hygiene as clean_filename
    (NFC, invisible characters removed, spaces stripped), with one
    difference: a name containing "/" or "\\" is REFUSED rather than cut.
    A browser may add a path in front of a FILE name, which is then safely
    dropped; a folder name is typed by a person, and silently turning
    "Courses/2026" into "2026" would surprise them. Both characters are the
    path separators of every operating system: a folder named with them
    could never be recreated on a disk (future zip download, Samba sync).
    Too long a name is refused too, for the same reason.
    """
    name = unicodedata.normalize("NFC", raw)
    name = _without_invisible_characters(name).strip()
    if not name:
        raise InvalidFolderNameError("The folder name is empty.")
    if name in {".", ".."}:
        raise InvalidFolderNameError('"." and ".." are reserved names.')
    if "/" in name or "\\" in name:
        raise InvalidFolderNameError('A folder name cannot contain "/" or "\\".')
    if len(name) > MAX_FOLDER_NAME_LENGTH:
        raise InvalidFolderNameError(f"A folder name is limited to {MAX_FOLDER_NAME_LENGTH} characters.")
    return name


def first_free_name(name: str, taken: set[str]) -> str:
    """``name`` if it is free, else "name (2)", "name (3)"... (like Windows
    Explorer or Google Drive when a copy keeps both versions).

    Used when a folder is UPLOADED next to an existing one with the same
    name: the upload never merges into, or mixes with, existing content.
    The suffix always fits in MAX_FOLDER_NAME_LENGTH (the name is shortened
    if needed).
    """
    if name not in taken:
        return name
    for number in range(2, len(taken) + 2):
        suffix = f" ({number})"
        candidate = name[: MAX_FOLDER_NAME_LENGTH - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
    # Unreachable: len(taken) + 1 candidates cannot all be in ``taken``.
    raise InvalidFolderNameError("No free name was found for this folder.")


def guess_mime_type(filename: str) -> str:
    """MIME type of a file, decided by the SERVER from its extension.

    The Content-Type declared by a client is not trusted: anyone can send
    "image/png" for an HTML page. Deriving the type from the name keeps the
    stored value consistent and predictable. (The safety of the download
    does not depend on this value anyway: see inline_media_type().)
    """
    mime_type, encoding = mimetypes.guess_type(filename)
    if encoding is not None:
        return _COMPRESSION_TYPES.get(encoding, GENERIC_TYPE)
    return (mime_type or GENERIC_TYPE).lower()


def inline_media_type(mime_type: str) -> str | None:
    """Media type to use when the file is VIEWED in the browser, or None if
    the file must not be displayed inline (it is then downloaded).

    Text formats are served as text/plain: combined with the
    ``X-Content-Type-Options: nosniff`` header, the browser shows the text
    and never interprets it as HTML.
    """
    if mime_type in _INLINE_AS_IS:
        return mime_type
    if mime_type.startswith("text/") or mime_type in _TEXT_LIKE:
        return "text/plain"
    return None


def can_preview(mime_type: str) -> bool:
    """True if the file manager may offer a "View" action for this type."""
    return inline_media_type(mime_type) is not None


# Rendered by the browser's own built-in player or viewer, never as a page.
_BUILT_IN_VIEWER_PREFIXES = ("audio/", "video/")


def needs_sandbox(media_type: str) -> bool:
    """True if the response must carry ``Content-Security-Policy: sandbox``.

    The sandbox makes the browser treat the response as a separate,
    script-less origin: even if a file were rendered as a page, it could
    not run code or reach the API. It is applied to everything (images,
    text, every download) EXCEPT the types shown by the browser's built-in
    viewers: PDF (pdf.js / PDFium refuse to run in a sandboxed document)
    and audio / video (native media player). For those, the sandbox would
    add nothing: with ``nosniff`` and a media or PDF type, the bytes can
    only be decoded by that viewer, never executed as a script.
    """
    return media_type != "application/pdf" and not media_type.startswith(_BUILT_IN_VIEWER_PREFIXES)
