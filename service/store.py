
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
















# PgStore from hand, not sqlalchemy
# class PostgresStore(AbstractStore):
#     """
#     Postgres Store object
#     """
#     @validate_arguments
#     def __init__(self,
#                  username: str,
#                  password: str,
#                  host: str,
#                  dbname: str | None = None,
#                  dbschema: str | None = None,
#                  port: int | None = None, # not currently used
#                  kwargs: dict[str, str] = {}):
        
#         logger.info(f"Top of PostgresStore.__init__().")

#         try:
#             username = urllib.parse.quote_plus(username)
#             password = urllib.parse.quote_plus(password)
#         except Exception as e:
#             msg = f"PostgresStore.__init__: Error parsing user/pass with urllib.parse. e: {repr(e)}"
#             logger.critical(msg)
#             e.args = [msg]
#             raise e
#         logger.info(f"Using username {username} and password: ***")

#         #postgresql://[userspec@][hostspec][/dbname][?paramspec]
#         conninfo = f"postgresql://{username}:{password}@{host}"
#         if dbname:
#             conninfo += f"/{dbname}"
#         if dbschema:
#             conninfo += f"?options=-csearch_path%3Ddbo,{dbschema}"
#         logger.info(f"Using conninfo: {conninfo}, with kwargs: {kwargs}")

