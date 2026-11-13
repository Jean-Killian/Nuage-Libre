"""Unit tests of the file rules (app/services/file_policy.py).

Pure functions: no database, no disk, no HTTP. Each test pins down one rule
of the private cloud, so a change of behaviour is always a conscious one.
"""

import pytest

from app.services.file_policy import (
    MAX_FILENAME_LENGTH,
    MAX_FOLDER_NAME_LENGTH,
    InvalidFileNameError,
    InvalidFolderNameError,
    can_preview,
    clean_filename,
    clean_folder_name,
    first_free_name,
    guess_mime_type,
    inline_media_type,
    needs_sandbox,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        # Only the last path component is kept (Unix and Windows separators).
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\report.pdf", "report.pdf"),
        # Control characters (CR/LF = header injection, NUL, TAB) are removed.
        ("evil\r\nSet-Cookie: x.txt", "evilSet-Cookie: x.txt"),
        ("nul\x00byte\t.txt", "nulbyte.txt"),
        # Right-to-left override: "invoice<RLO>fdp.exe" would DISPLAY as
        # "invoiceexe.pdf"; without the control character the trick is visible.
        ("invoice\u202efdp.exe", "invoicefdp.exe"),
        # Surrounding spaces are stripped.
        ("   spaced name.txt  ", "spaced name.txt"),
        # Emoji and accents are kept as they are.
        ("Vacances 🌴 été.jpg", "Vacances 🌴 été.jpg"),
    ],
)
def test_clean_filename(raw, expected):
    """T-POL-01: names are reduced to a tidy, safe display name."""
    assert clean_filename(raw) == expected


def test_clean_filename_normalises_macos_decomposed_accents():
    """T-POL-02: NFD names (as sent by macOS) are stored in NFC form."""
    decomposed = "e\u0301te\u0301.txt"  # "e" + combining acute accent, twice
    assert len(decomposed) == 9

    cleaned = clean_filename(decomposed)

    assert cleaned == "\u00e9t\u00e9.txt"  # "été.txt", one code point per letter
    assert len(cleaned) == 7


@pytest.mark.parametrize("raw", ["", "   ", ".", "..", "../", "a/..", "\r\n", "\u202e"])
def test_clean_filename_rejects_empty_names(raw):
    """T-POL-03: a name that is empty or only dots once cleaned is refused."""
    with pytest.raises(InvalidFileNameError):
        clean_filename(raw)


def test_long_names_are_shortened_keeping_the_extension():
    """T-POL-04: names longer than the column (255) keep their extension."""
    cleaned = clean_filename("a" * 400 + ".pdf")

    assert len(cleaned) == MAX_FILENAME_LENGTH
    assert cleaned.endswith(".pdf")


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("photo.png", "image/png"),
        ("PHOTO.JPG", "image/jpeg"),  # extensions are case-insensitive
        ("cours.pdf", "application/pdf"),
        ("notes.txt", "text/plain"),
        ("page.html", "text/html"),
        ("backup.tar.gz", "application/gzip"),  # compressed: the real type is gzip
        ("Makefile", "application/octet-stream"),  # unknown -> generic binary
        ("weird.zzzunknown", "application/octet-stream"),
    ],
)
def test_guess_mime_type(filename, expected):
    """T-POL-05: the MIME type is decided by the server from the extension."""
    assert guess_mime_type(filename) == expected


@pytest.mark.parametrize(
    ("mime_type", "expected"),
    [
        ("image/png", "image/png"),
        ("application/pdf", "application/pdf"),
        ("video/mp4", "video/mp4"),
        # Text formats are SHOWN as plain text, never interpreted:
        ("text/plain", "text/plain"),
        ("text/markdown", "text/plain"),
        ("text/html", "text/plain"),
        ("application/json", "text/plain"),
        # Script-capable or unknown formats are never displayed inline:
        ("image/svg+xml", None),
        ("application/xhtml+xml", None),
        ("application/octet-stream", None),
        ("application/zip", None),
    ],
)
def test_inline_media_type(mime_type, expected):
    """T-POL-06 / T-SEC-06: only script-free types may be displayed inline."""
    assert inline_media_type(mime_type) == expected
    assert can_preview(mime_type) is (expected is not None)


def test_sandbox_everywhere_except_built_in_viewers():
    """T-POL-07: every response is sandboxed, except the types shown by the
    browser's own viewers (PDF, audio, video), which a sandbox can break."""
    assert needs_sandbox("image/png") is True
    assert needs_sandbox("text/plain") is True
    assert needs_sandbox("text/html") is True
    assert needs_sandbox("application/octet-stream") is True
    assert needs_sandbox("application/pdf") is False
    assert needs_sandbox("audio/mpeg") is False
    assert needs_sandbox("video/mp4") is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Courses", "Courses"),
        ("  Vacances 🌴 été  ", "Vacances 🌴 été"),
        ("e\u0301te\u0301", "\u00e9t\u00e9"),  # NFD (macOS) -> NFC
        ("tab\there", "tabhere"),  # control characters removed
        ("a\u202eb", "ab"),  # bidi override removed
        (".hidden", ".hidden"),  # a leading dot is a legitimate name
        ("x" * MAX_FOLDER_NAME_LENGTH, "x" * MAX_FOLDER_NAME_LENGTH),
    ],
)
def test_clean_folder_name(raw, expected):
    """T-POL-08: folder names get the same hygiene as file names."""
    assert clean_folder_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("", "empty"),
        ("  \r\n ", "empty"),
        (".", "reserved"),
        ("..", "reserved"),
        ("Courses/2026", "cannot contain"),
        ("C:\\Users", "cannot contain"),
        ("x" * (MAX_FOLDER_NAME_LENGTH + 1), "limited"),
    ],
)
def test_clean_folder_name_refuses(raw, message):
    """T-POL-09: unlike file names, a folder name with a separator or too long
    is REFUSED with a clear reason, never silently cut."""
    with pytest.raises(InvalidFolderNameError, match=message):
        clean_folder_name(raw)


def test_first_free_name():
    """T-POL-10: "Photos" -> "Photos (2)" -> "Photos (3)"; the suffix always fits."""
    assert first_free_name("Photos", set()) == "Photos"
    assert first_free_name("Photos", {"Photos"}) == "Photos (2)"
    assert first_free_name("Photos", {"Photos", "Photos (2)", "Photos (4)"}) == "Photos (3)"
    long_name = "x" * MAX_FOLDER_NAME_LENGTH
    renamed = first_free_name(long_name, {long_name})
    assert len(renamed) == MAX_FOLDER_NAME_LENGTH
    assert renamed.endswith(" (2)")
