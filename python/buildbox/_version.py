"""The one place the version is written down.

`pyproject.toml` reads it from here rather than repeating it, and
`protocol.AGENT` builds its string from it. A device announces `python/0.1.0` on
the wire and an operator reads a version out of `pip show buildbox`; those two
must not be able to disagree, which is what happens when the same number is
written down twice.
"""

__version__ = "0.1.0"
