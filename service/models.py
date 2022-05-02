import re
from string import ascii_letters, digits
from secrets import choice
from datetime import datetime
from typing import List, Dict, Literal, Any, Set
from pydantic import BaseModel, Field, PrivateAttr, validator, root_validator
from tomlkit import table

from utils import parse_object_data
from stores import pg_store
from req_utils import g
from tapisservice.errors import DAOError
from tapisservice.logs import get_logger
logger = get_logger(__name__)

from sqlalchemy.inspection import inspect
from sqlmodel import Field, Session, SQLModel, select, JSON, Column

class TapisImportModel(BaseModel):
    class Config:
        validate_assignment = True
        extra = "forbid"

class TapisModel(SQLModel):
    class Config:
        orm_mode = True
        validate_assignment = True
        extra = "forbid"

    @staticmethod
    def get_site_tenant_engine(obj={}, tenant=None, site=None):
        # functions with self can provide self, otherwise provide tenant and site.
        tenant_id = tenant or getattr(obj, 'tenant_id', None) or 'tacc'
        site_id = site or getattr(obj, 'site_id', None) or 'tacc'
        store = pg_store[site_id][tenant_id]
        logger.info(f"Using site: {site_id}; tenant: {tenant_id}; store_engine: {store.engine}.")
        return site_id, tenant_id, store

    def db_create(self):
        """
        Creates a new row in the given table. Returns the primary key ID of the new row.
        """
        site, tenant, store = self.get_site_tenant_engine(obj=self)
        table_name = self.table_name()
        logger.info(f'Top of {table_name}.db_create() for site: {site}; tenant: {tenant}.')

        # Run command
        self_copy = self.copy() # Anything that goes into session is flushed at session end.
        store.run_fn('add', self)
        logger.info(f"Row successfully created in table {tenant}.{table_name}.")
        return self_copy

    def db_update(self):
        """
        Updates based on everything in this instance
        """
        site, tenant, store = self.get_site_tenant_engine(obj=self)
        table_name = self.table_name()
        logger.info(f'Top of {table_name}.db_update() for tenant.site: {tenant}.{site}')

        # Run command
        self_copy = self.copy() # Anything that goes into session is flushed at session end.
        store.run_fn('merge', self)
        logger.info(f"Row successfully updated in table {tenant}.{table_name}.")
        return self_copy

    @classmethod
    def table_name(cls):
        return str(cls.__name__).lower()

    @classmethod
    def from_db(cls, db_dict):
        """Construct a DAO from a db dict."""
        return cls(**db_dict)

    @classmethod
    def db_get_DAO(cls, pk_id):
        """Construct a DAO from a db dict queried with pk_id."""
        return cls.from_db(cls.db_get(pk_id))

    @classmethod
    def db_get_where(cls, where_params: List[List], tenant, site):
        """
        Gets the row with given primary key from the specified table.
        where_params = [key, oper, val]
        RETURNS CLASS
        """
        site, tenant, store = cls.get_site_tenant_engine(tenant=tenant, site=site)
        table_name = cls.table_name()
        logger.info(f'Top of {table_name}.db_get_all() for tenant.site: {tenant}.{site}')

        if not where_params:
            raise ValueError(f"where_dict must be specfied for db_get_where. Got empty")

        oper_aliases = {'.neq': '!=',
                        '.eq': '==',
                        '.lte': '<=',
                        '.lt': '<',
                        '.gte': '>=',
                        '.gt': '>',
                        '.nin': 'laterprocessing',
                        '.in': 'laterprocessing'}

        # Create base statement
        stmt = select(cls)
        for key, oper, val in where_params:
            if key not in cls.__fields__.keys():
                raise KeyError(f"key: {key} not found in model attrs: {cls.__fields__.keys()}")
            if oper not in oper_aliases:
                raise KeyError(f"oper: {oper} not found in oper aliases: {oper_aliases}")
            
            # Create where statement and add to stmt
            if oper == '.in':
                stmt = stmt.where(eval(f"cls.{key}.in_({val})")) # User.key.in_([123,456])
            elif oper == '.nin':
                stmt = stmt.where(eval(f"cls.{key}.not_in({val})")) # User.key.in_([123,456])
            else:
                if isinstance(val, str):
                    stmt = stmt.where(eval(f"cls.{key} {oper_aliases[oper]} '{val}'"))
                else:
                    stmt = stmt.where(eval(f"cls.{key} {oper_aliases[oper]} {val}"))

        # Run command
        results, _, _ = store.run_fn('exec', stmt, array_parse=True)

        return results

    @classmethod
    def db_get_with_pk(cls, pk_id, tenant, site):
        """
        Gets the row with given primary key from the specified table.
        RETURNS CLASS
        """
        site, tenant, store = cls.get_site_tenant_engine(tenant=tenant, site=site)
        table_name = cls.table_name()
        logger.info(f'Top of {table_name}.db_get_all() for tenant.site: {tenant}.{site}')

        # Create command
        primary_key = inspect(cls).primary_key[0].name
        stmt = select(cls).where(eval(f"cls.{primary_key}.in_([pk_id])"))

        # Run command
        result, _, _ = store.run_fn('scalar', stmt)
        return result

    @classmethod
    def db_get_all(cls, tenant, site):
        """
        Gets the row with given primary key from the specified table.
        """
        site, tenant, store = cls.get_site_tenant_engine(tenant=tenant, site=site)
        table_name = cls.table_name()
        logger.info(f'Top of {table_name}.db_get_all() for tenant.site: {tenant}.{site}')

        # Run command
        results = store.run_fn("query", cls, all=True, array_parse=True)
        logger.info(f"Got rows from table {tenant}.{table_name}.")

        return results


