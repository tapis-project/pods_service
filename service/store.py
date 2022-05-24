
from collections.abc import MutableMapping
from datetime import datetime, timedelta
from attr import validate
from neo4j import GraphDatabase
from pydantic import validate_arguments
from typing import Literal, Any, Dict, List
from enum import Enum
from psycopg2.errors import UniqueViolation, DatabaseError

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
from sqlalchemy.orm import sessionmaker

class PostgresStore():
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

        # We create SQLAlchemy objects using future=True to get ready for SA:2.0 (we follow that style)
        self.engine = create_engine(conninfo, future=True)
        # expire_on_commit is more of a opinion than something bad according to docs.
        # I believe it's good to keep information. Session.begin flushes.
        self.session = sessionmaker(self.engine, future=True, expire_on_commit=False)

    @validate_arguments
    def run(self,
            fn_name: str,
            fn_input,
            fn_params: Dict = {},
            scalars: bool = False,
            all: bool = False,
            first: bool = False,
            unique: bool = False,
            scalar_one: bool = False,
            autocommit: bool = False):

        with self.session.begin() as session:
            if autocommit:
                session.connection(execution_options={"isolation_level": "AUTOCOMMIT"})
            try:
                logger.info(f"PostgresStore.fn; Command: {fn_name} - Statement/Instance: {fn_input}")
                # Gets session function to run (sess.execute, sess.scalars, etc.)
                fn_to_run = getattr(session, fn_name)

                output = fn_to_run(fn_input, **fn_params)
                
                if unique:
                    output = output.unique()
                if first:
                    output = output.first()
                if scalars:
                    output = output.scalars()
                if all:
                    output = output.all()
                if scalar_one:
                    output = output.scalar_one()

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

        return output
