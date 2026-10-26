"""Create the first admin from settings, once, when no users exist."""

from __future__ import annotations

import logging

from sqlalchemy import func, select

from backend.config import settings
from backend.db import database
from backend.db.ui_models import User
from backend.ui.security import hash_password, normalize_email, password_problem, valid_email

logger = logging.getLogger(__name__)


async def seed_admin() -> bool:
    """Returns True if an admin was created. Never logs the password; never overwrites a user."""
    email = normalize_email(settings.UI_ADMIN_EMAIL)
    password = settings.UI_ADMIN_PASSWORD
    if not email or not password:
        return False
    if not valid_email(email) or password_problem(password, email):
        logger.warning("UI_ADMIN_EMAIL/UI_ADMIN_PASSWORD are set but invalid; no admin was seeded")
        return False
    try:
        async with database.async_session() as session:
            existing = (await session.execute(select(func.count()).select_from(User))).scalar_one()
            if existing:
                return False
            session.add(User(email=email, name="Administrator", password_hash=hash_password(password), role="admin", active=True))
            await session.commit()
    except Exception as error:  # noqa: BLE001 - the API must still start if the database is not ready
        logger.warning("Could not seed the admin user (%s)", type(error).__name__)
        return False
    logger.info("Seeded the first admin user", extra={"email": email})
    return True
