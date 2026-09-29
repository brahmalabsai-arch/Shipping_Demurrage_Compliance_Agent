"""Rebuild the compliance RAG index as exactly the approved rulebook in force.

New approvals are indexed automatically after their decision commits; run this once to index
rules approved before Phase 9, or to repair the index if an after-commit indexing failed (it is
logged as ``rag.index_failed``). Idempotent.

    .venv\\Scripts\\python.exe -m scripts.index_rulebook
"""

from config.log_setup import configure_logging
from config.settings import get_settings
from services import compliance_service
from services.db import session_scope


def main() -> None:
    configure_logging()
    with session_scope() as session:
        report = compliance_service.sync_rag_index(session)
    print(f"index at {get_settings().chroma_path}")
    print(f"indexed ({len(report.indexed)}): {', '.join(report.indexed) or '-'}")
    print(f"removed ({len(report.removed)}): {', '.join(report.removed) or '-'}")


if __name__ == "__main__":
    main()
