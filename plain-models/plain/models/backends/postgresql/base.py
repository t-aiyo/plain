import threading
import warnings
from contextlib import contextmanager
from functools import cached_property, lru_cache

import psycopg as Database
from psycopg import IsolationLevel, adapt, adapters, sql
from psycopg.postgres import types as pg_types
from psycopg.pq import Format
from psycopg.types.datetime import TimestamptzLoader
from psycopg.types.range import Range, RangeDumper
from psycopg.types.string import TextLoader

from plain.exceptions import ImproperlyConfigured
from plain.models.backends.base.base import BaseDatabaseWrapper
from plain.models.backends.utils import CursorDebugWrapper as BaseCursorDebugWrapper
from plain.models.db import DatabaseError as WrappedDatabaseError
from plain.models.db import db_connection

# Some of these import psycopg, so import them after checking if it's installed.
from .client import DatabaseClient  # NOQA isort:skip
from .creation import DatabaseCreation  # NOQA isort:skip
from .features import DatabaseFeatures  # NOQA isort:skip
from .introspection import DatabaseIntrospection  # NOQA isort:skip
from .operations import DatabaseOperations  # NOQA isort:skip
from .schema import DatabaseSchemaEditor  # NOQA isort:skip


# Type OIDs
TIMESTAMPTZ_OID = adapters.types["timestamptz"].oid
TSRANGE_OID = pg_types["tsrange"].oid
TSTZRANGE_OID = pg_types["tstzrange"].oid


class BaseTzLoader(TimestamptzLoader):
    """
    Load a PostgreSQL timestamptz using a specific timezone.
    The timezone can be None too, in which case it will be chopped.
    """

    timezone = None

    def load(self, data):
        res = super().load(data)
        return res.replace(tzinfo=self.timezone)


def register_tzloader(tz, context):
    class SpecificTzLoader(BaseTzLoader):
        timezone = tz

    context.adapters.register_loader("timestamptz", SpecificTzLoader)


class PlainRangeDumper(RangeDumper):
    """A Range dumper customized for Plain."""

    def upgrade(self, obj, format):
        dumper = super().upgrade(obj, format)
        if dumper is not self and dumper.oid == TSRANGE_OID:
            dumper.oid = TSTZRANGE_OID
        return dumper


@lru_cache
def get_adapters_template(timezone):
    ctx = adapt.AdaptersMap(adapters)
    # No-op JSON loader to avoid psycopg3 round trips
    ctx.register_loader("jsonb", TextLoader)
    # Treat inet/cidr as text
    ctx.register_loader("inet", TextLoader)
    ctx.register_loader("cidr", TextLoader)
    ctx.register_dumper(Range, PlainRangeDumper)
    register_tzloader(timezone, ctx)
    return ctx


def _get_varchar_column(data):
    if data["max_length"] is None:
        return "varchar"
    return "varchar({max_length})".format(**data)


