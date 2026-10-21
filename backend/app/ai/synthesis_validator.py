"""Validate an AI proposal for a request a fake service could not answer (Phase 5A).

Nothing the model returns is trusted. A proposal is accepted only if, in ordinary code:
  * it matches the strict per-protocol shape below (and nothing else);
  * it stays inside the snapshot: no tables or API resources the environment does not have, no columns of known
    tables that do not exist;
  * it changes nothing: a request that would write (INSERT, SET, POST ...) may only get an error;
  * it carries nothing sensitive: no credentials or secret-like strings, only reserved example email domains and
    documentation IP ranges, bounded sizes.
Rejections carry a short reason, never the rejected content.
"""
import json
import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError

from app.ai.synthesis_context import GapRequest
from app.sanitization.patterns import SECRET_KINDS, find_secrets

MAX_PROPOSAL_BYTES = 8 * 1024
MAX_ROWS, MAX_COLUMNS, MAX_CELL = 50, 20, 200

Scalar = StrictStr | StrictInt | float | StrictBool | None


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- per-protocol shapes -------------------------------------------------------------------------------------
class PgRows(_Strict):
    kind: Literal["rows"]
    columns: list[Annotated[str, Field(pattern=r"^[A-Za-z_?][A-Za-z0-9_?]{0,62}$")]] = Field(min_length=1,
                                                                                            max_length=MAX_COLUMNS)
    rows: list[list[Scalar]] = Field(max_length=MAX_ROWS)


class PgError(_Strict):
    kind: Literal["error"]
    code: Annotated[str, Field(pattern=r"^[0-9A-Z]{5}$")]
    message: Annotated[str, Field(min_length=1, max_length=MAX_CELL)]


class RedisValue(_Strict):
    kind: Literal["value"]
    value: StrictStr | StrictInt | None | list[StrictStr | StrictInt | None]


class RedisError(_Strict):
    kind: Literal["error"]
    message: Annotated[str, Field(pattern=r"^(ERR|WRONGTYPE|NOPERM|NOAUTH|NOSCRIPT|BUSY|LOADING|READONLY) ",
                                  max_length=MAX_CELL)]


class HttpResponse(_Strict):
    kind: Literal["response"]
    status: Literal[200, 400, 401, 403, 404, 405, 409, 422, 500, 501, 503]
    body: Any = None


SHAPES: dict[str, tuple[type[_Strict], ...]] = {
    "postgres": (PgRows, PgError), "redis": (RedisValue, RedisError), "http": (HttpResponse,),
}


def output_schema(proto: str) -> dict:
    """JSON schema for the provider (schema-constrained decoding where supported). Code still validates."""
    variants = [m.model_json_schema() for m in SHAPES[proto]]
    return variants[0] if len(variants) == 1 else {"oneOf": variants}


# ---- rules -----------------------------------------------------------------------------------------------------
# real SQLSTATEs a PostgreSQL server could plausibly send for an unusual query
PG_CODES = {
    "0A000", "08006", "22001", "22003", "22007", "22012", "22023", "22P02", "23502", "23503", "23505", "25006",
    "25P02", "28000", "28P01", "3D000", "3F000", "40001", "42501", "42601", "42602", "42702", "42703", "42704",
    "42710", "42803", "42804", "42809", "42883", "42P01", "42P02", "42P07", "42P10", "53300", "55000", "55006",
    "57014", "58P01", "XX000",
}
_PG_WRITE = re.compile(r"^\s*(insert|update|delete|merge|create|drop|alter|truncate|grant|revoke|copy|comment|"
                       r"vacuum|reindex|cluster|lock|do|call|set|reset|listen|notify|prepare|execute|refresh|"
                       r"security|import|load)\b", re.I)
_PG_RELATION = re.compile(r"\b(?:from|join|into|update|table)\s+((?:\"?[A-Za-z_][\w$]*\"?\.)?\"?[A-Za-z_][\w$]*\"?)",
                          re.I)
_PG_SYSTEM = ("pg_", "information_schema.", "pg_catalog.")
_PG_CTE = re.compile(r"(?:\bwith(?:\s+recursive)?|,)\s*\"?([A-Za-z_][\w$]*)\"?\s*(?:\([^)]*\)\s*)?as\s*(?:not\s+)?"
                     r"(?:materialized\s*)?\(", re.I)
_PG_FUNCS = {"?column?", "count", "sum", "avg", "min", "max", "version", "now", "current_user", "current_database",
             "current_schema", "session_user", "user", "exists", "coalesce"}
REDIS_WRITES = {"SET", "SETNX", "SETEX", "PSETEX", "MSET", "MSETNX", "GETSET", "GETDEL", "APPEND", "DEL", "UNLINK",
                "INCR", "INCRBY", "INCRBYFLOAT", "DECR", "DECRBY", "EXPIRE", "PEXPIRE", "EXPIREAT", "PERSIST",
                "RENAME", "RENAMENX", "MOVE", "COPY", "RESTORE", "MIGRATE", "HSET", "HSETNX", "HMSET", "HDEL",
                "HINCRBY", "LPUSH", "RPUSH", "LPOP", "RPOP", "LSET", "LREM", "LTRIM", "SADD", "SREM", "SPOP",
                "SMOVE", "ZADD", "ZREM", "ZINCRBY", "FLUSHALL", "FLUSHDB", "CONFIG", "SAVE", "BGSAVE",
                "BGREWRITEAOF", "SHUTDOWN", "SLAVEOF", "REPLICAOF", "MODULE", "EVAL", "EVALSHA", "SCRIPT",
                "FUNCTION", "ACL", "DEBUG", "SWAPDB", "PUBLISH", "XADD", "XDEL", "SETRANGE", "SETBIT"}
