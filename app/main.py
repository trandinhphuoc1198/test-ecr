import os
import json
import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

import boto3
import pymysql
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

APP_NAME = os.getenv("APP_NAME", "two-datasource-api")

# ---------- Helpers for JSON serialization ----------
class EnhancedJSONEncoder(json.JSONEncoder):
    def default(self, obj: Any):
        if isinstance(obj, (datetime.datetime, datetime.date)):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            # DynamoDB numbers come as Decimal
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)

def json_response(data: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        content=json.loads(json.dumps(data, cls=EnhancedJSONEncoder)),
        status_code=status_code,
    )

# ---------- MySQL (RDS) ----------
def get_mysql_connection_old_way():
    """
    Uses env vars:
      RDS_HOST, RDS_PORT, RDS_USER, RDS_PASSWORD, RDS_DB
    """
    host = os.getenv("RDS_HOST")
    port = int(os.getenv("RDS_PORT", "3306"))
    user = os.getenv("RDS_USER")
    password = os.getenv("RDS_PASSWORD")
    db = os.getenv("RDS_DB", "testDb")

    if not all([host, user, password, db]):
        raise RuntimeError("Missing required RDS env vars (RDS_HOST/RDS_USER/RDS_PASSWORD/RDS_DB).")

    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=db,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        read_timeout=30,
        write_timeout=30,
        autocommit=True,
    )

# ---------- MySQL (RDS with IAM Auth) ----------
def get_mysql_connection():
    """
    Uses IAM authentication instead of password

    Required env vars:
      RDS_HOST
      RDS_PORT
      RDS_USER
      RDS_DB
      AWS_REGION
    """

    host = os.getenv("RDS_HOST")
    port = int(os.getenv("RDS_PORT", "3306"))
    user = os.getenv("RDS_USER")
    db = os.getenv("RDS_DB", "testDb")
    region = os.getenv("AWS_REGION")

    if not all([host, user, db, region]):
        raise RuntimeError("Missing required env vars for IAM auth")

    # ✅ Generate IAM token
    client = boto3.client("rds", region_name=region)

    token = client.generate_db_auth_token(
        DBHostname=host,
        Port=port,
        DBUsername=user,
        Region=region,
    )

    # ✅ Connect using token (NOT password)
    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=token,
        database=db,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        read_timeout=30,
        write_timeout=30,
        autocommit=True,

        # ✅ REQUIRED for IAM auth
        ssl={"ssl": {}}
    )

def fetch_all_mysql_rows() -> List[Dict[str, Any]]:
    """
    Fetches all rows from testDb.TestTable.
    WARNING: For big tables, consider pagination/limits.
    """
    table = os.getenv("RDS_TABLE", "TestTable")
    db = os.getenv("RDS_DB", "testDb")
    sql = f"SELECT * FROM `{db}`.`{table}`;"

    conn = None
    try:
        conn = get_mysql_connection()
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            return rows
    finally:
        if conn:
            conn.close()

# ---------- DynamoDB ----------
def get_ddb_table():
    """
    Uses env vars:
      DDB_TABLE, AWS_REGION (or region from runtime)
    Auth: IAM role (recommended) or AWS creds in env (not recommended).
    """
    table_name = os.getenv("DDB_TABLE", "TestTable")
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")

    # region can be optional in AWS env, but it's better to set it explicitly
    ddb = boto3.resource("dynamodb", region_name=region) if region else boto3.resource("dynamodb")
    return ddb.Table(table_name)

def scan_all_items(table) -> List[Dict[str, Any]]:
    """
    Scan with pagination until all items collected.
    """
    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {}

    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))

        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key

    return items

# ---------- FastAPI ----------
app = FastAPI(title=APP_NAME)

@app.get("/")
def health():
    return {"status": "ok", "app": APP_NAME}

@app.get("/rds/testtable")
def get_rds_testtable():
    try:
        rows = fetch_all_mysql_rows()
        return json_response({"source": "rds-mysql", "count": len(rows), "data": rows})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"RDS query failed: {str(e)}")

@app.get("/dynamodb/testtable")
def get_ddb_testtable():
    try:
        table = get_ddb_table()
        items = scan_all_items(table)
        return json_response({"source": "dynamodb", "count": len(items), "data": items})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DynamoDB scan failed: {str(e)}")