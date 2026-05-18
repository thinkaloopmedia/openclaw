"""
Creates (or re-creates) all database tables.
Run directly:  python scripts/migrate.py [--drop]
"""

import asyncio
import sys

sys.path.insert(0, ".")  # allow imports from project root

from src.storage.db import create_tables, drop_tables


async def main(drop: bool) -> None:
    if drop:
        print("Dropping all tables...")
        await drop_tables()
    print("Creating tables...")
    await create_tables()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main(drop="--drop" in sys.argv))