#schema https://pydantic-docs.helpmanual.io/usage/schema/
class ExportedData(TapisModel, table=True, validate=True):
    # Required
    source_pod: str | None = Field(None, description = "Time (UTC) that this node was created.", primary_key = True)

    # Optional
    tag: List[str] = Field([], description = "Roles required to view this pod")
    description: str | None = Field(None, description = "Time (UTC) that this node was created.")
    roles_required: List[str] = Field([], description = "Roles required to view this pod")

    # Provided
    tenant_id: str = Field(None, description = "Tapis tenant used during creation of this pod.")
    site_id: str = Field(None, description = "Tapis site used during creation of this pod.")
    creation_ts: datetime | None = Field(None, description = "Time (UTC) that this node was created.")
    update_ts: datetime | None = Field(None, description = "Time (UTC) that this node was created.")
    roles_inherited: List[str] = Field([], description = "Inherited roles required to view this pod")
    export_path: str | None = Field(None, description = "Time (UTC) that this node was created.")
    source_owner: str | None = Field(None, description = "Time (UTC) that this node was created.")

class Pod(TapisModel, table=True, validate=True):
    # Required
    pod_name: str = Field(..., description = "Name of this pod.", primary_key = True)

    # Optional
    description: str = Field("", description = "Description of this pod.")
    database_type: str = Field("neo4j", description = "Which database this pod should attempt to create.")
    data_requests: List[str] = Field([], description = "Requested pod names.")
    roles_required: List[str] = Field([], description = "Roles required to view this pod")

    # Provided
    tenant_id: str = Field(g.request_tenant_id or 'tacc', description = "Tapis tenant used during creation of this pod.")
    site_id: str = Field(g.site_id or 'tacc', description = "Tapis site used during creation of this pod.")
    k8_name: str = Field(None, description = "Name to use for Kubernetes name.")
    status: str = Field("SUBMITTED", description = "Status of pod.")
    container_status: Dict = Field({}, description = "Status of container if exists. Gives phase.", sa_column=Column(JSON))
    data_attached: List[str] = Field([], description = "Data attached.")
    roles_inherited: List[str] = Field([], description = "Inherited roles required to view this pod")
    creation_ts: datetime | None = Field(None, description = "Time (UTC) that this node was created.")
    update_ts: datetime | None = Field(None, description = "Time (UTC) that this node was created.")
    pod_owner: str = Field(None, description = "Name of pod owner.")
    # attempt_naive_import:
    # naive_import_command: str | None = None
    # custom_import_command:
    # custom_refresh_command:
    # auto_refresh: bool

    @validator('pod_name')
    def check_pod_name(cls, v):
        # In case we want to add reserved keywords.
        reserved_names = []
        if v in reserved_names:
            raise ValueError(f"name overlaps with reserved names: {reserved_names}")
        # Regex match full name to ensure a-z0-9.
        res = re.fullmatch(r'[a-z][a-z0-9]+', v)
        if not res:
            raise ValueError(f"name must be lowercase alphanumeric. First character must be alpha.")
        return v

    @validator('database_type')
    def check_database_type(cls, v):
        database_types = ['neo4j', 'test-still-neo4j']
        if not v in database_types:
            raise ValueError(f"database_type must be in {database_types}")
        return v

    @root_validator(pre=False)
    def set_k8_name(cls, values):
        site_id = values.get('site_id')
        tenant_id = values.get('tenant_id')
        pod_name = values.get('pod_name')
        # kgservice-<site>-<tenant>-<pod_name>
        values['k8_name'] = f"kgservice-{site_id}-{tenant_id}-{pod_name}"
        return values