class DatabaseWrapper(BaseDatabaseWrapper):
    vendor = "postgresql"
    display_name = "PostgreSQL"
    # This dictionary maps Field objects to their associated PostgreSQL column
    # types, as strings. Column-type strings can contain format strings; they'll
    # be interpolated against the values of Field.__dict__ before being output.
    # If a column type is set to None, it won't be included in the output.
    data_types = {
        "PrimaryKeyField": "bigint",
        "BinaryField": "bytea",
        "BooleanField": "boolean",
        "CharField": _get_varchar_column,
        "DateField": "date",
        "DateTimeField": "timestamp with time zone",
        "DecimalField": "numeric(%(max_digits)s, %(decimal_places)s)",
        "DurationField": "interval",
        "FloatField": "double precision",
        "IntegerField": "integer",
        "BigIntegerField": "bigint",
        "IPAddressField": "inet",
        "GenericIPAddressField": "inet",
        "JSONField": "jsonb",
        "PositiveBigIntegerField": "bigint",
        "PositiveIntegerField": "integer",
        "PositiveSmallIntegerField": "smallint",
        "SmallIntegerField": "smallint",
        "TextField": "text",
        "TimeField": "time",
        "UUIDField": "uuid",
    }
    data_type_check_constraints = {
        "PositiveBigIntegerField": '"%(column)s" >= 0',
        "PositiveIntegerField": '"%(column)s" >= 0',
        "PositiveSmallIntegerField": '"%(column)s" >= 0',
    }
    data_types_suffix = {
        "PrimaryKeyField": "GENERATED BY DEFAULT AS IDENTITY",
    }
    operators = {
        "exact": "= %s",
        "iexact": "= UPPER(%s)",
        "contains": "LIKE %s",
        "icontains": "LIKE UPPER(%s)",
        "regex": "~ %s",
        "iregex": "~* %s",
        "gt": "> %s",
        "gte": ">= %s",
        "lt": "< %s",
        "lte": "<= %s",
        "startswith": "LIKE %s",
        "endswith": "LIKE %s",
        "istartswith": "LIKE UPPER(%s)",
        "iendswith": "LIKE UPPER(%s)",
    }

    # The patterns below are used to generate SQL pattern lookup clauses when
    # the right-hand side of the lookup isn't a raw string (it might be an expression
    # or the result of a bilateral transformation).
    # In those cases, special characters for LIKE operators (e.g. \, *, _) should be
    # escaped on database side.
    #
    # Note: we use str.format() here for readability as '%' is used as a wildcard for
    # the LIKE operator.
    pattern_esc = (
        r"REPLACE(REPLACE(REPLACE({}, E'\\', E'\\\\'), E'%%', E'\\%%'), E'_', E'\\_')"
    )
    pattern_ops = {
        "contains": "LIKE '%%' || {} || '%%'",
        "icontains": "LIKE '%%' || UPPER({}) || '%%'",
        "startswith": "LIKE {} || '%%'",
        "istartswith": "LIKE UPPER({}) || '%%'",
        "endswith": "LIKE '%%' || {}",
        "iendswith": "LIKE '%%' || UPPER({})",
    }

    Database = Database
    SchemaEditorClass = DatabaseSchemaEditor
    # Classes instantiated in __init__().
    client_class = DatabaseClient
    creation_class = DatabaseCreation
    features_class = DatabaseFeatures
    introspection_class = DatabaseIntrospection
    ops_class = DatabaseOperations
    # PostgreSQL backend-specific attributes.
    _named_cursor_idx = 0

    def get_database_version(self):
        """
        Return a tuple of the database's version.
        E.g. for pg_version 120004, return (12, 4).
        """
        return divmod(self.pg_version, 10000)

    def get_connection_params(self):
        settings_dict = self.settings_dict
        # None may be used to connect to the default 'postgres' db
        if settings_dict["NAME"] == "" and not settings_dict.get("OPTIONS", {}).get(
            "service"
        ):
            raise ImproperlyConfigured(
                "settings.DATABASE is improperly configured. "
                "Please supply the NAME or OPTIONS['service'] value."
            )
        if len(settings_dict["NAME"] or "") > self.ops.max_name_length():
            raise ImproperlyConfigured(
                "The database name '%s' (%d characters) is longer than "  # noqa: UP031
                "PostgreSQL's limit of %d characters. Supply a shorter NAME "
                "in settings.DATABASE."
                % (
                    settings_dict["NAME"],
                    len(settings_dict["NAME"]),
                    self.ops.max_name_length(),
                )
            )
        conn_params = {"client_encoding": "UTF8"}
        if settings_dict["NAME"]:
            conn_params = {
                "dbname": settings_dict["NAME"],
                **settings_dict["OPTIONS"],
            }
        elif settings_dict["NAME"] is None:
            # Connect to the default 'postgres' db.
            settings_dict.get("OPTIONS", {}).pop("service", None)
            conn_params = {"dbname": "postgres", **settings_dict["OPTIONS"]}
        else:
            conn_params = {**settings_dict["OPTIONS"]}

        conn_params.pop("assume_role", None)
        conn_params.pop("isolation_level", None)
        conn_params.pop("server_side_binding", None)
        if settings_dict["USER"]:
            conn_params["user"] = settings_dict["USER"]
        if settings_dict["PASSWORD"]:
            conn_params["password"] = settings_dict["PASSWORD"]
        if settings_dict["HOST"]:
            conn_params["host"] = settings_dict["HOST"]
        if settings_dict["PORT"]:
            conn_params["port"] = settings_dict["PORT"]
        conn_params["context"] = get_adapters_template(self.timezone)
        # Disable prepared statements by default to keep connection poolers
        # working. Can be reenabled via OPTIONS in the settings dict.
        conn_params["prepare_threshold"] = conn_params.pop("prepare_threshold", None)
        return conn_params

    def get_new_connection(self, conn_params):
        # self.isolation_level must be set:
        # - after connecting to the database in order to obtain the database's
        #   default when no value is explicitly specified in options.
        # - before calling _set_autocommit() because if autocommit is on, that
        #   will set connection.isolation_level to ISOLATION_LEVEL_AUTOCOMMIT.
        options = self.settings_dict["OPTIONS"]
        set_isolation_level = False
        try:
            isolation_level_value = options["isolation_level"]
        except KeyError:
            self.isolation_level = IsolationLevel.READ_COMMITTED
        else:
            # Set the isolation level to the value from OPTIONS.
            try:
                self.isolation_level = IsolationLevel(isolation_level_value)
                set_isolation_level = True
            except ValueError:
                raise ImproperlyConfigured(
                    f"Invalid transaction isolation level {isolation_level_value} "
                    f"specified. Use one of the psycopg.IsolationLevel values."
                )
        connection = self.Database.connect(**conn_params)
        if set_isolation_level:
            connection.isolation_level = self.isolation_level
        # Use server-side binding cursor if requested, otherwise standard cursor
        connection.cursor_factory = (
            ServerBindingCursor
            if options.get("server_side_binding") is True
            else Cursor
        )
        return connection

    def ensure_timezone(self):
        if self.connection is None:
            return False
        conn_timezone_name = self.connection.info.parameter_status("TimeZone")
        timezone_name = self.timezone_name
        if timezone_name and conn_timezone_name != timezone_name:
            with self.connection.cursor() as cursor:
                cursor.execute(self.ops.set_time_zone_sql(), [timezone_name])
            return True
        return False

    def ensure_role(self):
        if self.connection is None:
            return False
        if new_role := self.settings_dict.get("OPTIONS", {}).get("assume_role"):
            with self.connection.cursor() as cursor:
                sql = self.ops.compose_sql("SET ROLE %s", [new_role])
                cursor.execute(sql)
            return True
        return False

    def init_connection_state(self):
        super().init_connection_state()

        # Commit after setting the time zone.
        commit_tz = self.ensure_timezone()
        # Set the role on the connection. This is useful if the credential used
        # to login is not the same as the role that owns database resources. As
        # can be the case when using temporary or ephemeral credentials.
        commit_role = self.ensure_role()

        if (commit_role or commit_tz) and not self.get_autocommit():
            self.connection.commit()

    def create_cursor(self, name=None):
        if name:
            # In autocommit mode, the cursor will be used outside of a
            # transaction, hence use a holdable cursor.
            cursor = self.connection.cursor(
                name, scrollable=False, withhold=self.connection.autocommit
            )
        else:
            cursor = self.connection.cursor()

        # Register the cursor timezone only if the connection disagrees, to avoid copying the adapter map.
        tzloader = self.connection.adapters.get_loader(TIMESTAMPTZ_OID, Format.TEXT)
        if self.timezone != tzloader.timezone:
            register_tzloader(self.timezone, cursor)
        return cursor

    def tzinfo_factory(self, offset):
        return self.timezone

    def chunked_cursor(self):
        self._named_cursor_idx += 1
        # Get the current async task
        # Note that right now this is behind @async_unsafe, so this is
        # unreachable, but in future we'll start loosening this restriction.
        # For now, it's here so that every use of "threading" is
        # also async-compatible.
        task_ident = "sync"
        # Use that and the thread ident to get a unique name
        return self._cursor(
            name="_plain_curs_%d_%s_%d"  # noqa: UP031
            % (
                # Avoid reusing name in other threads / tasks
                threading.current_thread().ident,
                task_ident,
                self._named_cursor_idx,
            )
        )

    def _set_autocommit(self, autocommit):
        with self.wrap_database_errors:
            self.connection.autocommit = autocommit

    def check_constraints(self, table_names=None):
        """
        Check constraints by setting them to immediate. Return them to deferred
        afterward.
        """
        with self.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute("SET CONSTRAINTS ALL DEFERRED")

    def is_usable(self):
        try:
            # Use a psycopg cursor directly, bypassing Plain's utilities.
            with self.connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        except Database.Error:
            return False
        else:
            return True

    @contextmanager
    def _nodb_cursor(self):
        cursor = None
        try:
            with super()._nodb_cursor() as cursor:
                yield cursor
        except (Database.DatabaseError, WrappedDatabaseError):
            if cursor is not None:
                raise
            warnings.warn(
                "Normally Plain will use a connection to the 'postgres' database "
                "to avoid running initialization queries against the production "
                "database when it's not needed (for example, when running tests). "
                "Plain was unable to create a connection to the 'postgres' database "
                "and will use the first PostgreSQL database instead.",
                RuntimeWarning,
            )
            conn = self.__class__(
                {
                    **self.settings_dict,
                    "NAME": db_connection.settings_dict["NAME"],
                },
            )
            try:
                with conn.cursor() as cursor:
                    yield cursor
            finally:
                conn.close()

    @cached_property
    def pg_version(self):
        with self.temporary_connection():
            return self.connection.info.server_version

    def make_debug_cursor(self, cursor):
        return CursorDebugWrapper(cursor, self)


class CursorMixin:
    """
    A subclass of psycopg cursor implementing callproc.
    """

    def callproc(self, name, args=None):
        if not isinstance(name, sql.Identifier):
            name = sql.Identifier(name)

        qparts = [sql.SQL("SELECT * FROM "), name, sql.SQL("(")]
        if args:
            for item in args:
                qparts.append(sql.Literal(item))
                qparts.append(sql.SQL(","))
            del qparts[-1]

        qparts.append(sql.SQL(")"))
        stmt = sql.Composed(qparts)
        self.execute(stmt)
        return args


class ServerBindingCursor(CursorMixin, Database.Cursor):
    pass


class Cursor(CursorMixin, Database.ClientCursor):
    pass


class CursorDebugWrapper(BaseCursorDebugWrapper):
    def copy(self, statement):
        with self.debug_sql(statement):
            return self.cursor.copy(statement)
