from datetime import datetime
from pathlib import Path
import re


def main():
    today = datetime.now()
    prefix = f"{today.month}.{today.day}"
    base_dir = Path(__file__).resolve().parent

    filenames = [
        f"{prefix}大围.txt",
        f"{prefix}杀数字.txt",
        f"{prefix}生肖.txt",
    ]

    for filename in filenames:
        file_path = base_dir / filename
        if file_path.exists():
            print(f"已存在：{filename}")
            continue

        file_path.write_text("", encoding="utf-8")
        print(f"已创建：{filename}")

    dated_txt = re.compile(r"^(\d{1,2}\.\d{1,2}).+\.txt$")
    for file_path in base_dir.glob("*.txt"):
        match = dated_txt.match(file_path.name)
        if not match or match.group(1) == prefix:
            continue

        file_path.unlink()
        print(f"已删除旧日期：{file_path.name}")


if __name__ == "__main__":
    main()
