import os
import json
from pathlib import Path
from dotenv import load_dotenv
from pymongo import MongoClient
from bson import json_util

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

def seed():
    print(f"Connecting to MongoDB at {MONGO_URI}, database: {MONGO_DB}...")
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client[MONGO_DB]

    # Verify connection
    client.admin.command("ping")
    print("Connected successfully.")

    base_dir = Path(__file__).parent / "sample_data"
    employees_file = base_dir / "employees.json"
    attendance_file = base_dir / "attendance_logs.json"

    with open(employees_file, "r", encoding="utf-8") as f:
        employees_data = json_util.loads(f.read())

    with open(attendance_file, "r", encoding="utf-8") as f:
        attendance_data = json_util.loads(f.read())

    # Clear only the two collections
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})

    if employees_data:
        db.employees.insert_many(employees_data)
        print(f"Inserted {len(employees_data)} employees.")

    if attendance_data:
        db.attendance_logs.insert_many(attendance_data)
        print(f"Inserted {len(attendance_data)} attendance logs.")

    print("Seeding completed successfully.")

if __name__ == "__main__":
    seed()
