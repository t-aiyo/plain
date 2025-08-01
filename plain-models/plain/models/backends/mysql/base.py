"""
MySQL database backend for Plain.

Requires mysqlclient: https://pypi.org/project/mysqlclient/
"""

from functools import cached_property

import MySQLdb as Database
from MySQLdb.constants import CLIENT, FIELD_TYPE
from MySQLdb.converters import conversions

from plain.exceptions import ImproperlyConfigured
from plain.models.backends import utils as backend_utils
from plain.models.backends.base.base import BaseDatabaseWrapper
from plain.models.db import IntegrityError
from plain.utils.regex_helper import _lazy_re_compile

from .client import DatabaseClient
from .creation import DatabaseCreation
from .features import DatabaseFeatures
from .introspection import DatabaseIntrospection
from .operations import DatabaseOperations
from .schema import DatabaseSchemaEditor
from .validation import DatabaseValidation

# MySQLdb returns TIME columns as timedelta -- they are more like timedelta in
# terms of actual behavior as they are signed and include days -- and Plain
# expects time.
plain_conversions = {
    **conversions,
    **{FIELD_TYPE.TIME: backend_utils.typecast_time},
}

# This should match the numerical portion of the version numbers (we can treat
# versions like 5.0.24 and 5.0.24a as the same).
server_version_re = _lazy_re_compile(r"(\d{1,2})\.(\d{1,2})\.(\d{1,2})")


class CursorWrapper:
    """
    A thin wrapper around MySQLdb's normal cursor class that catches particular
    exception instances and reraises them with the correct types.

    Implemented as a wrapper, rather than a subclass, so that it isn't stuck
    to the particular underlying representation returned by Connection.cursor().
    """

    codes_for_integrityerror = (
        1048,  # Column cannot be null
        1690,  # BIGINT UNSIGNED value is out of range
        3819,  # CHECK constraint is violated
        4025,  # CHECK constraint failed
    )

    def __init__(self, cursor):
        self.cursor = cursor

    def execute(self, query, args=None):
        try:
            # args is None means no string interpolation
            return self.cursor.execute(query, args)
        except Database.OperationalError as e:
            # Map some error codes to IntegrityError, since they seem to be
            # misclassified and Plain would prefer the more logical place.
            if e.args[0] in self.codes_for_integrityerror:
                raise IntegrityError(*tuple(e.args))
            raise

    def executemany(self, query, args):
        try:
            return self.cursor.executemany(query, args)
        except Database.OperationalError as e:
            # Map some error codes to IntegrityError, since they seem to be
            # misclassified and Plain would prefer the more logical place.
            if e.args[0] in self.codes_for_integrityerror:
                raise IntegrityError(*tuple(e.args))
            raise

    def __getattr__(self, attr):
        return getattr(self.cursor, attr)

    def __iter__(self):
        return iter(self.cursor)