#         self.pool = ConnectionPool(conninfo, **kwargs, open=False)

    # @validate_arguments
    # def run(self,
    #         command: str,
    #         params: Dict[str, Any] | List | None = None):

    #     self.pool.open()
    #     # Withs will take care of closing cur and conn in the event of error.
    #     with self.pool.connection() as conn:
    #         with conn.cursor() as cur:
    #             # Attempt execute. psycopg3 uses server side bindings. So we can't print full command.
    #             # Attempt to mogrify (add params to command) command and execute command.
    #             try:
    #                 logger.info(f"PgRun Command: {command}; Params: {params}")
    #                 cur.execute(command, params)
    #             except UniqueViolation as e:
    #                 # For unique violations, we get just the violation and raise that msg.
    #                 msg = re.findall(r'DETAIL:  (.*)', str(e))
    #                 if msg:
    #                     msg = msg[0]
    #                 else:
    #                     msg = repr(e)
    #                 raise DAOError(msg)
    #             except DatabaseError as e:
    #                 msg = f"Error accessing database: e: {repr(e)}"
    #                 # Check to see if this is actually a unique constraint collision.
    #                 if "relation" in e.args[0] and "already exists" in e.args[0]:
    #                     msg = f"Constraints names must be unique. e: {repr(e)}" 
    #                 logger.error(msg)
    #                 e.args = [msg]
    #                 raise e
    #             except Exception as e:
    #                 msg = f"Error executing command: {command}; e: {repr(e)}"
    #                 logger.error(msg)
    #                 e.args = [msg]
    #                 raise e
                
    #             try:
    #                 # Get amount of rows affected. (-1 means that query doesn't return anything relevant for .rowcount)
    #                 affected_rows = cur.rowcount
                    
    #                 # Try and get object description, also data. Only gets return data, so usually just return an empty array.
    #                 obj_description = cur.description

    #                 # Try and get data back, note we use a custom psycopg row_factor to format returns.
    #                 try:
    #                     obj_unparsed_data = cur.fetchall()
    #                 except ProgrammingError:
    #                     # Got here because there's no results to fetch
    #                     obj_unparsed_data = []
    #             except Exception as e:
    #                 msg = f"Error parsing data from command: {command}; e: {repr(e)}"
    #                 logger.error(msg)
    #                 e.args = [msg]
    #                 raise e

    #     logger.info(f'PostgresStore.run: object_description: {obj_description}, unparsed_data: {obj_unparsed_data}, '
    #                 f'affected_rows: {affected_rows}')
    #     return obj_description, obj_unparsed_data, affected_rows

    
    # def get_row_from_table(table_name, pk_id, tenant, primary_key, db_instance=None):
    #     """
    #     Gets the row with given primary key from the specified table.
    #     """
    #     logger.info(f"Getting row with pk {pk_id} from table {tenant}.{table_name}...")
    #     if type(pk_id) == 'int' or type(pk_id) == 'float':
    #         command = f"SELECT * FROM {tenant}.{table_name} WHERE {primary_key} = {pk_id};"
    #     else:
    #         command = f"SELECT * FROM {tenant}.{table_name} WHERE {primary_key} = '{pk_id}';"
        
    #     # Run command
    #     try:
    #         obj_description, obj_unparsed_data, _ = do_transaction(command, db_instance)
    #         result = parse_object_data(obj_description, obj_unparsed_data)
    #         if len(result) == 0:
    #             msg = f"Error. Received no result when retrieving row with pk \'{pk_id}\' from view {tenant}.{table_name}."
    #             logger.error(msg)
    #             raise Exception(msg)
    #         expose_primary_key(result, primary_key)
    #         logger.info(f"Row {pk_id} successfully retrieved from view {tenant}.{table_name}.")
    #     except Exception as e:
    #         msg = f"Error retrieving row with pk \'{pk_id}\' from view {tenant}.{table_name}: {e}"
    #         logger.error(msg)
    #         raise Exception(msg)
    #     return result


    # def get_rows_from_table(table_name, search_params, tenant, limit, offset, db_instance, primary_key, **kwargs):
    #     """
    #     Gets all rows from given table with an optional limit and filter.
    #     """
    #     logger.info(f"Getting rows from table {tenant}.{table_name}")
    #     command = f"SELECT * FROM {tenant}.{table_name}"

    #     # Add search params, order, limit, and offset to command
    #     try:
    #         parameterized_values = []
    #         if search_params:
    #             search_command, parameterized_values = search_parse(search_params, tenant, table_name, db_instance)
    #             command += search_command

    #         if "order" in kwargs:
    #             order = kwargs["order"]
    #             order_command = order_parse(order, tenant, table_name, db_instance)
    #             command += order_command

    #         if limit:
    #             command += f" LIMIT {int(limit)} "
    #         if offset:
    #             command += f" OFFSET {int(offset)};"
    #     except Exception as e:
    #         msg = f"Unable to add order, limit, and offset for table {tenant}.{table_name}: {e}"
    #         logger.warning(msg)
    #         raise Exception(msg)

    #     # Run command
    #     try:
    #         obj_description, obj_unparsed_data, _ = do_transaction(command, db_instance, parameterized_values)
    #         result = parse_object_data(obj_description, obj_unparsed_data)
    #         expose_primary_key(result, primary_key)
    #         logger.info(f"Rows successfully retrieved from table {tenant}.{table_name}.")
    #     except Exception as e:
    #         msg = f"Error retrieving rows from table {tenant}.{table_name}: {e}"
    #         logger.error(msg)
    #         raise Exception(msg)
    #     return result

    # def delete_row(table_name, pk_id, tenant, primary_key, db_instance=None):
    #     """
    #     Deletes the specified row in the given table.
    #     """
    #     logger.info(f"Deleting row with pk {pk_id} in table {tenant}.{table_name}")
    #     if type(pk_id) == 'int' or type(pk_id) == 'float':
    #         command = f"DELETE FROM {tenant}.{table_name} WHERE {primary_key} = {pk_id};"
    #     else:
    #         command = f"DELETE FROM {tenant}.{table_name} WHERE {primary_key} = '{pk_id}';"

    #     # Run command
    #     try:
    #         _, _, affected_rows = do_transaction(command, db_instance)
    #         if affected_rows == 0:
    #             msg = f"Error. Delete row affected 0 rows, expected to delete 1."
    #             logger.error(msg)
    #             raise Exception(msg)
    #         logger.info(f"Row successfully deleted from table {tenant}.{table_name}.")
    #     except Exception as e:
    #         msg = f"Error deleting row ID {pk_id} in table {tenant}.{table_name}: {e}"
    #         logger.error(msg)
    #         raise Exception(msg)

    # def update_row_with_pk(table_name, pk_id, data, tenant, primary_key, db_instance=None):
    #     """
    #     Updates a specified row on a table with the given columns and associated values.
    #     """
    #     logger.info(f"Updating row with pk \'{pk_id}\' in {tenant}.{table_name}...")
    #     command = f"UPDATE {tenant}.{table_name} SET"
    #     for col in data:
    #         if type(data[col]) == str:
    #             command = f"{command} {col} = (\'{data[col]}\'), "
    #         else:
    #             command = f"{command} {col} = ({data[col]}), "
    #     if type(pk_id) == 'int' or type(pk_id) == 'float':
    #         command = command[:-2] + f" WHERE {primary_key} = {pk_id};"
    #     else:
    #         command = command[:-2] + f" WHERE {primary_key} = '{pk_id}';"

    #     # Run command
    #     try:
    #         _, _, affected_rows = do_transaction(command, db_instance)
    #         if affected_rows == 0:
    #             msg = f"Error. Delete row affected 0 rows, expected to delete 1."
    #             logger.error(msg)
    #             raise Exception(msg)
    #         logger.info(f"Row {pk_id} successfully updated in table {tenant}.{table_name}.")
    #     except Exception as e:
    #         msg = f"Error updating row ID {pk_id} in table {tenant}.{table_name}: {e}"
    #         logger.error(msg)
    #         raise Exception(msg)





    # def __getitem__(self, fields):
    #     """
    #     Simple match
    #     Gets and returns 'self[key]' or 'self[key][field1][field2][...]' as a dictionary
    #     """
        
    #     query = "match (n: $field) return n"
    #     parameters = {'field': fields}

    #     try:
    #         self.run(query, parameters)
    #     except Exception as e:
    #         print(e)
        
    #     key, _, subscripts = self._process_inputs(fields)
    #     result = self._db.find_one(
    #         {'_id': key},
    #         projection={'_id': False})
    #     if result == None:
    #         raise KeyError(f"'_id' of '{key}' not found")
    #     try:
    #         return eval('result' + subscripts)
    #     except KeyError:
    #         raise KeyError(f"Subscript of {subscripts} does not exists in document of '_id' {key}")

    # def __setitem__(self, fields, value):
    #     """
    #     Atomically does either:
    #     Sets 'self[key] = value' or sets 'self[key][field1][field2][...] = value'
    #     """
    #     key, dots, _ = self._process_inputs(fields)
    #     try:
    #         if isinstance(fields, str) and isinstance(value, dict):
    #             result = self._db.update_one(
    #                 filter={'_id': key},
    #                 update={'$set': value},
    #                 upsert=True)
    #         else:
    #             result = self._db.update_one(
    #                 filter={'_id': key},
    #                 update={'$set': {dots: value}},
    #                 upsert=True)
    #     except WriteError:
    #         raise WriteError(
    #             "Likely due to trying to set a subfield of a field that does not exists." +
    #             "\n Try setting a dict rather than a value. Ex. store['id_key', 'key', 'field'] = {'subfield': 'value'}")
    #     if result.raw_result['nModified'] == 0:
    #         if not 'upserted' in result.raw_result:
    #             logger.debug(f'Field not modified, old value likely the same as new. Key: {key}, Fields: {dots}, Value: {value}')

    # def __delitem__(self, fields):
    #     """
    #     Atomically does either:
    #     Deletes 'self[key]'
    #     Unsets 'self[key][field1][field2][...]'
    #     """
    #     key, dots, subscripts = self._process_inputs(fields)
    #     if not subscripts:
    #         result = self._db.delete_one({'_id': key})
    #         if result.raw_result['n'] == 0:
    #             logger.debug(f"No document with '_id' found. Key:{key}, Fields:{dots}")
    #     else:
    #         result = self._db.update_one(
    #             filter={'_id': key},
    #             update={'$unset': {f'{dots}': ''}})
    #         if result.raw_result['nModified'] == 0:
    #             logger.debug(f"Doc with specified fields not found. Key:{key}, Fields:{dots}")

    # def __iter__(self):
    #     for cursor in self._db.find():
    #         yield cursor['_id']
    #     # return self._db.scan_iter()

    # def __len__(self):
    #     """
    #     Returns the estimated document count of a store to give length
    #     We don't use '.count_documents()' as it's O(N) versus O(1) of estimated
    #     Length for a document or subdocument comes from len(store['key']['field1'][...]) using dict len()
    #     """
    #     return self._db.estimated_document_count()

    # def __repr__(self):
    #     """
    #     Returns a pretty string of the entire store with '_id' visible for developer use
    #     """
    #     return pprint.pformat(list(self._db.find()))

    # def _process_inputs(self, fields):
    #     """
    #     Takes in fields and returns the key corresponding with '_id', dot notation
    #     for getting to a specific field in a Mongo query/filter (ex. 'field1.field2.field3.field4')
    #     and the subscript notation for returning a specified field from a result dictionary
    #     (ex. `['field1']['field2']['field3']['field4']`)
    #     """
    #     if isinstance(fields, str):
    #         key = dots = fields
    #         subscripts = ''
    #     elif isinstance(fields, list) and len(fields) == 1:
    #         key = dots = fields[0]
    #         subscripts = ''
    #     else:
    #         key = fields[0]
    #         dots = '.'.join(fields[1:])
    #         subscripts = "['" + "']['".join(fields[1:]) + "']"
    #     return key, dots, subscripts

    # def _prepset(self, value):
    #     if type(value) is bytes:
    #         return value.decode('utf-8')
    #     return value

    # def pop_field(self, fields):
    #     """
    #     Atomically pops 'self[key] = value' or 'self[key][field1][field2][...] = value'
    #     """
    #     key, dots, subscripts = self._process_inputs(fields)
    #     if not subscripts:
    #         result = self._db.find_one(
    #             {'_id': key},
    #             projection={'_id': False})
    #         if result == None:
    #             raise KeyError(f"'_id' of '{key}' not found")
    #         del_result = self._db.delete_one({'_id': key})
    #         if del_result.raw_result['n'] == 0:
    #             raise KeyError(f"No document deleted")
    #         return result
    #     else:
    #         result = self._db.find_one_and_update(
    #             filter={'_id': key},
    #             update={'$unset': {dots: ''}})
    #         try:
    #             return eval('result' + subscripts)
    #         except KeyError:
    #             raise KeyError(f"Subscript of {subscripts} does not exist in document of '_id' {key}")

    # def set_with_expiry(self, fields, value, log_ex):
    #     """
    #     Atomically:
    #     Sets 'self[key] = value' or 'self[key][field1][field2][...] = value'
    #     Creates 'exp' subdocument in document root with current time for use with MongoDB TTL expiration index
    #     Note: MongoDB TTL checks every 60 secs to delete files
    #     """
    #     key, dots, _ = self._process_inputs(fields)
    #     time_to_expire = datetime.utcnow() + timedelta(seconds=log_ex)
    #     logger.debug(f"Set with expiry setting time to expire to : {time_to_expire} ")
    #     if len(fields) == 1 and isinstance(value, dict):
    #         result = self._db.update_one(
    #             filter={'_id': key},
    #             update={'$set': {'exp': time_to_expire},
    #                     '$set': value},
    #             upsert=True)
    #     else:
    #         result = self._db.update_one(
    #             filter={'_id': key},
    #             update={'$set': {'exp': time_to_expire, dots: self._prepset(value)}},
    #             upsert=True)

    # def full_update(self, key, value, upsert=False):
    #     result = self._db.update_one(key, value, upsert)
    #     return result

    # def getset(self, fields, value):
    #     """
    #     Atomically does either:
    #     Sets 'self[key] = value' and returns previous 'self[key]'
    #     Sets 'self[key][field1][field2][...] = value' and returns previous 'self[key][field1][field2][...]'
    #     """
    #     key, dots, subscripts = self._process_inputs(fields)
    #     result = self._db.find_one_and_update(
    #         filter={'_id': key, dots: {'$exists': True}},
    #         update={'$set': {dots: value}})
    #     if result == None:
    #         raise KeyError(f"Subscript of {subscripts} does not exist in document of '_id' {key}")   
    #     try:
    #         if len(fields) == 1:
    #             return eval(f"result['{key}']")
    #         else:
    #             return eval('result' + subscripts)
    #     except KeyError:
    #         raise KeyError(f"Subscript of {subscripts} does not exist in document of '_id' {key}")

    # def items(self, filter_inp=None, proj_inp={'_id': False}):
    #     " Either returns all with no inputs, or filters when given filters"
    #     return list(self._db.find(
    #         filter=filter_inp,
    #         projection=proj_inp))

    # def add_if_empty(self, fields, value):
    #     """
    #     Atomically:
    #     Sets 'self[key] = value' or 'self[key][field1][field2][...] = value'
    #     Only if the specified key/field(s) combo does not exist or is empty
    #     Returns the value if it was added; otherwise, returns None
    #     Note: Will not override a field set to a value in order to create a subfield
    #     """
    #     key, dots, _ = self._process_inputs(fields)
    #     try:
    #         if len(fields) == 1 and isinstance(value, dict):
    #             result = self._db.update_one(
    #                 filter={'_id': key},
    #                 update={'$setOnInsert': value},
    #                 upsert=True)
    #             if result.upserted_id:
    #                 return key
    #         elif len(fields) == 1:
    #             result = self._db.update_one(
    #                 filter={'_id': key},
    #                 update={'$setOnInsert': {dots: value}},
    #                 upsert=True)
    #             if result.upserted_id:
    #                 return key
    #         else:
    #             try:
    #                 result = self._db.update_one(
    #                     filter={'_id': key},
    #                     update={'$setOnInsert': {dots: value}},
    #                     upsert=True)
    #                 if result.upserted_id:
    #                     return fields
    #             except WriteError:
    #                 print("Likely due to trying to set a subfield of a field that is already set to one value")
    #                 pass
    #         return None
    #     except DuplicateKeyError:
    #         return None
    
    # def aggregate(self, pipeline, options = None):
    #     return self._db.aggregate(pipeline, options)

    # def create_index(self, index_list):
    #     return self._db.create_index(index_list)