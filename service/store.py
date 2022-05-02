
from collections.abc import MutableMapping
from datetime import datetime, timedelta
from attr import validate
from neo4j import GraphDatabase
from pydantic import validate_arguments
from typing import Literal, Any, Dict, List
from enum import Enum
from psycopg_pool import ConnectionPool
from psycopg.errors import UniqueViolation, ProgrammingError, DatabaseError

import re
import json
import os
import urllib.parse

import pprint

from tapisservice.errors import DAOError
from tapisservice.config import conf
from tapisservice.logs import get_logger
logger = get_logger(__name__)

from sqlmodel import create_engine, Session, select

class StoreMutexException(Exception):
    pass

class AbstractStore(MutableMapping):
    """A persitent dictionary."""

    def __getitem__(self, key):
        pass

    def __setitem__(self, key, value):
        pass

    def __delitem__(self, key):
        pass

    def __iter__(self):
        """Iterator for the keys."""
        pass

    def __len__(self):
        """Size of db."""
        pass

    def set_with_expiry(self, key, obj):
        """Set `key` to `obj` with automatic expiration of the configured seconds."""
        pass

    def update(self, key, field, value):
        "Atomic ``self[key][field] = value``."""
        pass

    def pop_field(self, key, field):
        "Atomic pop ``self[key][field]``."""
        pass

    def update_subfield(self, key, field1, field2, value):
        "Atomic ``self[key][field1][field2] = value``."""
        pass

    def getset(self, key, value):
        "Atomically: ``self[key] = value`` and return previous ``self[key]``."
        pass

    def mutex_acquire(self, key):
        """Try to use key as a mutex.
        Raise StoreMutexException if not available.
        """

        busy = self.getset(key, True)
        if busy:
            raise StoreMutexException(f'{key} is busy')

    def mutex_release(self, key):
        self[key] = False


class PostgresStore(AbstractStore):
    """
    Postgres Store object
    """
    @validate_arguments
    def __init__(self,
                 username: str,
                 password: str,
                 host: str,
                 dbname: str | None = None,
                 dbschema: str | None = None,
                 port: int | None = None, # not currently used
                 kwargs: dict[str, str] = {}):
        
        logger.info(f"Top of PostgresStore.__init__().")

        try:
            username = urllib.parse.quote_plus(username)
            password = urllib.parse.quote_plus(password)
        except Exception as e:
            msg = f"PostgresStore.__init__: Error parsing user/pass with urllib.parse. e: {repr(e)}"
            logger.critical(msg)
            e.args = [msg]
            raise e
        logger.info(f"Using username {username} and password: ***")

        #postgresql://[userspec@][hostspec][/dbname][?paramspec]
        conninfo = f"postgresql://{username}:{password}@{host}"
        if dbname:
            conninfo += f"/{dbname}"
        if dbschema:
            conninfo += f"?options=-csearch_path%3Ddbo,{dbschema}"
        logger.info(f"Using conninfo: {conninfo}, with kwargs: {kwargs}")

        self.engine = create_engine(conninfo)

    @validate_arguments
    def run(self,
            command: str,
            params: Dict[str, Any] | List | None = None,
            autocommit: bool = False):

        if autocommit:
            engine = self.engine.execution_options(isolation_level="AUTOCOMMIT")
        else:
            engine = self.engine

        with Session(engine) as session:
            try:
                logger.info(f"PostgresStore.run; Command: {command} - Params: {params}")
                result = session.execute(command, params)
            except UniqueViolation as e:
                # For unique violations, we get just the violation and raise that msg.
                msg = re.findall(r'DETAIL:  (.*)', str(e))
                if msg:
                    msg = msg[0]
                else:
                    msg = repr(e)
                raise DAOError(msg)
            except DatabaseError as e:
                msg = f"Error accessing database: e: {repr(e)}"
                # Check to see if this is actually a unique constraint collision.
                if "relation" in e.args[0] and "already exists" in e.args[0]:
                    msg = f"Constraints names must be unique. e: {repr(e)}" 
                logger.error(msg)
                e.args = [msg]
                raise e
            except Exception as e:
                msg = f"Error executing command: {command} - e: {repr(e)}"
                logger.error(msg)
                e.args = [msg]
                raise e

            # Get amount of rows affected. (-1 means that query doesn't return anything relevant for .rowcount)
            try:
                affected_rows = result.rowcount
            except:
                try:
                    affected_rows = result.cursor.rowcount
                except:
                    affected_rows = -1

            # Try and get object description, also data. Only gets return data, so usually just return an empty array.
            try:
                obj_description = result.description
            except:
                try:
                    obj_description = result.cursor.description
                except:
                    obj_description = []


            # Try and get data back, note we use a custom psycopg row_factor to format returns.
            try:
                obj_unparsed_data = result.fetchall()
            except:
                try:
                    obj_unparsed_data = result.cursor.fetchall()
                except:
                    # Got here because there's no results to fetch
                    obj_unparsed_data = []

        logger.info(f'PostgresStore.run: object_description: {obj_description}, unparsed_data: {obj_unparsed_data}, '
                    f'affected_rows: {affected_rows}')
        return obj_description, obj_unparsed_data, affected_rows




    @validate_arguments
    def run_fn(self,
               fn_name,
               fn_param,
               array_parse=False,
               all=False,
               autocommit=False):

        if autocommit:
            engine = self.engine.execution_options(isolation_level="AUTOCOMMIT")
        else:
            engine = self.engine

        with Session(engine, expire_on_commit=False) as session:
            try:
                logger.info(f"PostgresStore.fn; Command: {fn_name} - Params: {fn_param}")
                fn_to_run = getattr(session, fn_name)
                if all:
                    output = fn_to_run(fn_param).all()
                else:
                    output = fn_to_run(fn_param)

                ####### THIS CAN MESS THINGS UP!/You might need it.
                ####### Don't forget about this!
                # Gets array out of output.
                if array_parse:
                    output_list = []
                    for result in output:
                        output_list.append(result.copy())
                    output = output_list

                session.commit()
            except UniqueViolation as e:
                # For unique violations, we get just the violation and raise that msg.
                msg = re.findall(r'DETAIL:  (.*)', str(e))
                if msg:
                    msg = msg[0]
                else:
                    msg = repr(e)
                raise DAOError(msg)
            except DatabaseError as e:
                msg = f"Error accessing database: e: {repr(e)}"
                # Check to see if this is actually a unique constraint collision.
                if "relation" in e.args[0] and "already exists" in e.args[0]:
                    msg = f"Constraints names must be unique. e: {repr(e)}" 
                logger.error(msg)
                e.args = [msg]
                raise e
            except AttributeError as e:
                msg = f"Session doesn't have specified attribute. e: {e}"
                logger.error(msg)
                e.args = [msg]
                raise e
            except Exception as e:
                msg = f"Error executing command: {fn_name} - e: {repr(e)}"
                logger.error(msg)
                e.args = [msg]
                raise e

            # Get amount of rows affected. (-1 means that query doesn't return anything relevant for .rowcount)
            try:
                affected_rows = output.rowcount
            except:
                try:
                    affected_rows = output.cursor.rowcount
                except:
                    affected_rows = -1

            # Try and get object description, also data. Only gets return data, so usually just return an empty array.
            try:
                obj_description = output.description
            except:
                try:
                    obj_description = output.cursor.description
                except:
                    obj_description = []

        logger.info(f'PostgresStore.run: output: {output}, object_description: {obj_description}, affected_rows: {affected_rows}')
        return output, obj_description, affected_rows
