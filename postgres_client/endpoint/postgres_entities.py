from pydantic import BaseModel


class PostgresHealthResponse(BaseModel):
    status: str
    host: str
    port: int
    database: str


class DropTableResponse(BaseModel):
    table_name: str
    message: str


class ColumnDefinition(BaseModel):
    name: str
    type: str  # text, integer, bigint, serial, bigserial, boolean, float, date, timestamp, jsonb, varchar(N)
    nullable: bool = True
    primary_key: bool = False
    unique: bool = False
    default: str | None = None  # raw SQL expression, e.g. "now()", "0", "'pending'"


class CreateTableRequest(BaseModel):
    table_name: str
    columns: list[ColumnDefinition]
    if_not_exists: bool = True


class CreateTableResponse(BaseModel):
    table_name: str
    columns: list[str]
    ddl: str
    message: str
