"""Getting a KRX session out of pykrx without taking the pipeline down with it.

pykrx logs in to KRX **during ``import pykrx``**, not on first use. Every module
under it is imported transitively, and ``build_krx_session`` runs at that point
reading ``KRX_ID``/``KRX_PW`` from the environment. So a login that fails raises
from inside the library, at import time, with a traceback that names
``json/decoder.py`` rather than anything about credentials.

That is worth isolating for two reasons.

**It contradicts the failure rule.** CLAUDE.md requires a collector that fails
to record the failure and let the pipeline publish a partial report. An
exception escaping an import aborts the run instead, so the briefing that should
have said "kr_flow unavailable" says nothing at all.

**The traceback does not describe the problem.** Observed 2026-08-05: after
enough requests from one address, KRX answers the login endpoint with an HTML
error page, and ``resp.json()`` inside pykrx raises ``JSONDecodeError:
Expecting value: line 13 column 1``. Nothing in that mentions rate limiting,
KRX, or the credentials, and the same error appears for a wrong password, an
expired session and a site outage alike.
"""

from __future__ import annotations

import os
from typing import Any


class KrxSessionError(RuntimeError):
    """pykrx could not be imported, which means no KRX session was established."""


def import_pykrx_stock() -> Any:
    """Import and return ``pykrx.stock``, or raise :class:`KrxSessionError`.

    Callers catch this and turn it into a failed check rather than letting it
    propagate. Imported lazily by design: the collectors must stay importable
    offline, and in tests, without credentials.
    """
    try:
        from pykrx import stock
    except Exception as exc:  # noqa: BLE001 - pykrx raises several unrelated types
        missing = [k for k in ("KRX_ID", "KRX_PW") if not os.environ.get(k)]
        hint = (
            f"{' and '.join(missing)} not set"
            if missing
            else "credentials are set, so the usual cause is rate limiting — KRX serves "
            "an HTML error page instead of JSON when it is throttling an address, and "
            "it clears on its own"
        )
        raise KrxSessionError(
            f"could not establish a KRX session: {type(exc).__name__}: {exc}. {hint}."
        ) from exc
    return stock
