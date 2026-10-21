"""Create all database tables."""
import sys
import os

# allow running from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import Base, engine
# import models so they register with Base
from app import models  # noqa: F401

def main():
    print("Creating tables in database …")
    Base.metadata.create_all(bind=engine)
    print("Done. Tables created.")

if __name__ == "__main__":
    main()
