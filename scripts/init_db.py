"""建表 + 写入默认元数据（字段注册表 / 维度 / 导航树 / 数据格式）。

用法::

    python scripts/init_db.py            # 缺表就建，元数据幂等补齐
    python scripts/init_db.py --drop     # 先删所有表再建（数据全丢，慎用）
"""
import argparse
import os
import sys

# allow running from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import Base, SessionLocal, engine
from app.metadata import seed_metadata

# import models so they register with Base
from app import models  # noqa: F401


def main() -> None:
    ap = argparse.ArgumentParser(description="建表并写入默认元数据")
    ap.add_argument("--drop", action="store_true", help="先删除所有表（数据全丢）")
    args = ap.parse_args()

    if args.drop:
        print("Dropping all tables …")
        Base.metadata.drop_all(bind=engine)

    print("Creating tables in database …")
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        added = seed_metadata(db)
    finally:
        db.close()

    print("Done. Tables created.")
    print("  新增字段 %d / 导航树配置 %d / 数据格式 %d"
          % (added["attrs"], added["tree_profiles"], added["dataset_types"]))


if __name__ == "__main__":
    main()
