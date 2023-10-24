import re
from string import ascii_letters, digits
from secrets import choice
from datetime import datetime
from typing import List, Dict, Literal, Any, Set
from pydantic import BaseModel, Field, validator, root_validator

from stores import pg_store
from tapisservice.logs import get_logger
logger = get_logger(__name__)

from sqlalchemy import UniqueConstraint
from sqlalchemy.inspection import inspect
from sqlmodel import Field, Session, SQLModel, select, JSON, Column


class TapisApiModel(BaseModel):
    class Config:
        validate_assignment = True
        extra = "forbid"

class TapisModel(SQLModel):
    #__table_args__ = ({"schema": "defaulttables"},)
    class Config:
        orm_mode = True
        validate_assignment = True
        extra = "forbid"

    @staticmethod
    def get_site_tenant_session(obj={}, tenant=None, site=None):
        # functions with self can provide self, otherwise provide tenant and site.
        tenant_id = tenant or getattr(obj, 'tenant_id', None) or 'tacc'
        site_id = site or getattr(obj, 'site_id', None) or 'tacc'
        logger.info(f"Using site: {site_id}; tenant: {tenant_id}. Getting tenant pg obj.")
        store = pg_store[site_id][tenant_id]
        logger.debug(f"Using site: {site_id}; tenant: {tenant_id}; Session: {Session}.")
        return site_id, tenant_id, store

    def db_create(self):
        """
        Creates a new row in the given table. Returns the primary key ID of the new row.
        """
        site, tenant, store = self.get_site_tenant_session(obj=self)
        table_name = self.table_name()
        logger.info(f'Top of {table_name}.db_create() for site: {site}; tenant: {tenant}.')

        # Run command
        store.run("add", self)

        logger.info(f"Row successfully created in table {tenant}.{table_name}.")
        return self

    def db_update(self, log = None):
        """
        Updates based on everything in this instance
        """
        site, tenant, store = self.get_site_tenant_session(obj=self)
        table_name = self.table_name()
        logger.info(f'Top of {table_name}.db_update() for tenant.site: {tenant}.{site}')

        # We write logs when:
        # 1. log is given
        # 2. it's a pod
        # 3a. if there's no current action_logs (after a migration)
        # 3b. or if log is not in the most recent action_logs log
        if table_name == 'pod' and log and (not self.action_logs or log not in self.action_logs[-1]):
            self.action_logs.append(f"{datetime.utcnow().strftime('%y/%m/%d %H:%M')}: {log}")

        # Run command
        store.run("merge", self)
        
        logger.info(f"Row successfully updated in table {tenant}.{table_name}.")
        return self

    def db_delete(self):
        """
        Deletes db_object
        """
        site, tenant, store = self.get_site_tenant_session(obj=self)
        table_name = self.table_name()
        logger.info(f'Top of {table_name}.db_delete() for tenant.site: {tenant}.{site}')

        # Run command
        store.run("delete", self)
        
        logger.info(f"Row successfully deleted from table {tenant}.{table_name}.")
        return self

    def get_permissions(self):
        # create permissions dict {"username": [roles], ...} with current permissions.
        perm_dict = {}
        for permission in self.permissions:
            user, level = permission.split(':')
            perm_dict[user] = level
        return perm_dict

    @classmethod
    def table_name(cls):
        return str(cls.__name__).lower()

    @classmethod
    def from_db(cls, db_dict):
        """Construct a DAO from a db dict."""
        return cls(**db_dict)

    @classmethod
    def db_get_where(cls, where_params: List[List], tenant, site):
        """
        Gets the row with given primary key from the specified table.
        where_params = [key, oper, val]
        RETURNS CLASS
        """
        site, tenant, store = cls.get_site_tenant_session(tenant=tenant, site=site)
        table_name = cls.table_name()
        logger.debug(f'Top of {table_name}.db_get_all() for tenant.site: {tenant}.{site}')

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
        results = store.run("execute", stmt, scalars=True, all=True)

        return results

    @classmethod
    def db_get_with_pk(cls, pk_id, tenant, site):
        """
        Gets the row with given primary key from the specified table.
        RETURNS CLASS
        """
        site, tenant, store = cls.get_site_tenant_session(tenant=tenant, site=site)
        table_name = cls.table_name()
        logger.debug(f'Top of {table_name}.db_get_all() for tenant.site: {tenant}.{site}')

        # Create statement
        primary_key = inspect(cls).primary_key[0].name
        stmt = select(cls).where(eval(f"cls.{primary_key}.in_([pk_id])"))

        # Run command
        result = store.run("scalar", stmt)

        return result

    @classmethod
    def db_get_all(cls, tenant, site):
        """
        Gets the row with given primary key from the specified table.
        """
        site, tenant, store = cls.get_site_tenant_session(tenant=tenant, site=site)
        table_name = cls.table_name()
        logger.debug(f'Top of {table_name}.db_get_all() for tenant.site: {tenant}.{site}')

        # Create statement
        stmt = select(cls)

        # Run command
        results = store.run("execute", stmt, scalars=True, all=True)

        logger.info(f"Got rows from table {tenant}.{table_name}.")

        return results
