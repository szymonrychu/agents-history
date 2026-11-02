"""Apply the forward-only schema migrations of agents-history."""

from agents_common.db.migrate import run_migrations

from agents_history.settings import Settings


def migrate(settings: Settings) -> list[str]:
    """Apply pending migration files under the advisory lock; return the names applied."""
    return run_migrations(settings.database_url.get_secret_value(), "agents_history")