class Password(TapisModel, table=True, validate=True):
    # Required
    pod_name: str = Field(..., description = "Name of this pod.", primary_key = True)
    # Provided
    admin_username: str = Field("kgservice", description = "Admin username for pod.")
    admin_password: str = Field(None, description = "Admin password for pod.")
    user_username: str = Field(None, description = "User username for pod.")
    user_password: str = Field(None, description = "User password for pod.")
    
    @validator('admin_password')
    def add_admin_password(cls, v):
        password = ''.join(choice(ascii_letters + digits) for i in range(30))
        return password

    @validator('user_password')
    def add_user_password(cls, v):
        password = ''.join(choice(ascii_letters + digits) for i in range(30))
        return password

    @root_validator(pre=False)
    def set_user_username(cls, values):
        values['user_username'] = values.get('pod_name')
        return values


# class TapisReturn(TapisModel):
#     message: str = "The request was successful."
#     metadata: Dict = {}
#     result: str | List | int | Dict | None = None
#     status: str = "success"
#     version: str = "dev"


class NewPod(TapisImportModel):
    """
    Object with fields that users are allowed to specify for the Pod class.
    """
    # Required
    pod_name: str = Field(..., description = "Name of this pod.")

    # Optional
    description: str = Field("", description = "Description of this pod.")
    database_type: Literal["neo4j", "temp"] = Field("neo4j", description = "Which database this pod should attempt to create.")
    data_requests: List[str] = Field([], description = "Requested pod names.")
    roles_required: List[str] = Field([], description = "Roles required to view this pod")


class UpdatePod(TapisImportModel):
    """
    Object with fields that users are allowed to specify for the Pod class.
    """
    # Required
    pod_name: str = Field(..., description = "Name of this pod.")

    # Optional
    description: str = Field("", description = "Description of this pod.")
    database_type: Literal["neo4j", "temp"] = Field("neo4j", description = "Which database this pod should attempt to create.")
    data_requests: List[str] = Field([], description = "Requested pod names.")
    roles_required: List[str] = Field([], description = "Roles required to view this pod")











# # class TapisOldModel(BaseModel):
# #     _table_name: str = PrivateAttr()


#     def db_create(self):
#         """
#         Creates a new row in the given table. Returns the primary key ID of the new row.
#         """
#         if not self._table_name in ["pods", "exported_data"]:
#             raise KeyError("Table for db_create must be pod or exported_data.")

#         tenant = g.request_tenant_id
#         site = g.site_id
#         logger.info(f'Top of {self._table_name}.db_create() for tenant.site: {tenant}.{site}')

#         # Create query parts
#         primary_key = list(SCHEMAS[self._table_name])[0]
#         table_keys = list(SCHEMAS[self._table_name].keys())
#         columns_str = ", ".join(table_keys)
#         values_str = ", ".join(f"%({key})s" for key in table_keys) # format is %(keyname)s, ...
#         params = {}
#         for key in table_keys:
#             params[key] = eval(f"self.{key}")
#         command = f"INSERT INTO {tenant}.{self._table_name} ({columns_str}) VALUES ({values_str}) RETURNING {primary_key}"

#         # Run command
#         try:
#             obj_description, obj_unparsed_data, _ = pg_store[site][tenant].run(command, params)
#             result = parse_object_data(obj_description, obj_unparsed_data)
#             # We used "RETURNING %pk" in command, so we should get the PK value for later use.
#             result_id = result[0]
#             logger.info(f"Rows successfully created in table {tenant}.{self._table_name}.")
#         except Exception as e:
#             msg = f"Error creating row in table {tenant}.{self._table_name}. e: {repr(e)}"
#             logger.error(msg)
#             e.args = [msg]
#             raise e
#         return result