class DatabaseWrapper(BaseDatabaseWrapper):
    vendor = "mysql"
    # This dictionary maps Field objects to their associated MySQL column
    # types, as strings. Column-type strings can contain format strings; they'll
    # be interpolated against the values of Field.__dict__ before being output.
    # If a column type is set to None, it won't be included in the output.
    data_types = {
        "PrimaryKeyField": "bigint AUTO_INCREMENT",
        "BinaryField": "longblob",
        "BooleanField": "bool",
        "CharField": "varchar(%(max_length)s)",
        "DateField": "date",
        "DateTimeField": "datetime(6)",
        "DecimalField": "numeric(%(max_digits)s, %(decimal_places)s)",
        "DurationField": "bigint",
        "FloatField": "double precision",
        "IntegerField": "integer",
        "BigIntegerField": "bigint",
        "IPAddressField": "char(15)",
        "GenericIPAddressField": "char(39)",
        "JSONField": "json",
        "PositiveBigIntegerField": "bigint UNSIGNED",
        "PositiveIntegerField": "integer UNSIGNED",
        "PositiveSmallIntegerField": "smallint UNSIGNED",
        "SmallIntegerField": "smallint",
        "TextField": "longtext",
        "TimeField": "time(6)",
        "UUIDField": "char(32)",
    }

    # For these data types:
    # - MySQL < 8.0.13 doesn't accept default values and implicitly treats them
    #   as nullable
    # - all versions of MySQL and MariaDB don't support full width database
    #   indexes
    _limited_data_types = (
        "tinyblob",
        "blob",
        "mediumblob",
        "longblob",
        "tinytext",
        "text",
        "mediumtext",
        "longtext",
        "json",
    )

    operators = {
        "exact": "= %s",
        "iexact": "LIKE %s",
        "contains": "LIKE BINARY %s",
        "icontains": "LIKE %s",
        "gt": "> %s",
        "gte": ">= %s",
        "lt": "< %s",
        "lte": "<= %s",
        "startswith": "LIKE BINARY %s",
        "endswith": "LIKE BINARY %s",
        "istartswith": "LIKE %s",
        "iendswith": "LIKE %s",
    }

    # The patterns below are used to generate SQL pattern lookup clauses when
    # the right-hand side of the lookup isn't a raw string (it might be an expression
    # or the result of a bilateral transformation).
    # In those cases, special characters for LIKE operators (e.g. \, *, _) should be
    # escaped on database side.
    #
    # Note: we use str.format() here for readability as '%' is used as a wildcard for
    # the LIKE operator.
    pattern_esc = r"REPLACE(REPLACE(REPLACE({}, '\\', '\\\\'), '%%', '\%%'), '_', '\_')"
    pattern_ops = {
        "contains": "LIKE BINARY CONCAT('%%', {}, '%%')",
        "icontains": "LIKE CONCAT('%%', {}, '%%')",
        "startswith": "LIKE BINARY CONCAT({}, '%%')",
        "istartswith": "LIKE CONCAT({}, '%%')",
        "endswith": "LIKE BINARY CONCAT('%%', {})",
        "iendswith": "LIKE CONCAT('%%', {})",
    }

    isolation_levels = {
        "read uncommitted",
        "read committed",
        "repeatable read",
        "serializable",
    }

    Database = Database
    SchemaEditorClass = DatabaseSchemaEditor
    # Classes instantiated in __init__().
    client_class = DatabaseClient
    creation_class = DatabaseCreation
    features_class = DatabaseFeatures
    introspection_class = DatabaseIntrospection
    ops_class = DatabaseOperations
    validation_class = DatabaseValidation

    def get_database_version(self):
        return self.mysql_version

    def get_connection_params(self):
        kwargs = {
            "conv": plain_conversions,
            "charset": "utf8",
        }
        settings_dict = self.settings_dict
        if settings_dict["USER"]:
            kwargs["user"] = settings_dict["USER"]
        if settings_dict["NAME"]:
            kwargs["database"] = settings_dict["NAME"]
        if settings_dict["PASSWORD"]:
            kwargs["password"] = settings_dict["PASSWORD"]
        if settings_dict["HOST"].startswith("/"):
            kwargs["unix_socket"] = settings_dict["HOST"]
        elif settings_dict["HOST"]:
            kwargs["host"] = settings_dict["HOST"]
        if settings_dict["PORT"]:
            kwargs["port"] = int(settings_dict["PORT"])
        # We need the number of potentially affected rows after an
        # "UPDATE", not the number of changed rows.
        kwargs["client_flag"] = CLIENT.FOUND_ROWS
        # Validate the transaction isolation level, if specified.
        options = settings_dict["OPTIONS"].copy()
        isolation_level = options.pop("isolation_level", "read committed")
        if isolation_level:
            isolation_level = isolation_level.lower()
            if isolation_level not in self.isolation_levels:
                raise ImproperlyConfigured(
                    "Invalid transaction isolation level '{}' specified.\n"
                    "Use one of {}, or None.".format(
                        isolation_level,
                        ", ".join(f"'{s}'" for s in sorted(self.isolation_levels)),
                    )
                )
        self.isolation_level = isolation_level
        kwargs.update(options)
        return kwargs

    def get_new_connection(self, conn_params):
        connection = Database.connect(**conn_params)
        # bytes encoder in mysqlclient doesn't work and was added only to
        # prevent KeyErrors in Plain < 2.0. We can remove this workaround when
        # mysqlclient 2.1 becomes the minimal mysqlclient supported by Plain.
        # See https://github.com/PyMySQL/mysqlclient/issues/489
        if connection.encoders.get(bytes) is bytes:
            connection.encoders.pop(bytes)
        return connection

    def init_connection_state(self):
        super().init_connection_state()
        assignments = []
        if self.features.is_sql_auto_is_null_enabled:
            # SQL_AUTO_IS_NULL controls whether an AUTO_INCREMENT column on
            # a recently inserted row will return when the field is tested
            # for NULL. Disabling this brings this aspect of MySQL in line
            # with SQL standards.
            assignments.append("SET SQL_AUTO_IS_NULL = 0")

        if self.isolation_level:
            assignments.append(
                f"SET SESSION TRANSACTION ISOLATION LEVEL {self.isolation_level.upper()}"
            )

        if assignments:
            with self.cursor() as cursor:
                cursor.execute("; ".join(assignments))

    def create_cursor(self, name=None):
        cursor = self.connection.cursor()
        return CursorWrapper(cursor)

    def _rollback(self):
        try:
            BaseDatabaseWrapper._rollback(self)
        except Database.NotSupportedError:
            pass

    def _set_autocommit(self, autocommit):
        with self.wrap_database_errors:
            self.connection.autocommit(autocommit)

    def check_constraints(self, table_names=None):
        """Check ``table_names`` for rows with invalid foreign key references."""
        with self.cursor() as cursor:
            if table_names is None:
                table_names = self.introspection.table_names(cursor)
            for table_name in table_names:
                primary_key_column_name = self.introspection.get_primary_key_column(
                    cursor, table_name
                )
                if not primary_key_column_name:
                    continue
                relations = self.introspection.get_relations(cursor, table_name)
                for column_name, (
                    referenced_column_name,
                    referenced_table_name,
                ) in relations.items():
                    cursor.execute(
                        f"""
                        SELECT REFERRING.`{primary_key_column_name}`, REFERRING.`{column_name}` FROM `{table_name}` as REFERRING
                        LEFT JOIN `{referenced_table_name}` as REFERRED
                        ON (REFERRING.`{column_name}` = REFERRED.`{referenced_column_name}`)
                        WHERE REFERRING.`{column_name}` IS NOT NULL AND REFERRED.`{referenced_column_name}` IS NULL
                        """
                    )
                    for bad_row in cursor.fetchall():
                        raise IntegrityError(
                            f"The row in table '{table_name}' with primary key '{bad_row[0]}' has an "
                            f"invalid foreign key: {table_name}.{column_name} contains a value '{bad_row[1]}' that "
                            f"does not have a corresponding value in {referenced_table_name}.{referenced_column_name}."
                        )

    def is_usable(self):
        try:
            self.connection.ping()
        except Database.Error:
            return False
        else:
            return True

    @cached_property
    def display_name(self):
        return "MariaDB" if self.mysql_is_mariadb else "MySQL"

    @cached_property
    def data_type_check_constraints(self):
        if self.features.supports_column_check_constraints:
            check_constraints = {
                "PositiveBigIntegerField": "`%(column)s` >= 0",
                "PositiveIntegerField": "`%(column)s` >= 0",
                "PositiveSmallIntegerField": "`%(column)s` >= 0",
            }
            if self.mysql_is_mariadb and self.mysql_version < (10, 4, 3):
                # MariaDB < 10.4.3 doesn't automatically use the JSON_VALID as
                # a check constraint.
                check_constraints["JSONField"] = "JSON_VALID(`%(column)s`)"
            return check_constraints
        return {}

    @cached_property
    def mysql_server_data(self):
        with self.temporary_connection() as cursor:
            # Select some server variables and test if the time zone
            # definitions are installed. CONVERT_TZ returns NULL if 'UTC'
            # timezone isn't loaded into the mysql.time_zone table.
            cursor.execute(
                """
                SELECT VERSION(),
                       @@sql_mode,
                       @@default_storage_engine,
                       @@sql_auto_is_null,
                       @@lower_case_table_names,
                       CONVERT_TZ('2001-01-01 01:00:00', 'UTC', 'UTC') IS NOT NULL
            """
            )
            row = cursor.fetchone()
        return {
            "version": row[0],
            "sql_mode": row[1],
            "default_storage_engine": row[2],
            "sql_auto_is_null": bool(row[3]),
            "lower_case_table_names": bool(row[4]),
            "has_zoneinfo_database": bool(row[5]),
        }

    @cached_property
    def mysql_server_info(self):
        return self.mysql_server_data["version"]

    @cached_property
    def mysql_version(self):
        match = server_version_re.match(self.mysql_server_info)
        if not match:
            raise Exception(
                f"Unable to determine MySQL version from version string {self.mysql_server_info!r}"
            )
        return tuple(int(x) for x in match.groups())

    @cached_property
    def mysql_is_mariadb(self):
        return "mariadb" in self.mysql_server_info.lower()

    @cached_property
    def sql_mode(self):
        sql_mode = self.mysql_server_data["sql_mode"]
        return set(sql_mode.split(",") if sql_mode else ())
