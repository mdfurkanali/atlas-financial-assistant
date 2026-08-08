import os

import psycopg
from dotenv import load_dotenv

load_dotenv()

database_url = os.environ["DATABASE_URL"]

with psycopg.connect(
    database_url,
    sslmode="require",
    connect_timeout=10,
) as connection:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database(), NOW()")
        database_name, database_time = cursor.fetchone()

print("Database connection successful")
print("Database:", database_name)
print("Server time:", database_time)