_HTTP_READ = {"GET", "HEAD", "OPTIONS"}
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")
_SAFE_EMAIL_DOMAIN = re.compile(r"(?:^|\.)(?:example\.(?:com|net|org)|[A-Za-z0-9-]+\.(?:test|example|invalid|localhost)"
                                r"|test|example|invalid|localhost)$", re.I)
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_OK_IP = re.compile(r"^(?:203\.0\.113\.\d+|198\.51\.100\.\d+|192\.0\.2\.\d+|10\.20\.0\.\d+|127\.\d+\.\d+\.\d+|"
                    r"0\.0\.0\.0)$")


@dataclass
class Verdict:
    ok: bool
    reason: str = ""  # why it was rejected (never the content)
    proposal: dict | None = None  # the normalized, accepted proposal


def _reject(reason: str) -> Verdict:
    return Verdict(False, reason)


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _depth(value: Any, level: int = 0) -> int:
    if isinstance(value, dict):
        return max([_depth(v, level + 1) for v in value.values()], default=level + 1)
    if isinstance(value, list):
        return max([_depth(v, level + 1) for v in value], default=level + 1)
    return level


def _sensitive(proposal: dict) -> str | None:
    for text in _strings(proposal):
        if any(f.kind in SECRET_KINDS for f in find_secrets(text, SECRET_KINDS)):
            return "contains secret-like data"
        for domain in _EMAIL.findall(text):
            if not _SAFE_EMAIL_DOMAIN.search(domain):
                return "contains a non-example email address"
        for ip in _IP.findall(text):
            if not _OK_IP.match(ip):
                return "contains a non-documentation IP address"
    return None


def _relations(sql: str) -> list[str]:
    return [m.replace('"', "").lower() for m in _PG_RELATION.findall(sql)]


def _check_postgres(gap: GapRequest, p: PgRows | PgError, schema: dict[str, list[str]]) -> str | None:
    if isinstance(p, PgError):
        if p.code not in PG_CODES:
            return "unknown SQLSTATE"
        named = {n.lower().split(".")[-1] for n in re.findall(r'"([^"]{1,63})"', p.message)}
        if p.code == "42P01" and named & {t.lower() for t in schema}:
            return "claims a table of the environment does not exist"
        return None
    if _PG_WRITE.match(gap.text):
        return "a write statement may only get an error"
    rels = _relations(gap.text)
    tables = {t.lower(): [c.lower() for c in cols] for t, cols in schema.items()}
    ctes = {name.lower() for name in _PG_CTE.findall(gap.text)}  # WITH x AS (...) SELECT ... FROM x
    user_rels = [r.removeprefix("public.") for r in rels if not r.startswith(_PG_SYSTEM) and r not in ctes]
    unknown = [r for r in user_rels if r not in tables]
    if unknown:
        return "rows for a table the environment does not have"
    if any(len(row) != len(p.columns) for row in p.rows):
        return "row width does not match the columns"
    if any(isinstance(c, str) and len(c) > MAX_CELL for row in p.rows for c in row):
        return "cell too long"
    if user_rels:  # rows from known tables: columns must exist, or be named in the query (aliases), or be aggregates
        known = {c for r in user_rels for c in tables[r]}
        words = {w.lower() for w in re.findall(r"[A-Za-z_?][\w?]*", gap.text)}
        if any(c.lower() not in known and c.lower() not in words and c.lower() not in _PG_FUNCS for c in p.columns):
            return "column not in the schema"
    return None


def _check_redis(gap: GapRequest, p: RedisValue | RedisError) -> str | None:
    if isinstance(p, RedisError):
        return None
    command = (gap.text.split() or [""])[0].upper()
    if command in REDIS_WRITES:
        return "a write command may only get an error"
    items = p.value if isinstance(p.value, list) else [p.value]
    if len(items) > MAX_ROWS or any(isinstance(v, str) and len(v) > MAX_CELL * 4 for v in items):
        return "value too large"
    return None


def _check_http(gap: GapRequest, p: HttpResponse, schema: dict[str, list[str]]) -> str | None:
    method, _, target = gap.text.partition(" ")
    parts = [x for x in target.split("?", 1)[0].split("/") if x]
    if method.upper() not in _HTTP_READ and p.status < 400:
        return "a write request may only get an error status"
    if len(parts) >= 2 and parts[0] == "api" and parts[1].lower() not in {t.lower() for t in schema} and p.status < 400:
        return "success for an API resource the environment does not have"
    if _depth(p.body) > 5:
        return "body nested too deeply"
    return None


def validate(gap: GapRequest, raw: object, schema: dict[str, list[str]]) -> Verdict:
    """Validate the provider's raw answer for `gap` against the snapshot `schema` (table -> column names)."""
    if not isinstance(raw, dict):
        return _reject("response is not a JSON object")
    try:
        if len(json.dumps(raw)) > MAX_PROPOSAL_BYTES:
            return _reject("proposal too large")
    except (TypeError, ValueError):
        return _reject("proposal is not serializable")
    shapes = SHAPES.get(gap.proto)
    if shapes is None:
        return _reject("unknown protocol")
    parsed = None
    for shape in shapes:
        try:
            parsed = shape.model_validate(raw)
            break
        except ValidationError:
            continue
    if parsed is None:
        return _reject("does not match the schema")
    if gap.proto == "postgres":
        reason = _check_postgres(gap, parsed, schema)  # type: ignore[arg-type]
    elif gap.proto == "redis":
        reason = _check_redis(gap, parsed)  # type: ignore[arg-type]
    else:
        reason = _check_http(gap, parsed, schema)  # type: ignore[arg-type]
    proposal = parsed.model_dump()
    reason = reason or _sensitive(proposal)
    return _reject(reason) if reason else Verdict(True, "", proposal)
