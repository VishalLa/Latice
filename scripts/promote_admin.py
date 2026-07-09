from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.session import get_session
from database.user import User, UserRole

def promote(username: str) -> None:
    with get_session() as session:
        user = session.query(User).filter(User.username == username).first()
        if user is None:
            print(f"No user found with username={username!r}")
            return
        user.role = UserRole.ADMIN
        session.commit()
        print(f"{username!r} is now an ADMIN (sees all users' data).")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/promote_admin.py <username>")
        sys.exit(1)
    promote(sys.argv[1])