#     def db_update(self):
#         """
#         Updates based on everything in this instance
#         """
#         if not self._table_name in ["pods", "exported_data"]:
#             raise KeyError("Table for db_get must be pod or exported_data.")

#         tenant = g.request_tenant_id
#         site = g.site_id
#         logger.info(f'Top of {self._table_name}.db_update() for tenant.site: {tenant}.{site}')

#         # UPDATE weather SET temp_lo = $(temp_lo)s, temp_hi = $(temp_hi)s, prcp = $(prcp)s WHERE pk = pk;
#         # Create query parts
#         primary_key = list(SCHEMAS[self._table_name])[0]
#         table_keys = list(SCHEMAS[self._table_name].keys())
#         update_str = ", ".join(f"{key} = $({key})s" for key in table_keys) # temp_lo = $(temp_lo)s, temp = $(temp)s ...
#         params = {}
#         for key in table_keys:
#             params[key] = eval(f"self.{key}")
#         command = f"UPDATE {tenant}.{self._table_name} SET {update_str} WHERE {primary_key} = {eval(f'self.{primary_key}')}"

#         try:
#             _, _, affected_rows = pg_store[site][tenant].run(command)
#             logger.info(f"Successfully updated table {tenant}.{self._table_name}.")
#         except Exception as e:
#             msg = f"Error updating table {tenant}.{self._table_name}. e: {repr(e)}"
#             logger.error(msg)
#             e.args = [msg]
#             raise e

#         # Might want this to not actually error out.
#         if affected_rows == 0:
#             msg = f"Update row affected 0 rows."
#             logger.error(msg)
#             raise DAOError(msg)

#         return

#     def update_db(self, db_dict):
#         """Construct a DAO from a db dict."""
#         return self(**db_dict)

#     @classmethod
#     def from_db(cls, db_dict):
#         """Construct a DAO from a db dict."""
#         return cls(**db_dict)

#     @classmethod
#     def db_get_DAO(cls, pk_id):
#         """Construct a DAO from a db dict queried with pk_id."""
#         return cls.from_db(cls.db_get(pk_id))

#     @classmethod
#     def db_get(cls, pk_id):
#         """
#         Gets the row with given primary key from the specified table.
#         """
#         if not cls._table_name in ["pods", "exported_data"]:
#             raise KeyError("Table for db_get must be pod or exported_data.")

#         tenant = g.request_tenant_id or 'tacc'
#         site = g.site_id or 'tacc'
#         logger.info(f'Top of {cls._table_name}.db_get({pk_id}) for tenant.site: {tenant}.{site}')

#         # Create query
#         primary_key = list(SCHEMAS[cls._table_name])[0]
#         command = f"SELECT * FROM {tenant}.{cls._table_name} WHERE {primary_key} = %s;"

#         # Run command
#         try:
#             obj_description, obj_unparsed_data, _ = pg_store[site][tenant].run(command, [pk_id])
#             result = parse_object_data(obj_description, obj_unparsed_data)
#             logger.info(f"Row {pk_id} successfully retrieved from table {tenant}.{cls._table_name}.")
#         except Exception as e:
#             msg = f"Error getting row in table {tenant}.{cls._table_name}. e: {repr(e)}"
#             logger.error(msg)
#             e.args = [msg]
#             raise e

#         if len(result) == 0:
#             msg = f"Found no object with identifier '{pk_id}'"
#             logger.error(msg)
#             raise DAOError(msg)

#         return result[0]
    
#     @classmethod
#     def db_get_all(cls):
#         """
#         Gets the row with given primary key from the specified table.
#         """
#         if not cls._table_name in ["pods", "exported_data"]:
#             raise KeyError("Table for db_get must be pod or exported_data.")

#         tenant = g.request_tenant_id or 'tacc'
#         site = g.site_id or 'tacc'
#         logger.info(f'Top of {cls._table_name}.db_get_all() for tenant.site: {tenant}.{site}')

#         # Create query
#         command = f"SELECT * FROM {tenant}.{cls._table_name};"

#         # Run command
#         try:
#             obj_description, obj_unparsed_data, _ = pg_store[site][tenant].run(command)
#             result = parse_object_data(obj_description, obj_unparsed_data)
#             logger.info(f"Rows successfully retrieved from table {tenant}.{cls._table_name}.")
#         except Exception as e:
#             msg = f"Error getting rows from table {tenant}.{cls._table_name}. e: {repr(e)}"
#             logger.error(msg)
#             e.args = [msg]
#             raise e

#         return result

# #     @classmethod
# #     def db_update_with_pk(cls, pk_id, new_data):
# #         """
# #         Updates a specified row on a table with the given columns and associated values.
# #         """
# #         if not cls._table_name in ["pods", "exported_data"]:
# #             raise KeyError("Table for db_get must be pod or exported_data.")

# #         tenant = g.request_tenant_id or 'tacc'
# #         site = g.site_id or 'tacc'
# #         logger.info(f'Top of {cls._table_name}.db_update({pk_id}, {new_data}) for tenant.site: {tenant}.{site}')



# #         # From create
# #         # Create query parts
# #         primary_key = list(SCHEMAS[cls._table_name])[0]
# #         table_keys = list(SCHEMAS[cls._table_name].keys())

# #         columns_str = ", ".join(table_keys)
# #         values_str = ", ".join(f"%({key})s" for key in table_keys) # format is %(keyname)s, ...
# #         params = {}
# #         for key in table_keys:
# #             params[key] = eval(f"cls.{key}")
# #         command = f"INSERT INTO {tenant}.{cls._table_name} ({columns_str}) VALUES ({values_str}) RETURNING {primary_key}"




# #         # from old update
# #         # Create query
# #         command = f"UPDATE {tenant}.{cls._table_name} SET"

# #         for col in new_data:
# #             if type(new_data[col]) == str:
# #                 command = f"{command} {col} = (\'{new_data[col]}\'), "
# #             else:
# #                 command = f"{command} {col} = ({new_data[col]}), "
# #         if type(pk_id) == 'int' or type(pk_id) == 'float':
# #             command = command[:-2] + f" WHERE {primary_key} = {pk_id};"
# #         else:
# #             command = command[:-2] + f" WHERE {primary_key} = '{pk_id}';"


        

# #         # Run command
# #         try:
# #             _, _, affected_rows = pg_store[site][tenant].run(command)
# #             logger.info(f"Row {pk_id} successfully updated in table {tenant}.{cls._table_name}.")
# #         except Exception as e:
# #             msg = f"Error updating row ID {pk_id} in table {tenant}.{cls._table_name}. e: {repr(e)}"
# #             logger.error(msg)
# #             e.args = [msg]
# #             raise e

# #         if affected_rows == 0:
# #             msg = f"Update row affected 0 rows, expected to update 1."
# #             logger.error(msg)
# #             raise DAOError(msg)

# #         return













###########
#############
#BACKUP 2
############
##########
# class TapisModel(SQLModel):
#     class Config:
#         orm_mode = True
#         validate_assignment = True
#         extra = "forbid"

#     @staticmethod
#     def get_site_tenant_engine():
#         tenant = g.user_set_tenant or g.request_tenant_id or 'tacc'
#         site = g.user_set_site or g.site_id or 'tacc'
#         store = pg_store[site][tenant]
#         logger.info(f"Using site: {site}; tenant: {tenant}; store_obj: {store}.")
#         return site, tenant, store

#     def db_create(self):
#         """
#         Creates a new row in the given table. Returns the primary key ID of the new row.
#         """
#         site, tenant, store = self.get_site_tenant_engine()
#         table_name = self.table_name()
#         logger.info(f'Top of {table_name}.db_create() for site: {site}; tenant: {tenant}.')

#         store.run_fn('add', self)
#         logger.info(f"Row successfully created in table {tenant}.{table_name}.")
#         return self


#         # # Run command
#         # self_copy = self.copy() # Anything that goes into session is flushed at session end.
#         # with Session(engine, expire_on_commit=False) as session:
#         #     #logger.info(f"PostgresStore.run; Command: {command}; Params: {params}")
#         #     session.add(self)
#         #     session.commit()
#         # logger.info(f"Row successfully created in table {tenant}.{table_name}.")
#         # return self_copy

#     def db_update(self):
#         """
#         Updates based on everything in this instance
#         """
#         table_name = self.table_name()
#         tenant = g.request_tenant_id or 'tacc'
#         site = g.site_id or 'tacc'
#         logger.info(f'Top of {table_name}.db_create() for tenant.site: {tenant}.{site}')

#         # Run command
#         self_copy = self.copy() # Anything that goes into session is flushed at session end.
#         with Session(pg_store[site][tenant].engine) as session:
#             #logger.info(f"PostgresStore.run; Command: {command}; Params: {params}")
#             result = session.merge(self)
#         logger.info(f"Row successfully updated in table {tenant}.{table_name}.")
#         return self_copy

#     @classmethod
#     def table_name(cls):
#         return str(cls.__name__).lower()

#     @classmethod
#     def from_db(cls, db_dict):
#         """Construct a DAO from a db dict."""
#         return cls(**db_dict)

#     @classmethod
#     def db_get_DAO(cls, pk_id):
#         """Construct a DAO from a db dict queried with pk_id."""
#         return cls.from_db(cls.db_get(pk_id))

#     @classmethod
#     def db_get_where(cls, where_params: List):
#         """
#         Gets the row with given primary key from the specified table.
#         where_dict = {"where key": "where_val"}
#         RETURNS CLASS
#         """
#         table_name = cls.table_name()
#         tenant = g.request_tenant_id or 'tacc'
#         site = g.site_id or 'tacc'
#         logger.info(f'Top of {table_name}.db_get_all() for tenant.site: {tenant}.{site}')

#         if not where_params:
#             raise ValueError(f"where_dict must be specfied for db_get_where. Got empty")

#         oper_aliases = {'.neq': '!=',
#                         '.eq': '=',
#                         '.lte': '<=',
#                         '.lt': '<',
#                         '.gte': '>=',
#                         '.gt': '>',
#                         '.nin': 'laterprocessing',
#                         '.in': 'laterprocessing'}

#         # Create base statement
#         stmt = select(cls)

#         for key, oper, val in where_params:
#             if key not in cls.dict().keys():
#                 raise KeyError(f"key: {key} not found in model attrs: {cls.dict().keys()}")
#             if oper not in oper_aliases:
#                 raise KeyError(f"oper: {oper} not found in oper aliases: {oper_aliases}")
            
#             # Create where statement and add to stmt
#             if oper == '.in':
#                 stmt = stmt.where(eval(f"cls.{key}.in_({val})")) # User.key.in_([123,456])
#             elif oper == '.nin':
#                 stmt = stmt.where(eval(f"cls.{key}.not_in({val})")) # User.key.in_([123,456])
#             else:
#                 stmt = stmt.where(eval(f"cls.{key} {oper_aliases[oper]} {val}"))

#         # Run command
#         with Session(pg_store[site][tenant].engine) as session:
#             #logger.info(f"PostgresStore.run; Command: {command}; Params: {params}")
#             results = session.exec(stmt)
#             result_list = []
#             for result in results:
#                 result_list.append(result.copy())
#             session.commit()

#         return result_list

#     @classmethod
#     def db_get_with_pk(cls, pk_id):
#         """
#         Gets the row with given primary key from the specified table.
#         RETURNS CLASS
#         """
#         table_name = cls.table_name()
#         tenant = g.request_tenant_id or 'tacc'
#         site = g.site_id or 'tacc'
#         logger.info(f'Top of {table_name}.db_get_all() for tenant.site: {tenant}.{site}')

#         # Run command
#         with Session(pg_store[site][tenant].engine) as session:
#             #logger.info(f"PostgresStore.run; Command: {command}; Params: {params}")
#             primary_key = inspect(cls).primary_key[0].name
#             stmt = select(cls).where(eval(f"cls.{primary_key}.in_([pk_id]))"))
#             result = session.scalar(stmt)
#             output = result.copy()
#             session.commit()

#         return output

#     @classmethod
#     def db_get_all(cls):
#         """
#         Gets the row with given primary key from the specified table.
#         """
#         table_name = cls.table_name()
#         tenant = g.request_tenant_id or 'tacc'
#         site = g.site_id or 'tacc'
#         logger.info(f'Top of {table_name}.db_get_all() for tenant.site: {tenant}.{site}')

#         # Run command
#         with Session(pg_store[site][tenant].engine) as session:
#             #logger.info(f"PostgresStore.run; Command: {command}; Params: {params}")
#             results = session.query(cls).all()
#             logger.info(f"Got rows from table {tenant}.{table_name}.")

#             result_list = []
#             for result in results:
#                 result_list.append(result.copy())
#             session.commit()

#         return result_list

